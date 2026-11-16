"""Case persistence and TransitionContext assembly (case_service)."""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import dataclass

import psycopg

from visa_agent.domain.enums import CaseStatus, ErrorCode, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.domain.transitions import TransitionContext


FINISHED_CASE_STATUSES = frozenset({CaseStatus.CLOSED, CaseStatus.DELIVERED})


@dataclass(frozen=True, slots=True)
class CaseRecord:
    id: str
    tenant_id: str
    case_version: int
    status: CaseStatus
    route: RouteCode | None
    resume_status: CaseStatus | None
    customer_email: str | None
    title: str
    waiting_customer: bool
    waiting_third_party: bool
    human_takeover: bool
    system_blocked: bool
    paused: bool
    cancelled: bool
    current_delivery_id: str | None
    closure_seq: int = 0


@dataclass(frozen=True, slots=True)
class CustomerConfirmationRecord:
    id: str
    case_id: str
    case_version: int
    note: str
    created_at: str
    effective: bool


@dataclass(frozen=True, slots=True)
class FinalReviewRecord:
    id: str
    case_id: str
    case_version: int
    decision: str
    note: str
    created_at: str
    decision_seq: int
    effective: bool
    superseded: bool


@dataclass(frozen=True, slots=True)
class ReviewBoardSnapshot:
    """Current-version gate status + history with stale approvals marked."""

    case_id: str
    case_version: int
    customer_confirmation_effective: bool
    final_approval_effective: bool
    # Latest decision on the snapshot version: "approve" | "reject" | None.
    current_final_decision: str | None
    confirmations: tuple[CustomerConfirmationRecord, ...]
    reviews: tuple[FinalReviewRecord, ...]


def _parse_status(value: str | None) -> CaseStatus | None:
    if value is None:
        return None
    return CaseStatus(value)


def _parse_route(value: str | None) -> RouteCode | None:
    if value is None or value == "":
        return None
    return RouteCode(value)


def _row_to_case(row: tuple) -> CaseRecord:
    return CaseRecord(
        id=row[0],
        tenant_id=row[1],
        case_version=row[2],
        status=CaseStatus(row[3]),
        route=_parse_route(row[4]),
        resume_status=_parse_status(row[5]),
        customer_email=row[6],
        title=row[7] or "",
        waiting_customer=bool(row[8]),
        waiting_third_party=bool(row[9]),
        human_takeover=bool(row[10]),
        system_blocked=bool(row[11]),
        paused=bool(row[12]),
        cancelled=bool(row[13]),
        current_delivery_id=row[14],
        closure_seq=int(row[15] or 0),
    )


_CASE_SELECT = """
    SELECT id, tenant_id, case_version, status, route, resume_status,
           customer_email, title,
           waiting_customer, waiting_third_party, human_takeover,
           system_blocked, paused, cancelled, current_delivery_id,
           closure_seq
    FROM cases
"""


def _require_version(
    case: CaseRecord,
    expected_case_version: int | None,
    *,
    required: bool = False,
) -> None:
    if expected_case_version is None:
        if required:
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="expected_case_version is required",
            )
        return
    if case.case_version != expected_case_version:
        raise DomainError(
            code=ErrorCode.CONFLICT_VERSION,
            message="case_version changed since page was loaded",
            details=(
                f"expected={expected_case_version}",
                f"actual={case.case_version}",
            ),
        )


