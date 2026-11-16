"""Record a customer's stated visit purpose with its originating email."""

from __future__ import annotations

import re

import psycopg

from visa_agent.case_service.service import bump_case_version_in_conn, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus, RouteCode
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.email_adapter.threading import new_reply_body

PURPOSE_LABELS = {"tourism": "旅游", "family_visit": "探亲访友", "business": "商务"}
_LABELS = {
    "旅游": "tourism", "旅行": "tourism", "度假": "tourism", "观光": "tourism",
    "tourism": "tourism", "a holiday": "tourism",
    "探亲": "family_visit", "探亲访友": "family_visit", "访友": "family_visit", "a family visit": "family_visit",
    "商务": "business", "business": "business",
}
_TERMS = "|".join(re.escape(term) for term in sorted(_LABELS, key=len, reverse=True))
_STATEMENT = re.compile(
    rf"(?:访问目的|访英目的|来英国的目的|去英国的目的|目的)\s*(?:就是|是|为|[:：])\s*(?P<stated>{_TERMS})"
    rf"|(?:我|我们)(?:计划|想|打算|准备|是来|是去)?\s*(?:(?:去|来|到|在)?(?:英国和苏格兰|英国|伦敦|爱丁堡))\s*(?P<visit>{_TERMS})"
    rf"|(?:去英国是为了|来英国是为了|我是去|我们是去)(?P<direct>旅游|度假|观光|探亲|访友)"
    rf"|(?:^|[，,]\s*)(?P<short>旅游|度假|观光)(?=\s*(?:[。！!,，]|$))"
    rf"|\b(?:my (?:visit |travel )?purpose is|i(?: am|'m) (?:visiting|travell?ing) (?:the uk )?for|"
    rf"i(?: am|'m) (?:visiting|travell?ing) to (?:the uk|london) for)\s+(?P<english>{_TERMS})\b",
    re.I,
)
_UNCERTAIN = re.compile(
    r"不是|并非|不确定|可能|也许|是否|能否|请问|还是|或者|改为|改成|不只是|"
    r"\b(?:not|maybe|might|whether|or|only if|actually)\b|[？?]", re.I,
)
_OTHER_ACTIVITY = re.compile(r"工作|打工|长期学习|远程办公|\b(?:work|employment|long.term (?:study|course))\b", re.I)
_EDITABLE = (CaseStatus.LEAD, CaseStatus.INTAKE_AUTHORIZATION, CaseStatus.ROUTE_SCOPE_CONFIRMED,
             CaseStatus.COLLECT_AND_VERIFY, CaseStatus.ISSUE_RESOLUTION,
             CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION, CaseStatus.FINAL_REVIEW,
             CaseStatus.READY_TO_DELIVER)


def stated_visit_purpose(body: str) -> str | None:
    found = set()
    for line in re.split(r"(?<=[。！？!?；;])|\n|(?<=\.)\s+|[，,]\s*(?=(?:请问|能否|可以帮|能帮|can you|could you))", body, flags=re.I):
        matches = list(_STATEMENT.finditer(line))
        if not matches:
            if (_OTHER_ACTIVITY.search(line) or _UNCERTAIN.search(line)) and re.search(
                r"访英目的|访问目的|访英|来英国|去英国|\bpurpose\b", line, re.I,
            ):
                found.add("unclear")
            continue
        if _UNCERTAIN.search(line) or _OTHER_ACTIVITY.search(line):
            found.add("unclear")
            continue
        found.update(_LABELS[match[match.lastgroup].lower()] for match in matches)
    if not found:
        return None
    return next(iter(found)) if len(found) == 1 else "unclear"


def get_stated_visitor_purpose(conn: psycopg.Connection, tenant_id: str, case_id: str) -> str | None:
    row = conn.execute(
        "SELECT purpose FROM case_visitor_purposes WHERE tenant_id = %s AND case_id = %s",
        (tenant_id, case_id),
    ).fetchone()
    return row[0] if row else None


def record_visitor_purpose(
    settings: Settings, case_id: str, event_id: str, purpose: str | None,
    *, only_if_missing: bool = False,
) -> str | None:
    if not purpose:
        return None
    keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(**keys, conn=conn, for_update=True)
        if case.route != RouteCode.STANDARD_VISITOR or case.status not in _EDITABLE or case.human_takeover:
            return None
        source = conn.execute(
            """
            SELECT from_addr FROM email_events
            WHERE tenant_id = %s AND case_id = %s AND id = %s
              AND direction = 'inbound' AND attribution_status = 'attributed'
            """,
            (case.tenant_id, case.id, event_id),
        ).fetchone()
        customer = normalize_email(case.customer_email)
        if not source or not customer or normalize_email(source[0]) != customer:
            return None
        existing = get_stated_visitor_purpose(conn, case.tenant_id, case.id)
        if existing and (only_if_missing or existing == purpose):
            return existing
        bump_case_version_in_conn(**keys, conn=conn, expected_case_version=case.case_version)
        conn.execute(
            """
            INSERT INTO case_visitor_purposes (tenant_id, case_id, email_event_id, purpose)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, case_id) DO UPDATE
            SET email_event_id = EXCLUDED.email_event_id, purpose = EXCLUDED.purpose
            """,
            (case.tenant_id, case.id, event_id, purpose),
        )
    return purpose


def backfill_visitor_purposes(settings: Settings) -> None:
    """Use existing attributed correspondence for cases already collecting materials."""
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT c.id, e.id, e.body_text FROM cases c JOIN email_events e
              ON e.tenant_id = c.tenant_id AND e.case_id = c.id
            WHERE c.tenant_id = %s AND c.route = 'standard_visitor'
              AND c.status IN ('lead', 'intake_authorization', 'route_scope_confirmed', 'collect_and_verify',
                               'issue_resolution', 'draft_preparation')
              AND e.direction = 'inbound' AND e.attribution_status = 'attributed'
              AND lower(trim(e.from_addr)) = lower(trim(c.customer_email))
              AND NOT EXISTS (SELECT 1 FROM case_visitor_purposes p WHERE p.tenant_id = c.tenant_id AND p.case_id = c.id)
            ORDER BY c.id, e.created_at DESC, e.id DESC
            """,
            (settings.tenant_id,),
        ).fetchall()
    completed = set()
    for case_id, event_id, text in rows:
        if case_id not in completed and record_visitor_purpose(
            settings, case_id, event_id, stated_visit_purpose(new_reply_body(text)), only_if_missing=True,
        ):
            completed.add(case_id)
