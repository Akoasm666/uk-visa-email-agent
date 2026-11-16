"""Ask the customer to confirm the current pre-reviewed materials by email."""

from __future__ import annotations

import logging
import re

import psycopg

from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.case_service.itinerary import get_case_itinerary
from visa_agent.case_service.outreach import _MATERIAL_LINES, _OPENING_NEXT, advance_opening
from visa_agent.case_service.remediation import open_remediation
from visa_agent.case_service.service import (
    CaseRecord,
    build_transition_context,
    get_case,
    mark_inbound_processed,
    record_customer_confirmation,
)
from visa_agent.config import Settings
from visa_agent.document_pipeline.classify import requirement_for_classification
from visa_agent.domain.enums import CaseStatus, OutboundMessageKind, RequirementStatus
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.allowlist import effective_outbound_allowlist, normalize_email
from visa_agent.email_adapter.outbox import (
    MATERIAL_CONFIRMATION_OPERATION_PREFIX,
    OutboxRequest,
    enqueue_and_maybe_send,
)
from visa_agent.email_adapter.threading import reply_references, single_message_id
from visa_agent.rules.service import evaluate_case_requirements
from visa_agent.workflow.transitions import apply_transition

_LOG = logging.getLogger(__name__)
_COLLECTION_STAGES = (
    CaseStatus.COLLECT_AND_VERIFY, CaseStatus.DRAFT_PREPARATION,
    CaseStatus.CUSTOMER_CONFIRMATION,
)
_READY_FOR_CUSTOMER = frozenset({
    RequirementStatus.RECEIVED_PENDING_REVIEW,
    RequirementStatus.EVIDENCE_ACCEPTED,
    RequirementStatus.NOT_APPLICABLE,
    RequirementStatus.EXEMPTED_WITH_BASIS,
})
_CONFIRMATION = re.compile(
    r"(?:确认材料|确认本版材料|确认这版材料|我确认|确认|i confirm these materials|i confirm)[。.!！]?",
    re.I,
)


def _operation_id(case: CaseRecord) -> str:
    return f"{MATERIAL_CONFIRMATION_OPERATION_PREFIX}{case.tenant_id}:{case.id}:{case.case_version}"


def _held(case: CaseRecord) -> bool:
    return (
        not case.route or case.human_takeover or case.paused
        or case.cancelled or case.system_blocked or case.waiting_third_party
    )


def _matches_sent_request(conn: psycopg.Connection, case: CaseRecord, reply_to: str) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM outbox_messages WHERE tenant_id = %s AND case_id = %s AND case_version = %s
          AND send_operation_id = %s AND status = 'provider_accepted' AND rfc_message_id = %s
        """,
        (case.tenant_id, case.id, case.case_version, _operation_id(case), reply_to),
    ).fetchone() is not None


def is_current_material_confirmation_reply(
    settings: Settings, case: CaseRecord, *, sender: str, reply_to: str | None,
) -> bool:
    sender_email = normalize_email(sender)
    if not reply_to or _held(case) or not sender_email or sender_email != normalize_email(case.customer_email):
        return False
    with psycopg.connect(settings.database_url) as conn:
        return _matches_sent_request(conn, case, reply_to)


def material_confirmation_sent(settings: Settings, case: CaseRecord) -> bool:
    if case.status != CaseStatus.CUSTOMER_CONFIRMATION:
        return False
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute(
            """
            SELECT 1 FROM outbox_messages WHERE tenant_id = %s AND case_id = %s
              AND send_operation_id = %s AND status = 'provider_accepted'
            """,
            (case.tenant_id, case.id, _operation_id(case)),
        ).fetchone() is not None


def _ready(
    conn: psycopg.Connection, settings: Settings, case: CaseRecord, *,
    confirming_event_id: str | None = None,
) -> bool:
    # The case row is already locked. A customer confirmation is an input in progress.
    blocked = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM open_questions
            WHERE tenant_id = %(tenant)s AND case_id = %(case)s
              AND status IN ('open', 'waiting_customer', 'escalated_human')
            UNION ALL
            SELECT 1 FROM email_events
            WHERE tenant_id = %(tenant)s AND case_id = %(case)s AND direction = 'inbound'
              AND attribution_status = 'attributed' AND processing_status <> 'processed'
              AND id IS DISTINCT FROM %(event)s
        )
        """,
        {"tenant": case.tenant_id, "case": case.id, "event": confirming_event_id},
    ).fetchone()[0]
    if blocked:
        return False
    itinerary = get_case_itinerary(settings=settings, case_id=case.id, conn=conn)
    if itinerary and not (itinerary.confirmed or itinerary.advisor_reviewed):
        return False
    try:
        evaluation = evaluate_case_requirements(
            database_url=settings.database_url, tenant_id=case.tenant_id, case_id=case.id,
            conn=conn, preview_unbound=True, persist_states=False,
        )
    except DomainError:
        return False
    # Only detected sensitive issues block the customer's material check. An
    # unassessed trigger does not require a separate workbench checklist action.
    return all(
        (state.status in _READY_FOR_CUSTOMER and not (
            state.requirement_key == "translation_if_needed"
            and state.status == RequirementStatus.RECEIVED_PENDING_REVIEW
        ))
        or (state.severity == "transfer_human" and state.status == RequirementStatus.APPLICABILITY_UNKNOWN)
        for state in evaluation.states
        if state.severity in {"required", "transfer_human"}
    )


