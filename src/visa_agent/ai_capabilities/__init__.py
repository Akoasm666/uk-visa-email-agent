"""AI capabilities — candidate facts / drafts / customer Q&A; no send side effects."""

from visa_agent.ai_capabilities.draft import (
    DraftResult,
    SyntheticCaseContext,
    build_synthetic_context,
    generate_case_draft,
    list_case_drafts,
)
from visa_agent.ai_capabilities.gateway import (
    context_from_stored_payload,
    gate_model_output,
    groundedness_report,
    render_draft_from_grounded_facts,
)
from visa_agent.ai_capabilities.language import detect_reply_language

__all__ = [
    "DraftResult",
    "SyntheticCaseContext",
    "build_synthetic_context",
    "detect_reply_language",
    "generate_case_draft",
    "list_case_drafts",
    "gate_model_output",
    "groundedness_report",
    "context_from_stored_payload",
    "render_draft_from_grounded_facts",
]


def __getattr__(name: str):
    # Lazy exports to avoid circular imports with case_service.
    if name in {
        "QuestionAnswer",
        "answer_customer_question",
        "build_status_reply",
    }:
        from visa_agent.ai_capabilities import customer_reply as _cr

        return getattr(_cr, name)
    if name in {"lookup_ukvi_snippets", "select_ukvi_urls"}:
        from visa_agent.ai_capabilities import ukvi_lookup as _uk

        return getattr(_uk, name)
    raise AttributeError(name)
