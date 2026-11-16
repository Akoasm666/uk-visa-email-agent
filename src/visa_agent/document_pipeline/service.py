"""S2.1 document pipeline: quarantine originals + human field annotations.

Never modifies original bytes. OCR writes derived text only (no auto-confirm).
Material changes bump case_version in the same transaction as the write.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.errors import UniqueViolation

from visa_agent.case_service.service import (
    _require_version,
    bump_case_version_in_conn,
    get_case,
)
from visa_agent.document_pipeline.ocr import OcrProvider, default_ocr_provider
from visa_agent.domain.enums import CaseStatus, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.storage.files import LocalFileStore

_MAX_BYTES = 10 * 1024 * 1024
_REJECT_EXTENSIONS = frozenset(
    {
        ".exe",
        ".bat",
        ".cmd",
        ".com",
        ".scr",
        ".dll",
        ".msi",
        ".js",
        ".vbs",
        ".ps1",
        ".sh",
    }
)
_ALLOWED_QUARANTINE = frozenset(
    {"isolated", "rejected", "accepted_for_review"}
)
_HARD_REJECT_PREFIX = "disallowed extension "

# Test-only barriers for fixed concurrent ordering (no-op in production).
_TEST_BARRIERS: dict[str, Callable[[], None]] = {}


def _barrier(name: str) -> None:
    hook = _TEST_BARRIERS.get(name)
    if hook is not None:
        hook()


def _advisory_xact_lock(conn: psycopg.Connection, key: str) -> None:
    """Transaction-scoped lock shared by attach write/commit and orphan cleanup."""
    digest = hashlib.sha256(f"evidence-file:{key}".encode("utf-8")).digest()
    k1 = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    k2 = int.from_bytes(digest[4:8], "big") & 0x7FFFFFFF
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (k1, k2))


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: str
    tenant_id: str
    case_id: str
    case_version: int
    file_id: str
    content_sha256: str
    original_filename: str
    content_type: str
    byte_size: int
    classification: str
    quarantine_status: str
    rejection_reason: str
    source_email_event_id: str | None
    viewed_at: datetime | None = None
    advisor_reviewed_version: int | None = None
    translation_source_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceAnnotation:
    id: str
    tenant_id: str
    evidence_id: str
    case_id: str
    case_version: int
    field_key: str
    field_value: str
    note: str


@dataclass(frozen=True, slots=True)
class EvidenceOcrResult:
    id: str
    tenant_id: str
    evidence_id: str
    case_id: str
    case_version: int
    provider: str
    status: str
    extracted_text: str
    source_content_sha256: str
    detail: str


def _ext(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _hard_reject_reason(filename: str) -> str | None:
    ext = _ext(filename)
    if ext in _REJECT_EXTENSIONS:
        return f"{_HARD_REJECT_PREFIX}{ext}"
    return None


def _is_hard_rejected(evidence: EvidenceRecord) -> bool:
    if _hard_reject_reason(evidence.original_filename):
        return True
    return evidence.rejection_reason.startswith(_HARD_REJECT_PREFIX)


def _row_to_evidence(row: tuple) -> EvidenceRecord:
    return EvidenceRecord(
        id=row[0],
        tenant_id=row[1],
        case_id=row[2],
        case_version=row[3],
        file_id=row[4],
        content_sha256=row[5],
        original_filename=row[6] or "",
        content_type=row[7] or "application/octet-stream",
        byte_size=int(row[8] or 0),
        classification=row[9] or "unclassified",
        quarantine_status=row[10] or "isolated",
        rejection_reason=row[11] or "",
        source_email_event_id=row[12],
        viewed_at=row[13],
        advisor_reviewed_version=row[14],
        translation_source_id=row[15],
    )


def _require_attributed_inbound(
    *,
    conn: psycopg.Connection,
    tenant_id: str,
    case_id: str,
    email_event_id: str,
) -> None:
    """Lock the inbound email row for the rest of this transaction.

    Matches re-attribution's FOR UPDATE so attach and reassign cannot interleave.
    """
    row = conn.execute(
        """
        SELECT id, case_id, attribution_status, direction
        FROM email_events
        WHERE tenant_id = %s AND id = %s
        FOR UPDATE
        """,
        (tenant_id, email_event_id),
    ).fetchone()
    if row is None:
        raise DomainError(
            code=ErrorCode.NOT_FOUND,
            message="source email event not found",
            details=(email_event_id,),
        )
    if row[3] != "inbound":
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="source email must be inbound",
            details=(email_event_id, str(row[3])),
        )
    if row[2] != "attributed" or row[1] != case_id:
        raise DomainError(
            code=ErrorCode.EMAIL_UNATTRIBUTED,
            message="source email must be attributed to this case",
            details=(
                email_event_id,
                f"case_id={row[1]}",
                f"attribution={row[2]}",
            ),
        )
    _barrier("after_email_lock")


def attach_evidence(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    file_store: LocalFileStore,
    data: bytes,
    original_filename: str,
    content_type: str = "application/octet-stream",
    classification: str = "unclassified",
    source_email_event_id: str | None = None,
    expected_case_version: int | None = None,
    reuse_same_source: bool = False,
    translation_draft_id: str | None = None,
) -> EvidenceRecord:
    """Store original bytes immutably and create a quarantined evidence row.

    New evidence always bumps case_version in the same transaction so prior
    confirm/approve bindings become stale.
    """
    filename = (original_filename or "untitled").strip() or "untitled"
    if not data:
        raise DomainError(
            code=ErrorCode.ATTACHMENT_REJECTED,
            message="empty attachment rejected",
            details=(filename,),
        )
    if len(data) > _MAX_BYTES:
        raise DomainError(
            code=ErrorCode.ATTACHMENT_REJECTED,
            message="attachment exceeds size limit",
            details=(str(len(data)), str(_MAX_BYTES)),
        )

    digest = hashlib.sha256(data).hexdigest()
    hard = _hard_reject_reason(filename)
    quarantine = "rejected" if hard else "isolated"
    rejection = hard or ""

    label = (classification or "unclassified").strip() or "unclassified"
    if label.lower() in {"fuzzy", "unclear", "unknown"}:
        label = "unclassified"

    ext = _ext(filename) or ".bin"
    if translation_draft_id and (label != "translation" or ext != ".pdf" or not data.startswith(b"%PDF-")):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "请上传 PDF 译文")
    evidence_id = uuid.uuid4().hex
    created_new = False
    orphan_path: Path | None = None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.transaction():
                # Serialize attaches on this case; check dup before any file write.
                case = get_case(
                    database_url=database_url,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    conn=conn,
                    for_update=True,
                )
                _require_version(case, expected_case_version, required=True)
                translation_source_id = None
                if translation_draft_id:
                    if case.status in {CaseStatus.CLOSED, CaseStatus.DELIVERED, CaseStatus.SENT_AWAITING_ACK}:
                        raise DomainError(ErrorCode.VALIDATION_ERROR, "已发送或结束的案件仅可查看")
                    draft = get_evidence(
                        database_url=database_url, tenant_id=tenant_id, case_id=case_id,
                        evidence_id=translation_draft_id, conn=conn,
                    )
                    if (draft.classification != "translation_draft" or not draft.translation_source_id
                            or draft.quarantine_status == "rejected"):
                        raise DomainError(ErrorCode.VALIDATION_ERROR, "请选择有效的译文草稿")
                    translation_source_id = draft.translation_source_id

                if source_email_event_id:
                    _require_attributed_inbound(
                        conn=conn,
                        tenant_id=tenant_id,
                        case_id=case_id,
                        email_event_id=source_email_event_id,
                    )

                dup = conn.execute(
                    """
                    SELECT id, source_email_event_id, deleted_at FROM case_evidence
                    WHERE tenant_id = %s AND case_id = %s AND content_sha256 = %s
                      AND (deleted_at IS NULL OR source_email_event_id = %s)
                    ORDER BY deleted_at NULLS FIRST
                    LIMIT 1
                    """,
                    (tenant_id, case_id, digest, source_email_event_id),
                ).fetchone()
                if dup is not None:
                    if reuse_same_source and source_email_event_id and dup[1] == source_email_event_id and dup[2] is None:
                        return get_evidence(
                            database_url=database_url,
                            tenant_id=tenant_id,
                            case_id=case_id,
                            evidence_id=dup[0],
                            conn=conn,
                        )
                    raise DomainError(
                        code=ErrorCode.ATTACHMENT_REJECTED,
                        message=(
                            "identical evidence content already attached to case"
                        ),
                        details=(dup[0], digest),
                    )

                bumped = bump_case_version_in_conn(
                    conn=conn,
                    database_url=database_url,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    expected_case_version=case.case_version,
                    required=True,
                )

                file_id = LocalFileStore.file_id_for(digest, suffix=ext)
                # Serialize write / commit / cleanup for this exact on-disk path.
                _advisory_xact_lock(conn, file_id)
                file_id, file_digest, path, created_new = (
                    file_store.put_bytes_content_addressed(data, suffix=ext)
                )
                assert file_digest == digest
                assert file_id == LocalFileStore.file_id_for(digest, suffix=ext)
                if created_new:
                    orphan_path = path
                _barrier("after_file_write")

                try:
                    if translation_draft_id:
                        conn.execute(
                            "UPDATE case_evidence SET deleted_at = NOW() WHERE id = %s AND tenant_id = %s AND case_id = %s",
                            (translation_draft_id, tenant_id, case_id),
                        )
                    conn.execute(
                        """
                        INSERT INTO case_evidence (
                            id, tenant_id, case_id, case_version, file_id,
                            content_sha256, original_filename, content_type,
                            byte_size, classification, quarantine_status,
                            rejection_reason, source_email_event_id, translation_source_id
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            evidence_id,
                            tenant_id,
                            case_id,
                            bumped.case_version,
                            file_id,
                            digest,
                            filename,
                            content_type or "application/octet-stream",
                            len(data),
                            label,
                            quarantine,
                            rejection,
                            source_email_event_id,
                            translation_source_id,
                        ),
                    )
                except UniqueViolation as exc:
                    raise DomainError(
                        code=ErrorCode.ATTACHMENT_REJECTED,
                        message=(
                            "identical evidence content already attached to case"
                        ),
                        details=(digest,),
                    ) from exc
    except Exception:
        if created_new and orphan_path is not None:
            _cleanup_orphan_evidence_file(
                database_url=database_url,
                file_store=file_store,
                file_id=LocalFileStore.file_id_for(digest, suffix=ext),
                path=orphan_path,
            )
        raise

    return get_evidence(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )


