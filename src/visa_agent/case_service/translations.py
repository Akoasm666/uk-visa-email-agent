"""Prepare linked English drafts from the current source material."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import httpx
import psycopg

from visa_agent.ai_capabilities.document_translation import translate_ocr_pages, translation_language
from visa_agent.case_service.service import get_case
from visa_agent.config import Settings
from visa_agent.delivery.itinerary_document import PDF_MIME
from visa_agent.delivery.translation_document import translation_pdf
from visa_agent.domain.enums import CaseStatus, RouteCode
from visa_agent.storage.files import LocalFileStore

_LOG = logging.getLogger(__name__)
_ACTIVE = ("lead", "intake_authorization", "route_scope_confirmed", "collect_and_verify",
           "issue_resolution", "draft_preparation", "customer_confirmation", "final_review")
# Latest OCR, linked to the immutable original bytes. Synthetic OCR is used only in tests.
_LATEST_OCR = """
    LEFT JOIN LATERAL (
        SELECT extracted_text, created_at, status FROM evidence_ocr_results
        WHERE tenant_id = e.tenant_id AND case_id = e.case_id AND evidence_id = e.id
          AND provider = 'vision_local_v1' AND source_content_sha256 = e.content_sha256
        ORDER BY created_at DESC, id DESC LIMIT 1
    ) o ON TRUE
"""
_SUPPORTING_TYPES = ("bank", "funds", "employment", "sponsor", "purpose", "unclassified")


def case_translation_languages(
    conn: psycopg.Connection, tenant_id: str, case_id: str, *,
    included_ids: tuple[str, ...] | None = None,
) -> list[str | None]:
    rows = conn.execute(
        f"""
        SELECT e.classification, o.extracted_text, o.status, e.document_language, EXISTS (
            SELECT 1 FROM case_evidence signed
            WHERE signed.tenant_id = e.tenant_id AND signed.case_id = e.case_id
              AND signed.translation_source_id = e.id AND signed.classification = 'translation'
              AND signed.deleted_at IS NULL AND signed.quarantine_status <> 'rejected'
              AND (%s::text[] IS NULL OR signed.id = ANY(%s::text[]))
        )
        FROM case_evidence e {_LATEST_OCR}
        WHERE e.tenant_id = %s AND e.case_id = %s AND e.deleted_at IS NULL
          AND e.quarantine_status <> 'rejected' AND e.classification = ANY(%s)
          AND (%s::text[] IS NULL OR e.id = ANY(%s::text[]))
        """,
        (
            list(included_ids) if included_ids is not None else None,
            list(included_ids) if included_ids is not None else None,
            tenant_id, case_id, list(_SUPPORTING_TYPES),
            list(included_ids) if included_ids is not None else None,
            list(included_ids) if included_ids is not None else None,
        ),
    ).fetchall()
    return [
        "translation_uploaded" if covered else
        translation_language(classification, extracted, language) if status == "completed" else None
        for classification, extracted, status, language, covered in rows
    ]


def prepare_pending_translations(settings: Settings) -> int:
    """Process one untranslated source per poll, with retries after temporary model failures."""
    stamp = datetime.now(timezone.utc)
    with psycopg.connect(settings.database_url) as conn:
        candidates = conn.execute(
            f"""
            SELECT e.case_id, e.id, e.original_filename, e.content_sha256, e.classification,
                   o.extracted_text, e.translation_checked_at, e.document_language
            FROM case_evidence e JOIN cases c ON c.tenant_id = e.tenant_id AND c.id = e.case_id
            {_LATEST_OCR}
            WHERE e.tenant_id = %s AND c.route = 'standard_visitor' AND c.status = ANY(%s)
              AND NOT (c.human_takeover OR c.paused OR c.cancelled)
              AND e.deleted_at IS NULL AND e.quarantine_status <> 'rejected'
              AND e.classification = ANY(%s)
              AND o.status = 'completed' AND (
                  e.translation_checked_at IS NULL OR o.created_at > e.translation_checked_at
                  OR e.translation_checked_at < NOW() - INTERVAL '10 minutes'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM case_evidence d WHERE d.tenant_id = e.tenant_id
                    AND d.case_id = e.case_id AND d.translation_source_id = e.id
              )
            ORDER BY e.created_at, e.id
            """,
            (settings.tenant_id, list(_ACTIVE), list(_SUPPORTING_TYPES)),
        ).fetchall()
        target = next((row for row in candidates if translation_language(row[4], row[5], row[7]) == "translation_needed"), None)
        if not target:
            return 0
        case_id, source_id, filename, digest, _, extracted, previously_checked, language = target
        claim = conn.execute(
            """
            UPDATE case_evidence SET translation_checked_at = %s
            WHERE tenant_id = %s AND case_id = %s AND id = %s AND deleted_at IS NULL
              AND translation_checked_at IS NOT DISTINCT FROM %s
            """,
            (stamp, settings.tenant_id, case_id, source_id, previously_checked),
        ).rowcount
    if not claim:
        return 0
    try:
        pages = translate_ocr_pages(extracted, translate_all=True) if language == "other" else translate_ocr_pages(extracted)
        data = translation_pdf(filename, digest, pages)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        _LOG.info("Translation draft awaiting usable OCR or local translation (%s)", type(exc).__name__)
        return 0
    store = LocalFileStore(settings.file_storage_dir)
    file_id, pdf_digest, _, _ = store.put_bytes_content_addressed(data, suffix=".pdf")
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(
            database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id,
            conn=conn, for_update=True,
        )
        if case.route != RouteCode.STANDARD_VISITOR or case.status.value not in _ACTIVE or (
            case.human_takeover or case.paused or case.cancelled
        ):
            return 0
        result = conn.execute(
            """
            INSERT INTO case_evidence (
                id, tenant_id, case_id, case_version, file_id, content_sha256,
                original_filename, content_type, byte_size, classification, translation_source_id
            )
            SELECT %s, tenant_id, case_id, %s, %s, %s, %s, %s, %s, 'translation_draft', id
            FROM case_evidence WHERE tenant_id = %s AND case_id = %s AND id = %s
              AND deleted_at IS NULL AND quarantine_status <> 'rejected'
            ON CONFLICT DO NOTHING
            """,
            (uuid.uuid4().hex, case.case_version, file_id, pdf_digest,
             f"{filename} - 英文翻译草稿.pdf", PDF_MIME, len(data), case.tenant_id, case.id, source_id),
        ).rowcount
    return result
