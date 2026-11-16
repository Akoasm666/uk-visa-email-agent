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


@dataclass(frozen=True, slots=True)
class UnassignedMail:
    id: str
    sender: str
    subject: str
    body_text: str = ""


@dataclass(frozen=True, slots=True)
class AdvisorInbox:
    alerts: list[AdvisorAlert]
    unassigned_mail: list[UnassignedMail]
    pending_count: int


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


_INBOX_ITEMS = """
WITH inbox_items (
    kind, item_id, case_id, title, email, case_status, route, requirement_key,
    reason_code, reason_detail, updated_at
) AS (
    SELECT 'question', q.id, c.id, c.title, c.customer_email, c.status, c.route,
           q.requirement_key, q.reason_code, q.reason_detail, q.updated_at
    FROM open_questions q
    JOIN cases c ON c.id = q.case_id AND c.tenant_id = q.tenant_id
    WHERE q.tenant_id = %s AND q.status = 'escalated_human'
      AND c.status NOT IN ('closed', 'delivered')
    UNION ALL
    SELECT 'takeover', c.id, c.id, c.title, c.customer_email, c.status, c.route,
           '', 'human_takeover', '案件已标记为人工接管', c.updated_at
    FROM cases c
    WHERE c.tenant_id = %s AND c.human_takeover
      AND c.status NOT IN ('closed', 'delivered')
    UNION ALL
    SELECT 'mail', id, NULL, subject, from_addr, NULL, NULL, '', '', '', created_at
    FROM email_events
    WHERE tenant_id = %s AND direction = 'inbound'
      AND attribution_status = 'unattributed' AND case_id IS NULL
)
"""


def _advisor_alert(row: tuple) -> AdvisorAlert:
    kind, item_id, case_id, title, email, status, route, key, code, detail, updated = row
    return AdvisorAlert(
        question_id=item_id if kind == "question" else "",
        case_id=case_id,
        case_title=title or "",
        customer_email=email or "",
        case_status=status,
        route=route,
        requirement_key=key,
        reason_code=code,
        reason_detail=detail,
        alert_kind="escalate_question" if kind == "question" else "human_takeover",
        updated_sort=updated.isoformat(),
    )


def list_advisor_alerts(
    *, database_url: str, tenant_id: str, limit: int = 40,
) -> list[AdvisorAlert]:
    """List escalated questions and takeover alerts, most recent first."""
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            _INBOX_ITEMS + """
            SELECT * FROM inbox_items WHERE kind <> 'mail'
            ORDER BY updated_at DESC, item_id LIMIT %s
            """,
            (tenant_id, tenant_id, tenant_id, max(1, min(limit, 100))),
        ).fetchall()
    return [_advisor_alert(row) for row in rows]


def count_pending_advisor_inbox(*, database_url: str, tenant_id: str) -> int:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            _INBOX_ITEMS + "SELECT COUNT(*) FROM inbox_items",
            (tenant_id, tenant_id, tenant_id),
        ).fetchone()
    assert row is not None
    return row[0]


def get_advisor_inbox(*, database_url: str, tenant_id: str) -> AdvisorInbox:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            _INBOX_ITEMS + """
            , ranked AS (
                SELECT *, COUNT(*) OVER () AS pending_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY (kind = 'mail')
                        ORDER BY updated_at DESC, item_id
                    ) AS row_rank
                FROM inbox_items
            )
            SELECT * FROM ranked WHERE row_rank <= CASE WHEN kind = 'mail' THEN 100 ELSE 40 END
            ORDER BY row_rank
            """,
            (tenant_id, tenant_id, tenant_id),
        ).fetchall()
    alerts, mail = [], []
    for row in rows:
        item = row[:-2]
        if item[0] == "mail":
            mail.append(UnassignedMail(id=item[1], sender=item[4] or "", subject=item[3]))
        else:
            alerts.append(_advisor_alert(item))
    return AdvisorInbox(alerts, mail, rows[0][-2] if rows else 0)


def get_unassigned_mail(
    *, database_url: str, tenant_id: str, email_event_id: str,
) -> UnassignedMail | None:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT id, COALESCE(from_addr, ''), subject, body_text
            FROM email_events
            WHERE tenant_id = %s AND id = %s AND direction = 'inbound'
              AND attribution_status = 'unattributed' AND case_id IS NULL
            """,
            (tenant_id, email_event_id),
        ).fetchone()
    return UnassignedMail(*row) if row else None


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
    automatic_only: bool = False,
) -> OpenQuestion | None:
    """Close the active remediation for a slot after a clear re-upload."""
    key = (requirement_key or "").strip()
    if not key or not closed_by_evidence_id:
        return None
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            if automatic_only:
                previous = conn.execute(
                    _SELECT + """
                    WHERE tenant_id = %s AND case_id = %s AND requirement_key = %s
                      AND status = 'closed' AND closed_by_evidence_id = %s
                      AND severity = 'simple_auto'
                    LIMIT 1
                    """,
                    (tenant_id, case_id, key, closed_by_evidence_id),
                ).fetchone()
                if previous:
                    return _row(previous)
            row = conn.execute(
                """
                SELECT id FROM open_questions
                WHERE tenant_id = %s AND case_id = %s AND requirement_key = %s
                  AND status IN ('open', 'waiting_customer', 'escalated_human')
                  AND (NOT %s OR severity = 'simple_auto')
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (tenant_id, case_id, key, automatic_only),
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
