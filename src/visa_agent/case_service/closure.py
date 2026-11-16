"""End or terminate a case, notifying the customer only on normal completion."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

import psycopg

from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.case_service.service import CaseRecord, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus, ErrorCode, OutboundMessageKind, RequirementStatus
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.email_adapter.outbox import (
    CLOSURE_NOTICE_OPERATION_PREFIX,
    OutboxRequest,
    OutboxResult,
    closure_notice_operation_id,
    enqueue_and_maybe_send,
)
from visa_agent.email_adapter.threading import case_message_id, new_reply_body, reply_references, single_message_id
from visa_agent.rules.service import CaseRequirementState, evaluate_case_requirements
from visa_agent.workflow.transitions import apply_transition

_KIND = OutboundMessageKind.CASE_CLOSURE_NOTICE
_VERIFIED = {
    RequirementStatus.EVIDENCE_ACCEPTED,
    RequirementStatus.NOT_APPLICABLE,
    RequirementStatus.EXEMPTED_WITH_BASIS,
}


def required_materials_verified(states: Iterable[CaseRequirementState]) -> bool:
    required = [state for state in states if state.severity == "required"]
    return bool(required) and all(
        state.status in _VERIFIED
        or (state.requirement_key == "visit_purpose_allowed"
            and state.status == RequirementStatus.RECEIVED_PENDING_REVIEW
            and state.reason.endswith("（来自客户邮件）"))
        for state in required
    )


def termination_reasons(settings: Settings, case_ids: list[str]) -> dict[str, str]:
    if not case_ids:
        return {}
    with psycopg.connect(settings.database_url) as conn:
        return dict(conn.execute(
            """
            SELECT t.case_id, t.reason FROM case_terminations t
            JOIN cases c ON t.tenant_id = c.tenant_id AND t.case_id = c.id AND t.closure_seq = c.closure_seq
            WHERE t.tenant_id = %s AND t.case_id = ANY(%s) AND c.status = 'closed'
            """, (settings.tenant_id, case_ids),
        ).fetchall())


def _queue_notice(conn: psycopg.Connection, settings: Settings, case: CaseRecord, customer: str) -> None:
    thread = conn.execute(
        """
        SELECT rfc_message_id, in_reply_to, email_references, subject, body_text, direction
        FROM email_events
        WHERE tenant_id = %s AND case_id = %s AND attribution_status = 'attributed'
          AND rfc_message_id IS NOT NULL
          AND ((direction = 'inbound' AND lower(trim(from_addr)) = %s)
               OR (direction = 'outbound' AND to_addrs ? %s))
        ORDER BY (direction = 'inbound') DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (case.tenant_id, case.id, customer, customer),
    ).fetchone()
    parent = single_message_id(thread[0]) if thread else None
    references = reply_references(
        parent_id=parent, references=thread[2], parent_in_reply_to=thread[1],
    ) if parent else ()
    language = None
    if thread and thread[5] == "inbound":
        language = detect_reply_language(new_reply_body(thread[4]), thread[3])
    subject = " ".join((thread[3] or "").split()) if parent else ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    subject = subject or (
        "案件已结束" if language == "zh" else "Your visa assistance case has ended"
    )
    chinese = f"您好，您的案件（编号 {case.id[:8]}）已结束，本次签证服务已结束。感谢您的信任。"
    english = f"Hello, your visa assistance case ({case.id[:8]}) has been closed and our service has ended. Thank you for working with us."
    body = chinese if language == "zh" else english if language == "en" else f"{english}\n\n{chinese}"
    outbox_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO outbox_messages (
            id, tenant_id, send_operation_id, case_id, case_version, kind, recipients,
            subject, body_text, rfc_message_id, in_reply_to, email_references
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
        """,
        (outbox_id, case.tenant_id, closure_notice_operation_id(case.tenant_id, case.id, case.closure_seq),
         case.id, case.case_version, _KIND, json.dumps([customer]), subject, body,
         case_message_id(outbox_id, settings.email_from), parent, " ".join(references) or None),
    )


def close_customer_case(
    *, settings: Settings, case_id: str, expected_case_version: int | None,
    terminate: bool = False, reason: str = "",
) -> OutboxResult | None:
    reason = reason.strip()
    if terminate and len(reason) > 1000:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "终止理由最多填写 1000 字")
    keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"customer-inbound:{settings.tenant_id}:{case_id}",))
        case = get_case(**keys, conn=conn, for_update=True)
        if expected_case_version != case.case_version:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "案件内容已更新，请刷新后再操作")
        if case.status == CaseStatus.CLOSED:
            terminated = conn.execute(
                """
                SELECT 1 FROM case_terminations WHERE tenant_id = %s AND case_id = %s AND closure_seq = %s
                """, (case.tenant_id, case.id, case.closure_seq),
            ).fetchone() is not None
            if terminated != terminate:
                label = "案件已终止" if terminated else "案件已结束"
                raise DomainError(ErrorCode.VALIDATION_ERROR, label)
        else:
            customer = None
            if not terminate:
                if case.status != CaseStatus.READY_TO_DELIVER:
                    raise DomainError(ErrorCode.VALIDATION_ERROR, "只有交付中的案件才能结束")
                verified = False
                if case.route:
                    try:
                        evaluation = evaluate_case_requirements(**keys, conn=conn, preview_unbound=True,
                                                               persist_states=False)
                        verified = required_materials_verified(evaluation.states)
                    except DomainError as exc:
                        if exc.code not in {ErrorCode.NOT_FOUND, ErrorCode.VALIDATION_ERROR}:
                            raise
                if not verified:
                    raise DomainError(ErrorCode.VALIDATION_ERROR, "必需材料集齐并核对通过后才能结束案件")
                customer = normalize_email(case.customer_email)
                if not customer:
                    raise DomainError(ErrorCode.VALIDATION_ERROR, "请先填写客户邮箱，以便发送服务结束通知")
                if conn.execute(
                    """
                    SELECT 1 FROM outbox_messages
                    WHERE tenant_id = %s AND case_id = %s AND status = 'processing' LIMIT 1
                    """, (case.tenant_id, case.id),
                ).fetchone():
                    raise DomainError(ErrorCode.VALIDATION_ERROR, "案件正在发送邮件，请稍后结束")
            case = apply_transition(**keys, conn=conn, target=CaseStatus.CLOSED,
                                    expected_case_version=case.case_version)
            conn.execute(
                """
                UPDATE cases SET waiting_customer = FALSE, waiting_third_party = FALSE
                WHERE tenant_id = %s AND id = %s
                """, (case.tenant_id, case.id),
            )
            if terminate:
                conn.execute(
                    "INSERT INTO case_terminations (tenant_id, case_id, closure_seq, reason) VALUES (%s,%s,%s,%s)",
                    (case.tenant_id, case.id, case.closure_seq, reason),
                )
            elif customer:
                _queue_notice(conn, settings, case, customer)
    if terminate:
        return None
    return _send_notice(settings, closure_notice_operation_id(case.tenant_id, case.id, case.closure_seq))


def _send_notice(settings: Settings, operation_id: str) -> OutboxResult | None:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            """
            SELECT case_id, case_version, recipients, subject, body_text, in_reply_to, email_references
            FROM outbox_messages WHERE tenant_id = %s AND send_operation_id = %s AND kind = %s
            """, (settings.tenant_id, operation_id, _KIND),
        ).fetchone()
    if row is None:
        return None
    case_id, version, recipients, subject, body, parent, references = row
    return enqueue_and_maybe_send(settings=settings, req=OutboxRequest(
        kind=_KIND, recipients=tuple(recipients), subject=subject, body_text=body,
        send_operation_id=operation_id, case_id=case_id, case_version=version,
        in_reply_to=parent, references=tuple((references or "").split()),
    ))


def closure_notice_status(settings: Settings, case: CaseRecord) -> str | None:
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            """
            SELECT status FROM outbox_messages
            WHERE tenant_id = %s AND send_operation_id = %s AND kind = %s
            """, (case.tenant_id, closure_notice_operation_id(case.tenant_id, case.id, case.closure_seq), _KIND),
        ).fetchone()
    return row[0] if row else None


def resume_closure_notices(settings: Settings) -> None:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.closure_seq FROM cases c
            JOIN outbox_messages o ON o.tenant_id = c.tenant_id AND o.case_id = c.id
                AND o.send_operation_id = %s || c.tenant_id || ':' || c.id || ':' || c.closure_seq
            WHERE c.tenant_id = %s AND c.status = 'closed' AND o.kind = %s
              AND (o.status = 'pending' OR (o.status = 'failed' AND o.updated_at < NOW() - INTERVAL '5 minutes'))
            ORDER BY o.updated_at LIMIT 5
            """, (CLOSURE_NOTICE_OPERATION_PREFIX, settings.tenant_id, _KIND),
        ).fetchall()
    for case_id, sequence in rows:
        _send_notice(settings, closure_notice_operation_id(settings.tenant_id, case_id, sequence))
