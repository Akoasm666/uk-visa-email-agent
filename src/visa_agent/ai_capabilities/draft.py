"""Generate case drafts from synthetic context only (no side effects)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import psycopg

from visa_agent.ai_capabilities.client import ChatClient, SiliconFlowClient
from visa_agent.case_service.service import CaseRecord, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError

# Model may propose only candidate / unstated statuses — never human/doc verdicts.
_MODEL_ALLOWED_FACT_STATUSES = frozenset(
    {
        "pending_extraction",
        "customer_stated",
        "unknown",
    }
)

_SYSTEM_PROMPT = """You are a UK Standard Visitor visa email assistant drafting helper.
Return ONLY this JSON object (no other keys, no markdown):
{
  "draft_text": "short non-assertive note",
  "candidate_facts": [{"key": "...", "value": "...", "status": "customer_stated"}],
  "suggested_actions": ["human-readable next step", ...]
}
Rules:
- candidate_facts[].key/value MUST copy fields from synthetic_context only.
- candidate_facts[].status MUST be one of: pending_extraction, customer_stated, unknown.
- NEVER claim approval, grant, send, email-out, or publish (past or completed).
- draft_text/suggested_actions must not invent places, companions, employers, or amounts.
"""


@dataclass(frozen=True, slots=True)
class SyntheticCaseContext:
    """Fields safe to send to the model — no real title / case_id / email."""

    synthetic_case_label: str
    case_version: int
    status: str
    route: str | None
    synthetic_facts: tuple[dict[str, str], ...]
    synthetic_note: str


@dataclass(frozen=True, slots=True)
class DraftResult:
    model_call_id: str
    case_id: str
    case_version: int
    model: str
    draft_text: str
    candidate_facts: tuple[dict[str, Any], ...]
    suggested_actions: tuple[str, ...]
    prompt_digest: str


def build_synthetic_context(case: CaseRecord) -> SyntheticCaseContext:
    """Build model input from fixed synthetic demo facts (no real title/id)."""
    facts: list[dict[str, str]] = [
        {
            "key": "applicant_alias",
            "value": "SYN-DEMO-APPLICANT",
            "status": "customer_stated",
        },
        {
            "key": "visit_purpose",
            "value": "tourism_family_visit_demo",
            "status": "customer_stated",
        },
        {
            "key": "intended_stay_days",
            "value": "14",
            "status": "customer_stated",
        },
        {
            "key": "funds_evidence",
            "value": "synthetic_bank_summary_only_no_real_balances",
            "status": "pending_extraction",
        },
    ]
    note = (
        "All facts below are synthetic demo content for S3 model gateway. "
        "Do not treat them as real customer PII."
    )
    return SyntheticCaseContext(
        synthetic_case_label="SYN-DEMO-CASE",
        case_version=case.case_version,
        status=case.status.value,
        route=case.route.value if case.route else None,
        synthetic_facts=tuple(facts),
        synthetic_note=note,
    )


def _prompt_digest(context: SyntheticCaseContext) -> str:
    material = json.dumps(asdict(context), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_output(raw_text: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    parsed = _extract_json_object(raw_text)
    if parsed is None:
        raise DomainError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="model output was not valid JSON draft object",
            details=(raw_text[:200],),
        )

    draft_raw = parsed.get("draft_text")
    if not isinstance(draft_raw, str) or not draft_raw.strip():
        raise DomainError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="draft_text must be a non-empty string",
            details=(type(draft_raw).__name__,),
        )
    draft = draft_raw.strip()

    facts_raw = parsed.get("candidate_facts", [])
    actions_raw = parsed.get("suggested_actions", [])
    if not isinstance(facts_raw, list):
        raise DomainError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="candidate_facts must be an array",
        )
    if not isinstance(actions_raw, list):
        raise DomainError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="suggested_actions must be an array",
        )

    facts: list[dict[str, Any]] = []
    for item in facts_raw:
        if not isinstance(item, dict):
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="candidate_facts items must be objects",
                details=(type(item).__name__,),
            )
        key = item.get("key")
        value = item.get("value")
        status = item.get("status", "customer_stated")
        if not isinstance(key, str) or not isinstance(value, str):
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="candidate_facts key/value must be strings",
            )
        if not isinstance(status, str):
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="candidate_facts status must be a string",
            )
        if status not in _MODEL_ALLOWED_FACT_STATUSES:
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="model may not assign this fact status",
                details=(status,),
            )
        facts.append({"key": key, "value": value, "status": status})

    actions: list[str] = []
    for action in actions_raw:
        if not isinstance(action, str) or not action.strip():
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="suggested_actions items must be non-empty strings",
                details=(type(action).__name__,),
            )
        actions.append(action.strip())
    return draft, facts, actions


def generate_case_draft(
    *,
    settings: Settings,
    case_id: str,
    client: ChatClient | None = None,
) -> DraftResult:
    """Call the model with synthetic context and persist the audit row.

    Does not send email, approve, or mutate case status/version.
    Real case_id / title are stored only in local audit, not in model payload.
    """
    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    context = build_synthetic_context(case)
    digest = _prompt_digest(context)
    model_payload = {"synthetic_context": asdict(context)}
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(model_payload, ensure_ascii=False),
        },
    ]
    chat = client or SiliconFlowClient.from_settings(settings)
    provider = chat.complete(messages=messages, model=settings.siliconflow_model)
    try:
        content = provider["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DomainError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="model response missing choices[0].message.content",
        ) from exc
    if not isinstance(content, str):
        raise DomainError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="model content must be a string",
        )
    draft_text, facts, actions = _normalize_output(content)
    from visa_agent.ai_capabilities.gateway import gate_model_output

    # Validate raw+structured output first; persist only allowed fields (no raw claim text).
    draft_text, actions, safe_response = gate_model_output(
        context=context,
        draft_text=draft_text,
        facts=facts,
        actions=actions,
        raw_text=content,
    )
    model_call_id = uuid.uuid4().hex
    # Local audit keeps real identity; messages sent to the model stay synthetic-only.
    audit_blob = {
        "local_audit": {
            "case_id": case.id,
            "title": case.title,
            "case_version": case.case_version,
        },
        "messages": messages,
        "provider_id": provider.get("id"),
        "model_raw_discarded": True,
    }
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            INSERT INTO model_calls (
                id, tenant_id, case_id, case_version, model, prompt_digest,
                request_json, response_text, draft_text,
                candidate_facts, suggested_actions
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s,
                %s::jsonb, %s::jsonb
            )
            """,
            (
                model_call_id,
                settings.tenant_id,
                case.id,
                case.case_version,
                settings.siliconflow_model,
                digest,
                json.dumps(audit_blob, ensure_ascii=False),
                safe_response,
                draft_text,
                json.dumps(facts, ensure_ascii=False),
                json.dumps(actions, ensure_ascii=False),
            ),
        )
        conn.commit()
    return DraftResult(
        model_call_id=model_call_id,
        case_id=case.id,
        case_version=case.case_version,
        model=settings.siliconflow_model,
        draft_text=draft_text,
        candidate_facts=tuple(facts),
        suggested_actions=tuple(actions),
        prompt_digest=digest,
    )