def _cleanup_orphan_evidence_file(
    *,
    database_url: str,
    file_store: LocalFileStore,
    file_id: str,
    path: Path,
) -> None:
    """Delete a newly written file only if no row references this exact path id."""
    _barrier("cleanup_before_lock")
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            _advisory_xact_lock(conn, file_id)
            _barrier("cleanup_after_lock")
            refs = conn.execute(
                """
                SELECT 1 FROM case_evidence
                WHERE file_id = %s
                LIMIT 1
                """,
                (file_id,),
            ).fetchone()
            if refs is None:
                file_store.delete_file(path)


def set_evidence_classification(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str,
    classification: str,
    document_language: str | None = None,
) -> EvidenceRecord:
    """Update the checklist slot and invalidate snapshots when the class changes."""
    label = (classification or "unclassified").strip() or "unclassified"
    if label.lower() in {"fuzzy", "unclear", "unknown"}:
        label = "unclassified"
    with psycopg.connect(database_url) as conn:
        keys = dict(database_url=database_url, tenant_id=tenant_id, case_id=case_id)
        case = get_case(**keys, conn=conn, for_update=True)
        evidence = get_evidence(**keys, evidence_id=evidence_id, conn=conn)
        recorded_language = conn.execute(
            "SELECT document_language FROM case_evidence WHERE tenant_id = %s AND case_id = %s AND id = %s",
            (tenant_id, case_id, evidence_id),
        ).fetchone()[0]
        language = document_language if document_language in {"english", "welsh", "other"} else recorded_language
        if evidence.classification == label and language == recorded_language:
            return evidence
        if evidence.classification == "translation_draft" or evidence.translation_source_id:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请在材料清单里核对或替换译文")
        updated = bump_case_version_in_conn(**keys, conn=conn, expected_case_version=case.case_version)
        conn.execute(
            """
            UPDATE case_evidence
            SET classification = %s, case_version = %s, document_language = %s
            WHERE tenant_id = %s AND case_id = %s AND id = %s
              AND deleted_at IS NULL
            """,
            (label, updated.case_version, language, tenant_id, case_id, evidence_id),
        )
    return get_evidence(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )


