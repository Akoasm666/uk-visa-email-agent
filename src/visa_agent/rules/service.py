"""Versioned rule packs: human publish only; unknown never counts as pass."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import date

import psycopg

from visa_agent.case_service.service import (
    bump_case_version_in_conn,
    get_case,
)
from visa_agent.domain.enums import ErrorCode, RequirementStatus, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.rules.seed import (
    GRADUATE_PACK_ID,
    GRADUATE_PACK_TITLE,
    GRADUATE_PACK_VERSION,
    GRADUATE_REQUIREMENTS,
    GRADUATE_SOURCE_RETRIEVED_ON,
    GRADUATE_SOURCE_URL,
    SKILLED_WORKER_PACK_ID,
    SKILLED_WORKER_PACK_TITLE,
    SKILLED_WORKER_PACK_VERSION,
    SKILLED_WORKER_REQUIREMENTS,
    SKILLED_WORKER_SOURCE_RETRIEVED_ON,
    SKILLED_WORKER_SOURCE_URL,
    STUDENT_PACK_ID,
    STUDENT_PACK_TITLE,
    STUDENT_PACK_VERSION,
    STUDENT_REQUIREMENTS,
    STUDENT_SOURCE_RETRIEVED_ON,
    STUDENT_SOURCE_URL,
    SeedRequirement,
    VISITOR_PACK_ID,
    VISITOR_PACK_TITLE,
    VISITOR_PACK_TITLE_S3,
    VISITOR_PACK_VERSION,
    VISITOR_PACK_VERSION_S3,
    VISITOR_REQUIREMENTS,
    VISITOR_SOURCE_RETRIEVED_ON,
    VISITOR_SOURCE_RETRIEVED_ON_S3,
    VISITOR_SOURCE_URL,
)

# Statuses that mean "this required theme is satisfied".
_PASS_STATUSES = frozenset(
    {
        RequirementStatus.EVIDENCE_ACCEPTED.value,
        RequirementStatus.NOT_APPLICABLE.value,
        RequirementStatus.EXEMPTED_WITH_BASIS.value,
    }
)

# Evidence classifications that weakly map to requirement themes (demo heuristic).
_CLASSIFICATION_HINTS: dict[str, str] = {
    # Visitor
    "passport": "passport_valid",
    "bank": "funds_for_stay",
    "funds": "funds_for_stay",
    "sponsor": "sponsor_if_any",
    "translation": "translation_if_needed",
    "itinerary": "intention_to_leave",
    "employment": "home_ties",
    "purpose": "visit_purpose_allowed",
    # Student
    "cas": "cas_confirmation",
    "maintenance": "funds_maintenance",
    "course": "course_offer",
    "offer": "course_offer",
    # Skilled Worker
    "cos": "certificate_of_sponsorship",
    "sponsorship": "certificate_of_sponsorship",
    "salary": "salary_threshold",
    "skill": "job_skill_level",
    # Shared Student / SW
    "english": "english_language",
    "tb": "tuberculosis_test_if_required",
    "tuberculosis": "tuberculosis_test_if_required",
    # Graduate
    "degree": "qualification_confirmed",
    "qualification": "qualification_confirmed",
    "graduation": "uk_study_completed",
    "uk_study": "uk_study_completed",
    "permission": "current_permission_basis",
}

_SEED_LOCK_K1 = 920522
_SEED_LOCK_K2 = 1


@dataclass(frozen=True, slots=True)
class RuleRequirement:
    key: str
    title: str
    description: str
    severity: str


@dataclass(frozen=True, slots=True)
class RulePack:
    pack_id: str
    pack_version: int
    route: str
    title: str
    source_url: str
    source_retrieved_on: str | None
    published: bool
    published_at: str | None
    requirements: tuple[RuleRequirement, ...]


@dataclass(frozen=True, slots=True)
class CaseRequirementState:
    requirement_key: str
    status: str
    reason: str
    severity: str
    pack_id: str
    pack_version: int
    case_version: int


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    pack_id: str
    pack_version: int
    case_id: str
    case_version: int
    states: tuple[CaseRequirementState, ...]
    blocking_keys: tuple[str, ...]
    all_required_clear: bool


def _row_to_pack(row: tuple, reqs: list[tuple]) -> RulePack:
    retrieved = row[5].isoformat() if row[5] is not None else None
    published_at = row[7].isoformat() if row[7] is not None else None
    return RulePack(
        pack_id=row[0],
        pack_version=row[1],
        route=row[2],
        title=row[3],
        source_url=row[4] or "",
        source_retrieved_on=retrieved,
        published=bool(row[6]),
        published_at=published_at,
        requirements=tuple(
            RuleRequirement(
                key=r[0], title=r[1], description=r[2] or "", severity=r[3]
            )
            for r in reqs
        ),
    )


def _insert_pack_version(
    conn: psycopg.Connection,
    *,
    pack_id: str,
    pack_version: int,
    route: str,
    title: str,
    source_url: str,
    retrieved_on: str,
    requirements: tuple[SeedRequirement, ...],
) -> None:
    """Insert one pack version if absent. Never updates existing rows."""
    existing = conn.execute(
        """
        SELECT pack_id FROM rule_packs
        WHERE pack_id = %s AND pack_version = %s
        """,
        (pack_id, pack_version),
    ).fetchone()
    if existing is not None:
        return
    conn.execute(
        """
        INSERT INTO rule_packs (
            pack_id, pack_version, route, title,
            source_url, source_retrieved_on, published
        ) VALUES (%s, %s, %s, %s, %s, %s, FALSE)
        """,
        (
            pack_id,
            pack_version,
            route,
            title,
            source_url,
            date.fromisoformat(retrieved_on),
        ),
    )
    for req in requirements:
        conn.execute(
            """
            INSERT INTO rule_requirements (
                id, pack_id, pack_version, requirement_key,
                title, description, severity
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid.uuid4().hex,
                pack_id,
                pack_version,
                req.key,
                req.title,
                req.description,
                req.severity,
            ),
        )


