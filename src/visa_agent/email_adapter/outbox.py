"""Outbound outbox with W00 gates + optional SMTP dispatch.

Until case_service exists, only caseless ordinary_reply test sends are allowed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from psycopg.errors import UniqueViolation

from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus, ErrorCode, OutboundMessageKind
from visa_agent.domain.errors import DomainError
from visa_agent.domain.models import OutboundGateInput, evaluate_outbound_gate
from visa_agent.email_adapter.allowlist import effective_outbound_allowlist
from visa_agent.email_adapter.errors import (
    SendPartialAcceptedError,
    SendStatusUnknownError,
)
from visa_agent.email_adapter.smtp_client import send_smtp
from visa_agent.email_adapter.threading import case_message_id

# Re-export for callers/tests that import from outbox.
__all__ = ["OutboxRequest", "OutboxResult", "SendStatusUnknownError", "enqueue_and_maybe_send"]

SendFn = Callable[[Settings, list[str], str, str], str]
INBOUND_REPLY_OPERATION_PREFIX = "inbound-reply:"
MATERIAL_CONFIRMATION_OPERATION_PREFIX = "case-material-confirmation:"
TAKEOVER_REPLY_OPERATION_PREFIX = "advisor-takeover-reply:"
CLOSURE_NOTICE_OPERATION_PREFIX = "case-closure:"


def closure_notice_operation_id(tenant_id: str, case_id: str, closure_seq: int) -> str:
    return f"{CLOSURE_NOTICE_OPERATION_PREFIX}{tenant_id}:{case_id}:{closure_seq}"

_NO_RESEND_STATUSES = frozenset(
    {"processing", "provider_accepted", "status_unknown", "partial_accepted"}
)


@dataclass(frozen=True, slots=True)
class OutboxRequest:
    kind: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    send_operation_id: str | None = None
    send_now: bool = True
    case_id: str | None = None
    case_version: int | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachment_evidence_id: str | None = None
    advisor_authored: bool = False
    body_html: str | None = None
    attachment_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OutboxResult:
    outbox_id: str
    status: str
    block_reasons: tuple[str, ...]
    provider_response: str | None
    email_event_id: str | None
    send_operation_id: str
    resent_blocked: bool = False


def build_send_operation_id(
    *,
    tenant_id: str,
    kind: str,
    recipients: tuple[str, ...],
    subject: str,
    body_text: str,
    explicit: str | None = None,
    case_id: str | None = None,
    case_version: int | None = None,
    package_id: str | None = None,
) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    # Ordinary replies keep the S1/S3 material (no case/package binding) so
    # upgraded installs still hit prior outbox rows. Package sends include
    # case_id + case_version + package_id to avoid cross-package collisions.
    parts = [
        tenant_id,
        kind,
    ]
    if package_id:
        parts.extend(
            [
                case_id or "",
                str(case_version) if case_version is not None else "",
                package_id,
            ]
        )
    parts.extend(
        [
            ",".join(sorted(r.strip().lower() for r in recipients)),
            subject.strip(),
            body_text.strip(),
        ]
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _normalize_recipients(recipients: Any) -> tuple[str, ...]:
    if isinstance(recipients, str):
        recipients = json.loads(recipients)
    return tuple(sorted(str(r).strip().lower() for r in recipients))


def _content_matches(row: tuple, req: OutboxRequest) -> bool:
    # row: id, status, block_reasons, provider_response, email_event_id,
    #      send_operation_id, recipients, subject, body_text, kind,
    #      case_id, in_reply_to, email_references, attachment_evidence_id, case_version, body_html,
    #      attachment_evidence_ids
    stored_recipients = _normalize_recipients(row[6])
    req_recipients = _normalize_recipients(req.recipients)
    return (
        (row[9] or "") == req.kind
        and stored_recipients == req_recipients
        and (row[7] or "") == req.subject
        and (row[8] or "") == req.body_text
        and row[10] == req.case_id
        and row[11] == req.in_reply_to
        and (row[12] or "") == " ".join(req.references)
        and row[13] == req.attachment_evidence_id
        and (not req.advisor_authored or row[14] == req.case_version)
        and row[15] == req.body_html
        and tuple(row[16]) == req.attachment_evidence_ids
    )


def _s1_precheck(req: OutboxRequest) -> list[str]:
    reasons: list[str] = []
    if req.kind not in {OutboundMessageKind.ORDINARY_REPLY, OutboundMessageKind.CASE_CLOSURE_NOTICE}:
        reasons.append("VALIDATION_ERROR")
    if req.kind == OutboundMessageKind.CASE_CLOSURE_NOTICE and (
        not req.case_id or not req.case_version or req.attachment_evidence_id
    ):
        reasons.append("VALIDATION_ERROR")
    if len(req.recipients) != 1:
        reasons.append("VALIDATION_ERROR")
    takeover_reply = (req.send_operation_id or "").strip().startswith(TAKEOVER_REPLY_OPERATION_PREFIX)
    if takeover_reply != req.advisor_authored or (takeover_reply and not (
        req.kind == OutboundMessageKind.ORDINARY_REPLY and req.case_id and req.case_version
        and not req.attachment_evidence_id
    )):
        reasons.append("VALIDATION_ERROR")
    confirmation = (req.send_operation_id or "").startswith(MATERIAL_CONFIRMATION_OPERATION_PREFIX)
    if confirmation and not (
        req.case_id and req.case_version and req.in_reply_to
    ):
        reasons.append("VALIDATION_ERROR")
    if req.attachment_evidence_ids and not (
        confirmation and req.kind == OutboundMessageKind.ORDINARY_REPLY and not req.attachment_evidence_id
    ):
        reasons.append("VALIDATION_ERROR")
    if (req.attachment_evidence_id or req.body_html) and not (
        req.kind == OutboundMessageKind.ORDINARY_REPLY and req.case_id
        and (req.send_operation_id or "").startswith(INBOUND_REPLY_OPERATION_PREFIX)
    ):
        reasons.append("VALIDATION_ERROR")
    return reasons


def _gate_input(
    settings: Settings, req: OutboxRequest, *, case_lock: psycopg.Connection | None = None,
) -> OutboundGateInput:
    case = None
    confirmation = (req.send_operation_id or "").startswith(MATERIAL_CONFIRMATION_OPERATION_PREFIX)
    has_unprocessed = False
    inbound_reply = (req.send_operation_id or "").startswith(INBOUND_REPLY_OPERATION_PREFIX)
    closure_notice = req.kind == OutboundMessageKind.CASE_CLOSURE_NOTICE
    takeover_reply = (req.send_operation_id or "").strip().startswith(TAKEOVER_REPLY_OPERATION_PREFIX)
    if (confirmation or inbound_reply or closure_notice or takeover_reply) and req.case_id:
        from visa_agent.case_service.service import get_case

        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=req.case_id,
            conn=case_lock,
            for_update=case_lock is not None,
        )
        if confirmation:
            from visa_agent.send_control.service import _has_unprocessed_inbound

            with psycopg.connect(settings.database_url) as conn:
                has_unprocessed = _has_unprocessed_inbound(
                    conn, tenant_id=case.tenant_id, case_id=case.id,
                )
    valid_closure = bool(
        case and closure_notice and case.status == CaseStatus.CLOSED
        and req.send_operation_id == closure_notice_operation_id(case.tenant_id, case.id, case.closure_seq)
    )
    valid_takeover_reply = bool(case and takeover_reply and req.advisor_authored and case.human_takeover)
    return OutboundGateInput(
        kind=req.kind,
        recipient_emails=frozenset(req.recipients),
        allowlist=effective_outbound_allowlist(settings),
        human_takeover=case.human_takeover if case else False,
        human_send_approved=valid_closure or valid_takeover_reply,
        case_version=case.case_version if case else 1,
        outbox_case_version=(req.case_version or 0) if case else 1,
        has_unprocessed_inbound=has_unprocessed,
        system_blocked=(closure_notice and not valid_closure) or (takeover_reply and not valid_takeover_reply) or bool(case and (
            case.system_blocked
            or (case.status in {CaseStatus.CLOSED, CaseStatus.DELIVERED} and not valid_closure)
            or (confirmation and case.status not in {
                CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION,
            })
            or (confirmation and case.waiting_third_party)
            or req.recipients != ((case.customer_email or "").strip().lower(),)
        )),
        paused=bool(case and case.paused and not valid_closure),
        cancelled=bool(case and case.cancelled and not valid_closure),
        package_id=None,
        package_kind=None,
        final_package_approval=None,
    )


def _live_block_reasons(
    settings: Settings, req: OutboxRequest, *, case_lock: psycopg.Connection | None = None,
) -> tuple[str, ...]:
    pre = _s1_precheck(req)
    gate_reasons = tuple(evaluate_outbound_gate(_gate_input(settings, req, case_lock=case_lock)))
    return tuple(dict.fromkeys([*pre, *gate_reasons]))


def _select_sql() -> str:
    return """
        SELECT id, status, block_reasons, provider_response,
               email_event_id, send_operation_id, recipients, subject, body_text, kind,
               case_id, in_reply_to, email_references, attachment_evidence_id, case_version, body_html,
               attachment_evidence_ids
        FROM outbox_messages
        WHERE tenant_id = %s AND send_operation_id = %s
    """


def _row_to_result(
    row: tuple,
    *,
    resent_blocked: bool = False,
    block_reasons: tuple[str, ...] | None = None,
    status: str | None = None,
) -> OutboxResult:
    return OutboxResult(
        outbox_id=row[0],
        status=status if status is not None else row[1],
        block_reasons=block_reasons if block_reasons is not None else tuple(row[2] or []),
        provider_response=row[3],
        email_event_id=row[4],
        send_operation_id=row[5],
        resent_blocked=resent_blocked,
    )


def _mark_blocked(
    *,
    settings: Settings,
    outbox_id: str,
    reasons: tuple[str, ...],
) -> None:
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'blocked',
                block_reasons = %s::jsonb,
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'failed', 'blocked')
            """,
            (json.dumps(list(reasons)), outbox_id),
        )
        conn.commit()