def create_case(
    *,
    database_url: str,
    tenant_id: str,
    title: str = "",
    customer_email: str | None = None,
    route: RouteCode | None = RouteCode.STANDARD_VISITOR,
    status: CaseStatus = CaseStatus.LEAD,
) -> CaseRecord:
    case_id = uuid.uuid4().hex
    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO cases (
                id, tenant_id, case_version, status, route,
                customer_email, title
            ) VALUES (%s, %s, 1, %s, %s, %s, %s)
            """,
            (
                case_id,
                tenant_id,
                status.value,
                route.value if route else None,
                customer_email,
                title,
            ),
        )
        conn.commit()
    return get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)


def get_case(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    conn: psycopg.Connection | None = None,
    for_update: bool = False,
) -> CaseRecord:
    sql = _CASE_SELECT + " WHERE tenant_id = %s AND id = %s"
    if for_update:
        sql += " FOR UPDATE"

    def _fetch(c: psycopg.Connection) -> CaseRecord:
        row = c.execute(sql, (tenant_id, case_id)).fetchone()
        if row is None:
            raise DomainError(
                code=ErrorCode.NOT_FOUND,
                message="case not found",
                details=(case_id,),
            )
        return _row_to_case(row)

    if conn is not None:
        return _fetch(conn)
    with psycopg.connect(database_url) as owned:
        return _fetch(owned)


def _case_list_filter(
    *,
    tenant_id: str,
    q: str | None = None,
    status: str | None = None,
    route: str | None = None,
    waiting: str | None = None,
    active_only: bool = False,
) -> tuple[str, list]:
    clauses = ["tenant_id = %s"]
    params: list = [tenant_id]
    if active_only:
        clauses.append("status NOT IN ('closed', 'delivered')")
    needle = (q or "").strip()
    if needle:
        clauses.append(
            "("
            "title ILIKE %s OR customer_email ILIKE %s OR id ILIKE %s"
            ")"
        )
        like = f"%{needle}%"
        params.extend([like, like, like])
    st = (status or "").strip()
    if st:
        clauses.append("status = %s")
        params.append(st)
    rt = (route or "").strip()
    if rt:
        clauses.append("route = %s")
        params.append(rt)
    wait = (waiting or "").strip().lower()
    if wait == "customer":
        clauses.append("waiting_customer = TRUE")
    elif wait == "third_party":
        clauses.append("waiting_third_party = TRUE")
    elif wait == "takeover":
        clauses.append("human_takeover = TRUE")
    elif wait == "any_flag":
        clauses.append(
            "(waiting_customer OR waiting_third_party OR human_takeover "
            "OR system_blocked OR paused)"
        )
    return " WHERE " + " AND ".join(clauses), params


def list_cases(
    *,
    database_url: str,
    tenant_id: str,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    status: str | None = None,
    route: str | None = None,
    waiting: str | None = None,
    active_only: bool = False,
) -> list[CaseRecord]:
    """List cases with optional search, status, route and waiting filters."""
    limit = max(1, min(int(limit or 50), 200))
    where, params = _case_list_filter(
        tenant_id=tenant_id, q=q, status=status, route=route, waiting=waiting, active_only=active_only,
    )
    sql = _CASE_SELECT + where + " ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s"
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    return [_row_to_case(r) for r in rows]


_CASE_PAGE_STATUS_GROUPS = {
    "lead": ["lead", "intake_authorization", "route_scope_confirmed"],
    "customer_confirmation": ["draft_preparation", "customer_confirmation"],
    "ready_to_deliver": ["ready_to_deliver", "sent_awaiting_ack"],
}

_CURRENT_CASE_TERMINATION_EXISTS = """
    EXISTS (
        SELECT 1 FROM case_terminations t
        WHERE t.tenant_id = cases.tenant_id AND t.case_id = cases.id
          AND t.closure_seq = cases.closure_seq
    )
