"""Inbound email persistence with atomic dedupe (IMAP deferred; API injection)."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.errors import UniqueViolation

from visa_agent.email_adapter.threading import single_message_id
from visa_agent.storage.files import LocalFileStore


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class InboundEmail:
    from_addr: str
    to_addrs: tuple[str, ...]
    subject: str
    body_text: str
    rfc_message_id: str | None = None
    provider_message_id: str | None = None
    claimed_case_id: str | None = None
    attachments: tuple[InboundAttachment, ...] = ()
    in_reply_to: str | None = None
    references: str | None = None


@dataclass(frozen=True, slots=True)
class InboundResult:
    email_event_id: str
    dedupe_key: str
    attribution_status: str
    duplicate: bool
    raw_file_id: str | None


def build_dedupe_key(mail: InboundEmail) -> str:
    if mail.provider_message_id:
        return f"provider:{mail.provider_message_id.strip()}"
    if mail.rfc_message_id:
        return f"rfc:{mail.rfc_message_id.strip()}"
    material = "|".join(
        [
            mail.from_addr.strip().lower(),
            ",".join(a.strip().lower() for a in mail.to_addrs),
            mail.subject.strip(),
            mail.body_text.strip(),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _attachments_payload(
    attachments: tuple[InboundAttachment, ...] | list[InboundAttachment],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for att in attachments or ():
        out.append(
            {
                "filename": att.filename,
                "content_type": att.content_type,
                "content_base64": base64.b64encode(att.data).decode("ascii"),
            }
        )
    return out


def attachments_from_payload(raw: object) -> tuple[InboundAttachment, ...]:
    if not isinstance(raw, list):
        return ()
    items: list[InboundAttachment] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        b64 = row.get("content_base64") or ""
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        items.append(
            InboundAttachment(
                filename=str(row.get("filename") or "attachment.bin"),
                content_type=str(
                    row.get("content_type") or "application/octet-stream"
                ),
                data=data,
            )
        )
    return tuple(items)


def _raw_payload_from_mail(mail: InboundEmail) -> bytes:
    return json.dumps(
        {
            "from_addr": mail.from_addr,
            "to_addrs": list(mail.to_addrs),
            "subject": mail.subject,
            "body_text": mail.body_text,
            "rfc_message_id": mail.rfc_message_id,
            "provider_message_id": mail.provider_message_id,
            "claimed_case_id": mail.claimed_case_id,
            "attachments": _attachments_payload(mail.attachments),
            "in_reply_to": mail.in_reply_to,
            "references": mail.references,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _raw_payload_from_row(
    *,
    from_addr: str | None,
    to_addrs: object,
    subject: str | None,
    body_text: str | None,
    rfc_message_id: str | None,
    provider_message_id: str | None,
    claimed_case_id: str | None,
    in_reply_to: str | None,
    references: str | None,
    attachments: object = None,
) -> bytes:
    if isinstance(to_addrs, str):
        to_addrs = json.loads(to_addrs)
    return json.dumps(
        {
            "from_addr": from_addr,
            "to_addrs": list(to_addrs or []),
            "subject": subject or "",
            "body_text": body_text or "",
            "rfc_message_id": rfc_message_id,
            "provider_message_id": provider_message_id,
            "claimed_case_id": claimed_case_id,
            "attachments": attachments if isinstance(attachments, list) else [],
            "in_reply_to": in_reply_to,
            "references": references,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _file_present(file_store: LocalFileStore, raw_file_id: str | None) -> bool:
    if not raw_file_id:
        return False
    return (file_store.root / f"{raw_file_id}.json").is_file()


def _attach_original(
    *,
    file_store: LocalFileStore,
    tenant_id: str,
    dedupe_key: str,
    raw: bytes,
) -> str:
    raw_file_id, _, _ = file_store.put_bytes_deterministic(
        f"{tenant_id}:{dedupe_key}",
        raw,
        overwrite=True,
    )
    return raw_file_id


def _backfill_original(
    *,
    conn: psycopg.Connection,
    tenant_id: str,
    dedupe_key: str,
    file_store: LocalFileStore,
    row: tuple,
) -> str:
    """Persist original from the committed DB row when on-disk original is missing."""
    (
        event_id,
        _attribution,
        _raw_file_id,
        from_addr,
        to_addrs,
        subject,
        body_text,
        rfc_message_id,
        provider_message_id,
        claimed_case_id,
        in_reply_to,
        references,
    ) = row
    raw = _raw_payload_from_row(
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject=subject,
        body_text=body_text,
        rfc_message_id=rfc_message_id,
        provider_message_id=provider_message_id,
        claimed_case_id=claimed_case_id,
        in_reply_to=in_reply_to,
        references=references,
    )
    raw_file_id = _attach_original(
        file_store=file_store,
        tenant_id=tenant_id,
        dedupe_key=dedupe_key,
        raw=raw,
    )
    conn.execute(
        """
        UPDATE email_events
        SET raw_file_id = %s
        WHERE id = %s
        """,
        (raw_file_id, event_id),
    )
    conn.commit()
    return raw_file_id


def _existing_result(
    *,
    conn: psycopg.Connection,
    tenant_id: str,
    dedupe_key: str,
    file_store: LocalFileStore,
    existing: tuple,
) -> InboundResult:
    raw_file_id = existing[2]
    if not _file_present(file_store, raw_file_id):
        raw_file_id = _backfill_original(
            conn=conn,
            tenant_id=tenant_id,
            dedupe_key=dedupe_key,
            file_store=file_store,
            row=existing,
        )
        return InboundResult(
            email_event_id=existing[0],
            dedupe_key=dedupe_key,
            attribution_status=existing[1],
            duplicate=False,
            raw_file_id=raw_file_id,
        )
    return InboundResult(
        email_event_id=existing[0],
        dedupe_key=dedupe_key,
        attribution_status="duplicate",
        duplicate=True,
        raw_file_id=raw_file_id,
    )


_SELECT_EXISTING = """
    SELECT id, attribution_status, raw_file_id,
           from_addr, to_addrs, subject, body_text,
           rfc_message_id, provider_message_id, claimed_case_id,
           in_reply_to, email_references
    FROM email_events
    WHERE tenant_id = %s AND dedupe_key = %s
