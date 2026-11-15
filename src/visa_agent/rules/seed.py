"""Seed route packs from GOV.UK outlines (retrieved dates on each pack)."""

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

# --- Student (demo pack v1; separate from Visitor funds rules) ---
STUDENT_PACK_ID = "student"
STUDENT_PACK_VERSION = 1
STUDENT_PACK_TITLE = "Student — CAS / maintenance / English themes"
STUDENT_SOURCE_URL = "https://www.gov.uk/student-visa"
STUDENT_SOURCE_RETRIEVED_ON = "2026-11-15"
STUDENT_REQUIREMENTS: tuple[SeedRequirement, ...] = (
    SeedRequirement(
        key="passport_valid",
        title="Passport / travel document valid",
        description="Applicant holds a valid passport or travel document.",
    ),
    SeedRequirement(
        key="cas_confirmation",
        title="Confirmation of Acceptance for Studies (CAS)",
        description=(
            "Valid CAS from a licensed Student sponsor covering the course."
        ),
    ),
    SeedRequirement(
        key="funds_maintenance",
        title="Maintenance / course funds",
        description=(
            "Evidence of funds for course fees and living costs per Student "
            "route rules (CAS-linked periods). Not Visitor 'funds for stay'."
        ),
    ),
    SeedRequirement(
        key="english_language",
        title="English language requirement",
        description="Meets English language requirement for the Student route.",
    ),
    SeedRequirement(
        key="course_offer",
        title="Course / academic offer consistency",
        description="Course details consistent with CAS and stated study plan.",
    ),
    SeedRequirement(
        key="tuberculosis_test_if_required",
        title="TB test (if applicable)",
        description="TB certificate when required by nationality / residence.",
        severity="required",
    ),
    SeedRequirement(
        key="transfer_human_triggers",
        title="Default transfer-to-human triggers",
        description=(
            "Minors, dependants complexity, prior refusals, in-country switching "
            "disputes — escalate to human."
        ),
        severity="transfer_human",
    ),
)

# --- Skilled Worker (demo pack v1) ---
SKILLED_WORKER_PACK_ID = "skilled_worker"
SKILLED_WORKER_PACK_VERSION = 1
SKILLED_WORKER_PACK_TITLE = "Skilled Worker — CoS / skill / salary themes"
SKILLED_WORKER_SOURCE_URL = "https://www.gov.uk/skilled-worker-visa"
SKILLED_WORKER_SOURCE_RETRIEVED_ON = "2026-11-15"
SKILLED_WORKER_REQUIREMENTS: tuple[SeedRequirement, ...] = (
    SeedRequirement(
        key="passport_valid",
        title="Passport / travel document valid",
        description="Applicant holds a valid passport or travel document.",
    ),
    SeedRequirement(
        key="certificate_of_sponsorship",
        title="Certificate of Sponsorship (CoS)",
        description="Valid CoS from a licensed Skilled Worker sponsor.",
    ),
    SeedRequirement(
        key="job_skill_level",
        title="Eligible job / skill level",
        description="Role meets Skilled Worker skill-level eligibility.",
    ),
    SeedRequirement(
        key="salary_threshold",
        title="Salary threshold",
        description="Pay meets applicable Skilled Worker salary threshold.",
    ),
    SeedRequirement(
        key="english_language",
        title="English language requirement",
        description="Meets English language requirement for Skilled Worker.",
    ),
    SeedRequirement(
        key="tuberculosis_test_if_required",
        title="TB test (if applicable)",
        description="TB certificate when required by nationality / residence.",
    ),
    SeedRequirement(
        key="transfer_human_triggers",
        title="Default transfer-to-human triggers",
        description=(
            "Prior refusals, sponsor licensing issues, salary edge cases, "
            "dependants complexity — escalate to human."
        ),
        severity="transfer_human",
    ),
)

# --- Graduate (demo pack v1; post-study UK route) ---
GRADUATE_PACK_ID = "graduate"
GRADUATE_PACK_VERSION = 1
GRADUATE_PACK_TITLE = "Graduate — completed UK study / eligibility themes"
GRADUATE_SOURCE_URL = "https://www.gov.uk/graduate-visa"
GRADUATE_SOURCE_RETRIEVED_ON = "2026-11-15"
GRADUATE_REQUIREMENTS: tuple[SeedRequirement, ...] = (
    SeedRequirement(
        key="passport_valid",
        title="Passport / travel document valid",
        description="Applicant holds a valid passport or travel document.",
    ),
    SeedRequirement(
        key="uk_study_completed",
        title="Eligible UK study completed",
        description=(
            "Completed an eligible course with a Student / Tier 4 sponsor "
            "consistent with Graduate route eligibility."
        ),
    ),
    SeedRequirement(
        key="qualification_confirmed",
        title="Qualification / award confirmed",
        description=(
            "Sponsor / institution confirmation that the qualification was "
            "successfully completed."
        ),
    ),
    SeedRequirement(
        key="current_permission_basis",
        title="Current permission / switching basis",
        description=(
            "Applicant is in the UK with permission that allows switching "
            "to Graduate (or meets entry criteria if applying from overseas)."
        ),
    ),
    SeedRequirement(
        key="tuberculosis_test_if_required",
        title="TB test (if applicable)",
        description="TB certificate when required by nationality / residence.",
    ),
    SeedRequirement(
        key="transfer_human_triggers",
        title="Default transfer-to-human triggers",
        description=(
            "Prior refusals, study-completion disputes, dependants complexity, "
            "timing / switching edge cases — escalate to human."
        ),
        severity="transfer_human",
    ),
)
