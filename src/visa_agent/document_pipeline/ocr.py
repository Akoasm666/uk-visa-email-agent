"""OCR providers — extract derived text without mutating original evidence bytes.

OCR results are derived artifacts only — they never rewrite quarantine originals
and never auto-confirm field annotations (TL: human confirms fields).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree


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
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_TEXT_PART = re.compile(r"word/(?:document|header\d+|footer\d+|footnotes|endnotes)\.xml")
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


class LocalScanOcrProvider:
    """Read pixels through the local Vision helper, with no network calls."""

    name = "vision_local_v1"
    executable = "visa-agent-vision-ocr"

    def _extract_docx(self, data: bytes) -> OcrExtraction:
        lines = []
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 512 or sum(entry.file_size for entry in entries) > 48 * 1024 * 1024
                    or "word/document.xml" not in archive.namelist()
                    or any(
                        entry.filename.lower().startswith(("word/activex/", "word/embeddings/"))
                        or "vbaproject" in entry.filename.lower()
                        for entry in entries
                    )
                ):
                    return OcrExtraction("", "unsupported", self.name, "docx_content_not_supported")
                text_parts = sorted(entry.filename for entry in entries if _DOCX_TEXT_PART.fullmatch(entry.filename))
                for part in text_parts:
                    xml = ElementTree.fromstring(archive.read(part))
                    for paragraph in xml.iter(_WORD_NS + "p"):
                        line = "".join(run.text or "" for run in paragraph.iter(_WORD_NS + "t")).strip()
                        if line:
                            lines.append(line)
                images = [entry for entry in entries
                          if entry.filename.startswith("word/media/") and not entry.is_dir()]
                if len(images) > 12:
                    return OcrExtraction("", "unsupported", self.name, "docx_image_limit")
                for image in images:
                    result = self.extract(archive.read(image), filename=image.filename, content_type="")
                    if result.status != "completed":
                        return OcrExtraction("", result.status, self.name, "docx_embedded_image_unreadable")
                    if result.text.strip():
                        lines.append(result.text)
        except (ElementTree.ParseError, zipfile.BadZipFile, RuntimeError, OSError):
            return OcrExtraction("", "failed", self.name, "docx_text_extract_failed")
        return OcrExtraction("\n".join(lines), "completed", self.name, "docx_text_and_images")

    def extract(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> OcrExtraction:
        if data.startswith(b"%PDF-"):
            format_name = "pdf"
        elif data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            format_name = "raster"
        elif data.startswith(b"PK\x03\x04") and (_ext(filename) == ".docx" or content_type == _DOCX_MIME):
            return self._extract_docx(data)
        elif content_type.split(";", 1)[0].strip().lower() in _TEXTUAL_TYPES:
            try:
                text = data.decode("utf-8").strip()
            except UnicodeDecodeError:
                return OcrExtraction("", "failed", self.name, "invalid_utf8")
            return OcrExtraction(text, "completed", self.name, "utf8_text_decode")
        else:
            return OcrExtraction("", "unsupported", self.name, "unsupported_scan_format")
        executable = os.environ.get("VISA_AGENT_VISION_OCR")
        local_helper = Path(sys.executable).parent / self.executable
        if not executable:
            executable = str(local_helper) if local_helper.is_file() else shutil.which(self.executable)
        if not executable:
            return OcrExtraction("", "failed", self.name, "ocr_helper_missing")
        try:
            with tempfile.TemporaryDirectory(prefix="visa-scan-") as directory:
                scan = Path(directory) / "input"
                scan.write_bytes(data)
                result = subprocess.run(
                    [executable, format_name, str(scan)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return OcrExtraction("", "failed", self.name, "ocr_timeout")
        except OSError:
            return OcrExtraction("", "failed", self.name, "ocr_helper_unavailable")
        if result.returncode:
            reason = {2: "encrypted_pdf", 3: "scan_processing_limit"}.get(
                result.returncode, "scan_decode_or_ocr_failed"
            )
            return OcrExtraction("", "failed", self.name, reason)
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or not 1 <= len(pages) <= 12:
                raise ValueError("invalid page count")
            texts = [page["text"] for page in pages]
            metadata = {
                "pages": [
                    {"sharpness": page.get("sharpness"), "touches_edge": page["touchesEdge"]}
                    for page in pages
                ]
            }
            return OcrExtraction("\f".join(texts), "completed", self.name, json.dumps(metadata))
        except (ValueError, KeyError, TypeError, AttributeError):
            return OcrExtraction("", "failed", self.name, "ocr_invalid_result")


def default_ocr_provider() -> OcrProvider:
    return LocalScanOcrProvider()
