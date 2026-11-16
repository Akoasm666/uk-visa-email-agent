"""IMAP poll → persist_inbound (allowlisted round-trip receive path)."""

from __future__ import annotations

import email
import imaplib
from collections import defaultdict
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from typing import Protocol

import psycopg

from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.email_adapter.inbound import (
    InboundAttachment,
    InboundEmail,
    persist_inbound,
)
from visa_agent.email_adapter.proxy import SocksIMAP4SSL
from visa_agent.email_adapter.threading import message_ids
from visa_agent.email_adapter.tls import mail_tls_context
from visa_agent.storage.files import LocalFileStore


@dataclass(frozen=True, slots=True)
class PolledInbound:
    email_event_id: str
    subject: str
    from_addr: str
    duplicate: bool
    provider_message_id: str | None
    uid: str
    uidvalidity: str


@dataclass(frozen=True, slots=True)
class PollFailure:
    uid: str
    uidvalidity: str
    provider_message_id: str
    error: str
    subject: str = ""
    from_addr: str = ""


@dataclass(frozen=True, slots=True)
class PollResult:
    messages: tuple[PolledInbound, ...]
    failures: tuple[PollFailure, ...]
    mailbox: str
    uidvalidity: str

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def partial(self) -> bool:
        return bool(self.messages) and bool(self.failures)

    @property
    def success(self) -> bool:
        """True only when every attempted message persisted (or inbox was empty)."""
        return not self.failures


class ImapUidClient(Protocol):
    """UID-capable mailbox handle (real imaplib or test double)."""

    def uid(
        self, command: str, *args: str | bytes | None
    ) -> tuple[str, list[bytes | tuple[bytes, bytes] | None] | None]: ...


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _body_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _extract_attachments(msg: Message) -> tuple[InboundAttachment, ...]:
    """Pull attachment parts (and inline non-text binaries) from a MIME message."""
    items: list[InboundAttachment] = []
    if not msg.is_multipart():
        return ()
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if filename:
            filename = _decode_header(filename)
        ctype = part.get_content_type() or "application/octet-stream"
        is_attachment = "attachment" in disp.lower()
        is_inline_file = bool(filename) and ctype not in {
            "text/plain",
            "text/html",
        }
        if not (is_attachment or is_inline_file):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        items.append(
            InboundAttachment(
                filename=filename or "attachment.bin",
                content_type=ctype,
                data=bytes(payload),
            )
        )
    return tuple(items)


def _require_imap(settings: Settings) -> None:
    if not (
        settings.email_imap_host
        and settings.email_imap_user
        and settings.email_imap_password
    ):
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="IMAP is not configured",
            details=("set EMAIL_IMAP_HOST/USER/PASSWORD in .env",),
        )


def stable_imap_provider_id(
    *,
    imap_user: str,
    mailbox: str,
    uidvalidity: str,
    uid: str,
) -> str:
    """Stable id: account + mailbox + UIDVALIDITY + UID (not sequence numbers)."""
    return (
        f"imap:{imap_user.strip().lower()}"
        f":{mailbox.strip()}"
        f":{uidvalidity.strip()}"
        f":{uid.strip()}"
    )


def _extract_fetch_body(
    fetched: list[bytes | tuple[bytes, bytes] | None] | None,
) -> bytes | None:
    if not fetched:
        return None
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2:
            raw = item[1]
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)
    return None


def _unseen_uids(client: ImapUidClient, *criteria: str) -> list[bytes]:
    typ, data = client.uid("SEARCH", None, "UNSEEN", *criteria)
    if typ != "OK":
        raise DomainError(code=ErrorCode.SYSTEM_BLOCKED, message="IMAP search failed")
    return data[0].split() if data and isinstance(data[0], bytes) else []


