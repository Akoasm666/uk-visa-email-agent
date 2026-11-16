"""Preliminary document quality checks (demo-synthetic + light heuristics)."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from visa_agent.document_pipeline.classify import classify_scan

# Explicit demo markers so acceptance tests are reproducible without a vision model.
_BLURRY_MARKERS = re.compile(
    r"(BLURRY|OUT[_ ]?OF[_ ]?FOCUS|模糊|不清晰|看不清|虚化)",
    re.I,
)
_CLEAR_MARKERS = re.compile(
    r"(CLEAR[_ ]?SCAN|SHARP|清晰|高清)",
    re.I,
)
_MISSING_PAGE_MARKERS = re.compile(
    r"(MISSING[_ ]?PAGE|INCOMPLETE|缺页|少页|不完整)",
    re.I,
)
_PASSPORT_MARKERS = re.compile(
    r"(passport|护照|bio[- ]?data|nationality|passport\s*no)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class QualityFinding:
    code: str  # blurry | missing_pages | unreadable | unclear_classification | ok
    severity: str  # simple_auto | complex_human
    detail_en: str
    detail_zh: str
    page_hint: str = "p.1"


_PASSPORT_NUMBER = re.compile(
    r"(?im)^\s*passport[ \t]+(?:no\.?|number|#)\s*[:.]?\s*([A-Z0-9]{6,12})\b"
)
_PASSPORT_HEADING = re.compile(r"(?im)^\s*(?:passport|护照)\s*$")
_PERSON_NAME = r"([A-Z][A-Z'’ -]{1,45})"
_PERSON_DATE = r"(\d{4}[- /.]\d{1,2}[- /.]\d{1,2}|\d{1,2}[- /.]\d{1,2}[- /.]\d{4}|\d{1,2}[ -][A-Z]{3}[ -]\d{4})"
_PASSPORT_FIELDS = {
    "surname": re.compile(rf"(?im)^\s*surname[ \t]*:?\s*{_PERSON_NAME}[ \t]*$"),
    "given_names": re.compile(rf"(?im)^\s*given names?[ \t]*:?\s*{_PERSON_NAME}[ \t]*$"),
    "birth_date": re.compile(rf"(?im)^\s*date of birth[ \t]*:?\s*{_PERSON_DATE}[ \t]*$"),
    "expiry_date": re.compile(rf"(?im)^\s*date of expiry[ \t]*:?\s*{_PERSON_DATE}[ \t]*$"),
}
_BILINGUAL_NUMBER = re.compile(r"^护照号码\s*/\s*passport\s*(?:no\.?|number)\s*$", re.I)
_BILINGUAL_NAME = re.compile(r"^姓名\s*/\s*name\s*$", re.I)
_BILINGUAL_DATES = {
    "birth_date": re.compile(r"^出生日期\s*/\s*date of birth\s*$", re.I),
    "expiry_date": re.compile(r"^有效期(?:至)?\s*/\s*date of expiry\s*$", re.I),
}
_BLURRY_SCORE = 0.12
_CLEAR_SCORE = 0.28


@dataclass(frozen=True, slots=True)
class PassportReading:
    identified: bool
    fields: dict[str, str]
    conflicting_fields: bool = False

    @property
    def complete(self) -> bool:
        return {"number", "surname", "given_names", "birth_date", "expiry_date"} <= self.fields.keys()


def _date_value(value: str) -> str:
    value = re.sub(r"(?<=\d)\s+\d{1,2}月\s*/\s*(?=[A-Z]{3}\b)", " ", value.strip().upper())
    value = re.sub(r"[./ -]+", "-", value.strip().upper())
    for format_name in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, format_name).strftime("%y%m%d")
        except ValueError:
            continue
    return ""


def _mrz_checksum(value: str) -> str:
    weights = (7, 3, 1)
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return str(sum((digits.index(char) if char != "<" else 0) * weights[i % 3]
                   for i, char in enumerate(value)) % 10)


def _mrz_fields(text: str) -> dict[str, str]:
    lines = [re.sub(r"\s+", "", line).upper() for line in text.splitlines()]
    for names, credentials in zip(lines, lines[1:]):
        # ICAO TD3 supports a document-type letter after P as well as legacy filler.
        if not re.fullmatch(r"P[A-Z<][A-Z<]{3}[A-Z<]{39}", names):
            continue
        if not re.fullmatch(r"[A-Z0-9<]{9}[0-9][A-Z<]{3}[0-9]{7}[MFX<][0-9]{7}[A-Z0-9<]{15}[0-9]", credentials):
            continue
        fields = {
            "number": credentials[:9].rstrip("<"),
            "birth_date": credentials[13:19],
            "expiry_date": credentials[21:27],
        }
        checks = ((credentials[:9], credentials[9]), (fields["birth_date"], credentials[19]),
                  (fields["expiry_date"], credentials[27]))
        if any(_mrz_checksum(value) != check for value, check in checks):
            continue
        surname, separator, given_names = names[5:].partition("<<")
        if separator:
            fields.update(surname=surname.replace("<", " ").strip(),
                          given_names=given_names.replace("<", " ").strip())
        return {key: value for key, value in fields.items() if value}
    return {}


def read_passport(text: str) -> PassportReading:
    mrz = _mrz_fields(text)
    numbers = {number.upper() for number in _PASSPORT_NUMBER.findall(text)
               if any(char.isdigit() for char in number)}
    candidates = {"number": numbers}
    for key, pattern in _PASSPORT_FIELDS.items():
        candidates[key] = {
            _date_value(value) if key.endswith("_date") else " ".join(value.upper().split())
            for value in pattern.findall(text)
        } - {""}
    # Bilingual cards often put the label and value on separate OCR lines.
    lines = [line.strip() for line in unicodedata.normalize("NFKC", text).upper().splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if _BILINGUAL_NUMBER.fullmatch(line):
            values = re.findall(r"\b[A-Z]{1,2}\d{7,8}\b", "\n".join(lines[index + 1:index + 4]))
            candidates["number"].update(values)
        elif _BILINGUAL_NAME.fullmatch(line):
            for value in lines[index + 1:index + 3]:
                name = re.fullmatch(rf"{_PERSON_NAME},\s*{_PERSON_NAME}", value)
                if name:
                    candidates["surname"].add(name[1].strip())
                    candidates["given_names"].add(name[2].strip())
        for key, label in _BILINGUAL_DATES.items():
            if label.fullmatch(line) and index + 1 < len(lines):
                date = _date_value(lines[index + 1])
                if date:
                    candidates[key].add(date)
    for key, value in mrz.items():
        candidates[key].add(value)
    fields = {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}
    identified = bool(mrz or (_PASSPORT_HEADING.search(text) and fields)
                      or (numbers and len(fields) >= 3))
    return PassportReading(identified, fields, any(len(values) > 1 for values in candidates.values()))


def passport_fields_conflict(first: PassportReading, second: PassportReading) -> bool:
    return any(value != second.fields[key] for key, value in first.fields.items() if key in second.fields)


def _looks_like_chinese_identity_card(text: str) -> bool:
    """Recognize a resident ID as extra evidence without exposing its number."""
    normalized = unicodedata.normalize("NFKC", text).upper()
    if "居民身份证" in normalized or "公民身份号码" in normalized:
        return True
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    for number in re.findall(r"(?m)^[ \t]*([0-9]{17}[0-9X])[ \t]*$", normalized):
        try:
            datetime.strptime(number[6:14], "%Y%m%d")
        except ValueError:
            continue
        index = sum(int(digit) * weight for digit, weight in zip(number[:17], weights)) % 11
        if number[-1] == "10X98765432"[index]:
            return True
    return False


def scan_review(
    *, filename: str, ocr_text: str, ocr_status: str, ocr_detail: str,
    visitor_passport_auto: bool, agent_classification: str | None = None,
    agent_readability: str | None = None,
) -> tuple[str, QualityFinding, PassportReading]:
    """Only a verified scan's pixel extraction can qualify for automatic action."""
    passport = read_passport(ocr_text)
    classification = agent_classification
    if classification is None:
        classification = classify_scan(filename=filename, ocr_text=ocr_text,
                                       has_passport_page=passport.identified)

    def result(code: str, detail_en: str, detail_zh: str, *, automatic: bool = False):
        severity = "simple_auto" if automatic else "complex_human"
        return classification, QualityFinding(code, severity, detail_en, detail_zh), passport

    if classification == "identity_card":
        return result("optional_identity_card", "", "", automatic=True)
    if ocr_status != "completed":
        return result("ocr_unavailable", "The scan could not be read automatically.", "扫描件无法自动读取。")
    if classification == "unclassified":
        if agent_classification is None and visitor_passport_auto and not passport.identified and _looks_like_chinese_identity_card(ocr_text):
            return result("optional_identity_card", "", "", automatic=True)
        return result("unclear_classification", "The document type needs advisor review.", "材料类型需顾问核对。")
    if classification != "passport":
        if agent_readability == "clear" and ocr_text.strip():
            return result("ok", "", "", automatic=True)
        return result("advisor_document_review", "The document needs advisor review.", "材料需顾问核对。")
    if not visitor_passport_auto:
        return result("advisor_document_review", "The document needs advisor review.", "材料需顾问核对。")
    if passport.conflicting_fields:
        return result("passport_data_conflict", "Passport fields disagree.", "护照内的身份字段不一致。")
    try:
        pages = json.loads(ocr_detail)["pages"]
        page = pages[0] if len(pages) == 1 else None
        sharpness = float(page["sharpness"]) if page else None
        touches_edge = page["touches_edge"] if page else True
    except (ValueError, TypeError, KeyError, IndexError):
        sharpness, touches_edge = None, True
    if touches_edge:
        return result("incomplete_scan", "Scan edges or page coverage need review.", "扫描边缘或页面完整性需顾问核对。")
    if sharpness is not None and 0 <= sharpness < _BLURRY_SCORE:
        return result(
            "blurry",
            "p.1: The passport scan is too blurry. Please reply with a sharp colour scan of the full bio-data page.",
            "第 1 页：护照扫描过于模糊，请直接回复并附上完整资料页的清晰彩色扫描件。",
            automatic=True,
        )
    if not passport.complete:
        return result("incomplete_scan", "Passport bio-data fields are missing or unreadable.", "护照资料页字段缺失或无法读取。")
    if sharpness is not None and sharpness >= _CLEAR_SCORE:
        return result("ok", "", "", automatic=True)
    return result("uncertain_scan_quality", "Scan readability needs advisor review.", "扫描清晰度需顾问核对。")