def _insert_visitor_pack_version(
    conn: psycopg.Connection,
    *,
    pack_version: int,
    title: str,
    retrieved_on: str,
) -> None:
    _insert_pack_version(
        conn,
        pack_id=VISITOR_PACK_ID,
        pack_version=pack_version,
        route=RouteCode.STANDARD_VISITOR.value,
        title=title,
        source_url=VISITOR_SOURCE_URL,
        retrieved_on=retrieved_on,
        requirements=VISITOR_REQUIREMENTS,
    )


def ensure_seed_visitor_pack(*, database_url: str) -> RulePack:
    """Insert Visitor pack v1 and S3 v2 when absent.

    Never mutates an existing pack (published or not). S3 evaluator changes
    live on v2 — publish + rebind via impact scan / set_case_active_pack.
    Returns v1 for backward-compatible callers.
    Also seeds Student / Skilled Worker / Graduate v1 packs (idempotent).
    """
    ensure_seed_route_packs(database_url=database_url)
    return get_rule_pack(
        database_url=database_url,
        pack_id=VISITOR_PACK_ID,
        pack_version=VISITOR_PACK_VERSION,
    )


def ensure_seed_route_packs(*, database_url: str) -> list[RulePack]:
    """Seed Visitor (+ S3 v2), Student, Skilled Worker, Graduate packs when absent."""
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (_SEED_LOCK_K1, _SEED_LOCK_K2),
            )
            _insert_visitor_pack_version(
                conn,
                pack_version=VISITOR_PACK_VERSION,
                title=VISITOR_PACK_TITLE,
                retrieved_on=VISITOR_SOURCE_RETRIEVED_ON,
            )
            _insert_visitor_pack_version(
                conn,
                pack_version=VISITOR_PACK_VERSION_S3,
                title=VISITOR_PACK_TITLE_S3,
                retrieved_on=VISITOR_SOURCE_RETRIEVED_ON_S3,
            )
            _insert_pack_version(
                conn,
                pack_id=STUDENT_PACK_ID,
                pack_version=STUDENT_PACK_VERSION,
                route=RouteCode.STUDENT.value,
                title=STUDENT_PACK_TITLE,
                source_url=STUDENT_SOURCE_URL,
                retrieved_on=STUDENT_SOURCE_RETRIEVED_ON,
                requirements=STUDENT_REQUIREMENTS,
            )
            _insert_pack_version(
                conn,
                pack_id=SKILLED_WORKER_PACK_ID,
                pack_version=SKILLED_WORKER_PACK_VERSION,
                route=RouteCode.SKILLED_WORKER.value,
                title=SKILLED_WORKER_PACK_TITLE,
                source_url=SKILLED_WORKER_SOURCE_URL,
                retrieved_on=SKILLED_WORKER_SOURCE_RETRIEVED_ON,
                requirements=SKILLED_WORKER_REQUIREMENTS,
            )
            _insert_pack_version(
                conn,
                pack_id=GRADUATE_PACK_ID,
                pack_version=GRADUATE_PACK_VERSION,
                route=RouteCode.GRADUATE.value,
                title=GRADUATE_PACK_TITLE,
                source_url=GRADUATE_SOURCE_URL,
                retrieved_on=GRADUATE_SOURCE_RETRIEVED_ON,
                requirements=GRADUATE_REQUIREMENTS,
            )
    return [
        get_rule_pack(
            database_url=database_url,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION,
        ),
        get_rule_pack(
            database_url=database_url,
            pack_id=STUDENT_PACK_ID,
            pack_version=STUDENT_PACK_VERSION,
        ),
        get_rule_pack(
            database_url=database_url,
            pack_id=SKILLED_WORKER_PACK_ID,
            pack_version=SKILLED_WORKER_PACK_VERSION,
        ),
        get_rule_pack(
            database_url=database_url,
            pack_id=GRADUATE_PACK_ID,
            pack_version=GRADUATE_PACK_VERSION,
        ),
    ]


