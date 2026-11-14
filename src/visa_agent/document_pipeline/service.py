"""S2.1 document pipeline: quarantine originals + human field annotations.

Never modifies original bytes. OCR auto-confirm is out of scope (TL decision).
Material changes bump case_version in the same transaction as the write.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.errors import UniqueViolation

from visa_agent.case_service.service import (
    _require_version,
    bump_case_version_in_conn,
    get_case,
)
from visa_agent.domain.enums import ErrorCode
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

                if source_email_event_id:
                    _require_attributed_inbound(
                        conn=conn,
                        tenant_id=tenant_id,
                        case_id=case_id,
                        email_event_id=source_email_event_id,
                    )

                dup = conn.execute(
                    """
                    SELECT id FROM case_evidence
                    WHERE tenant_id = %s AND case_id = %s AND content_sha256 = %s
                    """,
                    (tenant_id, case_id, digest),
                ).fetchone()
                if dup is not None:
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
                    conn.execute(
                        """
                        INSERT INTO case_evidence (
                            id, tenant_id, case_id, case_version, file_id,
                            content_sha256, original_filename, content_type,
                            byte_size, classification, quarantine_status,
                            rejection_reason, source_email_event_id
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s
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
               quarantine_status, rejection_reason, source_email_event_id
        FROM case_evidence
        WHERE tenant_id = %s AND case_id = %s AND id = %s
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
                   quarantine_status, rejection_reason, source_email_event_id
            FROM case_evidence
            WHERE tenant_id = %s AND case_id = %s
            ORDER BY created_at DESC
            """,
            (tenant_id, case_id),
        ).fetchall()
    return [_row_to_evidence(r) for r in rows]


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