"""


def list_case_page(
    *,
    database_url: str,
    tenant_id: str,
    page: int,
    page_size: int,
    q: str | None = None,
    status: str | None = None,
    route: str | None = None,
    waiting: str | None = None,
) -> tuple[list[CaseRecord], int, int]:
    """Return the requested workbench page and its filtered page count."""
    where, params = _case_list_filter(tenant_id=tenant_id, q=q, route=route, waiting=waiting)
    stage = (status or "").strip()
    if stage == "terminated":
        where += f" AND status = 'closed' AND {_CURRENT_CASE_TERMINATION_EXISTS}"
    elif stage == "closed":
        where += f"""
            AND (status = 'delivered' OR (status = 'closed' AND NOT {_CURRENT_CASE_TERMINATION_EXISTS}))
        """
    elif stage:
        where += " AND status = ANY(%s)"
        params.append(_CASE_PAGE_STATUS_GROUPS.get(stage, [stage]))
    with psycopg.connect(database_url) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        count, = conn.execute("SELECT COUNT(*) FROM cases" + where, params).fetchone()
        total_pages = (count + page_size - 1) // page_size
        page = min(page, max(1, total_pages))
        rows = conn.execute(
            _CASE_SELECT + where + " ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return [_row_to_case(row) for row in rows], page, total_pages


def build_transition_context(
    *,
    database_url: str | None = None,
    case: CaseRecord,
    conn: psycopg.Connection | None = None,
) -> TransitionContext:
    """Assemble TransitionContext from authoritative case-bound records."""

    def _load(c: psycopg.Connection) -> TransitionContext:
        conf = c.execute(
            """
            SELECT case_version FROM case_customer_confirmations
            WHERE tenant_id = %s AND case_id = %s AND case_version = %s
            """,
            (case.tenant_id, case.id, case.case_version),
        ).fetchone()
        review = c.execute(
            """
            SELECT case_version, decision FROM case_final_reviews
            WHERE tenant_id = %s AND case_id = %s AND case_version = %s
            ORDER BY decision_seq DESC
            LIMIT 1
            """,
            (case.tenant_id, case.id, case.case_version),
        ).fetchone()
        # One-shot grant bound to this closed/delivered episode + version.
        reopen = c.execute(
            """
            SELECT id FROM case_reopen_grants
            WHERE tenant_id = %s AND case_id = %s
              AND authorized = TRUE
              AND consumed_at IS NULL
              AND bound_from_status = %s
              AND bound_case_version = %s
              AND bound_closure_seq = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                case.tenant_id,
                case.id,
                case.status.value,
                case.case_version,
                case.closure_seq,
            ),
        ).fetchone()
        receipt = None
        delivery_ok = None
        if case.current_delivery_id:
            delivery_ok = c.execute(
                """
                SELECT d.id, d.sent_at, d.receipt_at, d.case_version,
                       d.package_id, d.outbox_id, p.package_kind
                FROM case_deliveries d
                LEFT JOIN delivery_packages p
                  ON p.id = d.package_id AND p.tenant_id = d.tenant_id
                WHERE d.tenant_id = %s AND d.case_id = %s AND d.id = %s
                """,
                (case.tenant_id, case.id, case.current_delivery_id),
            ).fetchone()
            if (
                delivery_ok
                and delivery_ok[1] is not None
                and delivery_ok[2] is not None
                and delivery_ok[3] == case.case_version
                and delivery_ok[4]
                and delivery_ok[5]
                and (delivery_ok[6] or "") == "final_package"
            ):
                receipt = (delivery_ok[0],)

        approval_version = None
        if review is not None and review[1] == "approve":
            approval_version = review[0]
        return TransitionContext(
            case_version=case.case_version,
            customer_confirmation_case_version=conf[0] if conf else None,
            final_review_approval_case_version=approval_version,
            reopen_authorized=reopen is not None,
            resume_status=case.resume_status,
            delivery_id=case.current_delivery_id,
            receipt_for_delivery_id=receipt[0] if receipt else None,
        )

    if conn is not None:
        return _load(conn)
    assert database_url is not None
    with psycopg.connect(database_url) as owned:
        return _load(owned)


def set_case_route(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    route: RouteCode,
    expected_case_version: int | None = None,
) -> CaseRecord:
    """Bind or change case route; clear pack binding and bump version on change.

    Route changes invalidate prior confirmations/approvals and any active pack
    so Student/Worker packs cannot silently stay bound to a Visitor case.
    """
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            if case.route == route:
                return case
            # Late route changes after collect risk stale evidence checklists.
            if case.status not in {
                CaseStatus.LEAD,
                CaseStatus.INTAKE_AUTHORIZATION,
                CaseStatus.ROUTE_SCOPE_CONFIRMED,
            }:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message=(
                        "route can only be set while lead / intake / "
                        "route_scope_confirmed"
                    ),
                    details=(case.status.value,),
                )
            bumped = bump_case_version_in_conn(
                conn=conn,
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_case_version=case.case_version,
                required=True,
            )
            conn.execute(
                """
                UPDATE cases
                SET route = %s,
                    active_pack_id = NULL,
                    active_pack_version = NULL,
                    updated_at = NOW()
                WHERE tenant_id = %s AND id = %s AND case_version = %s
                """,
                (route.value, tenant_id, case_id, bumped.case_version),
            )
    return get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)


def set_takeover(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    active: bool,
    expected_case_version: int | None = None,
) -> CaseRecord:
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version)
            conn.execute(
                """
                UPDATE cases
                SET human_takeover = %s, updated_at = NOW()
                WHERE tenant_id = %s AND id = %s AND case_version = %s
                """,
                (active, tenant_id, case_id, case.case_version),
            )
    return get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)


