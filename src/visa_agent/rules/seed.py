"""Seed Standard Visitor pack v1 from docs/05 outline (retrieved 2026-11-14)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SeedRequirement:
    key: str
    title: str
    description: str
    severity: str = "required"


VISITOR_PACK_ID = "standard_visitor"
VISITOR_PACK_VERSION = 1
# S3: polarity + funds-context evaluator; publish + rebind via impact/switch flow.
VISITOR_PACK_VERSION_S3 = 2
VISITOR_PACK_TITLE = "Standard Visitor — core genuine-visitor themes"
VISITOR_PACK_TITLE_S3 = (
    "Standard Visitor — S3 polarity + student-funds context gates"
)
VISITOR_SOURCE_URL = (
    "https://www.gov.uk/government/publications/"
    "visitor-visa-guide-to-supporting-documents/"
    "guide-to-supporting-documents-visiting-the-uk"
)
VISITOR_SOURCE_RETRIEVED_ON = "2026-11-14"
VISITOR_SOURCE_RETRIEVED_ON_S3 = "2026-11-15"

VISITOR_REQUIREMENTS: tuple[SeedRequirement, ...] = (
    SeedRequirement(
        key="passport_valid",
        title="Passport / travel document valid",
        description="Applicant holds a valid passport or travel document.",
    ),
    SeedRequirement(
        key="visit_purpose_allowed",
        title="Visit purpose is an allowed activity",
        description=(
            "Stated purpose matches permitted Standard Visitor activities "
            "and is consistent with materials."
        ),
    ),
    SeedRequirement(
        key="intention_to_leave",
        title="Intention to leave the UK after the visit",
        description="Credible basis that the applicant will leave at the end of the stay.",
    ),
    SeedRequirement(
        key="funds_for_stay",
        title="Funds to cover stay and onward travel",
        description=(
            "Can meet the cost of the visit and return/onward travel for self "
            "(and dependants if any). Not Student fixed-funds formulas."
        ),
    ),
    SeedRequirement(
        key="home_ties",
        title="Circumstances in country of residence",
        description="Work / study / self-employment or other ties consistent with the account.",
    ),
    SeedRequirement(
        key="sponsor_if_any",
        title="Sponsor support (if applicable)",
        description=(
            "If sponsored: support offered, relationship, sponsor means, "
            "and UK status where relevant."
        ),
        severity="required",
    ),
    SeedRequirement(
        key="translation_if_needed",
        title="Full translation of non-English/Welsh documents",
        description=(
            "Complete translation plus translator name, signature, date, and contact details."
        ),
    ),
    SeedRequirement(
        key="transfer_human_triggers",
        title="Default transfer-to-human triggers",
        description=(
            "Minors, complex sponsorship, refusal history, in-country switching, "
            "unclear activity boundaries, medical/PPE purposes — escalate to human."
        ),
        severity="transfer_human",
    ),
)