def review_attachment_quality(
    *,
    filename: str,
    data: bytes,
    ocr_text: str = "",
    ocr_status: str = "",
    classification: str = "unclassified",
) -> QualityFinding:
    """Return a preliminary quality finding for one attachment.

    Simple issues (blurry / missing pages / unreadable) → auto remediation email.
    Unclear classification after OCR → escalate to human (no auto policy guess).
    """
    name = filename or ""
    blob = b""
    try:
        blob = data or b""
    except Exception:  # noqa: BLE001
        blob = b""
    text_probe = " ".join(
        [
            name,
            ocr_text or "",
            blob.decode("utf-8", errors="ignore")[:4000],
        ]
    )

    if classification == "unclassified" and ocr_status in {
        "completed",
        "unsupported",
        "failed",
        "",
    }:
        # Still unclassified after OCR attempt → human triage.
        if not _PASSPORT_MARKERS.search(text_probe) and not re.search(
            r"(bank|流水|statement|cas|cos|ielts)", text_probe, re.I
        ):
            return QualityFinding(
                code="unclear_classification",
                severity="complex_human",
                detail_en=(
                    f"{name}: could not tell which checklist item this is — "
                    "advisor will classify"
                ),
                detail_zh=f"{name}：无法判断属于哪一类材料，已转顾问人工分类",
                page_hint="file",
            )

    if _MISSING_PAGE_MARKERS.search(text_probe):
        return QualityFinding(
            code="missing_pages",
            severity="simple_auto",
            detail_en=f"{name} p.1: appears incomplete / missing pages",
            detail_zh=f"{name} 第1页起：看起来不完整或有缺页，请传齐全部页",
            page_hint="p.1+",
        )

    if _BLURRY_MARKERS.search(text_probe) and not _CLEAR_MARKERS.search(text_probe):
        page = "p.1"
        return QualityFinding(
            code="blurry",
            severity="simple_auto",
            detail_en=(
                f"{name} {page}: scan is too blurry / out of focus — "
                "please re-upload a sharp colour scan of the passport bio-data page"
            ),
            detail_zh=(
                f"{name} {page}：扫描过于模糊，请重传护照资料页清晰彩色扫描件"
                "（四角完整、文字可读）"
            ),
            page_hint=page,
        )

    if ocr_status in {"failed", "unsupported"} and not ocr_text.strip():
        return QualityFinding(
            code="unreadable",
            severity="simple_auto",
            detail_en=(
                f"{name}: we could not read this file — "
                "please send a clearer PDF/JPG/PNG"
            ),
            detail_zh=f"{name}：无法自动识别内容，请重传更清晰的 PDF/JPG/PNG",
            page_hint="file",
        )

    return QualityFinding(
        code="ok",
        severity="simple_auto",
        detail_en="",
        detail_zh="",
        page_hint="",
    )