def record_customer_confirmation(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    note: str = "",
    expected_case_version: int | None = None,
    conn: psycopg.Connection | None = None,
) -> None:
    with (nullcontext(conn) if conn is not None else psycopg.connect(database_url)) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            conn.execute(
                """
                INSERT INTO case_customer_confirmations (
                    id, tenant_id, case_id, case_version, note
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, case_id, case_version) DO UPDATE
                SET note = EXCLUDED.note, created_at = NOW()
                """,
                (uuid.uuid4().hex, tenant_id, case_id, case.case_version, note),
            )


def _confirmations_from_rows(
    rows: list[tuple], *, case_version: int
) -> list[CustomerConfirmationRecord]:
    return [
        CustomerConfirmationRecord(
            id=r[0],
            case_id=r[1],
            case_version=r[2],
            note=r[3] or "",
            created_at=r[4].isoformat() if r[4] is not None else "",
            effective=int(r[2]) == case_version,
        )
        for r in rows
    ]


def _reviews_from_rows(
    rows: list[tuple], *, case_version: int
) -> list[FinalReviewRecord]:
    """Mark only the highest decision_seq for the snapshot version as effective.

    ``rows`` must already be ordered by decision_seq DESC.
    Row shape: id, case_id, case_version, decision, note, created_at, decision_seq
    """
    latest_current_id: str | None = None
    for r in rows:
        if int(r[2]) == case_version:
            latest_current_id = r[0]
            break
    out: list[FinalReviewRecord] = []
    for r in rows:
        ver = int(r[2])
        is_current_ver = ver == case_version
        is_latest = latest_current_id is not None and r[0] == latest_current_id
        out.append(
            FinalReviewRecord(
                id=r[0],
                case_id=r[1],
                case_version=ver,
                decision=r[3],
                note=r[4] or "",
                created_at=r[5].isoformat() if r[5] is not None else "",
                decision_seq=int(r[6] or 0),
                effective=is_current_ver and is_latest,
                superseded=is_current_ver and not is_latest,
            )
        )
    return out


def _load_review_histories(
    conn: psycopg.Connection,
    *,
    tenant_id: str,
    case_id: str,
    case_version: int,
) -> tuple[list[CustomerConfirmationRecord], list[FinalReviewRecord]]:
    """Read histories visible at/before the snapshot case_version."""
    conf_rows = conn.execute(
        """
        SELECT id, case_id, case_version, note, created_at
        FROM case_customer_confirmations
        WHERE tenant_id = %s AND case_id = %s
          AND case_version <= %s
        ORDER BY case_version DESC, created_at DESC
        """,
        (tenant_id, case_id, case_version),
    ).fetchall()
    review_rows = conn.execute(
        """
        SELECT id, case_id, case_version, decision, note, created_at, decision_seq
        FROM case_final_reviews
        WHERE tenant_id = %s AND case_id = %s
          AND case_version <= %s
        ORDER BY decision_seq DESC, created_at DESC
        """,
        (tenant_id, case_id, case_version),
    ).fetchall()
    return (
        _confirmations_from_rows(list(conf_rows), case_version=case_version),
        _reviews_from_rows(list(review_rows), case_version=case_version),
    )


def _repeatable_read_conn(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url)
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn


