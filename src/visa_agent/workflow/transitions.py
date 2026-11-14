"""Workflow applies transitions only after validate_transition."""

from __future__ import annotations

import psycopg

from visa_agent.case_service.service import (
    CaseRecord,
    build_transition_context,
    consume_reopen_grant,
    get_case,
    persist_case_status,
)
from visa_agent.domain.enums import CaseStatus, DeliveryKind, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.domain.transitions import (
    TransitionContext,
    forward_candidates,
    validate_transition,
)


def _assert_send_authorized(ctx: TransitionContext) -> None:
    """Current-version confirmation + approval required before (re)send."""
    validate_transition(
        CaseStatus.READY_TO_DELIVER,
        CaseStatus.SENT_AWAITING_ACK,
        ctx,
    )


def _require_bound_final_delivery(
    conn: psycopg.Connection,
    *,
    case: CaseRecord,
) -> None:
    """SENT/DELIVERED must bind the real final-package send — never invent one."""
    if not case.current_delivery_id:
        raise DomainError(
            code=ErrorCode.DELIVERY_GATE_FAILED,
            message=(
                "final package must be sent (with outbox linkage) "
                "before awaiting acknowledgement"
            ),
        )
    row = conn.execute(
        """
        SELECT d.case_version, d.package_id, d.outbox_id, d.sent_at, p.package_kind
        FROM case_deliveries d
        LEFT JOIN delivery_packages p
          ON p.id = d.package_id AND p.tenant_id = d.tenant_id
        WHERE d.tenant_id = %s AND d.case_id = %s AND d.id = %s
        """,
        (case.tenant_id, case.id, case.current_delivery_id),
    ).fetchone()
    if row is None:
        raise DomainError(
            code=ErrorCode.DELIVERY_GATE_FAILED,
            message="current delivery record missing",
            details=(case.current_delivery_id,),
        )
    case_version, package_id, outbox_id, sent_at, package_kind = row
    if int(case_version) != case.case_version:
        raise DomainError(
            code=ErrorCode.CONFLICT_VERSION,
            message="current delivery is bound to a different case_version",
            details=(str(case_version), str(case.case_version)),
        )
    if not package_id or not outbox_id or sent_at is None:
        raise DomainError(
            code=ErrorCode.DELIVERY_GATE_FAILED,
            message="current delivery is not linked to a sent package/outbox",
            details=(case.current_delivery_id,),
        )
    if (package_kind or "") != DeliveryKind.FINAL_PACKAGE.value:
        raise DomainError(
            code=ErrorCode.DELIVERY_GATE_FAILED,
            message="current delivery must be a final_package send",
            details=(package_kind or "",),
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

            entering_sent = (
                target == CaseStatus.SENT_AWAITING_ACK
                and case.status != CaseStatus.SENT_AWAITING_ACK
            )
            if entering_sent:
                _assert_send_authorized(ctx)
                _require_bound_final_delivery(conn, case=case)

            if target == CaseStatus.DELIVERED and case.status != CaseStatus.DELIVERED:
                _require_bound_final_delivery(conn, case=case)

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

            # Never invent a simulated delivery on status change — current_delivery_id
            # is owned by the final-package send path.
            updated = persist_case_status(
                conn=conn,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_status=case.status,
                expected_case_version=case.case_version,
                status=target,
                resume_status=new_resume,
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
            if target == CaseStatus.SENT_AWAITING_ACK and case.status != target:
                with psycopg.connect(database_url) as conn:
                    _require_bound_final_delivery(conn, case=case)
            validate_transition(case.status, target, ctx)
        except DomainError:
            continue
        allowed.append(target)
    return allowed