def get_evidence(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str,
    conn: psycopg.Connection | None = None,
) -> EvidenceRecord:
    sql = """
        SELECT id, tenant_id, case_id, case_version, file_id, content_sha256,
               original_filename, content_type, byte_size, classification,
               quarantine_status, rejection_reason, source_email_event_id, viewed_at,
               advisor_reviewed_version, translation_source_id
        FROM case_evidence
        WHERE tenant_id = %s AND case_id = %s AND id = %s
          AND deleted_at IS NULL
    """

    def _fetch(c: psycopg.Connection) -> EvidenceRecord:
        row = c.execute(sql, (tenant_id, case_id, evidence_id)).fetchone()
        if row is None:
            raise DomainError(
                code=ErrorCode.NOT_FOUND,
                message="evidence not found",
                details=(evidence_id,),
            )
        return _row_to_evidence(row)

    if conn is not None:
        return _fetch(conn)
    with psycopg.connect(database_url) as owned:
        return _fetch(owned)


def list_evidence(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> list[EvidenceRecord]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, tenant_id, case_id, case_version, file_id, content_sha256,
                   original_filename, content_type, byte_size, classification,
                   quarantine_status, rejection_reason, source_email_event_id, viewed_at,
                   advisor_reviewed_version, translation_source_id
            FROM case_evidence
            WHERE tenant_id = %s AND case_id = %s
              AND deleted_at IS NULL
            ORDER BY created_at DESC
            """,
            (tenant_id, case_id),
        ).fetchall()
    return [_row_to_evidence(r) for r in rows]


def mark_evidence_viewed(
    *, database_url: str, tenant_id: str, case_id: str, evidence_id: str,
) -> None:
    """Remember the first preview independently of the material and case version."""
    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            UPDATE case_evidence SET viewed_at = NOW()
            WHERE tenant_id = %s AND case_id = %s AND id = %s
              AND viewed_at IS NULL AND deleted_at IS NULL AND quarantine_status <> 'rejected'
            """,
            (tenant_id, case_id, evidence_id),
        )