def list_customer_confirmations(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> list[CustomerConfirmationRecord]:
    with _repeatable_read_conn(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
            )
            confs, _reviews = _load_review_histories(
                conn,
                tenant_id=tenant_id,
                case_id=case_id,
                case_version=case.case_version,
            )
    return confs


def list_final_reviews(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> list[FinalReviewRecord]:
    with _repeatable_read_conn(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
            )
            _confs, reviews = _load_review_histories(
                conn,
                tenant_id=tenant_id,
                case_id=case_id,
                case_version=case.case_version,
            )
    return reviews


def get_case_review_page(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> tuple[CaseRecord, ReviewBoardSnapshot]:
    """Case header + histories from one REPEATABLE READ snapshot."""
    with _repeatable_read_conn(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
            )
            confirmations, reviews = _load_review_histories(
                conn,
                tenant_id=tenant_id,
                case_id=case_id,
                case_version=case.case_version,
            )
    conf_ok = any(c.effective for c in confirmations)
    current_decision: str | None = None
    for r in reviews:
        if r.effective:
            current_decision = r.decision
            break
    board = ReviewBoardSnapshot(
        case_id=case.id,
        case_version=case.case_version,
        customer_confirmation_effective=conf_ok,
        final_approval_effective=current_decision == "approve",
        current_final_decision=current_decision,
        confirmations=tuple(confirmations),
        reviews=tuple(reviews),
    )
    return case, board


def get_review_board(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> ReviewBoardSnapshot:
    """Load confirmation/review history against one case version snapshot."""
    _case, board = get_case_review_page(
        database_url=database_url, tenant_id=tenant_id, case_id=case_id
    )
    return board


def record_final_review(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    decision: str,
    note: str = "",
    expected_case_version: int | None = None,
    package_id: str | None = None,
    recipients: list[str] | None = None,
    reviewer_id: str | None = None,
    conn: psycopg.Connection | None = None,
) -> None:
    if decision not in {"approve", "reject"}:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="decision must be approve or reject",
        )
    import json

    pkg_id = (package_id or "").strip() or None
    actor = (reviewer_id or "").strip() or None
    recip_json = None
    if recipients is not None:
        recip_json = json.dumps(
            sorted({r.strip().lower() for r in recipients if r.strip()})
        )
    with (nullcontext(conn) if conn is not None else psycopg.connect(database_url)) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            if pkg_id is not None:
                if not actor:
                    raise DomainError(
                        code=ErrorCode.UNAUTHORIZED,
                        message="reviewer_id required for package-bound review",
                    )
                owned = conn.execute(
                    """
                    SELECT case_version FROM delivery_packages
                    WHERE tenant_id = %s AND case_id = %s AND id = %s
                    """,
                    (tenant_id, case_id, pkg_id),
                ).fetchone()
                if owned is None:
                    raise DomainError(
                        code=ErrorCode.NOT_FOUND,
                        message="delivery package not found on case",
                        details=(pkg_id,),
                    )
                if int(owned[0]) != case.case_version:
                    raise DomainError(
                        code=ErrorCode.CONFLICT_VERSION,
                        message="package was assembled for a different case_version",
                        details=(str(owned[0]), str(case.case_version)),
                    )
                if decision == "approve":
                    approved: list[str] = []
                    if recip_json:
                        approved = list(json.loads(recip_json))
                    if not approved:
                        raise DomainError(
                            code=ErrorCode.VALIDATION_ERROR,
                            message="recipients required for final-package approval",
                        )
            # Monotonic seq under the case lock — not PostgreSQL txn-start NOW().
            seq_row = conn.execute(
                """
                UPDATE cases
                SET final_review_seq = final_review_seq + 1,
                    updated_at = NOW()
                WHERE tenant_id = %s AND id = %s
                RETURNING final_review_seq
                """,
                (tenant_id, case_id),
            ).fetchone()
            assert seq_row is not None
            decision_seq = int(seq_row[0])
            conn.execute(
                """
                INSERT INTO case_final_reviews (
                    id, tenant_id, case_id, case_version, decision, note,
                    decision_seq, package_id, recipients, reviewer_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    uuid.uuid4().hex,
                    tenant_id,
                    case_id,
                    case.case_version,
                    decision,
                    note,
                    decision_seq,
                    pkg_id,
                    recip_json,
                    actor,
                ),
            )


def record_reopen_grant(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    note: str = "",
    expected_case_version: int | None = None,
    expected_closure_seq: int | None = None,
) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            if expected_closure_seq is None:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="expected_closure_seq is required",
                )
            if expected_closure_seq != case.closure_seq:
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message="closure_seq changed since page was loaded",
                    details=(
                        f"expected={expected_closure_seq}",
                        f"actual={case.closure_seq}",
                    ),
                )
            if case.status not in {CaseStatus.CLOSED, CaseStatus.DELIVERED}:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="reopen grant only allowed while closed or delivered",
                    details=(case.status.value,),
                )
            if case.closure_seq < 1:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="closure episode missing; cannot grant reopen",
                )
            # One active grant per closure episode — re-submit refreshes version binding.
            existing = conn.execute(
                """
                SELECT id FROM case_reopen_grants
                WHERE tenant_id = %s AND case_id = %s
                  AND bound_closure_seq = %s
                  AND authorized = TRUE
                  AND consumed_at IS NULL
                """,
                (tenant_id, case_id, case.closure_seq),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE case_reopen_grants
                    SET note = %s,
                        bound_from_status = %s,
                        bound_case_version = %s,
                        created_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        note,
                        case.status.value,
                        case.case_version,
                        existing[0],
                    ),
                )
                return
            conn.execute(
                """
                INSERT INTO case_reopen_grants (
                    id, tenant_id, case_id, authorized, note,
                    bound_from_status, bound_case_version, bound_closure_seq
                ) VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4().hex,
                    tenant_id,
                    case_id,
                    note,
                    case.status.value,
                    case.case_version,
                    case.closure_seq,
                ),
            )


def consume_reopen_grant(
    *,
    conn: psycopg.Connection,
    case: CaseRecord,
) -> None:
    # Consume every unused grant for this closure episode (defensive).
    updated = conn.execute(
        """
        UPDATE case_reopen_grants
        SET consumed_at = NOW()
        WHERE tenant_id = %s AND case_id = %s
          AND authorized = TRUE
          AND consumed_at IS NULL
          AND bound_from_status = %s
          AND bound_case_version = %s
          AND bound_closure_seq = %s
        RETURNING id
        """,
        (
            case.tenant_id,
            case.id,
            case.status.value,
            case.case_version,
            case.closure_seq,
        ),
    ).fetchall()
    if not updated:
        raise DomainError(
            code=ErrorCode.REOPEN_UNAUTHORIZED,
            message="no unused reopen grant for this closed/delivered episode",
        )


def bump_case_version_in_conn(
    *,
    conn: psycopg.Connection,
    database_url: str,
    tenant_id: str,
    case_id: str,
    expected_case_version: int | None = None,
    required: bool = False,
) -> CaseRecord:
    """Bump case_version inside an existing transaction (caller holds conn)."""
    case = get_case(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        conn=conn,
        for_update=True,
    )
    _require_version(case, expected_case_version, required=required)
    conn.execute(
        """
        UPDATE cases
        SET case_version = case_version + 1,
            current_delivery_id = NULL,
            updated_at = NOW()
        WHERE tenant_id = %s AND id = %s AND case_version = %s
        """,
        (tenant_id, case_id, case.case_version),
    )
    return get_case(
        database_url=database_url,
        tenant_id=tenant_id,
        case_id=case_id,
        conn=conn,
    )


def bump_case_version(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    expected_case_version: int | None = None,
) -> CaseRecord:
    """Invalidate prior confirmations/approvals by advancing case_version."""
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            bump_case_version_in_conn(
                conn=conn,
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                expected_case_version=expected_case_version,
            )
    return get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)


def record_delivery_receipt(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    delivery_id: str | None = None,
    expected_case_version: int | None = None,
    expected_delivery_id: str | None = None,
    conn: psycopg.Connection | None = None,
) -> None:
    with (nullcontext(conn) if conn is not None else psycopg.connect(database_url)) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version)
            # Page must submit the delivery it displayed; must still be current.
            page_delivery = expected_delivery_id or delivery_id
            if not page_delivery:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="expected_delivery_id is required",
                )
            if not case.current_delivery_id:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="no current delivery to acknowledge",
                )
            if page_delivery != case.current_delivery_id:
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message="delivery_id is no longer the current delivery",
                    details=(
                        f"expected={page_delivery}",
                        f"current={case.current_delivery_id}",
                    ),
                )
            bound = conn.execute(
                """
                SELECT d.id, d.package_id, d.outbox_id, p.package_kind
                FROM case_deliveries d
                LEFT JOIN delivery_packages p
                  ON p.id = d.package_id AND p.tenant_id = d.tenant_id
                WHERE d.tenant_id = %s AND d.case_id = %s AND d.id = %s
                  AND d.case_version = %s
                  AND d.sent_at IS NOT NULL
                """,
                (tenant_id, case_id, page_delivery, case.case_version),
            ).fetchone()
            if (
                bound is None
                or not bound[1]
                or not bound[2]
                or (bound[3] or "") != "final_package"
            ):
                raise DomainError(
                    code=ErrorCode.DELIVERY_GATE_FAILED,
                    message=(
                        "receipt must bind a final_package delivery "
                        "with outbox linkage"
                    ),
                    details=(page_delivery,),
                )
            updated = conn.execute(
                """
                UPDATE case_deliveries
                SET receipt_at = NOW()
                WHERE tenant_id = %s AND case_id = %s AND id = %s
                  AND case_version = %s
                  AND sent_at IS NOT NULL
                  AND package_id IS NOT NULL
                  AND outbox_id IS NOT NULL
                RETURNING id
                """,
                (tenant_id, case_id, page_delivery, case.case_version),
            ).fetchone()
            if updated is None:
                raise DomainError(
                    code=ErrorCode.DELIVERY_RECEIPT_REQUIRED,
                    message=(
                        "delivery missing, not sent for current version, "
                        "or already unbound"
                    ),
                    details=(page_delivery,),
                )