def _case_thread_uids(
    client: ImapUidClient, settings: Settings, limit: int,
) -> list[bytes]:
    """Personal inbox mode: fetch full MIME only after verifying sender and thread."""
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT lower(trim(c.customer_email)), o.rfc_message_id
            FROM outbox_messages o
            JOIN cases c ON c.tenant_id = o.tenant_id AND c.id = o.case_id
            WHERE c.tenant_id = %s AND c.status NOT IN (%s, %s)
              AND lower(trim(c.customer_email)) = ANY(%s)
              AND o.status = 'provider_accepted' AND o.rfc_message_id IS NOT NULL
            """,
            (settings.tenant_id, CaseStatus.CLOSED.value, CaseStatus.DELIVERED.value,
             sorted(settings.outbound_allowlist)),
        ).fetchall()
    anchors: dict[str, set[str]] = defaultdict(set)
    for sender, message_id in rows:
        if normalize_email(sender) == sender and sender.isascii():
            anchors[sender].add(message_id)

    candidates: set[bytes] = set()
    for sender in anchors:
        quoted = '"' + sender.replace("\\", "\\\\").replace('"', '\\"') + '"'
        candidates.update(_unseen_uids(client, "FROM", quoted))

    matched: list[bytes] = []
    for uid in sorted(candidates, key=int):
        typ, data = client.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES)])",
        )
        headers = _extract_fetch_body(data) if typ == "OK" else None
        if headers is None:
            raise DomainError(code=ErrorCode.SYSTEM_BLOCKED, message="IMAP header fetch failed")
        message = email.message_from_bytes(headers)
        sender = normalize_email(_decode_header(message.get("From")))
        references = message_ids(message.get("In-Reply-To")) + message_ids(message.get("References"))
        if sender and anchors.get(sender, set()).intersection(references):
            matched.append(uid)
            if len(matched) == limit:
                break
    return matched


def _process_unseen(
    *,
    client: ImapUidClient,
    settings: Settings,
    file_store: LocalFileStore,
    limit: int,
    mailbox: str,
    uidvalidity: str,
) -> PollResult:
    """UID SEARCH UNSEEN + BODY.PEEK; mark \\Seen only after successful persist."""
    uids = (
        _case_thread_uids(client, settings, limit)
        if settings.email_imap_case_threads_only
        else _unseen_uids(client)[:limit]
    )
    messages: list[PolledInbound] = []
    failures: list[PollFailure] = []

    for uid_b in uids:
        uid = (
            uid_b.decode()
            if isinstance(uid_b, (bytes, bytearray))
            else str(uid_b)
        )
        provider_id = stable_imap_provider_id(
            imap_user=settings.email_imap_user,
            mailbox=mailbox,
            uidvalidity=uidvalidity,
            uid=uid,
        )
        typ, fetched = client.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            failures.append(
                PollFailure(
                    uid=uid,
                    uidvalidity=uidvalidity,
                    provider_message_id=provider_id,
                    error=f"UID FETCH failed: {typ}",
                )
            )
            continue
        raw = _extract_fetch_body(fetched)  # type: ignore[arg-type]
        if raw is None:
            failures.append(
                PollFailure(
                    uid=uid,
                    uidvalidity=uidvalidity,
                    provider_message_id=provider_id,
                    error="UID FETCH returned no body",
                )
            )
            continue

        msg = email.message_from_bytes(raw)
        from_addr = _decode_header(msg.get("From"))
        if "<" in from_addr and ">" in from_addr:
            from_addr = from_addr.split("<", 1)[1].split(">", 1)[0].strip()
        to_raw = _decode_header(msg.get("To"))
        to_addrs = tuple(
            p.strip()
            for p in to_raw.replace(";", ",").split(",")
            if p.strip()
        )
        subject = _decode_header(msg.get("Subject"))
        rfc_id = (msg.get("Message-ID") or "").strip() or None
        # Prefer RFC Message-ID when present; otherwise stable IMAP triple.
        dedupe_provider_id = rfc_id or provider_id

        try:
            persisted = persist_inbound(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                mail=InboundEmail(
                    from_addr=from_addr,
                    to_addrs=to_addrs or (settings.email_from,),
                    subject=subject,
                    body_text=_body_text(msg),
                    rfc_message_id=rfc_id,
                    provider_message_id=dedupe_provider_id,
                    attachments=_extract_attachments(msg),
                    in_reply_to=" ".join(map(str, msg.get_all("In-Reply-To", ()))) or None,
                    references=" ".join(map(str, msg.get_all("References", ()))) or None,
                ),
                file_store=file_store,
            )
            from visa_agent.case_service.inbound_agent import process_persisted_inbound

            process_persisted_inbound(
                settings=settings, email_event_id=persisted.email_event_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Leave UNSEEN so the next poll can retry this message.
            failures.append(
                PollFailure(
                    uid=uid,
                    uidvalidity=uidvalidity,
                    provider_message_id=dedupe_provider_id,
                    error=str(exc),
                    subject=subject,
                    from_addr=from_addr,
                )
            )
            continue

        client.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        messages.append(
            PolledInbound(
                email_event_id=persisted.email_event_id,
                subject=subject,
                from_addr=from_addr,
                duplicate=persisted.duplicate,
                provider_message_id=dedupe_provider_id,
                uid=uid,
                uidvalidity=uidvalidity,
            )
        )

    return PollResult(
        messages=tuple(messages),
        failures=tuple(failures),
        mailbox=mailbox,
        uidvalidity=uidvalidity,
    )


def _uidvalidity_from_imap(imap: imaplib.IMAP4) -> str:
    values = imap.untagged_responses.get("UIDVALIDITY")
    if not values:
        raise DomainError(
            code=ErrorCode.SYSTEM_BLOCKED,
            message="IMAP SELECT did not return UIDVALIDITY",
        )
    raw = values[-1]
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode()
    return str(raw)


def poll_imap_inbox(
    *,
    settings: Settings,
    file_store: LocalFileStore,
    limit: int = 10,
    mailbox: str = "INBOX",
    client: ImapUidClient | None = None,
    uidvalidity: str | None = None,
) -> PollResult:
    """Fetch recent unseen messages and persist as inbound events."""
    _require_imap(settings)
    limit = max(1, min(limit, 50))

    if client is not None:
        if not uidvalidity:
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="uidvalidity is required when injecting an IMAP client",
            )
        return _process_unseen(
            client=client,
            settings=settings,
            file_store=file_store,
            limit=limit,
            mailbox=mailbox,
            uidvalidity=uidvalidity,
        )

    try:
        endpoint = (settings.email_imap_host, settings.email_imap_port)
        context = mail_tls_context()
        imap = (
            SocksIMAP4SSL(*endpoint, proxy_url=settings.email_proxy_url, ssl_context=context, timeout=30)
            if settings.email_proxy_url
            else imaplib.IMAP4_SSL(*endpoint, ssl_context=context, timeout=30)
        )
        try:
            imap.login(settings.email_imap_user, settings.email_imap_password)
            typ, _ = imap.select(mailbox, readonly=False)
            if typ != "OK":
                raise DomainError(
                    code=ErrorCode.SYSTEM_BLOCKED,
                    message=f"IMAP select failed for {mailbox}",
                )
            uv = _uidvalidity_from_imap(imap)
            return _process_unseen(
                client=imap,
                settings=settings,
                file_store=file_store,
                limit=limit,
                mailbox=mailbox,
                uidvalidity=uv,
            )
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            code=ErrorCode.SYSTEM_BLOCKED,
            message="IMAP poll failed",
            details=(str(exc),),
        ) from exc
