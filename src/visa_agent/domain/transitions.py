"""Case status transition table — single source of truth for allowed moves.

Ownership
---------
- **case_service** loads TransitionContext against the *current* case version
  (customer confirmation, final-review approval, reopen grant, resume stage,
  delivery receipt binding).
- **workflow** may only apply a transition after ``validate_transition``.

Target-based gates (single rule set)
------------------------------------
Whether a transition needs context and which preconditions apply are derived
from the *same* ``_requirements_for(current, target)`` helper — do not fork.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from visa_agent.domain.enums import CaseStatus, ErrorCode
from visa_agent.domain.errors import DomainError

CONTROL_FLAGS: frozenset[str] = frozenset(
    {
        "waiting_customer",
        "waiting_third_party",
        "human_takeover",
        "system_blocked",
        "paused",
        "cancelled",
    }
)

Requirement = Literal[
    "customer_confirmation",
    "final_approval",
    "reopen",
    "resume_status",
    "delivery_receipt",
]

# Main pipeline order (issue_resolution is a side state, not listed here).
_FORWARD: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.LEAD: frozenset(
        {CaseStatus.INTAKE_AUTHORIZATION, CaseStatus.ISSUE_RESOLUTION, CaseStatus.CLOSED}
    ),
    CaseStatus.INTAKE_AUTHORIZATION: frozenset(
        {
            CaseStatus.ROUTE_SCOPE_CONFIRMED,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.ROUTE_SCOPE_CONFIRMED: frozenset(
        {
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.COLLECT_AND_VERIFY: frozenset(
        {
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.DRAFT_PREPARATION,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.ISSUE_RESOLUTION: frozenset(
        {
            # Concrete exits are constrained by resume_status at validation time.
            CaseStatus.LEAD,
            CaseStatus.INTAKE_AUTHORIZATION,
            CaseStatus.ROUTE_SCOPE_CONFIRMED,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.DRAFT_PREPARATION,
            CaseStatus.CUSTOMER_CONFIRMATION,
            CaseStatus.FINAL_REVIEW,
            CaseStatus.READY_TO_DELIVER,
            CaseStatus.SENT_AWAITING_ACK,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.DRAFT_PREPARATION: frozenset(
        {
            CaseStatus.CUSTOMER_CONFIRMATION,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.CUSTOMER_CONFIRMATION: frozenset(
        {
            CaseStatus.FINAL_REVIEW,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.FINAL_REVIEW: frozenset(
        {
            CaseStatus.READY_TO_DELIVER,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.DRAFT_PREPARATION,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.READY_TO_DELIVER: frozenset(
        {
            CaseStatus.SENT_AWAITING_ACK,
            CaseStatus.FINAL_REVIEW,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.SENT_AWAITING_ACK: frozenset(
        {
            CaseStatus.DELIVERED,
            CaseStatus.READY_TO_DELIVER,
            CaseStatus.FINAL_REVIEW,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.DELIVERED: frozenset(
        {
            CaseStatus.CLOSED,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.ISSUE_RESOLUTION,
        }
    ),
    CaseStatus.CLOSED: frozenset(
        {CaseStatus.LEAD, CaseStatus.INTAKE_AUTHORIZATION}
    ),
}


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """Preconditions that case_service must resolve for the current case version."""

    case_version: int
    customer_confirmation_case_version: int | None = None
    final_review_approval_case_version: int | None = None
    reopen_authorized: bool = False
    # Stage captured when entering issue_resolution; required to leave it.
    resume_status: CaseStatus | None = None
    # Current outbound/delivery record id awaiting customer ack.
    delivery_id: str | None = None
    # Receipt must bind to the same delivery_id.
    receipt_for_delivery_id: str | None = None


def _requirements_for(
    current: CaseStatus, target: CaseStatus
) -> frozenset[Requirement]:
    """Single source for context-needed and precondition checks."""
    req: set[Requirement] = set()

    if target == CaseStatus.FINAL_REVIEW:
        req.add("customer_confirmation")

    if target == CaseStatus.READY_TO_DELIVER:
        req.add("customer_confirmation")
        req.add("final_approval")

    # Send / resend must still be authorized on the *current* case version.
    if target == CaseStatus.SENT_AWAITING_ACK:
        req.add("customer_confirmation")
        req.add("final_approval")

    if target == CaseStatus.DELIVERED:
        req.add("delivery_receipt")
        req.add("customer_confirmation")
        req.add("final_approval")

    if (
        current == CaseStatus.CLOSED
        and target in {CaseStatus.LEAD, CaseStatus.INTAKE_AUTHORIZATION}
    ) or (
        current == CaseStatus.DELIVERED
        and target
        in {CaseStatus.COLLECT_AND_VERIFY, CaseStatus.ISSUE_RESOLUTION}
    ):
        req.add("reopen")

    if current == CaseStatus.ISSUE_RESOLUTION and target != CaseStatus.CLOSED:
        req.add("resume_status")

    return frozenset(req)


def _issue_resolution_target_allowed(
    resume: CaseStatus, target: CaseStatus
) -> bool:
    """Return to resume stage, or continue only via resume's normal forwards."""
    if target == CaseStatus.CLOSED:
        return True
    if target == resume:
        return True
    return target in _FORWARD.get(resume, frozenset()) and target != CaseStatus.ISSUE_RESOLUTION