def _reopen_pending(*, settings: Settings, outbox_id: str) -> None:
    """Clear a prior block/fail so API status and DB row stay aligned."""
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'pending',
                block_reasons = '[]'::jsonb,
                rfc_message_id = CASE WHEN case_id IS NULL THEN rfc_message_id
                                      ELSE COALESCE(rfc_message_id, %s) END,
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'failed', 'blocked')
            """,
            (case_message_id(outbox_id, settings.email_from), outbox_id),
        )
        conn.commit()


def enqueue_and_maybe_send(
    *,
    settings: Settings,
    req: OutboxRequest,
    send_fn: SendFn | None = None,
    _force_persist_error: BaseException | None = None,
) -> OutboxResult:
    op_id = build_send_operation_id(
        tenant_id=settings.tenant_id,
        kind=req.kind,
        recipients=req.recipients,
        subject=req.subject,
        body_text=req.body_text,
        explicit=req.send_operation_id,
    )

    with psycopg.connect(settings.database_url) as conn:
        existing = conn.execute(
            _select_sql(), (settings.tenant_id, op_id)
        ).fetchone()

    if req.kind == OutboundMessageKind.CASE_CLOSURE_NOTICE and existing is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "Closure notices must be queued when closing a case")

    if existing is not None:
        if not _content_matches(existing, req):
            # Reject replay with divergent kind/recipients/subject/body.
            return _row_to_result(
                existing,
                resent_blocked=True,
                block_reasons=("VALIDATION_ERROR",),
            )
        if existing[1] in _NO_RESEND_STATUSES:
            return _row_to_result(existing, resent_blocked=True)

        # pending / failed / blocked — re-evaluate live gates on every retry.
        reasons = _live_block_reasons(settings, req)
        if reasons:
            _mark_blocked(
                settings=settings, outbox_id=existing[0], reasons=reasons
            )
            return OutboxResult(
                outbox_id=existing[0],
                status="blocked",
                block_reasons=reasons,
                provider_response=existing[3],
                email_event_id=existing[4],
                send_operation_id=existing[5],
                resent_blocked=True,
            )
        # Gates pass again (e.g. allowlist restored) — reopen so DB matches API.
        _reopen_pending(settings=settings, outbox_id=existing[0])
        outbox_id = existing[0]
    else:
        reasons = _live_block_reasons(settings, req)
        outbox_id = uuid.uuid4().hex
        message_id = (
            case_message_id(outbox_id, settings.email_from) if req.case_id else None
        )
        references = " ".join(req.references) or None
        if reasons:
            with psycopg.connect(settings.database_url) as conn:
                try:
                    with conn.transaction():
                        conn.execute(
                            """
                            INSERT INTO outbox_messages (
                                id, tenant_id, send_operation_id, case_id, case_version,
                                kind, recipients, subject, body_text, status, block_reasons,
                                rfc_message_id, in_reply_to, email_references, attachment_evidence_id, body_html,
                                attachment_evidence_ids
                            ) VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s::jsonb, %s, %s, 'blocked', %s::jsonb,
                                %s, %s, %s, %s, %s, %s
                            )
                            """,
                            (
                                outbox_id,
                                settings.tenant_id,
                                op_id,
                                req.case_id,
                                req.case_version if req.case_version is not None else 1,
                                req.kind,
                                json.dumps(list(req.recipients)),
                                req.subject,
                                req.body_text,
                                json.dumps(list(reasons)),
                                message_id,
                                req.in_reply_to,
                                references,
                                req.attachment_evidence_id,
                                req.body_html,
                                list(req.attachment_evidence_ids),
                            ),
                        )
                except UniqueViolation:
                    row = conn.execute(
                        _select_sql(), (settings.tenant_id, op_id)
                    ).fetchone()
                    assert row is not None
                    if not _content_matches(row, req):
                        return _row_to_result(
                            row,
                            resent_blocked=True,
                            block_reasons=("VALIDATION_ERROR",),
                        )
                    return _row_to_result(
                        row,
                        resent_blocked=True,
                        status="blocked",
                        block_reasons=reasons,
                    )
            return OutboxResult(
                outbox_id=outbox_id,
                status="blocked",
                block_reasons=reasons,
                provider_response=None,
                email_event_id=None,
                send_operation_id=op_id,
            )

        with psycopg.connect(settings.database_url) as conn:
            try:
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO outbox_messages (
                            id, tenant_id, send_operation_id, case_id, case_version,
                            kind, recipients, subject, body_text, status, block_reasons,
                            rfc_message_id, in_reply_to, email_references, attachment_evidence_id, body_html,
                            attachment_evidence_ids
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s::jsonb, %s, %s, 'pending', '[]'::jsonb,
                            %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            outbox_id,
                            settings.tenant_id,
                            op_id,
                            req.case_id,
                            req.case_version if req.case_version is not None else 1,
                            req.kind,
                            json.dumps(list(req.recipients)),
                            req.subject,
                            req.body_text,
                            message_id,
                            req.in_reply_to,
                            references,
                            req.attachment_evidence_id,
                            req.body_html,
                            list(req.attachment_evidence_ids),
                        ),
                    )
            except UniqueViolation:
                row = conn.execute(
                    _select_sql(), (settings.tenant_id, op_id)
                ).fetchone()
                assert row is not None
                if not _content_matches(row, req):
                    return _row_to_result(
                        row,
                        resent_blocked=True,
                        block_reasons=("VALIDATION_ERROR",),
                    )
                if row[1] in _NO_RESEND_STATUSES:
                    return _row_to_result(row, resent_blocked=True)
                outbox_id = row[0]

    # Always send the frozen content stored on the outbox row.
    with psycopg.connect(settings.database_url) as conn:
        frozen = conn.execute(
            """
            SELECT recipients, subject, body_text,
                   rfc_message_id, in_reply_to, email_references, case_id, attachment_evidence_id, body_html,
                   attachment_evidence_ids
            FROM outbox_messages WHERE id = %s
            """,
            (outbox_id,),
        ).fetchone()
        assert frozen is not None
        frozen_recipients = [str(r) for r in (
            json.loads(frozen[0]) if isinstance(frozen[0], str) else frozen[0]
        )]
        frozen_subject = frozen[1]
        frozen_body = frozen[2]
        message_id, in_reply_to, references, case_id, itinerary_id, body_html, attachment_ids = frozen[3:]

    if not req.send_now:
        return OutboxResult(
            outbox_id=outbox_id,
            status="pending",
            block_reasons=(),
            provider_response=None,
            email_event_id=None,
            send_operation_id=op_id,
        )

    with psycopg.connect(settings.database_url) as conn:
        # Check the case version and send permissions under its lock before claiming the attachments.
        case_lock = conn if req.advisor_authored or req.attachment_evidence_ids else None
        reasons = _live_block_reasons(settings, req, case_lock=case_lock)
        if reasons:
            _mark_blocked(settings=settings, outbox_id=outbox_id, reasons=reasons)
            return OutboxResult(
                outbox_id=outbox_id,
                status="blocked",
                block_reasons=reasons,
                provider_response=None,
                email_event_id=None,
                send_operation_id=op_id,
                resent_blocked=True,
            )
        claimed = conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'processing', updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'failed', 'blocked')
            RETURNING id
            """,
            (outbox_id,),
        ).fetchone()
        conn.commit()
        if claimed is None:
            row = conn.execute(
                _select_sql(), (settings.tenant_id, op_id)
            ).fetchone()
            assert row is not None
            return _row_to_result(row, resent_blocked=True)

    provider_response: str | None = None
    email_event_id: str | None = None
    status = "processing"

    try:
        message_kwargs = {}
        if body_html:
            message_kwargs["body_html"] = body_html
        if itinerary_id or attachment_ids:
            from visa_agent.document_pipeline.service import get_evidence
            from visa_agent.storage.files import LocalFileStore

            store = LocalFileStore(settings.file_storage_dir)
            attachments = []
            for evidence_id in ([itinerary_id] if itinerary_id else attachment_ids):
                evidence = get_evidence(
                    database_url=settings.database_url, tenant_id=settings.tenant_id,
                    case_id=case_id, evidence_id=evidence_id,
                )
                if evidence.quarantine_status == "rejected" or (
                    itinerary_id and evidence.classification != "itinerary"
                ):
                    raise ValueError("An attachment is unavailable")
                data = store.path_for_file_id(evidence.file_id).read_bytes()
                if hashlib.sha256(data).hexdigest() != evidence.content_sha256:
                    raise ValueError("An attachment failed its integrity check")
                attachments.append((evidence.original_filename, data))
            message_kwargs["attachments"] = attachments
        if send_fn:
            provider_response = send_fn(
                settings, frozen_recipients, frozen_subject, frozen_body, **message_kwargs
            )
        else:
            thread_headers = {}
            if message_id:
                thread_headers = {
                    "message_id": message_id,
                    "in_reply_to": in_reply_to,
                    "references": references,
                }
            provider_response = send_smtp(
                settings, recipients=frozen_recipients,
                subject=frozen_subject, body_text=frozen_body, **thread_headers, **message_kwargs,
            )
    except SendPartialAcceptedError as exc:
        status = "partial_accepted"
        provider_response = (exc.provider_response or str(exc))[:500]
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = %s, provider_response = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, provider_response, outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status=status,
            block_reasons=(),
            provider_response=provider_response,
            email_event_id=None,
            send_operation_id=op_id,
        )
    except SendStatusUnknownError as exc:
        status = "status_unknown"
        provider_response = str(exc)
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = %s, provider_response = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, provider_response, outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status=status,
            block_reasons=(),
            provider_response=provider_response,
            email_event_id=None,
            send_operation_id=op_id,
        )
    except TimeoutError as exc:
        status = "status_unknown"
        provider_response = f"timeout:{exc}"
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = %s, provider_response = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, provider_response, outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status=status,
            block_reasons=(),
            provider_response=provider_response,
            email_event_id=None,
            send_operation_id=op_id,
        )
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        provider_response = str(exc)
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = %s, provider_response = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, provider_response, outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status=status,
            block_reasons=(),
            provider_response=provider_response,
            email_event_id=None,
            send_operation_id=op_id,
        )

    # Provider call returned success — local persistence failure ⇒ status_unknown.
    email_event_id = uuid.uuid4().hex
    try:
        if _force_persist_error is not None:
            raise _force_persist_error
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                INSERT INTO email_events (
                    id, tenant_id, direction, provider_message_id, rfc_message_id,
                    dedupe_key, from_addr, to_addrs, subject, body_text,
                    case_id, attribution_status, processing_status,
                    in_reply_to, email_references
                ) VALUES (
                    %s, %s, 'outbound', %s, %s,
                    %s, %s, %s::jsonb, %s, %s,
                    %s, %s, 'processed', %s, %s
                )
                """,
                (
                    email_event_id,
                    settings.tenant_id,
                    provider_response,
                    message_id,
                    f"outbox:{outbox_id}",
                    settings.email_from,
                    json.dumps(frozen_recipients),
                    frozen_subject,
                    frozen_body,
                    case_id,
                    "attributed" if case_id else "unattributed",
                    in_reply_to,
                    references,
                ),
            )
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'provider_accepted',
                    provider_response = %s,
                    email_event_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (provider_response, email_event_id, outbox_id),
            )
            conn.commit()
        status = "provider_accepted"
    except Exception as exc:  # noqa: BLE001
        status = "status_unknown"
        note = f"provider_accepted_locally_unconfirmed:{exc}"
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'status_unknown',
                    provider_response = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (note, outbox_id),
            )
            conn.commit()
        provider_response = note
        email_event_id = None

    return OutboxResult(
        outbox_id=outbox_id,
        status=status,
        block_reasons=(),
        provider_response=provider_response,
        email_event_id=email_event_id,
        send_operation_id=op_id,
    )
