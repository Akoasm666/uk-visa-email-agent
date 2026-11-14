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
from visa_agent.domain.enums import OutboundMessageKind
from visa_agent.domain.models import OutboundGateInput, evaluate_outbound_gate
from visa_agent.email_adapter.errors import SendStatusUnknownError
from visa_agent.email_adapter.smtp_client import send_smtp

# Re-export for callers/tests that import from outbox.
__all__ = ["OutboxRequest", "OutboxResult", "SendStatusUnknownError", "enqueue_and_maybe_send"]

SendFn = Callable[[Settings, list[str], str, str], str]

_NO_RESEND_STATUSES = frozenset(
    {"processing", "provider_accepted", "status_unknown"}
)


@dataclass(frozen=True, slots=True)
class OutboxRequest:
    kind: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    send_operation_id: str | None = None
    send_now: bool = True


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
) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    material = "|".join(
        [
            tenant_id,
            kind,
            ",".join(sorted(r.strip().lower() for r in recipients)),
            subject.strip(),
            body_text.strip(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalize_recipients(recipients: Any) -> tuple[str, ...]:
    if isinstance(recipients, str):
        recipients = json.loads(recipients)
    return tuple(sorted(str(r).strip().lower() for r in recipients))


def _content_matches(row: tuple, req: OutboxRequest) -> bool:
    # row: id, status, block_reasons, provider_response, email_event_id,
    #      send_operation_id, recipients, subject, body_text, kind
    stored_recipients = _normalize_recipients(row[6])
    req_recipients = _normalize_recipients(req.recipients)
    return (
        (row[9] or "") == req.kind
        and stored_recipients == req_recipients
        and (row[7] or "") == req.subject
        and (row[8] or "") == req.body_text
    )


def _s1_precheck(req: OutboxRequest) -> list[str]:
    reasons: list[str] = []
    if req.kind != OutboundMessageKind.ORDINARY_REPLY.value:
        reasons.append("VALIDATION_ERROR")
    if len(req.recipients) != 1:
        reasons.append("VALIDATION_ERROR")
    return reasons


def _gate_input(settings: Settings, req: OutboxRequest) -> OutboundGateInput:
    return OutboundGateInput(
        kind=req.kind,
        recipient_emails=frozenset(req.recipients),
        allowlist=settings.outbound_allowlist,
        human_takeover=False,
        human_send_approved=False,
        case_version=1,
        outbox_case_version=1,
        has_unprocessed_inbound=False,
        system_blocked=False,
        paused=False,
        cancelled=False,
        package_id=None,
        package_kind=None,
        final_package_approval=None,
    )


def _live_block_reasons(settings: Settings, req: OutboxRequest) -> tuple[str, ...]:
    pre = _s1_precheck(req)
    gate_reasons = tuple(evaluate_outbound_gate(_gate_input(settings, req)))
    return tuple(dict.fromkeys([*pre, *gate_reasons]))


def _select_sql() -> str:
    return """
        SELECT id, status, block_reasons, provider_response,
               email_event_id, send_operation_id, recipients, subject, body_text, kind
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
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'failed', 'blocked')
            """,
            (outbox_id,),
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
        if reasons:
            with psycopg.connect(settings.database_url) as conn:
                try:
                    with conn.transaction():
                        conn.execute(
                            """
                            INSERT INTO outbox_messages (
                                id, tenant_id, send_operation_id, case_id, case_version,
                                kind, recipients, subject, body_text, status, block_reasons
                            ) VALUES (
                                %s, %s, %s, NULL, 1,
                                %s, %s::jsonb, %s, %s, 'blocked', %s::jsonb
                            )
                            """,
                            (
                                outbox_id,
                                settings.tenant_id,
                                op_id,
                                req.kind,
                                json.dumps(list(req.recipients)),
                                req.subject,
                                req.body_text,
                                json.dumps(list(reasons)),
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
                            kind, recipients, subject, body_text, status, block_reasons
                        ) VALUES (
                            %s, %s, %s, NULL, 1,
                            %s, %s::jsonb, %s, %s, 'pending', '[]'::jsonb
                        )
                        """,
                        (
                            outbox_id,
                            settings.tenant_id,
                            op_id,
                            req.kind,
                            json.dumps(list(req.recipients)),
                            req.subject,
                            req.body_text,
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

    dispatcher = send_fn or (
        lambda s, recipients, subject, body: send_smtp(
            s, recipients=recipients, subject=subject, body_text=body
        )
    )

    # Always send the frozen content stored on the outbox row.
    with psycopg.connect(settings.database_url) as conn:
        frozen = conn.execute(
            """
            SELECT recipients, subject, body_text
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

    if not req.send_now:
        return OutboxResult(
            outbox_id=outbox_id,
            status="pending",
            block_reasons=(),
            provider_response=None,
            email_event_id=None,
            send_operation_id=op_id,
        )

    # Re-check gates immediately before claim (allowlist may have changed).
    reasons = _live_block_reasons(settings, req)
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

    with psycopg.connect(settings.database_url) as conn:
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
        provider_response = dispatcher(
            settings, frozen_recipients, frozen_subject, frozen_body
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
                    id, tenant_id, direction, provider_message_id,
                    dedupe_key, from_addr, to_addrs, subject, body_text,
                    case_id, attribution_status
                ) VALUES (
                    %s, %s, 'outbound', %s,
                    %s, %s, %s::jsonb, %s, %s,
                    NULL, 'unattributed'
                )
                """,
                (
                    email_event_id,
                    settings.tenant_id,
                    provider_response,
                    f"outbox:{outbox_id}",
                    settings.email_from,
                    json.dumps(frozen_recipients),
                    frozen_subject,
                    frozen_body,
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
