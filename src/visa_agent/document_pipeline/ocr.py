"""OCR providers — extract derived text without mutating original evidence bytes.

Default provider is a local synthetic extractor suitable for tests and demos.
Plug a real engine by implementing ``OcrProvider`` and passing it to
``run_ocr_on_evidence`` (or set ``VISA_AGENT_OCR_PROVIDER`` when wiring the app).

OCR results are derived artifacts only — they never rewrite quarantine originals
and never auto-confirm field annotations (TL: human confirms fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OcrExtraction:
    """Result of one OCR pass over immutable original bytes."""

    text: str
    status: str  # completed | failed | unsupported
    provider: str
    detail: str = ""


class OcrProvider(Protocol):
    """Pluggable OCR engine. Implementations must not modify ``data``."""

    name: str

    def extract(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> OcrExtraction:
        ...


_TEXTUAL_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "application/json",
        "application/xml",
        "text/xml",
        "text/html",
    }
)
_TEXTUAL_EXT = frozenset(
    {".txt", ".csv", ".json", ".xml", ".html", ".htm", ".md", ".log"}
)
# Embedded printable runs inside synthetic PDF/image demo bytes.
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7E\n\r\t]{4,}")


def _ext(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


class SyntheticOcrProvider:
    """Deterministic local OCR stand-in (no external engine).

    - UTF-8 / textual MIME or extensions → decode as text
    - PDF / image / octet streams → extract long printable ASCII runs if present
    - Otherwise → ``unsupported`` (still a stored OCR row; originals untouched)

    Replace with Tesseract/cloud OCR by implementing ``OcrProvider`` the same way.
    """

    name = "synthetic_local_v1"

    def extract(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> OcrExtraction:
        # Defensive copy — callers may pass a shared buffer; we never write back.
        payload = bytes(data)
        ctype = (content_type or "").split(";", 1)[0].strip().lower()
        ext = _ext(filename)

        if ctype in _TEXTUAL_TYPES or ext in _TEXTUAL_EXT:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = payload.decode("utf-8", errors="replace")
            return OcrExtraction(
                text=text.strip(),
                status="completed",
                provider=self.name,
                detail="utf8_text_decode",
            )

        # Synthetic PDFs / demo images often embed plaintext for demos.
        if ext in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"} or (
            ctype.startswith("image/") or ctype == "application/pdf"
        ):
            runs = [m.group(0).decode("ascii", errors="ignore") for m in _PRINTABLE_RUN.finditer(payload)]
            joined = "\n".join(r.strip() for r in runs if r.strip())
            if joined:
                return OcrExtraction(
                    text=joined,
                    status="completed",
                    provider=self.name,
                    detail="printable_run_extract",
                )
            return OcrExtraction(
                text="",
                status="unsupported",
                provider=self.name,
                detail="no_printable_runs_in_binary; plug a real OCR engine",
            )

        # Last resort: try utf-8 if mostly text-like.
        if payload and sum(1 for b in payload if 32 <= b < 127 or b in (9, 10, 13)) / len(
            payload
        ) > 0.85:
            text = payload.decode("utf-8", errors="replace").strip()
            if text:
                return OcrExtraction(
                    text=text,
                    status="completed",
                    provider=self.name,
                    detail="heuristic_textish",
                )

        return OcrExtraction(
            text="",
            status="unsupported",
            provider=self.name,
            detail=f"no extractor for content_type={ctype or 'unknown'} ext={ext or 'none'}",
        )


def default_ocr_provider() -> OcrProvider:
    return SyntheticOcrProvider()
