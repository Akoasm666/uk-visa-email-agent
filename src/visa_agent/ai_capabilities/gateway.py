"""W07 model gateway — grounded facts + template-rendered prose; no freeform invent."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from visa_agent.ai_capabilities.draft import SyntheticCaseContext
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError

# Completed status claims — forbidden anywhere in model output before persist.
_COMPLETED_SIDE_EFFECT_PATTERNS = (
    re.compile(
        r"\b(has been|have been|was|were)\s+(granted|approved|sent|published|emailed)\b",
        re.I,
    ),
    re.compile(
        r"\b(we|i)\s+(have\s+)?(approved|granted|sent|published|emailed)\b",
        re.I,
    ),
    re.compile(
        r"\b(your|the)\s+(visa|application)\s+(has been|is|was)\s+"
        r"(granted|approved)\b",
        re.I,
    ),
    re.compile(r"\bvisa\s+(has\s+been\s+)?granted\b", re.I),
    re.compile(r"\bapplication\s+(is\s+|has\s+been\s+)?approved\b", re.I),
    re.compile(r"\b(approved|granted|sent|emailed)\b", re.I),
    re.compile(
        r"\b(documents?|materials?)\s+(have\s+been\s+|were\s+|was\s+)?emailed\b",
        re.I,
    ),
    re.compile(r"\b(has\s+been|have\s+been|were|was)\s+emailed\b", re.I),
    re.compile(
        r"已(发送|批准|发布|获批|获签|寄出)|批准通过|签证已(获批|批准|获签)|"
        r"已经批准|已经寄出|材料已经寄出|材料也已寄出|申请已经批准|"
        r"申请已获签|已获签|也已寄出",
        re.I,
    ),
)

# Safe request phrases allowed in templates (not factual claims about a named employer).
_TEMPLATE_ACTIONS = (
    "请提供公司出具的在职证明",
    "请通过邮件发来护照资料页扫描件",
    "1. 护照资料页\n2. 行程说明\n3. 资金摘要",
)

# Customer-facing labels — never show raw field keys in the email body.
_FIELD_LABEL_ZH: dict[str, str] = {
    "applicant_alias": "称呼",
    "visit_purpose": "访问目的",
    "intended_stay_days": "计划停留天数",
    "funds_evidence": "资金材料",
}

_VALUE_LABEL_ZH: dict[str, str] = {
    "SYN-DEMO-APPLICANT": "演示申请人",
    "tourism_family_visit_demo": "探亲 / 旅游",
    "synthetic_bank_summary_only_no_real_balances": (
        "已收到银行摘要类材料（演示，不含真实余额）"
    ),
}


def _friendly_fact_value(key: str, value: str) -> str:
    mapped = _VALUE_LABEL_ZH.get(value)
    if mapped:
        return mapped
    if key == "intended_stay_days":
        return f"{value} 天"
    # Soften leftover snake_case demo tokens without inventing new facts.
    if re.fullmatch(r"[a-z0-9_]+", value) and "_" in value:
        return value.replace("_", " ")
    return value


def format_customer_draft_from_facts(
    facts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[str, list[str]]:
    """Plain Chinese confirmation note from grounded fact key/value pairs."""
    grounded = {
        str(f.get("key", "")).strip().lower(): str(f.get("value", "")).strip()
        for f in facts
        if str(f.get("key", "")).strip()
    }
    lines = [
        "您好，",
        "",
        "根据我们目前核对过的信息，请您确认是否正确：",
        "",
    ]
    preferred = (
        "applicant_alias",
        "visit_purpose",
        "intended_stay_days",
        "funds_evidence",
    )
    seen: set[str] = set()
    for key in preferred:
        if key not in grounded:
            continue
        label = _FIELD_LABEL_ZH.get(key, key)
        lines.append(f"· {label}：{_friendly_fact_value(key, grounded[key])}")
        seen.add(key)
    for key, value in sorted(grounded.items()):
        if key in seen:
            continue
        label = _FIELD_LABEL_ZH.get(key, key.replace("_", " "))
        lines.append(f"· {label}：{_friendly_fact_value(key, value)}")
    if not grounded:
        lines.append("· 目前还没有可确认的信息，我们核对后再联系您。")
    lines.extend(
        [
            "",
            "如有出入请直接回复本邮件。",
            "",
            "说明：这只是进度核对，不是签证结果，也不是寄送回执。",
        ]
    )
    actions = list(_TEMPLATE_ACTIONS)
    if "intended_stay_days" in grounded:
        days = _friendly_fact_value(
            "intended_stay_days", grounded["intended_stay_days"]
        )
        actions.insert(0, f"请确认计划停留 {days} 是否仍然准确")
    return "\n".join(lines), actions


def _fact_map(context: SyntheticCaseContext) -> dict[str, str]:
    return {
        str(f.get("key", "")).strip().lower(): str(f.get("value", "")).strip()
        for f in context.synthetic_facts
        if str(f.get("key", "")).strip()
    }


def assert_no_side_effect_claims(
    *,
    draft_text: str = "",
    actions: list[str] | None = None,
    raw_text: str = "",
) -> None:
    """Reject completed approve/send claims in model prose or raw payload."""
    blob = "\n".join(
        [
            raw_text or "",
            draft_text or "",
            "\n".join(actions or []),
        ]
    )
    for pattern in _COMPLETED_SIDE_EFFECT_PATTERNS:
        if pattern.search(blob):
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="model output asserts a completed side effect",
                details=(pattern.pattern,),
            )


def assert_facts_grounded(
    *,
    context: SyntheticCaseContext,
    facts: list[dict[str, Any]],
) -> None:
    """Each candidate fact key must exist; value must equal that field's source value."""
    fmap = _fact_map(context)
    for item in facts:
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", "")).strip()
        key_l = key.lower()
        if key_l not in fmap:
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="candidate fact key is not grounded in synthetic context",
                details=(key, value[:80]),
            )
        if fmap[key_l] != value:
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="candidate fact value does not match synthetic field source",
                details=(key, value[:80], fmap[key_l][:80]),
            )