def list_case_drafts(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    limit: int = 5,
) -> list[DraftResult]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, case_id, case_version, model, draft_text,
                   candidate_facts, suggested_actions, prompt_digest
            FROM model_calls
            WHERE tenant_id = %s AND case_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (tenant_id, case_id, limit),
        ).fetchall()
    results: list[DraftResult] = []
    for row in rows:
        facts = row[5] if isinstance(row[5], list) else json.loads(row[5] or "[]")
        actions = row[6] if isinstance(row[6], list) else json.loads(row[6] or "[]")
        results.append(
            DraftResult(
                model_call_id=row[0],
                case_id=row[1],
                case_version=row[2],
                model=row[3],
                draft_text=row[4] or "",
                candidate_facts=tuple(facts),
                suggested_actions=tuple(str(a) for a in actions),
                prompt_digest=row[7],
            )
        )
    return results


def load_latest_draft_audit(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> tuple[DraftResult, dict[str, Any]] | None:
    """Latest draft plus request_json audit (includes generation synthetic_context)."""
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT id, case_id, case_version, model, draft_text,
                   candidate_facts, suggested_actions, prompt_digest, request_json
            FROM model_calls
            WHERE tenant_id = %s AND case_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, case_id),
        ).fetchone()
    if row is None:
        return None
    facts = row[5] if isinstance(row[5], list) else json.loads(row[5] or "[]")
    actions = row[6] if isinstance(row[6], list) else json.loads(row[6] or "[]")
    audit = row[8] if isinstance(row[8], dict) else json.loads(row[8] or "{}")
    draft = DraftResult(
        model_call_id=row[0],
        case_id=row[1],
        case_version=row[2],
        model=row[3],
        draft_text=row[4] or "",
        candidate_facts=tuple(facts),
        suggested_actions=tuple(str(a) for a in actions),
        prompt_digest=row[7],
    )
    return draft, audit
