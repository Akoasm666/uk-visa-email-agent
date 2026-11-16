from __future__ import annotations

from dataclasses import dataclass

from visa_agent.domain.enums import (
    CaseStatus,
    DeliveryKind,
    OutboundMessageKind,
    RouteCode,
)

_KNOWN_OUTBOUND_KINDS = frozenset(k.value for k in OutboundMessageKind)
_KNOWN_PACKAGE_KINDS = frozenset(k.value for k in DeliveryKind)


@dataclass(frozen=True, slots=True)
class CaseRef:
    """Minimal shared case identity used across modules."""

    tenant_id: str
    case_id: str
    case_version: int
    status: CaseStatus
    route: RouteCode | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.case_id:
            raise ValueError("tenant_id and case_id are required")
        if self.case_version < 1:
            raise ValueError("case_version must be >= 1")


@dataclass(frozen=True, slots=True)
class ModuleBoundary:
    """Declarative module I/O contract for documentation and tests."""

    name: str
    may_mutate_case: bool
    may_send_email: bool
    may_approve: bool
    outputs: tuple[str, ...]


MODULE_BOUNDARIES: tuple[ModuleBoundary, ...] = (
    ModuleBoundary(
        name="email_adapter",
        may_mutate_case=False,
        may_send_email=False,
        may_approve=False,
        outputs=("email_event_id", "body_ref", "attachment_refs", "auth_hints"),
    ),
    ModuleBoundary(
        name="case_service",
        may_mutate_case=True,
        may_send_email=False,
        may_approve=False,
        outputs=(
            "case_ref",
            "facts",
            "grants",
            "requirement_states",
            "transition_context",
        ),
    ),
    ModuleBoundary(
        name="document_pipeline",
        may_mutate_case=False,
        may_send_email=False,
        may_approve=False,
        outputs=("evidence_id", "annotation_candidates"),
    ),
    ModuleBoundary(
        name="workflow",
        may_mutate_case=True,
        may_send_email=False,
        may_approve=False,
        outputs=("status", "control_flags", "reminders", "outbox_enqueue"),
    ),
    ModuleBoundary(
        name="rules",
        may_mutate_case=False,
        may_send_email=False,
        may_approve=False,
        outputs=("requirements", "check_results", "unknown_reasons"),
    ),
    ModuleBoundary(
        name="ai_capabilities",
        may_mutate_case=False,
        may_send_email=False,
        may_approve=False,
        outputs=("candidate_facts", "draft_text", "suggested_actions"),
    ),
    ModuleBoundary(
        name="human_workbench",
        may_mutate_case=True,
        may_send_email=False,
        may_approve=True,
        outputs=("review_decision", "annotations", "takeover", "reopen_grant"),
    ),
    ModuleBoundary(
        name="delivery",
        may_mutate_case=False,
        may_send_email=False,
        may_approve=False,
        outputs=("package_manifest", "file_hashes", "gate_results", "package_kind"),
    ),
    ModuleBoundary(
        name="send_control",
        may_mutate_case=False,
        may_send_email=True,
        may_approve=False,
        outputs=("outbox_status", "provider_message_id"),
    ),
)


@dataclass(frozen=True, slots=True)
class FinalPackageApproval:
    """Human approval that must bind case version, package, and recipients."""

    case_version: int
    package_id: str
    recipient_emails: frozenset[str]

    def __post_init__(self) -> None:
        if self.case_version < 1:
            raise ValueError("case_version must be >= 1")
        if not self.package_id:
            raise ValueError("package_id is required")
        if not self.recipient_emails:
            raise ValueError("recipient_emails must be non-empty")


@dataclass(frozen=True, slots=True)
class OutboundGateInput:
    """Inputs required by outbound hard gates.

    Whether final-package approval is required is decided by the *actual*
    ``package_kind`` from delivery (not by a self-declared progress label).
    """

    kind: str
    recipient_emails: frozenset[str]
    allowlist: frozenset[str]
    human_takeover: bool
    human_send_approved: bool
    case_version: int
    outbox_case_version: int
    has_unprocessed_inbound: bool
    system_blocked: bool
    paused: bool
    cancelled: bool
    package_id: str | None = None
    package_kind: str | None = None
    final_package_approval: FinalPackageApproval | None = None


def _normalize_emails(emails: frozenset[str]) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in emails)


def _dedupe(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for code in reasons:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered


def _check_final_package_approval(
    inp: OutboundGateInput,
    recipients: frozenset[str],
    reasons: list[str],
) -> None:
    approval = inp.final_package_approval
    if approval is None:
        reasons.append("REVIEW_APPROVAL_REQUIRED")
        return
    approved_recipients = _normalize_emails(approval.recipient_emails)
    if approval.case_version != inp.case_version:
        reasons.append("FINAL_PACKAGE_APPROVAL_MISMATCH")
    if not inp.package_id or approval.package_id != inp.package_id:
        reasons.append("FINAL_PACKAGE_APPROVAL_MISMATCH")
    if approved_recipients != recipients:
        reasons.append("FINAL_PACKAGE_APPROVAL_MISMATCH")


def evaluate_outbound_gate(inp: OutboundGateInput) -> list[str]:
    """Return blocking error codes; empty list means send is allowed."""
    reasons: list[str] = []
    recipients = _normalize_emails(inp.recipient_emails)
    allow = _normalize_emails(inp.allowlist)

    if inp.kind not in _KNOWN_OUTBOUND_KINDS:
        reasons.append("VALIDATION_ERROR")
    if inp.package_kind is not None and inp.package_kind not in _KNOWN_PACKAGE_KINDS:
        reasons.append("VALIDATION_ERROR")

    if not recipients:
        reasons.append("VALIDATION_ERROR")
    if not recipients.issubset(allow):
        reasons.append("OUTBOUND_NOT_ALLOWLISTED")
    if inp.human_takeover and not inp.human_send_approved:
        reasons.append("TAKEOVER_ACTIVE")
    if inp.case_version != inp.outbox_case_version:
        reasons.append("CONFLICT_VERSION")
    if inp.has_unprocessed_inbound:
        reasons.append("DELIVERY_GATE_FAILED")
    if inp.system_blocked or inp.paused or inp.cancelled:
        reasons.append("SYSTEM_BLOCKED")

    if inp.kind in {OutboundMessageKind.ORDINARY_REPLY, OutboundMessageKind.CASE_CLOSURE_NOTICE} and (
        inp.package_id is not None or inp.package_kind is not None
    ):
        reasons.append("VALIDATION_ERROR")

    # Package-bearing kinds need a package identity + declared package type.
    if inp.kind in {
        OutboundMessageKind.PROGRESS_PACKAGE.value,
        OutboundMessageKind.FINAL_PACKAGE.value,
    }:
        if not inp.package_id or inp.package_kind is None:
            reasons.append("VALIDATION_ERROR")
        if (
            inp.kind == OutboundMessageKind.FINAL_PACKAGE.value
            and inp.package_kind != DeliveryKind.FINAL_PACKAGE.value
        ):
            reasons.append("VALIDATION_ERROR")

    # Actual final packages always need final approval — even if mislabeled
    # as progress_package on the outbound kind.
    actual_is_final = inp.package_kind == DeliveryKind.FINAL_PACKAGE.value
    declared_final = inp.kind == OutboundMessageKind.FINAL_PACKAGE.value
    if actual_is_final or declared_final:
        if not inp.package_id or not actual_is_final:
            reasons.append("VALIDATION_ERROR")
        _check_final_package_approval(inp, recipients, reasons)

    return _dedupe(reasons)