def delete_evidence(
    *, database_url: str, tenant_id: str, case_id: str, evidence_id: str,
    expected_case_version: int | None,
) -> None:
    """Remove the file from active case use and invalidate approvals atomically."""
    keys = dict(database_url=database_url, tenant_id=tenant_id, case_id=case_id)
    with psycopg.connect(database_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"customer-inbound:{tenant_id}:{case_id}",))
        case = get_case(**keys, conn=conn, for_update=True)
        _require_version(case, expected_case_version, required=True)
        if case.status in {CaseStatus.CLOSED, CaseStatus.DELIVERED, CaseStatus.SENT_AWAITING_ACK}:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "已发送或关闭的案件不能删除材料")
        busy = conn.execute(
            """
            SELECT 1 FROM outbox_messages
            WHERE tenant_id = %s AND case_id = %s AND status = 'processing'
            LIMIT 1
            """,
            (tenant_id, case_id),
        ).fetchone()
        if busy:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "案件正在发信，请稍后再删除材料")
        removed = conn.execute(
            """
            UPDATE case_evidence SET deleted_at = NOW()
            WHERE tenant_id = %s AND case_id = %s AND id = %s AND deleted_at IS NULL
            RETURNING id
            """,
            (tenant_id, case_id, evidence_id),
        ).fetchone()
        if removed is None:
            raise DomainError(ErrorCode.NOT_FOUND, "材料已删除或不存在")
        conn.execute(
            """
            UPDATE case_evidence SET deleted_at = NOW()
            WHERE tenant_id = %s AND case_id = %s AND translation_source_id = %s AND deleted_at IS NULL
            """,
            (tenant_id, case_id, evidence_id),
        )
        conn.execute(
            """
            UPDATE case_itinerary_requests
            SET draft_text = '', draft_evidence_id = NULL, draft_message_id = NULL,
                draft_source_email_event_id = NULL, confirmed_at = NULL,
                last_reply_text = '', awaiting_details = FALSE
            WHERE tenant_id = %s AND case_id = %s AND draft_evidence_id = %s
            """,
            (tenant_id, case_id, evidence_id),
        )
        updated = bump_case_version_in_conn(
            **keys, conn=conn, expected_case_version=case.case_version, required=True,
        )
        conn.execute(
            """
            UPDATE open_questions SET status = 'closed', closed_at = NOW(), updated_at = NOW()
            WHERE tenant_id = %s AND case_id = %s AND evidence_id = %s
              AND requirement_key = 'advisor_unclassified' AND status = 'escalated_human'
            """,
            (tenant_id, case_id, evidence_id),
        )
        conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (requirement_key) id
                FROM open_questions WHERE tenant_id = %s AND case_id = %s
                ORDER BY requirement_key, created_at DESC, id DESC
            )
            UPDATE open_questions AS q
            SET status = CASE WHEN q.severity = 'complex_human'
                THEN 'escalated_human' ELSE 'waiting_customer' END,
                closed_by_evidence_id = NULL, closed_at = NULL, updated_at = NOW(),
                case_version = %s
            FROM latest WHERE q.id = latest.id AND q.status = 'closed'
              AND q.closed_by_evidence_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM open_questions active
                  WHERE active.tenant_id = q.tenant_id AND active.case_id = q.case_id
                    AND active.requirement_key = q.requirement_key AND active.status <> 'closed'
              )
            """,
            (tenant_id, case_id, updated.case_version, evidence_id),
        )


def set_evidence_quarantine(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str,
    quarantine_status: str,
    rejection_reason: str = "",
    expected_case_version: int | None = None,
) -> EvidenceRecord:
    if quarantine_status not in _ALLOWED_QUARANTINE:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="invalid quarantine_status",
            details=(quarantine_status,),
        )

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            evidence = get_evidence(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                evidence_id=evidence_id,
                conn=conn,
            )
            if _is_hard_rejected(evidence) and quarantine_status != "rejected":
                raise DomainError(
                    code=ErrorCode.ATTACHMENT_REJECTED,
                    message="hard-rejected evidence cannot leave rejected status",
                    details=(
                        evidence_id,
                        evidence.rejection_reason or "hard reject",
                    ),
                )
            if evidence.classification == "translation_draft" and quarantine_status == "accepted_for_review":
                raise DomainError(ErrorCode.VALIDATION_ERROR, "请在材料清单里核对译文")

            bumped = bump_case_version_in_conn(
                conn=conn,
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_case_version=case.case_version,
                required=True,
            )
            reason = rejection_reason
            if _is_hard_rejected(evidence):
                reason = evidence.rejection_reason
            conn.execute(
                """
                UPDATE case_evidence
                SET quarantine_status = %s,
                    rejection_reason = %s,
                    case_version = %s
                WHERE tenant_id = %s AND case_id = %s AND id = %s
                """,
                (
                    quarantine_status,
                    reason,
                    bumped.case_version,
                    tenant_id,
                    case_id,
                    evidence_id,
                ),
            )

    return get_evidence(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )


def record_annotation(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str,
    field_key: str,
    field_value: str,
    note: str = "",
    expected_case_version: int | None = None,
) -> EvidenceAnnotation:
    """Human annotation on evidence. Does not alter original file bytes.

    Always bumps case_version in the same transaction as the annotation insert.
    """
    key = (field_key or "").strip()
    value = (field_value or "").strip()
    if not key or not value:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="field_key and field_value are required",
        )

    ann_id = uuid.uuid4().hex
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            evidence = get_evidence(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                evidence_id=evidence_id,
                conn=conn,
            )
            if evidence.quarantine_status == "rejected" or _is_hard_rejected(
                evidence
            ):
                raise DomainError(
                    code=ErrorCode.ATTACHMENT_REJECTED,
                    message="cannot annotate rejected evidence",
                    details=(evidence_id,),
                )

            bumped = bump_case_version_in_conn(
                conn=conn,
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_case_version=case.case_version,
                required=True,
            )
            conn.execute(
                """
                INSERT INTO evidence_annotations (
                    id, tenant_id, evidence_id, case_id, case_version,
                    field_key, field_value, note
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ann_id,
                    tenant_id,
                    evidence_id,
                    case_id,
                    bumped.case_version,
                    key,
                    value,
                    note or "",
                ),
            )
            bound_version = bumped.case_version

    return EvidenceAnnotation(
        id=ann_id,
        tenant_id=tenant_id,
        evidence_id=evidence_id,
        case_id=case_id,
        case_version=bound_version,
        field_key=key,
        field_value=value,
        note=note or "",
    )


