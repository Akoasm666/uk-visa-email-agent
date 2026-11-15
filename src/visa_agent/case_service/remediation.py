"""OpenQuestion / remediation items — open on quality fail, close on clear re-upload."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError

# Stable checklist-adjacent keys for advisor inbox (not rule-pack requirements).
ADVISOR_UNCLASSIFIED = "advisor_unclassified"
ADVISOR_CUSTOMER_QUESTION = "advisor_customer_question"


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    id: str
    case_id: str
    requirement_key: str
    status: str
    severity: str
    reason_code: str
    reason_detail: str
    evidence_id: str | None
    closed_by_evidence_id: str | None
    source_email_event_id: str | None


@dataclass(frozen=True, slots=True)
class AdvisorAlert:
    """One row for the advisor inbox / 提醒页."""

    question_id: str
    case_id: str
    case_title: str
    customer_email: str
    case_status: str
    route: str | None
    requirement_key: str
    reason_code: str
    reason_detail: str
    alert_kind: str  # escalate_question | human_takeover | waiting_flags
    updated_sort: str


def _row(row: tuple) -> OpenQuestion:
    return OpenQuestion(
        id=row[0],
        case_id=row[1],
        requirement_key=row[2] or "",
        status=row[3] or "",
        severity=row[4] or "",
        reason_code=row[5] or "",
        reason_detail=row[6] or "",
        evidence_id=row[7],
        closed_by_evidence_id=row[8],
        source_email_event_id=row[9],
    )


_SELECT = """
    SELECT id, case_id, requirement_key, status, severity, reason_code,
           reason_detail, evidence_id, closed_by_evidence_id, source_email_event_id
    FROM open_questions
