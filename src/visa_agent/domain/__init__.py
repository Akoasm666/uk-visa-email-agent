"""Shared domain contracts. Status enums and shared shapes need TL approval to change."""

from visa_agent.domain.enums import (
    CaseStatus,
    DeliveryKind,
    ErrorCode,
    FactStatus,
    OutboundMessageKind,
    RequirementStatus,
    RouteCode,
)
from visa_agent.domain.errors import DomainError
from visa_agent.domain.models import (
    CaseRef,
    FinalPackageApproval,
    ModuleBoundary,
    OutboundGateInput,
    evaluate_outbound_gate,
)
from visa_agent.domain.transitions import (
    CONTROL_FLAGS,
    TransitionContext,
    can_transition,
    validate_transition,
)

__all__ = [
    "CONTROL_FLAGS",
    "CaseRef",
    "CaseStatus",
    "DeliveryKind",
    "DomainError",
    "ErrorCode",
    "FactStatus",
    "FinalPackageApproval",
    "ModuleBoundary",
    "OutboundGateInput",
    "OutboundMessageKind",
    "RequirementStatus",
    "RouteCode",
    "TransitionContext",
    "can_transition",
    "evaluate_outbound_gate",
    "validate_transition",
]