_INBOUND_CONCLUSIONS = frozenset(
    {"acknowledged", "stop_delivery", "material_change"}
)
_INBOUND_CONCLUSIONS_INVALIDATE = frozenset({"stop_delivery", "material_change"})


def mark_inbound_processed(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    email_event_id: str,
    conclusion: str,
    processed_by: str,
    expected_case_version: int | None = None,
    conn: psycopg.Connection | None = None,
) -> CaseRecord:
    """Explicit authorized action that clears the unprocessed-inbound send gate.

    ``stop_delivery`` / ``material_change`` bump ``case_version`` so prior package
    approvals and confirmations no longer authorize send.
    """
    conclusion_key = (conclusion or "").strip().lower()
    actor = (processed_by or "").strip()
    if conclusion_key not in _INBOUND_CONCLUSIONS:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="invalid inbound processing conclusion",
            details=(conclusion_key,),
        )
    if not actor:
        raise DomainError(
            code=ErrorCode.UNAUTHORIZED,
            message="processed_by (actor) required to mark inbound processed",
        )
    with (nullcontext(conn) if conn is not None else psycopg.connect(database_url)) as conn:
        with conn.transaction():
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version, required=True)
            row = conn.execute(
                """
                SELECT id, attribution_status, processing_status
                FROM email_events
                WHERE tenant_id = %s AND id = %s
                  AND direction = 'inbound'
                  AND case_id = %s
                FOR UPDATE
                """,
                (tenant_id, email_event_id, case_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    code=ErrorCode.NOT_FOUND,
                    message="inbound email not found on case",
                    details=(email_event_id,),
                )
            if (row[1] or "") != "attributed":
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="inbound must be attributed before it can be marked processed",
                    details=(row[1] or "",),
                )
            conn.execute(
                """
                UPDATE email_events
                SET processing_status = 'processed',
                    processing_conclusion = %s,
                    processed_by = %s,
                    processed_at = NOW()
                WHERE tenant_id = %s AND id = %s
                """,
                (conclusion_key, actor, tenant_id, email_event_id),
            )
            if conclusion_key in _INBOUND_CONCLUSIONS_INVALIDATE:
                bump_case_version_in_conn(
                    conn=conn,
                    database_url=database_url,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    expected_case_version=case.case_version,
                    required=True,
                )
            if conclusion_key == "stop_delivery":
                conn.execute(
                    "UPDATE cases SET human_takeover = TRUE WHERE tenant_id = %s AND id = %s",
                    (tenant_id, case_id),
                )
    return get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)


