"""Heuristic classification of inbound attachments → checklist slots."""

from __future__ import annotations

import re

# classification label used on case_evidence → requirement key
_CLASS_TO_REQ: dict[str, str] = {
    "passport": "passport_valid",
    "bank": "funds_for_stay",
    "funds": "funds_for_stay",
    "sponsor": "sponsor_if_any",
    "translation": "translation_if_needed",
    "itinerary": "intention_to_leave",
    "employment": "home_ties",
    "purpose": "visit_purpose_allowed",
    "cas": "cas_confirmation",
    "maintenance": "funds_maintenance",
    "course": "course_offer",
    "offer": "course_offer",
    "cos": "certificate_of_sponsorship",
    "sponsorship": "certificate_of_sponsorship",
    "salary": "salary_threshold",
    "skill": "job_skill_level",
    "english": "english_language",
    "tb": "tuberculosis_test_if_required",
    "tuberculosis": "tuberculosis_test_if_required",
    "degree": "qualification_confirmed",
    "qualification": "qualification_confirmed",
    "graduation": "uk_study_completed",
    "uk_study": "uk_study_completed",
    "permission": "current_permission_basis",
}

_FILENAME_HINTS: tuple[tuple[str, str], ...] = (
    ("passport", "passport"),
    ("护照", "passport"),
    ("bank", "bank"),
    ("statement", "bank"),
    ("流水", "bank"),
    ("funds", "funds"),
    ("存款", "funds"),
    ("sponsor", "sponsor"),
    ("担保", "sponsor"),
    ("cas", "cas"),
    ("cos", "cos"),
    ("itinerary", "itinerary"),
    ("行程", "itinerary"),
    ("flight", "itinerary"),
    ("english", "english"),
    ("ielts", "english"),
    ("toefl", "english"),
    ("tb", "tb"),
    ("tuberculosis", "tb"),
    ("employment", "employment"),
    ("在职", "employment"),
    ("offer", "offer"),
    ("course", "course"),
    ("degree", "degree"),
    ("毕业", "graduation"),
    ("salary", "salary"),
    ("工资", "salary"),
    ("translation", "translation"),
    ("翻译", "translation"),
    ("purpose", "purpose"),
    ("permission", "permission"),
)

_OCR_HINTS: tuple[tuple[str, str], ...] = (
    ("passport", "passport"),
    ("passport no", "passport"),
    ("nationality", "passport"),
    ("bank statement", "bank"),
    ("account balance", "bank"),
    ("iban", "bank"),
    ("银行流水", "bank"),
    ("账户余额", "bank"),
    ("在职证明", "employment"),
    ("在读证明", "employment"),
    ("学籍证明", "employment"),
    ("资助声明", "sponsor"),
    ("经济担保信", "sponsor"),
    ("certificate of sponsorship", "cos"),
    ("confirmation of acceptance", "cas"),
    ("cas number", "cas"),
    ("ielts", "english"),
    ("tuberculosis", "tb"),
)


def classify_attachment(
    *,
    filename: str,
    ocr_text: str = "",
) -> tuple[str, str | None]:
    """Return ``(classification, requirement_key_or_None)``."""
    name = (filename or "").lower()
    for needle, cls in _FILENAME_HINTS:
        if needle.lower() in name:
            return cls, _CLASS_TO_REQ.get(cls)

    blob = (ocr_text or "").lower()
    if blob:
        for needle, cls in _OCR_HINTS:
            if needle in blob:
                return cls, _CLASS_TO_REQ.get(cls)

    return "unclassified", None


def requirement_for_classification(classification: str, *, route: str | None = None) -> str | None:
    label = (classification or "").strip().lower()
    if route == "student" and label in {"bank", "funds"}:
        return "funds_maintenance"
    return _CLASS_TO_REQ.get(label)


def classify_scan(*, filename: str, ocr_text: str, has_passport_page: bool) -> str:
    """Classify a real scan from its OCR content and reject conflicting hints."""
    text = ocr_text.lower()
    matches = {
        cls for needle, cls in _OCR_HINTS if cls != "passport" and needle in text
    }
    if has_passport_page:
        matches.add("passport")
    if len(matches) != 1:
        return "unclassified"
    label = matches.pop()
    stem = filename.rsplit(".", 1)[0].lower()
    words = set(re.findall(r"[^\W_]+", stem))
    hints = {
        cls for needle, cls in _FILENAME_HINTS
        if needle in words or (not needle.isascii() and needle in stem)
    }
    if any(_CLASS_TO_REQ[hint] != _CLASS_TO_REQ[label] for hint in hints):
        return "unclassified"
    return label