def can_transition(
    current: CaseStatus,
    target: CaseStatus,
    ctx: TransitionContext | None = None,
) -> bool:
    if current == target:
        return True
    if target not in _FORWARD.get(current, frozenset()):
        return False
    if current == CaseStatus.ISSUE_RESOLUTION and target != CaseStatus.CLOSED:
        # Edge exists in the table; resume binding is enforced when ctx is present.
        if ctx is None:
            return True
        if ctx.resume_status is None:
            return False
        return _issue_resolution_target_allowed(ctx.resume_status, target)
    return True


def _requires_transition_context(current: CaseStatus, target: CaseStatus) -> bool:
    return bool(_requirements_for(current, target))


def _precondition_failures(
    current: CaseStatus,
    target: CaseStatus,
    ctx: TransitionContext,
) -> list[tuple[ErrorCode, str]]:
    failures: list[tuple[ErrorCode, str]] = []
    requirements = _requirements_for(current, target)

    if "customer_confirmation" in requirements:
        if ctx.customer_confirmation_case_version is None:
            failures.append(
                (
                    ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED,
                    "customer confirmation missing for current case version",
                )
            )
        elif ctx.customer_confirmation_case_version != ctx.case_version:
            failures.append(
                (
                    ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED,
                    "customer confirmation not bound to current case version",
                )
            )

    if "final_approval" in requirements:
        if ctx.final_review_approval_case_version is None:
            failures.append(
                (
                    ErrorCode.REVIEW_APPROVAL_REQUIRED,
                    "final review approval missing for current case version",
                )
            )
        elif ctx.final_review_approval_case_version != ctx.case_version:
            failures.append(
                (
                    ErrorCode.REVIEW_APPROVAL_REQUIRED,
                    "final review approval not bound to current case version",
                )
            )

    if "reopen" in requirements and not ctx.reopen_authorized:
        failures.append(
            (
                ErrorCode.REOPEN_UNAUTHORIZED,
                "reopen authorization required",
            )
        )

    if "resume_status" in requirements:
        if ctx.resume_status is None:
            failures.append(
                (
                    ErrorCode.VALIDATION_ERROR,
                    "resume_status required when leaving issue_resolution",
                )
            )
        elif not _issue_resolution_target_allowed(ctx.resume_status, target):
            failures.append(
                (
                    ErrorCode.VALIDATION_ERROR,
                    "issue_resolution may only return to resume_status or continue via that stage's forwards",
                )
            )

    if "delivery_receipt" in requirements:
        if not ctx.delivery_id:
            failures.append(
                (
                    ErrorCode.DELIVERY_RECEIPT_REQUIRED,
                    "delivery_id required for delivered transition",
                )
            )
        elif ctx.receipt_for_delivery_id is None:
            failures.append(
                (
                    ErrorCode.DELIVERY_RECEIPT_REQUIRED,
                    "delivery receipt missing for current delivery record",
                )
            )
        elif ctx.receipt_for_delivery_id != ctx.delivery_id:
            failures.append(
                (
                    ErrorCode.DELIVERY_RECEIPT_REQUIRED,
                    "delivery receipt not bound to current delivery record",
                )
            )

    return failures


def validate_transition(
    current: CaseStatus,
    target: CaseStatus,
    ctx: TransitionContext | None = None,
) -> None:
    """Reject illegal edges and gated moves lacking case_service preconditions."""
    if current == target:
        return
    if not can_transition(current, target, ctx):
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="illegal case status transition",
            details=(f"{current.value}->{target.value}",),
        )
    if ctx is None:
        if _requires_transition_context(current, target):
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="transition context required from case_service",
                details=(f"{current.value}->{target.value}",),
            )
        return

    failures = _precondition_failures(current, target, ctx)
    if failures:
        code, message = failures[0]
        raise DomainError(
            code=code,
            message=message,
            details=tuple(f"{c.value}:{m}" for c, m in failures),
        )


def forward_candidates(current: CaseStatus) -> frozenset[CaseStatus]:
    """Graph edges from ``current`` (precondition checks applied separately)."""
    return _FORWARD.get(current, frozenset())


def allowed_targets(
    current: CaseStatus,
    ctx: TransitionContext | None = None,
) -> frozenset[CaseStatus]:
    candidates = forward_candidates(current) | {current}
    return frozenset(s for s in candidates if can_transition(current, s, ctx))
