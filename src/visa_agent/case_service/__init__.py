from visa_agent.case_service.remediation import (
    AdvisorAlert,
    OpenQuestion,
    acknowledge_advisor_question,
    close_remediation,
    list_advisor_alerts,
    list_open_questions,
    open_remediation,
)
from visa_agent.case_service.outreach import (
    OpeningOutreachResult,
    require_customer_email,
    send_opening_outreach,
    try_auto_attribute_inbound_by_customer_email,
)
from visa_agent.case_service.service import (
    CaseRecord,
    CustomerConfirmationRecord,
    FinalReviewRecord,
    ReviewBoardSnapshot,
    attribute_inbound_to_case,
    build_transition_context,
    create_case,
    get_case,
    get_case_review_page,
    get_review_board,
    list_cases,
    list_customer_confirmations,
    list_final_reviews,
    mark_inbound_processed,
    record_customer_confirmation,
    record_delivery_receipt,
    record_final_review,
    record_reopen_grant,
    set_case_route,
    set_takeover,
)


def __getattr__(name: str):
    # The inbox uses document services, which also import case versioning.
    if name in {"InboundProcessResult", "process_customer_inbound", "process_inbound_after_attribution"}:
        from visa_agent.case_service import inbound_agent

        return getattr(inbound_agent, name)
    raise AttributeError(name)


__all__ = [
    "CaseRecord",
    "CustomerConfirmationRecord",
    "FinalReviewRecord",
    "AdvisorAlert",
    "InboundProcessResult",
    "OpeningOutreachResult",
    "OpenQuestion",
    "ReviewBoardSnapshot",
    "acknowledge_advisor_question",
    "attribute_inbound_to_case",
    "build_transition_context",
    "close_remediation",
    "create_case",
    "get_case",
    "get_case_review_page",
    "get_review_board",
    "list_advisor_alerts",
    "list_cases",
    "list_customer_confirmations",
    "list_final_reviews",
    "list_open_questions",
    "mark_inbound_processed",
    "open_remediation",
    "process_customer_inbound",
    "process_inbound_after_attribution",
    "record_customer_confirmation",
    "record_delivery_receipt",
    "record_final_review",
    "record_reopen_grant",
    "require_customer_email",
    "send_opening_outreach",
    "set_case_route",
    "set_takeover",
    "try_auto_attribute_inbound_by_customer_email",
]
