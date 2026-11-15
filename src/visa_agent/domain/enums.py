"""Shared enumerations — change only with TL approval."""

from enum import StrEnum


class CaseStatus(StrEnum):
    LEAD = "lead"
    INTAKE_AUTHORIZATION = "intake_authorization"
    ROUTE_SCOPE_CONFIRMED = "route_scope_confirmed"
    COLLECT_AND_VERIFY = "collect_and_verify"
    ISSUE_RESOLUTION = "issue_resolution"
    DRAFT_PREPARATION = "draft_preparation"
    CUSTOMER_CONFIRMATION = "customer_confirmation"
    FINAL_REVIEW = "final_review"
    READY_TO_DELIVER = "ready_to_deliver"
    SENT_AWAITING_ACK = "sent_awaiting_ack"
    DELIVERED = "delivered"
    CLOSED = "closed"


class FactStatus(StrEnum):
    PENDING_EXTRACTION = "pending_extraction"
    CUSTOMER_STATED = "customer_stated"
    DOCUMENT_SUPPORTED = "document_supported"
    HUMAN_VERIFIED = "human_verified"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class RequirementStatus(StrEnum):
    APPLICABILITY_UNKNOWN = "applicability_unknown"
    APPLICABLE_MISSING = "applicable_missing"
    RECEIVED_PENDING_REVIEW = "received_pending_review"
    EVIDENCE_ACCEPTED = "evidence_accepted"
    NEEDS_CORRECTION = "needs_correction"
    NOT_APPLICABLE = "not_applicable"
    EXEMPTED_WITH_BASIS = "exempted_with_basis"


class RouteCode(StrEnum):
    STANDARD_VISITOR = "standard_visitor"
    STUDENT = "student"
    SKILLED_WORKER = "skilled_worker"
    GRADUATE = "graduate"


class DeliveryKind(StrEnum):
    FINAL_PACKAGE = "final_package"
    PROGRESS_PACKAGE = "progress_package"
    HANDOFF_OR_TERMINATION = "handoff_or_termination"


class OutboundMessageKind(StrEnum):
    """Outbound traffic kinds — final packages use stricter approval binding."""

    ORDINARY_REPLY = "ordinary_reply"
    PROGRESS_PACKAGE = "progress_package"
    FINAL_PACKAGE = "final_package"


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT_VERSION = "CONFLICT_VERSION"
    IDEMPOTENCY_REPLAY = "IDEMPOTENCY_REPLAY"
    EMAIL_UNATTRIBUTED = "EMAIL_UNATTRIBUTED"
    EMAIL_DUPLICATE = "EMAIL_DUPLICATE"
    ATTACHMENT_REJECTED = "ATTACHMENT_REJECTED"
    OUTBOUND_NOT_ALLOWLISTED = "OUTBOUND_NOT_ALLOWLISTED"
    OUTBOUND_STATUS_UNKNOWN = "OUTBOUND_STATUS_UNKNOWN"
    TAKEOVER_ACTIVE = "TAKEOVER_ACTIVE"
    CUSTOMER_CONFIRMATION_REQUIRED = "CUSTOMER_CONFIRMATION_REQUIRED"
    REVIEW_APPROVAL_REQUIRED = "REVIEW_APPROVAL_REQUIRED"
    REOPEN_UNAUTHORIZED = "REOPEN_UNAUTHORIZED"
    FINAL_PACKAGE_APPROVAL_MISMATCH = "FINAL_PACKAGE_APPROVAL_MISMATCH"
    DELIVERY_RECEIPT_REQUIRED = "DELIVERY_RECEIPT_REQUIRED"
    RULE_UNKNOWN = "RULE_UNKNOWN"
    FACT_CONFLICT = "FACT_CONFLICT"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_GROUNDEDNESS = "MODEL_GROUNDEDNESS"
    DELIVERY_GATE_FAILED = "DELIVERY_GATE_FAILED"
    SYSTEM_BLOCKED = "SYSTEM_BLOCKED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