"""


def list_open_questions(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    active_only: bool = True,
) -> list[OpenQuestion]:
    sql = _SELECT + " WHERE tenant_id = %s AND case_id = %s"
    params: list = [tenant_id, case_id]
    if active_only:
        sql += " AND status IN ('open', 'waiting_customer', 'escalated_human')"
    sql += " ORDER BY created_at ASC"
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def open_requirement_keys(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> set[str]:
    return {
        q.requirement_key
        for q in list_open_questions(
            database_url=database_url,
            tenant_id=tenant_id,
            case_id=case_id,
            active_only=True,
        )
        if q.requirement_key
    }


def open_remediation(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    requirement_key: str,
    reason_code: str,
    reason_detail: str,
    severity: str = "simple_auto",
    evidence_id: str | None = None,
    source_email_event_id: str | None = None,
    case_version: int = 1,
) -> OpenQuestion:
    """Create or refresh an active remediation for a checklist slot."""
    key = (requirement_key or "").strip()
    if not key:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="requirement_key required for remediation",
        )
    status = (
        "escalated_human" if severity == "complex_human" else "waiting_customer"
    )
    qid = uuid.uuid4().hex
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            existing = conn.execute(
                """
                SELECT id FROM open_questions
                WHERE tenant_id = %s AND case_id = %s AND requirement_key = %s
                  AND status IN ('open', 'waiting_customer', 'escalated_human')
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (tenant_id, case_id, key),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE open_questions
                    SET reason_code = %s,
                        reason_detail = %s,
                        severity = %s,
                        status = %s,
                        evidence_id = COALESCE(%s, evidence_id),
                        source_email_event_id = COALESCE(%s, source_email_event_id),
                        case_version = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        reason_code,
                        reason_detail,
                        severity,
                        status,
                        evidence_id,
                        source_email_event_id,
                        case_version,
                        existing[0],
                    ),
                )
                qid = existing[0]
            else:
                conn.execute(
                    """
                    INSERT INTO open_questions (
                        id, tenant_id, case_id, case_version, requirement_key,
                        status, severity, reason_code, reason_detail,
                        evidence_id, source_email_event_id
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        qid,
                        tenant_id,
                        case_id,
                        case_version,
                        key,
                        status,
                        severity,
                        reason_code,
                        reason_detail,
                        evidence_id,
                        source_email_event_id,
                    ),
                )
        row = conn.execute(
            _SELECT + " WHERE id = %s", (qid,)
        ).fetchone()
    assert row is not None
    return _row(row)


def list_advisor_alerts(
    *,
    database_url: str,
    tenant_id: str,
    limit: int = 40,
) -> list[AdvisorAlert]:
    """Escalated open_questions + cases already under human_takeover."""
    limit = max(1, min(limit, 100))
    alerts: list[AdvisorAlert] = []
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT q.id, q.case_id, c.title, c.customer_email, c.status, c.route,
                   q.requirement_key, q.reason_code, q.reason_detail,
                   COALESCE(q.updated_at, q.created_at)::text
            FROM open_questions q
            JOIN cases c ON c.id = q.case_id AND c.tenant_id = q.tenant_id
            WHERE q.tenant_id = %s
              AND q.status = 'escalated_human'
            ORDER BY q.updated_at DESC NULLS LAST, q.created_at DESC
            LIMIT %s
            """,
            (tenant_id, limit),
        ).fetchall()
        for r in rows:
            alerts.append(
                AdvisorAlert(
                    question_id=r[0],
                    case_id=r[1],
                    case_title=r[2] or "",
                    customer_email=r[3] or "",
                    case_status=r[4] or "",
                    route=r[5],
                    requirement_key=r[6] or "",
                    reason_code=r[7] or "",
                    reason_detail=r[8] or "",
                    alert_kind="escalate_question",
                    updated_sort=r[9] or "",
                )
            )
        takeover = conn.execute(
            """
            SELECT id, title, customer_email, status, route, updated_at::text
            FROM cases
            WHERE tenant_id = %s AND human_takeover = TRUE
              AND status NOT IN ('closed', 'delivered')
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (tenant_id, limit),
        ).fetchall()
        seen_cases = {a.case_id for a in alerts}
        for r in takeover:
            if r[0] in seen_cases:
                continue
            alerts.append(
                AdvisorAlert(
                    question_id="",
                    case_id=r[0],
                    case_title=r[1] or "",
                    customer_email=r[2] or "",
                    case_status=r[3] or "",
                    route=r[4],
                    requirement_key="",
                    reason_code="human_takeover",
                    reason_detail="案件已标记为人工接管",
                    alert_kind="human_takeover",
                    updated_sort=r[5] or "",
                )
            )
    alerts.sort(key=lambda a: a.updated_sort, reverse=True)
    return alerts[:limit]


def acknowledge_advisor_question(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    question_id: str,
    actor: str = "advisor",
) -> OpenQuestion | None:
    """Advisor marks an escalated item handled (closes the open_question)."""
    actor = (actor or "advisor").strip() or "advisor"
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            row = conn.execute(
                """
                SELECT id, requirement_key FROM open_questions
                WHERE tenant_id = %s AND case_id = %s AND id = %s
                  AND status = 'escalated_human'
                FOR UPDATE
                """,
                (tenant_id, case_id, question_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'closed',
                    reason_detail = CASE
                      WHEN reason_detail = '' THEN %s
                      ELSE reason_detail || ' | ' || %s
                    END,
                    closed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    f"acknowledged_by:{actor}",
                    f"acknowledged_by:{actor}",
                    row[0],
                ),
            )
        out = conn.execute(_SELECT + " WHERE id = %s", (row[0],)).fetchone()
    return _row(out) if out else None


def close_remediation(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    requirement_key: str,
    closed_by_evidence_id: str,
) -> OpenQuestion | None:
    """Close the active remediation for a slot after a clear re-upload."""
    key = (requirement_key or "").strip()
    if not key or not closed_by_evidence_id:
        return None
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            row = conn.execute(
                """
                SELECT id FROM open_questions
                WHERE tenant_id = %s AND case_id = %s AND requirement_key = %s
                  AND status IN ('open', 'waiting_customer', 'escalated_human')
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (tenant_id, case_id, key),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'closed',
                    closed_by_evidence_id = %s,
                    closed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (closed_by_evidence_id, row[0]),
            )
        out = conn.execute(_SELECT + " WHERE id = %s", (row[0],)).fetchone()
    return _row(out) if out else None