def list_annotations(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str | None = None,
) -> list[EvidenceAnnotation]:
    with psycopg.connect(database_url) as conn:
        if evidence_id:
            rows = conn.execute(
                """
                SELECT id, tenant_id, evidence_id, case_id, case_version,
                       field_key, field_value, note
                FROM evidence_annotations
                WHERE tenant_id = %s AND case_id = %s AND evidence_id = %s
                  AND evidence_id IN (SELECT id FROM case_evidence WHERE deleted_at IS NULL)
                ORDER BY created_at DESC
                """,
                (tenant_id, case_id, evidence_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, tenant_id, evidence_id, case_id, case_version,
                       field_key, field_value, note
                FROM evidence_annotations
                WHERE tenant_id = %s AND case_id = %s
                  AND evidence_id IN (SELECT id FROM case_evidence WHERE deleted_at IS NULL)
                ORDER BY created_at DESC
                """,
                (tenant_id, case_id),
            ).fetchall()
    return [
        EvidenceAnnotation(
            id=r[0],
            tenant_id=r[1],
            evidence_id=r[2],
            case_id=r[3],
            case_version=r[4],
            field_key=r[5],
            field_value=r[6],
            note=r[7] or "",
        )
        for r in rows
    ]


def _row_to_ocr(row: tuple) -> EvidenceOcrResult:
    return EvidenceOcrResult(
        id=row[0],
        tenant_id=row[1],
        evidence_id=row[2],
        case_id=row[3],
        case_version=row[4],
        provider=row[5] or "",
        status=row[6] or "",
        extracted_text=row[7] or "",
        source_content_sha256=row[8] or "",
        detail=row[9] or "",
    )


def run_ocr_on_evidence(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str,
    file_store: LocalFileStore,
    expected_case_version: int | None = None,
    provider: OcrProvider | None = None,
) -> EvidenceOcrResult:
    """Extract derived OCR text; never mutates original quarantine bytes.

    Bumps case_version so prior confirm/approve bindings become stale (material
    derived artifact). Does not write evidence_annotations (no auto-confirm).
    """
    engine = provider or default_ocr_provider()
    ocr_id = uuid.uuid4().hex

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            evidence = get_evidence(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                evidence_id=evidence_id,
                conn=conn,
            )
            if evidence.quarantine_status == "rejected" or _is_hard_rejected(
                evidence
            ):
                raise DomainError(
                    code=ErrorCode.ATTACHMENT_REJECTED,
                    message="cannot OCR rejected evidence",
                    details=(evidence_id,),
                )

            path = file_store.path_for_file_id(evidence.file_id)
            if not path.is_file():
                raise DomainError(
                    code=ErrorCode.NOT_FOUND,
                    message="evidence original file missing on disk",
                    details=(evidence.file_id,),
                )
            original_bytes = path.read_bytes()
            live_digest = hashlib.sha256(original_bytes).hexdigest()
            if live_digest != evidence.content_sha256:
                raise DomainError(
                    code=ErrorCode.SYSTEM_BLOCKED,
                    message="evidence original bytes diverge from recorded sha256",
                    details=(evidence.content_sha256, live_digest),
                )

            extraction = engine.extract(
                original_bytes,
                filename=evidence.original_filename,
                content_type=evidence.content_type,
            )
            # Re-read + verify isolation: OCR must not have rewritten the file.
            after_bytes = path.read_bytes()
            after_digest = hashlib.sha256(after_bytes).hexdigest()
            if after_digest != evidence.content_sha256:
                raise DomainError(
                    code=ErrorCode.SYSTEM_BLOCKED,
                    message="OCR path mutated original evidence bytes",
                    details=(evidence.file_id,),
                )

            bumped = bump_case_version_in_conn(
                conn=conn,
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_case_version=case.case_version,
                required=True,
            )
            status = extraction.status
            if status not in {"completed", "failed", "unsupported", "pending"}:
                status = "failed"
            conn.execute(
                """
                INSERT INTO evidence_ocr_results (
                    id, tenant_id, evidence_id, case_id, case_version,
                    provider, status, extracted_text, source_content_sha256, detail
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ocr_id,
                    tenant_id,
                    evidence_id,
                    case_id,
                    bumped.case_version,
                    extraction.provider or engine.name,
                    status,
                    extraction.text or "",
                    evidence.content_sha256,
                    extraction.detail or "",
                ),
            )

    return get_ocr_result(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        ocr_id=ocr_id,
    )


def get_ocr_result(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    ocr_id: str,
) -> EvidenceOcrResult:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT id, tenant_id, evidence_id, case_id, case_version,
                   provider, status, extracted_text, source_content_sha256, detail
            FROM evidence_ocr_results
            WHERE tenant_id = %s AND case_id = %s AND id = %s
              AND evidence_id IN (SELECT id FROM case_evidence WHERE deleted_at IS NULL)
            """,
            (tenant_id, case_id, ocr_id),
        ).fetchone()
    if row is None:
        raise DomainError(
            code=ErrorCode.NOT_FOUND,
            message="OCR result not found",
            details=(ocr_id,),
        )
    return _row_to_ocr(row)


def list_ocr_results(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    evidence_id: str | None = None,
) -> list[EvidenceOcrResult]:
    with psycopg.connect(database_url) as conn:
        if evidence_id:
            rows = conn.execute(
                """
                SELECT id, tenant_id, evidence_id, case_id, case_version,
                       provider, status, extracted_text, source_content_sha256, detail
                FROM evidence_ocr_results
                WHERE tenant_id = %s AND case_id = %s AND evidence_id = %s
                  AND evidence_id IN (SELECT id FROM case_evidence WHERE deleted_at IS NULL)
                ORDER BY created_at DESC
                """,
                (tenant_id, case_id, evidence_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, tenant_id, evidence_id, case_id, case_version,
                       provider, status, extracted_text, source_content_sha256, detail
                FROM evidence_ocr_results
                WHERE tenant_id = %s AND case_id = %s
                  AND evidence_id IN (SELECT id FROM case_evidence WHERE deleted_at IS NULL)
                ORDER BY created_at DESC
                """,
                (tenant_id, case_id),
            ).fetchall()
    return [_row_to_ocr(r) for r in rows]


def latest_ocr_by_evidence(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> dict[str, EvidenceOcrResult]:
    """Map evidence_id → newest OCR result for workbench / API summaries."""
    items = list_ocr_results(
        database_url=database_url, tenant_id=tenant_id, case_id=case_id
    )
    out: dict[str, EvidenceOcrResult] = {}
    for item in items:
        if item.evidence_id not in out:
            out[item.evidence_id] = item
    return out