def render_draft_from_grounded_facts(
    *,
    context: SyntheticCaseContext,
    facts: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Persist only template prose bound to already-validated candidate facts.

    Model freeform draft/actions are discarded so invented places, companions,
    employers, or mutated quantities cannot be stored.
    """
    assert_facts_grounded(context=context, facts=facts)
    return format_customer_draft_from_facts(facts)


def build_allowed_response_payload(
    *,
    draft_text: str,
    facts: list[dict[str, Any]],
    actions: list[str],
) -> str:
    """Only allowed structured fields are persisted as response_text."""
    import json

    return json.dumps(
        {
            "draft_text": draft_text,
            "candidate_facts": facts,
            "suggested_actions": actions,
        },
        ensure_ascii=False,
    )


def gate_model_output(
    *,
    context: SyntheticCaseContext,
    draft_text: str,
    facts: list[dict[str, Any]],
    actions: list[str],
    raw_text: str = "",
) -> tuple[str, list[str], str]:
    """Validate raw+structured model output; return template body/actions + safe JSON."""
    assert_no_side_effect_claims(
        draft_text=draft_text, actions=actions, raw_text=raw_text
    )
    assert_facts_grounded(context=context, facts=facts)
    rendered_body, rendered_actions = render_draft_from_grounded_facts(
        context=context, facts=facts
    )
    safe_response = build_allowed_response_payload(
        draft_text=rendered_body,
        facts=facts,
        actions=rendered_actions,
    )
    # Final guard: persisted payload must itself be free of completed claims.
    assert_no_side_effect_claims(raw_text=safe_response)
    return rendered_body, rendered_actions, safe_response


def groundedness_report(
    *,
    context: SyntheticCaseContext | None,
    facts: list[dict[str, Any]],
    draft_text: str = "",
    actions: list[str] | None = None,
    draft_case_version: int | None = None,
    current_case_version: int | None = None,
    has_draft: bool = True,
    generation_context_missing: bool = False,
) -> dict[str, Any]:
    """Non-raising diagnostic; unverifiable when generation-time context is absent."""
    actions = actions or []
    if not has_draft:
        return {
            "status": "no_draft",
            "all_grounded": None,
            "facts": [],
            "stale": False,
            "draft_case_version": None,
            "current_case_version": current_case_version,
            "context_keys": [],
            "synthetic_only": True,
            "verifiable": False,
        }

    if generation_context_missing or context is None:
        return {
            "status": "unverifiable",
            "all_grounded": None,
            "facts": [
                {
                    "key": str(f.get("key", "")),
                    "value": str(f.get("value", "")),
                    "grounded": None,
                    "detail": "generation-time synthetic context missing",
                }
                for f in facts
            ],
            "text_grounded": None,
            "side_effect_ok": None,
            "stale": (
                draft_case_version is not None
                and current_case_version is not None
                and draft_case_version != current_case_version
            ),
            "draft_case_version": draft_case_version,
            "current_case_version": current_case_version,
            "context_case_version": None,
            "context_keys": [],
            "synthetic_only": True,
            "verifiable": False,
            "detail": "generation-time grounding corpus unavailable; not verified",
        }

    rows = []
    for item in facts:
        key = str(item.get("key", ""))
        value = str(item.get("value", ""))
        try:
            assert_facts_grounded(context=context, facts=[item])
            ok = True
            detail = "ok"
        except DomainError as exc:
            ok = False
            detail = exc.message
        rows.append({"key": key, "value": value, "grounded": ok, "detail": detail})

    # Stored body/actions must match template render of grounded facts.
    text_ok = False
    text_detail = "facts not fully grounded; template not applicable"
    if all(r["grounded"] for r in rows):
        try:
            expected_body, expected_actions = render_draft_from_grounded_facts(
                context=context, facts=facts
            )
            text_ok = draft_text == expected_body and list(actions) == list(
                expected_actions
            )
            text_detail = (
                "template_bound"
                if text_ok
                else "stored prose is not the field-bound template render"
            )
        except DomainError as exc:
            text_ok = False
            text_detail = exc.message

    side_ok = True
    side_detail = "ok"
    try:
        assert_no_side_effect_claims(draft_text=draft_text, actions=actions)
    except DomainError as exc:
        side_ok = False
        side_detail = exc.message

    stale = (
        draft_case_version is not None
        and current_case_version is not None
        and draft_case_version != current_case_version
    )
    all_grounded = all(r["grounded"] for r in rows) and text_ok and side_ok
    return {
        "status": "stale" if stale else "current",
        "all_grounded": all_grounded,
        "facts": rows,
        "text_grounded": text_ok,
        "text_detail": text_detail,
        "side_effect_ok": side_ok,
        "side_effect_detail": side_detail,
        "stale": stale,
        "draft_case_version": draft_case_version,
        "current_case_version": current_case_version,
        "context_case_version": context.case_version,
        "context_keys": [str(f.get("key")) for f in context.synthetic_facts],
        "synthetic_only": True,
        "verifiable": True,
        "template_rendered": True,
        "payload_preview_keys": list(asdict(context).keys()),
    }


def context_from_stored_payload(
    *,
    synthetic_payload: dict[str, Any] | None,
) -> SyntheticCaseContext | None:
    """Rebuild generation-time context; None when payload/facts are missing."""
    if not synthetic_payload or not isinstance(synthetic_payload, dict):
        return None
    facts_raw = synthetic_payload.get("synthetic_facts")
    if not isinstance(facts_raw, list) or not facts_raw:
        return None
    try:
        facts = tuple(
            {
                "key": str(f.get("key", "")),
                "value": str(f.get("value", "")),
                "status": str(f.get("status", "")),
            }
            for f in facts_raw
            if isinstance(f, dict) and str(f.get("key", "")).strip()
        )
        if not facts:
            return None
        route = synthetic_payload.get("route")
        return SyntheticCaseContext(
            synthetic_case_label=str(
                synthetic_payload.get("synthetic_case_label") or "SYN-DEMO-CASE"
            ),
            case_version=int(synthetic_payload.get("case_version") or 0),
            status=str(synthetic_payload.get("status") or ""),
            route=str(route) if route is not None else None,
            synthetic_facts=facts,
            synthetic_note=str(synthetic_payload.get("synthetic_note") or ""),
        )
    except (TypeError, ValueError):
        return None
