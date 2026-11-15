"""IMAP poll → persist_inbound (allowlisted round-trip receive path)."""

from __future__ import annotations

import email
import imaplib
import ssl
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from typing import Protocol

from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.inbound import (
    InboundAttachment,
    InboundEmail,
    persist_inbound,
)
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
    typ, data = client.uid("SEARCH", None, "UNSEEN")
    if typ != "OK" or not data or not data[0]:
        return PollResult(
            messages=(),
            failures=(),
            mailbox=mailbox,
            uidvalidity=uidvalidity,
        )
    raw_ids = data[0]
    if not isinstance(raw_ids, (bytes, bytearray)):
        return PollResult(
            messages=(),
            failures=(),
            mailbox=mailbox,
            uidvalidity=uidvalidity,
        )
    uids = raw_ids.split()[-limit:]
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
                ),
                file_store=file_store,
            )
            attributed_case_id: str | None = None
            if (
                persisted.attribution_status == "unattributed"
                and not persisted.duplicate
            ):
                from visa_agent.case_service.outreach import (
                    try_auto_attribute_inbound_by_customer_email,
                )

                attributed_case_id = try_auto_attribute_inbound_by_customer_email(
                    settings=settings,
                    email_event_id=persisted.email_event_id,
                    from_addr=from_addr,
                )
            if attributed_case_id and not persisted.duplicate:
                from visa_agent.case_service.inbound_agent import (
                    process_inbound_after_attribution,
                )

                process_inbound_after_attribution(
                    settings=settings,
                    case_id=attributed_case_id,
                    email_event_id=persisted.email_event_id,
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
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        context = ssl.create_default_context()

    try:
        imap = imaplib.IMAP4_SSL(
            settings.email_imap_host,
            settings.email_imap_port,
            ssl_context=context,
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
