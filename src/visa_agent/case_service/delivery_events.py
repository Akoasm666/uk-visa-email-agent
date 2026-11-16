"""Apply a customer's explicit receipt to the matching sent final package."""

from __future__ import annotations

import re
from dataclasses import replace

import psycopg

from visa_agent.case_service.service import (
    build_transition_context, get_case, mark_inbound_processed, record_delivery_receipt,
)
from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus
from visa_agent.domain.errors import DomainError
from visa_agent.domain.transitions import validate_transition
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.workflow.transitions import apply_transition

_RECEIPT = re.compile(
    r"(?:确认收件|确认收到(?:材料包)?|(?:我)?已收到(?:材料包)?|收到(?:[，, ]*谢谢)?|"
    r"received(?:[, ]+thank you)?|i confirm receipt|i(?: have)? received the (?:package|documents))[。.!！]?",
    re.I,
)


def acknowledge_delivery_from_reply(
    settings: Settings, case_id: str, *, event_id: str, sender: str, body: str, reply_to: str | None,
    semantic_receipt: bool = False,
) -> bool:
    customer = normalize_email(sender)
    if not customer or not reply_to or not (semantic_receipt or _RECEIPT.fullmatch(body.strip())):
        return False
    keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(**keys, conn=conn, for_update=True)
        if (
            case.status != CaseStatus.SENT_AWAITING_ACK or customer != normalize_email(case.customer_email)
            or case.human_takeover or case.cancelled or case.paused or case.system_blocked
        ):
            return False
        matched = conn.execute(
            """
            SELECT 1 FROM case_deliveries d
            JOIN outbox_messages o ON o.tenant_id = d.tenant_id AND o.case_id = d.case_id AND o.id = d.outbox_id
            WHERE d.tenant_id = %s AND d.case_id = %s AND d.id = %s AND d.case_version = %s
              AND d.sent_at IS NOT NULL AND NOT d.simulated_send AND o.package_kind = 'final_package'
              AND o.status = 'provider_accepted' AND o.rfc_message_id = %s
              AND o.recipients ? %s
              AND NOT EXISTS (
                  SELECT 1 FROM email_events e WHERE e.tenant_id = d.tenant_id AND e.case_id = d.case_id
                    AND e.direction = 'inbound' AND e.processing_status = 'unprocessed' AND e.id <> %s
              )
            """,
            (case.tenant_id, case.id, case.current_delivery_id, case.case_version, reply_to, customer, event_id),
        ).fetchone()
        if not matched:
            return False
        # Record only if confirming receipt will also complete this delivery.
        context = build_transition_context(case=case, conn=conn)
        try:
            validate_transition(case.status, CaseStatus.DELIVERED,
                                replace(context, receipt_for_delivery_id=case.current_delivery_id))
        except DomainError:
            return False
        record_delivery_receipt(
            **keys, expected_delivery_id=case.current_delivery_id, expected_case_version=case.case_version, conn=conn,
        )
        mark_inbound_processed(
            **keys, email_event_id=event_id, conclusion="acknowledged", processed_by="inbound_agent",
            expected_case_version=case.case_version, conn=conn,
        )
        apply_transition(
            **keys, target=CaseStatus.DELIVERED, expected_case_version=case.case_version, conn=conn,
        )
        conn.execute(
            "UPDATE cases SET waiting_customer = FALSE WHERE tenant_id = %s AND id = %s",
            (settings.tenant_id, case_id),
        )
    return True