def default_pack_for_route(route: RouteCode | str) -> tuple[str, int]:
    """Canonical seed pack id + version for a route (unpublished until publish)."""
    code = route.value if isinstance(route, RouteCode) else str(route)
    if code == RouteCode.STANDARD_VISITOR.value:
        return VISITOR_PACK_ID, VISITOR_PACK_VERSION
    if code == RouteCode.STUDENT.value:
        return STUDENT_PACK_ID, STUDENT_PACK_VERSION
    if code == RouteCode.SKILLED_WORKER.value:
        return SKILLED_WORKER_PACK_ID, SKILLED_WORKER_PACK_VERSION
    if code == RouteCode.GRADUATE.value:
        return GRADUATE_PACK_ID, GRADUATE_PACK_VERSION
    raise DomainError(
        code=ErrorCode.VALIDATION_ERROR,
        message="unsupported route for rule pack binding",
        details=(code,),
    )


def get_rule_pack(
    *,
    database_url: str,
    pack_id: str,
    pack_version: int,
    conn: psycopg.Connection | None = None,
) -> RulePack:
    def _load(c: psycopg.Connection) -> RulePack:
        row = c.execute(
            """
            SELECT pack_id, pack_version, route, title, source_url,
                   source_retrieved_on, published, published_at
            FROM rule_packs
            WHERE pack_id = %s AND pack_version = %s
            """,
            (pack_id, pack_version),
        ).fetchone()
        if row is None:
            raise DomainError(
                code=ErrorCode.NOT_FOUND,
                message="rule pack not found",
                details=(pack_id, str(pack_version)),
            )
        reqs = c.execute(
            """
            SELECT requirement_key, title, description, severity
            FROM rule_requirements
            WHERE pack_id = %s AND pack_version = %s
            ORDER BY requirement_key
            """,
            (pack_id, pack_version),
        ).fetchall()
        return _row_to_pack(row, list(reqs))

    if conn is not None:
        return _load(conn)
    with psycopg.connect(database_url) as owned:
        return _load(owned)