def attribute_inbound_to_case(
    *,
    database_url: str,
    tenant_id: str,
    email_event_id: str,
    case_id: str,
    force_reattribute: bool = False,
    reason: str = "",
    active_case_only: bool = False,
) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            # Serialize with send claim so attribution cannot slip between gate and claim.
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            if active_case_only and case.status in FINISHED_CASE_STATUSES:
                raise DomainError(ErrorCode.VALIDATION_ERROR, "案件已结束，仅可查看")
            busy = conn.execute(
                """
                SELECT 1 FROM outbox_messages
                WHERE tenant_id = %s AND case_id = %s AND status = 'processing'
                LIMIT 1
                """,
                (tenant_id, case_id),
            ).fetchone()
            if busy is not None:
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message=(
                        "case has a send in processing; "
                        "finish or fail it before attributing inbound"
                    ),
                )
            row = conn.execute(
                """
                SELECT id, case_id, attribution_status
                FROM email_events
                WHERE tenant_id = %s AND id = %s AND direction = 'inbound'
                FOR UPDATE
                """,
                (tenant_id, email_event_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    code=ErrorCode.NOT_FOUND,
                    message="inbound email event not found",
                    details=(email_event_id,),
                )
            existing_case_id = row[1]
            if existing_case_id is None or existing_case_id == "":
                conn.execute(
                    """
                    UPDATE email_events
                    SET case_id = %s,
                        attribution_status = 'attributed',
                        processing_status = 'unprocessed'
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (case_id, tenant_id, email_event_id),
                )
                conn.execute(
                    """
                    INSERT INTO email_attribution_audit (
                        id, tenant_id, email_event_id, from_case_id, to_case_id, reason
                    ) VALUES (%s, %s, %s, NULL, %s, %s)
                    """,
                    (
                        uuid.uuid4().hex,
                        tenant_id,
                        email_event_id,
                        case_id,
                        reason or "initial_attribution",
                    ),
                )
                return

            if existing_case_id == case_id:
                return

            if not force_reattribute:
                raise DomainError(
                    code=ErrorCode.CONFLICT_VERSION,
                    message=(
                        "email already attributed to another case; "
                        "use force_reattribute with reason to reassign"
                    ),
                    details=(f"from={existing_case_id}", f"to={case_id}"),
                )
            if not reason.strip():
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="reason required for re-attribution",
                )
            # Evidence still pointing at this email would leave the old case
            # able to deliver under stale approvals — block until cleared.
            linked = conn.execute(
                """
                SELECT id, case_id FROM case_evidence
                WHERE tenant_id = %s AND source_email_event_id = %s
                  AND deleted_at IS NULL
                ORDER BY created_at
                """,
                (tenant_id, email_event_id),
            ).fetchall()
            if linked:
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message=(
                        "email has linked evidence; handle those materials "
                        "and their approvals before re-attribution"
                    ),
                    details=tuple(f"{row[1]}:{row[0]}" for row in linked),
                )
            conn.execute(
                """
                UPDATE email_events
                SET case_id = %s,
                    attribution_status = 'attributed',
                    processing_status = 'unprocessed'
                WHERE tenant_id = %s AND id = %s
                """,
                (case_id, tenant_id, email_event_id),
            )
            conn.execute(
                """
                INSERT INTO email_attribution_audit (
                    id, tenant_id, email_event_id, from_case_id, to_case_id, reason
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4().hex,
                    tenant_id,
                    email_event_id,
                    existing_case_id,
                    case_id,
                    reason.strip(),
                ),
            )


_CASE_RETURNING = """
    RETURNING id, tenant_id, case_version, status, route, resume_status,
              customer_email, title,
              waiting_customer, waiting_third_party, human_takeover,
              system_blocked, paused, cancelled, current_delivery_id,
              closure_seq
"""


def persist_case_status(
    *,
    conn: psycopg.Connection,
    tenant_id: str,
    case_id: str,
    expected_status: CaseStatus,
    expected_case_version: int,
    status: CaseStatus,
    resume_status: CaseStatus | None,
    current_delivery_id: str | None | object = ...,
    bump_closure_seq: bool = False,
) -> CaseRecord:
    """Update case row only if status+version still match the validated snapshot."""
    closure_expr = "closure_seq + 1" if bump_closure_seq else "closure_seq"
    if current_delivery_id is ...:
        updated = conn.execute(
            f"""
            UPDATE cases
            SET status = %s,
                resume_status = %s,
                closure_seq = {closure_expr},
                updated_at = NOW()
            WHERE tenant_id = %s AND id = %s
              AND status = %s AND case_version = %s
            {_CASE_RETURNING}
            """,
            (
                status.value,
                resume_status.value if resume_status else None,
                tenant_id,
                case_id,
                expected_status.value,
                expected_case_version,
            ),
        ).fetchone()
    else:
        updated = conn.execute(
            f"""
            UPDATE cases
            SET status = %s,
                resume_status = %s,
                current_delivery_id = %s,
                closure_seq = {closure_expr},
                updated_at = NOW()
            WHERE tenant_id = %s AND id = %s
              AND status = %s AND case_version = %s
            {_CASE_RETURNING}
            """,
            (
                status.value,
                resume_status.value if resume_status else None,
                current_delivery_id,
                tenant_id,
                case_id,
                expected_status.value,
                expected_case_version,
            ),
        ).fetchone()
    if updated is None:
        raise DomainError(
            code=ErrorCode.CONFLICT_VERSION,
            message="case status or version changed during transition",
            details=(case_id,),
        )
    return _row_to_case(updated)
