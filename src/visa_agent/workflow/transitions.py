"""Workflow applies transitions only after validate_transition."""

from __future__ import annotations

import uuid

import psycopg

from visa_agent.case_service.service import (
    CaseRecord,
    build_transition_context,
    consume_reopen_grant,
    get_case,
    persist_case_status,
)
from visa_agent.domain.enums import CaseStatus, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.domain.transitions import (
    TransitionContext,
    forward_candidates,
    validate_transition,
)


def _assert_send_authorized(ctx: TransitionContext) -> None:
    """Current-version confirmation + approval required before (re)send."""
    # Use the shared requirement set for entering sent_awaiting_ack.
    validate_transition(
        CaseStatus.READY_TO_DELIVER,
        CaseStatus.SENT_AWAITING_ACK,
        ctx,
    )


def apply_transition(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    target: CaseStatus,
    expected_case_version: int | None = None,
) -> CaseRecord:
    """Validate and persist transition atomically (status + version + delivery)."""
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            if (
                expected_case_version is not None
                and case.case_version != expected_case_version
            ):
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message="case_version changed since page was loaded",
                    details=(
                        f"expected={expected_case_version}",
                        f"actual={case.case_version}",
                    ),
                )

            ctx = build_transition_context(case=case, conn=conn)

            # Create/repair send record only when current version is still approved.
            needs_delivery = target == CaseStatus.SENT_AWAITING_ACK and (
                case.status != CaseStatus.SENT_AWAITING_ACK
                or not case.current_delivery_id
            )
            if needs_delivery:
                _assert_send_authorized(ctx)

            validate_transition(case.status, target, ctx)

            if target == CaseStatus.ISSUE_RESOLUTION:
                new_resume = (
                    case.status
                    if case.status != CaseStatus.ISSUE_RESOLUTION
                    else case.resume_status
                )
            elif case.status == CaseStatus.ISSUE_RESOLUTION:
                new_resume = None
            else:
                new_resume = case.resume_status

            reopen_edge = (
                case.status == CaseStatus.CLOSED
                and target in {CaseStatus.LEAD, CaseStatus.INTAKE_AUTHORIZATION}
            ) or (
                case.status == CaseStatus.DELIVERED
                and target
                in {CaseStatus.COLLECT_AND_VERIFY, CaseStatus.ISSUE_RESOLUTION}
            )
            if reopen_edge:
                consume_reopen_grant(conn=conn, case=case)

            entering_closure = target in {
                CaseStatus.CLOSED,
                CaseStatus.DELIVERED,
            } and case.status != target

            delivery_id: str | None = None
            if needs_delivery:
                delivery_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO case_deliveries (
                        id, tenant_id, case_id, case_version,
                        sent_at, simulated_send
                    ) VALUES (%s, %s, %s, %s, NOW(), TRUE)
                    """,
                    (delivery_id, tenant_id, case_id, case.case_version),
                )

            updated = persist_case_status(
                conn=conn,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_status=case.status,
                expected_case_version=case.case_version,
                status=target,
                resume_status=new_resume,
                current_delivery_id=delivery_id if needs_delivery else ...,
                bump_closure_seq=entering_closure,
            )
            return updated


def list_allowed_targets(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> list[CaseStatus]:
    """Only statuses that pass live validate_transition (not merely graph edges)."""
    case = get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)
    ctx = build_transition_context(database_url=database_url, case=case)
    candidates = forward_candidates(case.status)
    allowed: list[CaseStatus] = []
    for target in sorted(candidates, key=lambda s: s.value):
        try:
            validate_transition(case.status, target, ctx)
        except DomainError:
            continue
        allowed.append(target)
    return allowed