def list_rule_packs(*, database_url: str) -> list[RulePack]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT pack_id, pack_version FROM rule_packs
            ORDER BY pack_id, pack_version DESC
            """
        ).fetchall()
    return [
        get_rule_pack(
            database_url=database_url, pack_id=r[0], pack_version=r[1]
        )
        for r in rows
    ]


def _assert_pack_publishable(pack: RulePack) -> None:
    if not (pack.source_url or "").strip():
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="rule pack missing source_url; cannot publish",
            details=(pack.pack_id, str(pack.pack_version)),
        )
    if not pack.source_retrieved_on:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="rule pack missing source_retrieved_on; cannot publish",
            details=(pack.pack_id, str(pack.pack_version)),
        )
    if not pack.requirements:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="rule pack has no requirements; cannot publish",
            details=(pack.pack_id, str(pack.pack_version)),
        )


def publish_rule_pack(
    *,
    database_url: str,
    pack_id: str,
    pack_version: int,
    provided_token: str,
    configured_token: str,
) -> RulePack:
    """Publish only with a server-configured admin token (disabled when unset)."""
    expected = (configured_token or "").strip()
    if not expected:
        raise DomainError(
            code=ErrorCode.UNAUTHORIZED,
            message="rule pack publish is disabled (no admin token configured)",
        )
    offered = (provided_token or "").strip()
    if not offered or not secrets.compare_digest(offered, expected):
        raise DomainError(
            code=ErrorCode.UNAUTHORIZED,
            message="rule pack publish requires a valid admin credential",
        )
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            pack = get_rule_pack(
                database_url=database_url,
                pack_id=pack_id,
                pack_version=pack_version,
                conn=conn,
            )
            if pack.published:
                # Idempotent: do not rewrite published_at.
                return pack
            _assert_pack_publishable(pack)
            updated = conn.execute(
                """
                UPDATE rule_packs
                SET published = TRUE, published_at = NOW()
                WHERE pack_id = %s AND pack_version = %s AND published = FALSE
                RETURNING pack_id
                """,
                (pack_id, pack_version),
            ).fetchone()
            if updated is None:
                # Lost race to another publisher — reload.
                return get_rule_pack(
                    database_url=database_url,
                    pack_id=pack_id,
                    pack_version=pack_version,
                    conn=conn,
                )
    return get_rule_pack(
        database_url=database_url, pack_id=pack_id, pack_version=pack_version
    )


def set_case_active_pack(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    pack_id: str,
    pack_version: int,
):
    """Bind published pack matching case route; bump version on change to invalidate approvals."""
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            pack = get_rule_pack(
                database_url=database_url,
                pack_id=pack_id,
                pack_version=pack_version,
                conn=conn,
            )
            if not pack.published:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="only published rule packs can be bound to a case",
                    details=(pack_id, str(pack_version)),
                )
            case_route = case.route.value if case.route else None
            if case_route is None:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="case has no route; cannot bind a rule pack",
                    details=(case_id,),
                )
            if pack.route != case_route:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="rule pack route does not match case route",
                    details=(pack.route, case_route),
                )

            binding = _load_case_pack_binding(
                conn, tenant_id=tenant_id, case_id=case_id
            )
            if binding == (pack_id, pack_version):
                return case

            bump_case_version_in_conn(
                conn=conn,
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
            )
            conn.execute(
                """
                UPDATE cases
                SET active_pack_id = %s,
                    active_pack_version = %s,
                    updated_at = NOW()
                WHERE tenant_id = %s AND id = %s
                """,
                (pack_id, pack_version, tenant_id, case_id),
            )
            return get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
            )


def _load_case_pack_binding(
    conn: psycopg.Connection, *, tenant_id: str, case_id: str
) -> tuple[str, int] | None:
    row = conn.execute(
        """
        SELECT active_pack_id, active_pack_version FROM cases
        WHERE tenant_id = %s AND id = %s
        """,
        (tenant_id, case_id),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return str(row[0]), int(row[1])


def _hint_status_for_requirement(
    *,
    requirement_key: str,
    severity: str,
    classifications: set[str],
    annotation_keys: set[str],
    annotation_pairs: list | None = None,
    pack_version: int = 1,
    route: str | None = None,
) -> tuple[str, str]:
    """Demo heuristic: map isolated evidence / annotations to requirement states.

    Unknown stays unknown — never treated as pass.
    Visitor pack v1: legacy key-presence transfer; no student-funds formula gate.
    Visitor pack v2+ (S3): shared visitor_pack polarity + funds-context formula gate.
    Student / Skilled Worker / Graduate: classification/annotation hints only
    (no Visitor polarity / student-funds gates).
    """
    from visa_agent.rules.visitor_pack import (
        AnnotationPair,
        assess_visitor_signals,
        detect_transfer_triggers,
    )

    pairs: list[AnnotationPair] = list(annotation_pairs or [])
    is_visitor = route in (None, RouteCode.STANDARD_VISITOR.value)

    if is_visitor and pack_version >= VISITOR_PACK_VERSION_S3:
        assessment = assess_visitor_signals(
            classifications=classifications,
            annotations=pairs,
        )
        if severity == "transfer_human":
            if assessment.transfer_polarity == "affirmative":
                return (
                    RequirementStatus.NEEDS_CORRECTION.value,
                    "transfer-to-human trigger affirmative; human review required",
                )
            if assessment.transfer_polarity == "negative":
                return (
                    RequirementStatus.NOT_APPLICABLE.value,
                    "transfer triggers assessed negative / not applicable",
                )
            return (
                RequirementStatus.APPLICABILITY_UNKNOWN.value,
                "transfer-human applicability not yet assessed",
            )
        if requirement_key == "funds_for_stay" and assessment.student_funds_hits:
            return (
                RequirementStatus.NEEDS_CORRECTION.value,
                "Student fixed-funds / CAS formula citation rejected for Visitor",
            )
    elif severity == "transfer_human":
        # Visitor v1 + Student/SW: key presence only.
        if detect_transfer_triggers(annotation_keys):
            return (
                RequirementStatus.NEEDS_CORRECTION.value,
                "transfer-to-human trigger annotated; human review required",
            )
        return (
            RequirementStatus.APPLICABILITY_UNKNOWN.value,
            "transfer-human applicability not yet assessed",
        )

    matched_class = any(
        _CLASSIFICATION_HINTS.get(c) == requirement_key for c in classifications
    )
    matched_ann = requirement_key in annotation_keys or any(
        a.startswith(requirement_key) for a in annotation_keys
    )
    if matched_ann:
        return (
            RequirementStatus.RECEIVED_PENDING_REVIEW.value,
            "human annotation present; pending review",
        )
    if matched_class:
        return (
            RequirementStatus.RECEIVED_PENDING_REVIEW.value,
            "matching evidence classification present; pending review",
        )
    if requirement_key == "sponsor_if_any" and "sponsor" not in classifications:
        return (
            RequirementStatus.APPLICABILITY_UNKNOWN.value,
            "sponsor applicability unknown without evidence or annotation",
        )
    if (
        requirement_key == "tuberculosis_test_if_required"
        and "tb" not in classifications
        and "tuberculosis" not in classifications
        and requirement_key not in annotation_keys
    ):
        return (
            RequirementStatus.APPLICABILITY_UNKNOWN.value,
            "TB applicability unknown without evidence or annotation",
        )
    return (
        RequirementStatus.APPLICABLE_MISSING.value,
        "no supporting evidence or annotation yet",
    )


def _is_blocking(*, severity: str, status: str) -> bool:
    if status in _PASS_STATUSES:
        return False
    if severity == "required":
        return True
    if severity == "transfer_human":
        # Unassessed or escalated transfer triggers always block clear.
        return True
    return False


def evaluate_case_requirements(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> EvaluationResult:
    """Formal evaluate against the case's currently bound published pack only."""
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            binding = _load_case_pack_binding(
                conn, tenant_id=tenant_id, case_id=case_id
            )
            if binding is None:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="case has no active rule pack; bind a published pack first",
                )
            pack_id, pack_version = binding
            pack = get_rule_pack(
                database_url=database_url,
                pack_id=pack_id,
                pack_version=pack_version,
                conn=conn,
            )
            if not pack.published:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="cannot evaluate against an unpublished rule pack",
                )
            case_route = case.route.value if case.route else None
            if case_route is None or pack.route != case_route:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="active rule pack route does not match case route",
                    details=(pack.route, case_route or ""),
                )
            if not pack.requirements:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="active rule pack has no requirements",
                    details=(pack.pack_id, str(pack.pack_version)),
                )

            class_rows = conn.execute(
                """
                SELECT classification FROM case_evidence
                WHERE tenant_id = %s AND case_id = %s
                  AND quarantine_status <> 'rejected'
                """,
                (tenant_id, case_id),
            ).fetchall()
            classifications = {
                (r[0] or "unclassified").strip().lower() for r in class_rows
            }
            remediation_rows = conn.execute(
                """
                SELECT requirement_key, reason_detail
                FROM open_questions
                WHERE tenant_id = %s AND case_id = %s
                  AND status IN ('open', 'waiting_customer', 'escalated_human')
                """,
                (tenant_id, case_id),
            ).fetchall()
            open_remediations = {
                (r[0] or "").strip(): (r[1] or "").strip()
                for r in remediation_rows
                if (r[0] or "").strip()
            }
            ann_rows = conn.execute(
                """
                SELECT a.field_key, a.field_value
                FROM evidence_annotations a
                JOIN case_evidence e
                  ON e.tenant_id = a.tenant_id
                 AND e.id = a.evidence_id
                 AND e.case_id = a.case_id
                WHERE a.tenant_id = %s AND a.case_id = %s
                  AND e.quarantine_status <> 'rejected'
                """,
                (tenant_id, case_id),
            ).fetchall()
            from visa_agent.rules.visitor_pack import AnnotationPair

            annotation_keys = {(r[0] or "").strip().lower() for r in ann_rows}
            annotation_pairs = [
                AnnotationPair(
                    key=(r[0] or "").strip().lower(),
                    value=(r[1] or "").strip(),
                )
                for r in ann_rows
            ]

            # Re-read binding after evidence reads; refuse stale pack writes.
            binding_now = _load_case_pack_binding(
                conn, tenant_id=tenant_id, case_id=case_id
            )
            if binding_now != (pack_id, pack_version):
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message="active rule pack changed during evaluation",
                )
            case_now = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
            )
            if case_now.case_version != case.case_version:
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message="case_version changed during evaluation",
                )

            states: list[CaseRequirementState] = []
            blocking: list[str] = []
            for req in pack.requirements:
                status, reason = _hint_status_for_requirement(
                    requirement_key=req.key,
                    severity=req.severity,
                    classifications=classifications,
                    annotation_keys=annotation_keys,
                    annotation_pairs=annotation_pairs,
                    pack_version=pack.pack_version,
                    route=pack.route,
                )
                if req.key in open_remediations:
                    status = RequirementStatus.NEEDS_CORRECTION.value
                    reason = (
                        open_remediations[req.key]
                        or "open remediation: awaiting clearer re-upload"
                    )
                states.append(
                    CaseRequirementState(
                        requirement_key=req.key,
                        status=status,
                        reason=reason,
                        severity=req.severity,
                        pack_id=pack.pack_id,
                        pack_version=pack.pack_version,
                        case_version=case.case_version,
                    )
                )
                if _is_blocking(severity=req.severity, status=status):
                    blocking.append(req.key)

            for st in states:
                conn.execute(
                    """
                    INSERT INTO case_requirement_states (
                        id, tenant_id, case_id, case_version, pack_id, pack_version,
                        requirement_key, status, reason, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (tenant_id, case_id, pack_id, pack_version, requirement_key)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        reason = EXCLUDED.reason,
                        case_version = EXCLUDED.case_version,
                        updated_at = NOW()
                    WHERE case_requirement_states.case_version
                          <= EXCLUDED.case_version
                    """,
                    (
                        uuid.uuid4().hex,
                        tenant_id,
                        case_id,
                        case.case_version,
                        st.pack_id,
                        st.pack_version,
                        st.requirement_key,
                        st.status,
                        st.reason,
                    ),
                )

            return EvaluationResult(
                pack_id=pack.pack_id,
                pack_version=pack.pack_version,
                case_id=case_id,
                case_version=case.case_version,
                states=tuple(states),
                blocking_keys=tuple(blocking),
                all_required_clear=len(blocking) == 0,
            )


