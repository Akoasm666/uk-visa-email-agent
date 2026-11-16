"""W09 delivery — assemble packages from accepted evidence only."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from visa_agent.case_service.service import get_case
from visa_agent.domain.enums import DeliveryKind, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.storage.files import LocalFileStore

_KNOWN_KINDS = frozenset(k.value for k in DeliveryKind)
# Only human-accepted evidence may enter a delivery package (no unconfirmed fill).
_ACCEPTED = "accepted_for_review"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ManifestItem:
    evidence_id: str
    file_id: str
    content_sha256: str
    original_filename: str
    classification: str
    byte_size: int
    frozen_rel: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryPackage:
    package_id: str
    tenant_id: str
    case_id: str
    case_version: int
    package_kind: str
    items: tuple[ManifestItem, ...]
    manifest_digest: str

    def manifest_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "evidence_id": i.evidence_id,
                "file_id": i.file_id,
                "content_sha256": i.content_sha256,
                "original_filename": i.original_filename,
                "classification": i.classification,
                "byte_size": i.byte_size,
                "frozen_rel": i.frozen_rel,
            }
            for i in self.items
        ]


def _safe_filename(name: str) -> str:
    base = Path(name or "attachment.bin").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "attachment.bin"
    return cleaned[:180]


def _digest_manifest(items: list[ManifestItem]) -> str:
    material = json.dumps(
        [
            {
                "evidence_id": i.evidence_id,
                "file_id": i.file_id,
                "content_sha256": i.content_sha256,
                "original_filename": i.original_filename,
                "classification": i.classification,
                "byte_size": i.byte_size,
                "frozen_rel": i.frozen_rel,
            }
            for i in sorted(items, key=lambda x: x.evidence_id)
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_package(row: tuple) -> DeliveryPackage:
    raw = row[5] if isinstance(row[5], list) else json.loads(row[5] or "[]")
    items = tuple(
        ManifestItem(
            evidence_id=str(i["evidence_id"]),
            file_id=str(i["file_id"]),
            content_sha256=str(i["content_sha256"]),
            original_filename=str(i.get("original_filename") or ""),
            classification=str(i.get("classification") or "unclassified"),
            byte_size=int(i.get("byte_size") or 0),
            frozen_rel=str(i.get("frozen_rel") or ""),
        )
        for i in raw
    )
    return DeliveryPackage(
        package_id=row[0],
        tenant_id=row[1],
        case_id=row[2],
        case_version=int(row[3]),
        package_kind=row[4],
        items=items,
        manifest_digest=row[6],
    )


def assemble_package(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    package_kind: str,
    file_store: LocalFileStore,
    evidence_ids: list[str] | None = None,
    expected_case_version: int | None = None,
) -> DeliveryPackage:
    """Snapshot accepted evidence into an immutable delivery package.

    File bytes are copied under ``packages/{package_id}/…`` so later mutation of
    the live evidence path cannot change what will be sent.
    """
    kind = (package_kind or "").strip()
    if kind not in _KNOWN_KINDS:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="unknown package_kind",
            details=(kind,),
        )
    package_id = uuid.uuid4().hex
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            if expected_case_version is not None and case.case_version != expected_case_version:
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message="case_version mismatch",
                    details=(str(expected_case_version), str(case.case_version)),
                )
            if evidence_ids:
                wanted = list(dict.fromkeys(evidence_ids))
                rows = conn.execute(
                    """
                    SELECT id, file_id, content_sha256, original_filename,
                           classification, byte_size, quarantine_status,
                           advisor_reviewed_version = case_version AS advisor_reviewed
                    FROM case_evidence
                    WHERE tenant_id = %s AND case_id = %s AND id = ANY(%s) AND deleted_at IS NULL
                    ORDER BY created_at ASC, id ASC
                    """,
                    (tenant_id, case_id, wanted),
                ).fetchall()
                found = {r[0] for r in rows}
                missing = [e for e in wanted if e not in found]
                if missing:
                    raise DomainError(
                        code=ErrorCode.NOT_FOUND,
                        message="evidence not found on case",
                        details=tuple(missing),
                    )
            else:
                rows = conn.execute(
                    """
                    SELECT id, file_id, content_sha256, original_filename,
                           classification, byte_size, quarantine_status,
                           advisor_reviewed_version = case_version AS advisor_reviewed
                    FROM case_evidence
                    WHERE tenant_id = %s AND case_id = %s AND deleted_at IS NULL
                      AND classification <> 'translation_draft'
                    ORDER BY created_at ASC, id ASC
                    """,
                    (tenant_id, case_id),
                ).fetchall()

            items: list[ManifestItem] = []
            for r in rows:
                if r[4] == "translation_draft":
                    raise DomainError(ErrorCode.VALIDATION_ERROR, "unreviewed translation draft cannot enter a package")
                reviewed_itinerary = r[4] == "itinerary" and r[6] == "isolated" and r[7]
                if r[6] != _ACCEPTED and not reviewed_itinerary:
                    raise DomainError(
                        code=ErrorCode.VALIDATION_ERROR,
                        message="only accepted_for_review evidence may enter a package",
                        details=(r[0], r[6] or ""),
                    )
                evidence_id = str(r[0])
                file_id = str(r[1])
                expected_sha = str(r[2])
                original_filename = r[3] or ""
                live_path = file_store.path_for_file_id(file_id)
                if not live_path.is_file():
                    raise DomainError(
                        code=ErrorCode.VALIDATION_ERROR,
                        message="evidence file missing at assemble time",
                        details=(evidence_id, file_id),
                    )
                data = live_path.read_bytes()
                live_sha = hashlib.sha256(data).hexdigest()
                if live_sha != expected_sha:
                    raise DomainError(
                        code=ErrorCode.VALIDATION_ERROR,
                        message="evidence bytes do not match stored content_sha256",
                        details=(evidence_id, expected_sha, live_sha),
                    )
                if int(r[5] or 0) and int(r[5]) != len(data):
                    raise DomainError(
                        code=ErrorCode.VALIDATION_ERROR,
                        message="evidence byte_size does not match file bytes",
                        details=(evidence_id,),
                    )
                safe = _safe_filename(original_filename)
                frozen_rel = f"packages/{package_id}/{evidence_id}__{safe}"
                frozen_path = file_store.root / frozen_rel
                frozen_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = frozen_path.with_suffix(frozen_path.suffix + f".tmp-{uuid.uuid4().hex}")
                tmp.write_bytes(data)
                tmp.replace(frozen_path)
                items.append(
                    ManifestItem(
                        evidence_id=evidence_id,
                        file_id=file_id,
                        content_sha256=expected_sha,
                        original_filename=original_filename,
                        classification=r[4] or "unclassified",
                        byte_size=len(data),
                        frozen_rel=frozen_rel,
                    )
                )
            if not items:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="package requires at least one accepted evidence item",
                )
            digest = _digest_manifest(items)
            payload = [
                {
                    "evidence_id": i.evidence_id,
                    "file_id": i.file_id,
                    "content_sha256": i.content_sha256,
                    "original_filename": i.original_filename,
                    "classification": i.classification,
                    "byte_size": i.byte_size,
                    "frozen_rel": i.frozen_rel,
                }
                for i in items
            ]
            conn.execute(
                """
                INSERT INTO delivery_packages (
                    id, tenant_id, case_id, case_version, package_kind,
                    manifest_json, manifest_digest
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    package_id,
                    tenant_id,
                    case_id,
                    case.case_version,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    digest,
                ),
            )
            # Do not clear unprocessed inbound here — that requires an explicit
            # mark_inbound_processed action so "stop delivery" mail stays blocking.
    return get_package(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        package_id=package_id,
    )