def _set_phase(
    conn: psycopg.Connection, settings: Settings, case: CaseRecord,
    target: CaseStatus, *, waiting: bool,
) -> CaseRecord:
    if case.status != target:
        case = apply_transition(
            database_url=settings.database_url, tenant_id=case.tenant_id,
            case_id=case.id, target=target, expected_case_version=case.case_version, conn=conn,
        )
    if case.waiting_customer != waiting:
        conn.execute(
            "UPDATE cases SET waiting_customer = %s WHERE tenant_id = %s AND id = %s",
            (waiting, case.tenant_id, case.id),
        )
    return case


def _request(
    conn: psycopg.Connection, case: CaseRecord, customer: str,
) -> OutboxRequest | None:
    existing = conn.execute(
        """
        SELECT status, subject, body_text, in_reply_to, email_references,
               updated_at > NOW() - INTERVAL '5 minutes', attachment_evidence_ids
        FROM outbox_messages WHERE tenant_id = %s AND send_operation_id = %s
        """,
        (case.tenant_id, _operation_id(case)),
    ).fetchone()
    if existing:
        status, subject, body, parent, references, recent, attachment_ids = existing
        if status in {"failed", "blocked", "processing"} and recent:
            return None
        if status == "processing":
            conn.execute(
                """
                UPDATE outbox_messages SET status = 'status_unknown', updated_at = NOW()
                WHERE tenant_id = %s AND send_operation_id = %s AND status = 'processing'
                """,
                (case.tenant_id, _operation_id(case)),
            )
        if attachment_ids or status not in {"pending", "failed", "blocked"}:
            return OutboxRequest(
                kind=OutboundMessageKind.ORDINARY_REPLY, recipients=(customer,), subject=subject,
                body_text=body, send_operation_id=_operation_id(case), case_id=case.id,
                case_version=case.case_version, in_reply_to=parent,
                references=tuple((references or "").split()),
                attachment_evidence_ids=tuple(attachment_ids),
            )

    thread = conn.execute(
        """
        SELECT rfc_message_id, in_reply_to, email_references, subject, body_text
        FROM email_events WHERE tenant_id = %s AND case_id = %s AND direction = 'inbound'
          AND attribution_status = 'attributed' AND processing_status = 'processed'
          AND lower(trim(from_addr)) = %s AND rfc_message_id IS NOT NULL
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (case.tenant_id, case.id, customer),
    ).fetchone()
    if not thread or not (parent := single_message_id(thread[0])):
        return None
    _, earlier, refs, subject, body = thread
    zh = detect_reply_language(body or "", subject or "") == "zh"
    rows = conn.execute(
        """
        SELECT e.id, e.original_filename, e.classification FROM case_evidence e
        WHERE e.tenant_id = %s AND e.case_id = %s AND e.deleted_at IS NULL
          AND e.quarantine_status <> 'rejected' AND e.classification <> 'unclassified'
          AND NOT EXISTS (
              SELECT 1 FROM open_questions q
              WHERE q.tenant_id = e.tenant_id AND q.case_id = e.case_id AND q.evidence_id = e.id
                AND q.closed_by_evidence_id <> e.id
          )
        ORDER BY e.created_at, e.id
        """,
        (case.tenant_id, case.id),
    ).fetchall()
    files = [
        (evidence_id, filename)
        for evidence_id, filename, classification in rows
        if requirement_for_classification(classification, route=case.route) in _MATERIAL_LINES
    ]
    materials = "\n".join(f"- {' '.join(filename.split())}" for _, filename in files)
    if zh:
        intro = f"附件是您的签证申请材料。\n\n附件清单：\n{materials}\n\n" if files else ""
        body = (
            f"您好，{intro}请核对申请人信息、访问目的、出行安排和资金说明。\n\n"
            "如有更正或补充，请直接回复并说明。信息无误的话，请回复「确认材料」，"
            "我们会将材料交给顾问终审。\n"
        )
    else:
        intro = f"Your visa application materials are attached.\n\nAttachments:\n{materials}\n\n" if files else ""
        body = (
            f"Hello,\n\n{intro}Please check your personal details, purpose of visit, travel plans and financial information.\n\n"
            "Reply with any corrections or additions. If everything is correct, reply “I confirm these materials” "
            "and we will pass them to your advisor for final review.\n"
        )
    subject = " ".join((subject or "").split())
    if not subject:
        subject = "请确认材料" if zh else "Please confirm these materials"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    thread_references = reply_references(parent_id=parent, references=refs, parent_in_reply_to=earlier)
    attachment_ids = tuple(evidence_id for evidence_id, _ in files)
    if existing:
        # Only upgrade legacy messages that the provider has not accepted.
        updated = conn.execute(
            """
            UPDATE outbox_messages SET subject = %s, body_text = %s, in_reply_to = %s,
                email_references = %s, attachment_evidence_ids = %s
            WHERE tenant_id = %s AND send_operation_id = %s
              AND status IN ('pending', 'failed', 'blocked') AND cardinality(attachment_evidence_ids) = 0
            """,
            (subject, body, parent, " ".join(thread_references) or None, list(attachment_ids),
             case.tenant_id, _operation_id(case)),
        )
        if not updated.rowcount:
            return None
    return OutboxRequest(
        kind=OutboundMessageKind.ORDINARY_REPLY, recipients=(customer,), subject=subject,
        body_text=body, send_operation_id=_operation_id(case), case_id=case.id,
        case_version=case.case_version, in_reply_to=parent,
        references=thread_references, attachment_evidence_ids=attachment_ids,
    )


def advance_material_confirmation(settings: Settings, case_id: str) -> None:
    """Run while holding the case's inbound lock. Only accepted mail starts the wait."""
    keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(**keys, conn=conn, for_update=True)
        if _held(case):
            return
        if case.status == CaseStatus.ISSUE_RESOLUTION and case.resume_status in {
            *_OPENING_NEXT, *_COLLECTION_STAGES,
        }:
            if conn.execute(
                """
                SELECT 1 FROM open_questions WHERE tenant_id = %s AND case_id = %s
                  AND status IN ('open', 'waiting_customer', 'escalated_human') LIMIT 1
                """,
                (case.tenant_id, case.id),
            ).fetchone():
                return
            case = _set_phase(conn, settings, case, case.resume_status, waiting=case.waiting_customer)
        case = advance_opening(settings, case, conn)
        if case.status in {CaseStatus.FINAL_REVIEW, CaseStatus.READY_TO_DELIVER}:
            ctx = build_transition_context(case=case, conn=conn)
            if ctx.customer_confirmation_case_version != case.case_version:
                prior_request = conn.execute(
                    """
                    SELECT 1 FROM outbox_messages WHERE tenant_id = %s AND case_id = %s
                      AND starts_with(send_operation_id, %s) AND status = 'provider_accepted' LIMIT 1
                    """,
                    (case.tenant_id, case.id, MATERIAL_CONFIRMATION_OPERATION_PREFIX),
                ).fetchone()
                if not prior_request:
                    return
                case = _set_phase(conn, settings, case, CaseStatus.COLLECT_AND_VERIFY, waiting=False)
            elif case.status == CaseStatus.FINAL_REVIEW:
                if ctx.final_review_approval_case_version == case.case_version:
                    _set_phase(conn, settings, case, CaseStatus.READY_TO_DELIVER, waiting=False)
                return
            elif ctx.final_review_approval_case_version != case.case_version:
                _set_phase(conn, settings, case, CaseStatus.FINAL_REVIEW, waiting=False)
                return
        if case.status == CaseStatus.READY_TO_DELIVER:
            accepted = conn.execute(
                """
                SELECT 1 FROM case_deliveries d
                JOIN outbox_messages o ON o.tenant_id = d.tenant_id AND o.case_id = d.case_id AND o.id = d.outbox_id
                WHERE d.tenant_id = %s AND d.case_id = %s AND d.id = %s AND d.case_version = %s
                  AND d.sent_at IS NOT NULL AND NOT d.simulated_send
                  AND o.status = 'provider_accepted' AND o.package_kind = 'final_package'
                """,
                (case.tenant_id, case.id, case.current_delivery_id, case.case_version),
            ).fetchone()
            if accepted:
                _set_phase(conn, settings, case, CaseStatus.SENT_AWAITING_ACK, waiting=True)
            return
        if case.status not in _COLLECTION_STAGES:
            return
        if not conn.execute(
            """
            SELECT 1 FROM email_events WHERE tenant_id = %s AND case_id = %s AND direction = 'inbound'
              AND attribution_status = 'attributed' AND processing_status = 'processed'
              AND lower(trim(from_addr)) = %s AND rfc_message_id IS NOT NULL LIMIT 1
            """,
            (case.tenant_id, case.id, normalize_email(case.customer_email)),
        ).fetchone():
            return
        if not _ready(conn, settings, case):
            if case.status in {CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION}:
                _set_phase(conn, settings, case, CaseStatus.COLLECT_AND_VERIFY, waiting=case.waiting_customer)
            return
        customer = normalize_email(case.customer_email)
        if not customer or customer not in effective_outbound_allowlist(settings):
            return
        request = _request(conn, case, customer)
        if request is None:
            return
        if case.status == CaseStatus.COLLECT_AND_VERIFY:
            _set_phase(conn, settings, case, CaseStatus.DRAFT_PREPARATION, waiting=False)

    result = enqueue_and_maybe_send(settings=settings, req=request)
    if result.status in {"status_unknown", "partial_accepted"}:
        open_remediation(
            **keys, requirement_key="advisor_material_confirmation_delivery",
            reason_code="material_confirmation_delivery_unknown",
            reason_detail="材料确认邮件的发送结果不明确，请核对发件记录。",
            severity="complex_human", case_version=request.case_version,
        )
        return
    if result.status != "provider_accepted" or result.block_reasons:
        return
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(**keys, conn=conn, for_update=True)
        if case.case_version == request.case_version and not _held(case) and case.status in {
            CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION,
        } and _ready(conn, settings, case):
            _set_phase(conn, settings, case, CaseStatus.CUSTOMER_CONFIRMATION, waiting=True)


def confirm_materials_from_reply(
    settings: Settings, case_id: str, *, event_id: str, sender: str,
    body: str, reply_to: str | None, semantic_confirmation: bool = False,
) -> bool:
    if not reply_to or not (semantic_confirmation or _CONFIRMATION.fullmatch(body.strip())):
        return False
    keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(**keys, conn=conn, for_update=True)
        if _held(case) or case.status not in _COLLECTION_STAGES or not (
            normalize_email(sender) and normalize_email(sender) == normalize_email(case.customer_email)
        ):
            return False
        if not _matches_sent_request(conn, case, reply_to) or not _ready(
            conn, settings, case, confirming_event_id=event_id,
        ):
            return False
        if case.status == CaseStatus.COLLECT_AND_VERIFY:
            case = _set_phase(conn, settings, case, CaseStatus.DRAFT_PREPARATION, waiting=False)
        case = _set_phase(conn, settings, case, CaseStatus.CUSTOMER_CONFIRMATION, waiting=False)
        note = f"Customer confirmed materials by email. Inbound event: {event_id}"
        record_customer_confirmation(**keys, note=note, expected_case_version=case.case_version, conn=conn)
        mark_inbound_processed(
            **keys, email_event_id=event_id, conclusion="acknowledged",
            processed_by="inbound_agent", expected_case_version=case.case_version, conn=conn,
        )
        _set_phase(conn, settings, case, CaseStatus.FINAL_REVIEW, waiting=False)
    return True


def resume_material_confirmations(settings: Settings) -> None:
    """Resume after SMTP failures or after an advisor clears a real exception."""
    with psycopg.connect(settings.database_url, autocommit=True) as lock:
        candidates = lock.execute(
            """
            SELECT id FROM cases WHERE tenant_id = %s AND route IS NOT NULL AND status = ANY(%s)
              AND NOT (human_takeover OR paused OR cancelled OR system_blocked OR waiting_third_party)
              AND (status <> ALL(%s) OR EXISTS (
                  SELECT 1 FROM outbox_messages o WHERE o.tenant_id = cases.tenant_id
                    AND o.case_id = cases.id AND o.status = 'provider_accepted'
                    AND (starts_with(o.send_operation_id, 'case-opening:')
                      OR starts_with(o.send_operation_id, 'inbound-reply:'))
              ))
            """,
            (settings.tenant_id,
             [state.value for state in (*_OPENING_NEXT, *_COLLECTION_STAGES,
                                        CaseStatus.ISSUE_RESOLUTION, CaseStatus.FINAL_REVIEW,
                                        CaseStatus.READY_TO_DELIVER)],
             [state.value for state in _OPENING_NEXT]),
        ).fetchall()
        for (case_id,) in candidates:
            key = f"customer-inbound:{settings.tenant_id}:{case_id}"
            if not lock.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)).fetchone()[0]:
                continue
            try:
                advance_material_confirmation(settings, case_id)
            except (DomainError, psycopg.Error) as exc:
                _LOG.warning("Cannot advance material confirmation (%s)", type(exc).__name__)
            finally:
                lock.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