def list_case_requirements(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> list[CaseRequirementState]:
    """Return states for the case's current version + currently bound pack only."""
    with psycopg.connect(database_url) as conn:
        case = get_case(
            database_url=database_url,
            tenant_id=tenant_id,
            case_id=case_id,
            conn=conn,
        )
        binding = _load_case_pack_binding(
            conn, tenant_id=tenant_id, case_id=case_id
        )
        if binding is None:
            return []
        pack_id, pack_version = binding
        rows = conn.execute(
            """
            SELECT s.requirement_key, s.status, s.reason, s.pack_id, s.pack_version,
                   s.case_version, COALESCE(r.severity, 'required')
            FROM case_requirement_states s
            LEFT JOIN rule_requirements r
              ON r.pack_id = s.pack_id
             AND r.pack_version = s.pack_version
             AND r.requirement_key = s.requirement_key
            WHERE s.tenant_id = %s AND s.case_id = %s
              AND s.pack_id = %s AND s.pack_version = %s
              AND s.case_version = %s
            ORDER BY s.requirement_key
            """,
            (tenant_id, case_id, pack_id, pack_version, case.case_version),
        ).fetchall()
    return [
        CaseRequirementState(
            requirement_key=r[0],
            status=r[1],
            reason=r[2] or "",
            pack_id=r[3],
            pack_version=r[4],
            case_version=r[5],
            severity=r[6],
        )
        for r in rows
    ]


def impact_scan(
    *,
    database_url: str,
    tenant_id: str,
    pack_id: str,
    from_version: int,
    to_version: int | None = None,
) -> list[dict[str, object]]:
    """List this tenant's cases still bound to from_version."""
    if to_version is not None:
        get_rule_pack(
            database_url=database_url, pack_id=pack_id, pack_version=to_version
        )
    get_rule_pack(
        database_url=database_url, pack_id=pack_id, pack_version=from_version
    )
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, case_version, active_pack_id, active_pack_version, status
            FROM cases
            WHERE tenant_id = %s
              AND active_pack_id = %s
              AND active_pack_version = %s
            ORDER BY updated_at DESC
            """,
            (tenant_id, pack_id, from_version),
        ).fetchall()
    return [
        {
            "case_id": r[0],
            "case_version": r[1],
            "active_pack_id": r[2],
            "active_pack_version": r[3],
            "status": r[4],
            "target_pack_version": to_version,
        }
        for r in rows
    ]
