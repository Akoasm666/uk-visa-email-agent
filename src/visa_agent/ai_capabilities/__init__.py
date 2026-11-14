"""AI capabilities — candidate facts / drafts only; no side effects."""

from visa_agent.ai_capabilities.draft import (
    DraftResult,
    SyntheticCaseContext,
    build_synthetic_context,
    generate_case_draft,
    list_case_drafts,
)

__all__ = [
    "DraftResult",
    "SyntheticCaseContext",
    "build_synthetic_context",
    "generate_case_draft",
    "list_case_drafts",
]
