"""Case persistence and TransitionContext assembly (case_service)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from visa_agent.domain.enums import CaseStatus, ErrorCode, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.domain.transitions import TransitionContext


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


def list_cases(
    *,
    database_url: str,
    tenant_id: str,
    limit: int = 50,
) -> list[CaseRecord]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            _CASE_SELECT
            + " WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT %s",
            (tenant_id, limit),
        ).fetchall()
    return [_row_to_case(r) for r in rows]


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
            SELECT case_version FROM case_final_reviews
            WHERE tenant_id = %s AND case_id = %s AND case_version = %s
              AND decision = 'approve'
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
                SELECT id, sent_at, receipt_at, case_version
                FROM case_deliveries
                WHERE tenant_id = %s AND case_id = %s AND id = %s
                """,
                (case.tenant_id, case.id, case.current_delivery_id),
            ).fetchone()
            if (
                delivery_ok
                and delivery_ok[1] is not None
                and delivery_ok[2] is not None
                and delivery_ok[3] == case.case_version
            ):
                receipt = (delivery_ok[0],)

        return TransitionContext(
            case_version=case.case_version,
            customer_confirmation_case_version=conf[0] if conf else None,
            final_review_approval_case_version=review[0] if review else None,
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


def record_final_review(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    decision: str,
    note: str = "",
    expected_case_version: int | None = None,
) -> None:
    if decision not in {"approve", "reject"}:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="decision must be approve or reject",
        )
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
            conn.execute(
                """
                INSERT INTO case_final_reviews (
                    id, tenant_id, case_id, case_version, decision, note
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, case_id, case_version) DO UPDATE
                SET decision = EXCLUDED.decision,
                    note = EXCLUDED.note,
                    created_at = NOW()
                """,
                (
                    uuid.uuid4().hex,
                    tenant_id,
                    case_id,
                    case.case_version,
                    decision,
                    note,
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
            case = get_case(
                database_url=database_url,
                tenant_id=tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            _require_version(case, expected_case_version)
            # Stale delivery must not satisfy delivered after a version bump.
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
    return get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)


def record_delivery_receipt(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    delivery_id: str | None = None,
    expected_case_version: int | None = None,
    expected_delivery_id: str | None = None,
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
            updated = conn.execute(
                """
                UPDATE case_deliveries
                SET receipt_at = NOW()
                WHERE tenant_id = %s AND case_id = %s AND id = %s
                  AND case_version = %s
                  AND sent_at IS NOT NULL
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


def attribute_inbound_to_case(
    *,
    database_url: str,
    tenant_id: str,
    email_event_id: str,
    case_id: str,
    force_reattribute: bool = False,
    reason: str = "",
) -> None:
    get_case(database_url=database_url, tenant_id=tenant_id, case_id=case_id)
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
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
                    SET case_id = %s, attribution_status = 'attributed'
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
            conn.execute(
                """
                UPDATE email_events
                SET case_id = %s, attribution_status = 'attributed'
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
