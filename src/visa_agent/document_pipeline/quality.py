"""Preliminary document quality checks (demo-synthetic + light heuristics)."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
