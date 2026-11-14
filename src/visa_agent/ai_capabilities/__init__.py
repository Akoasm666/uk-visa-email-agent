"""AI capabilities — candidate facts / drafts only; no side effects."""

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

__all__ = [
    "DraftResult",
    "SyntheticCaseContext",
    "build_synthetic_context",
    "generate_case_draft",
    "list_case_drafts",
    "gate_model_output",
    "groundedness_report",
    "context_from_stored_payload",
    "render_draft_from_grounded_facts",
]