def get_package(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    package_id: str,
) -> DeliveryPackage:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT id, tenant_id, case_id, case_version, package_kind,
                   manifest_json, manifest_digest
            FROM delivery_packages
            WHERE tenant_id = %s AND case_id = %s AND id = %s
            """,
            (tenant_id, case_id, package_id),
        ).fetchone()
    if row is None:
        raise DomainError(
            code=ErrorCode.NOT_FOUND,
            message="delivery package not found",
            details=(package_id,),
        )
    return _row_to_package(row)


def verify_package_bytes(
    *,
    file_store: LocalFileStore,
    package: DeliveryPackage,
) -> list[tuple[str, bytes]]:
    """Verify frozen package bytes against the snapshotted manifest; return attachments.

    Returns ``[(original_filename, data), ...]`` in manifest order. Any mismatch
    of digest, size, missing frozen file, or mutated bytes raises DELIVERY_GATE_FAILED.
    """
    attachments: list[tuple[str, bytes]] = []
    for item in package.items:
        if not item.frozen_rel:
            raise DomainError(
                code=ErrorCode.DELIVERY_GATE_FAILED,
                message="package item missing frozen blob path",
                details=(item.evidence_id,),
            )
        frozen_path = file_store.root / item.frozen_rel
        if not frozen_path.is_file():
            raise DomainError(
                code=ErrorCode.DELIVERY_GATE_FAILED,
                message="frozen package file missing at send time",
                details=(item.evidence_id, item.frozen_rel),
            )
        data = frozen_path.read_bytes()
        live_sha = hashlib.sha256(data).hexdigest()
        if live_sha != item.content_sha256:
            raise DomainError(
                code=ErrorCode.DELIVERY_GATE_FAILED,
                message="frozen package bytes do not match manifest hash",
                details=(item.evidence_id, item.content_sha256, live_sha),
            )
        if len(data) != item.byte_size:
            raise DomainError(
                code=ErrorCode.DELIVERY_GATE_FAILED,
                message="frozen package byte_size mismatch",
                details=(item.evidence_id,),
            )
        if not item.original_filename:
            raise DomainError(
                code=ErrorCode.DELIVERY_GATE_FAILED,
                message="package item missing original_filename",
                details=(item.evidence_id,),
            )
        attachments.append((item.original_filename, data))
    recomputed = _digest_manifest(list(package.items))
    if recomputed != package.manifest_digest:
        raise DomainError(
            code=ErrorCode.DELIVERY_GATE_FAILED,
            message="manifest digest mismatch",
            details=(package.manifest_digest, recomputed),
        )
    return attachments


def verify_manifest_unchanged(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    package: DeliveryPackage,
    file_store: LocalFileStore | None = None,
) -> list[tuple[str, bytes]] | None:
    """Refuse send if frozen package content no longer matches the assembled digest.

    When ``file_store`` is provided, returns verified attachment bytes.
    """
    if file_store is None:
        # Legacy call sites — still require frozen_rel + digest integrity.
        for item in package.items:
            if not item.frozen_rel or not item.content_sha256:
                raise DomainError(
                    code=ErrorCode.DELIVERY_GATE_FAILED,
                    message="package missing frozen content binding",
                    details=(item.evidence_id,),
                )
        live = _digest_manifest(list(package.items))
        if live != package.manifest_digest:
            raise DomainError(
                code=ErrorCode.DELIVERY_GATE_FAILED,
                message="manifest digest mismatch",
                details=(package.manifest_digest, live),
            )
        return None
    return verify_package_bytes(file_store=file_store, package=package)