"""


def persist_inbound(
    *,
    database_url: str,
    tenant_id: str,
    mail: InboundEmail,
    file_store: LocalFileStore,
) -> InboundResult:
    """Insert the inbound row first; the INSERT winner alone writes the original."""
    dedupe_key = build_dedupe_key(mail)
    event_id = uuid.uuid4().hex

    with psycopg.connect(database_url) as conn:
        existing = conn.execute(
            _SELECT_EXISTING, (tenant_id, dedupe_key)
        ).fetchone()
        if existing is not None:
            return _existing_result(
                conn=conn,
                tenant_id=tenant_id,
                dedupe_key=dedupe_key,
                file_store=file_store,
                existing=existing,
            )

        inserted_id: str | None = None
        attribution = "unattributed"
        raw_file_id: str | None = None
        try:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO email_events (
                        id, tenant_id, direction, provider_message_id, rfc_message_id,
                        dedupe_key, from_addr, to_addrs, subject, body_text, raw_file_id,
                        case_id, claimed_case_id, attribution_status, processing_status,
                        in_reply_to, email_references
                    ) VALUES (
                        %s, %s, 'inbound', %s, %s,
                        %s, %s, %s::jsonb, %s, %s, NULL,
                        NULL, %s, 'unattributed', 'unprocessed', %s, %s
                    )
                    ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
                    RETURNING id, attribution_status
                    """,
                    (
                        event_id,
                        tenant_id,
                        mail.provider_message_id,
                        single_message_id(mail.rfc_message_id),
                        dedupe_key,
                        mail.from_addr,
                        json.dumps(list(mail.to_addrs)),
                        mail.subject,
                        mail.body_text,
                        mail.claimed_case_id,
                        mail.in_reply_to,
                        mail.references,
                    ),
                ).fetchone()
                if row is not None:
                    # Same request that won the insert attaches the original.
                    raw_file_id = _attach_original(
                        file_store=file_store,
                        tenant_id=tenant_id,
                        dedupe_key=dedupe_key,
                        raw=_raw_payload_from_mail(mail),
                    )
                    conn.execute(
                        """
                        UPDATE email_events
                        SET raw_file_id = %s
                        WHERE id = %s
                        """,
                        (raw_file_id, row[0]),
                    )
                    inserted_id = row[0]
                    attribution = row[1]
        except UniqueViolation:
            inserted_id = None

        if inserted_id is None:
            existing = conn.execute(
                _SELECT_EXISTING, (tenant_id, dedupe_key)
            ).fetchone()
            if existing is None:
                raise RuntimeError("dedupe conflict but row missing")
            return _existing_result(
                conn=conn,
                tenant_id=tenant_id,
                dedupe_key=dedupe_key,
                file_store=file_store,
                existing=existing,
            )

        return InboundResult(
            email_event_id=inserted_id,
            dedupe_key=dedupe_key,
            attribution_status=attribution,
            duplicate=False,
            raw_file_id=raw_file_id,
        )
