"""Customer-facing inbound replies: match language; ground Q&A on UKVI snippets."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from typing import TYPE_CHECKING

from visa_agent.ai_capabilities.client import ChatClient, SiliconFlowClient
from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.ai_capabilities.research import gather_research
from visa_agent.ai_capabilities.ukvi_lookup import UkviSnippet
from visa_agent.ai_capabilities.web_search import SearchHit
from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError

if TYPE_CHECKING:
    from visa_agent.case_service.service import CaseRecord

_SYSTEM_ZH = """你是英国签证材料准备助手，用中文回复客户邮件问题。
规则：
- 根据提供的检索摘录回答（优先 GOV.UK / 官方来源；其他网页仅作补充）。
- 摘录不足以支撑时明确说不确定，并建议以官网为准；不要编造政策。
- 禁止声称签证已获批、已寄出、已批准、保证过签。
- 不要编造金额、雇主、行程或政策例外。
- 语气专业、简短；先直接答问题。
返回纯文本正文，不要 markdown 标题。"""

_SYSTEM_EN = """You are a UK visa materials assistant answering a customer email question.
Rules:
- Answer from the provided research excerpts (prefer GOV.UK / official sources; other pages are secondary).
- If excerpts are insufficient, say so and point to official pages — do not invent policy.
- Never claim a visa was granted, approved, sent, or guaranteed.
- Do not invent amounts, employers, itineraries, or policy exceptions.
- Be concise and professional; answer the question first.
Return plain text only (no markdown headings)."""


@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    language: str
    text: str
    source_urls: tuple[str, ...]
    used_model: bool
    used_ukvi: bool
    needs_advisor: bool = False
    confidence: float = 0.0
    used_web_search: bool = False


def _fallback_answer(*, language: str, snippets: tuple[UkviSnippet, ...]) -> str:
    urls = [s.url for s in snippets]
    if language == "zh":
        lines = [
            "关于您的问题：我已根据公开网页检索（优先英国政府官网）做了核对。",
            "具体是否适用要以您的个人情况和官方最新页面为准；我不能保证签证结果。",
        ]
        if urls:
            lines.append("可参考：")
            lines.extend(f"- {u}" for u in urls[:4])
        else:
            lines.append(
                "当前检索结果不足，请您直接查看："
                "https://www.gov.uk/browse/visas-immigration"
            )
        return "\n".join(lines)
    lines = [
        "Regarding your question: I checked publicly available sources "
        "(preferring GOV.UK).",
        "Whether it applies depends on your circumstances and the latest official page; "
        "I cannot guarantee a visa outcome.",
    ]
    if urls:
        lines.append("Useful links:")
        lines.extend(f"- {u}" for u in urls[:4])
    else:
        lines.append(
            "I could not gather enough sources just now — please check: "
            "https://www.gov.uk/browse/visas-immigration"
        )
    return "\n".join(lines)


def _extract_assistant_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return str(content or "").strip()


# Personal outcome / case judgement — NOT general "how do I prepare funds".
# File-type "这是什么材料" is handled by attachment classify → advisor_unclassified.
_CASE_SPECIFIC = re.compile(
    r"("
    r"我这种情况|我的(余额|资金|情况|案子)|够不够|能不能过|能否过签|会不会拒|"
    r"帮我判断|帮我看看能不能|个案判断|"
    r"my (balance|situation|case).{0,20}(enough|ok|approve)|"
    r"will I (get|be) (refused|approved)|am I eligible"
    r")",
    re.I,
)

_BASIC_POLICY = re.compile(
    r"("
    r"资金|流水|存款|证明|护照|材料|清单|要求|政策|准备|怎么|如何|"
    r"访问签|visitor|funds|bank statement|passport|document|requirement|"
    r"checklist|how (do|to|should)|what (do|does|should)"
    r")",
    re.I,
)

# Only escalate when the *answer* explicitly hands off — not ordinary disclaimers
# like「不能保证签证结果」.
_ADVISOR_HANDOFF_IN_ANSWER = re.compile(
    r"("
    r"建议咨询顾问|请顾问|转顾问|需要顾问|人工(跟进|处理|判断)|"
    r"无法仅根据公开说明判断您的个案|"
    r"advisor will follow|please (ask|contact) (your )?advisor|"
    r"needs? (a )?human (review|advisor)"
    r")",
    re.I,
)


def question_needs_human_judgement(question: str) -> bool:
    """True when the ask is about *this* customer's materials/outcome, not general policy."""
    q = (question or "").strip()
    if not q:
        return False
    if _CASE_SPECIFIC.search(q):
        # Mixed: "这份材料算什么 + 资金政策怎么准备" — still needs human for the file part,
        # but only escalate when case-specific dominates or no basic-policy framing alone.
        return True
    return False


def answer_customer_question(
    *,
    settings: Settings,
    case: "CaseRecord",
    question: str,
    language: str | None = None,
    chat: ChatClient | None = None,
    ukvi_snippets: tuple[UkviSnippet, ...] | None = None,
    web_hits: tuple[SearchHit, ...] | None = None,
    enable_web_search: bool = True,
) -> QuestionAnswer:
    """Answer using UKVI pages + web search; high confidence → no advisor escalate.

    Escalate when: case-specific judgement, explicit handoff in answer, or
    research confidence below threshold (and not a thin basic-policy link dump).
    """
    from visa_agent.ai_capabilities.gateway import assert_no_side_effect_claims

    lang = language or detect_reply_language(question)
    route = case.route.value if case.route else None
    research = gather_research(
        question=question,
        route=route,
        enable_web_search=enable_web_search,
        ukvi_snippets=ukvi_snippets,
        web_hits=web_hits,
    )
    snippets = research.snippets_for_prompt
    urls = tuple(h.url for h in research.hits)
    case_specific = question_needs_human_judgement(question)
    basic_policy = bool(_BASIC_POLICY.search(question or ""))
    low_conf = not research.high_confidence

    def _needs(handoff: bool = False) -> bool:
        if case_specific or handoff:
            return True
        if research.high_confidence:
            return False
        # General policy Q: answer from UKVI/web (or GOV.UK fallback links).
        # Do not open advisor inbox just because retrieval was thin / offline.
        if basic_policy:
            return False
        return low_conf or not snippets

    if not settings.model_configured and chat is None:
        return QuestionAnswer(
            language=lang,
            text=_fallback_answer(language=lang, snippets=snippets),
            source_urls=urls,
            used_model=False,
            used_ukvi=research.used_ukvi,
            needs_advisor=_needs(),
            confidence=research.confidence,
            used_web_search=research.used_web_search,
        )

    provider: ChatClient = chat or SiliconFlowClient.from_settings(settings)
    excerpt_blob = "\n\n".join(
        f"SOURCE: {h.url}\nTITLE: {h.title}\n"
        f"AUTHORITY: {h.authority:.2f}\nEXCERPT: {h.snippet}"
        for h in research.hits
    ) or "(no research excerpts)"
    user_payload = {
        "customer_question": question,
        "reply_language": lang,
        "case_route": route,
        "case_status": case.status.value,
        "research_confidence": research.confidence,
        "research_excerpts": excerpt_blob,
        "instruction": (
            "Prefer higher AUTHORITY sources (especially GOV.UK). "
            "Use other web excerpts only when they agree with official guidance "
            "or fill practical gaps. "
            "If the customer asks you to judge THEIR specific document or outcome, "
            "say you will flag it for the advisor — do not invent a verdict."
        ),
    }
    messages = [
        {
            "role": "system",
            "content": _SYSTEM_ZH if lang == "zh" else _SYSTEM_EN,
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    try:
        raw = provider.complete(messages=messages, model=settings.siliconflow_model)
        text = _extract_assistant_text(raw)
        assert_no_side_effect_claims(draft_text=text, raw_text=text)
        if not text or len(text) < 20:
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="empty model answer",
            )
        if urls and not any(u in text for u in urls):
            if lang == "zh":
                text = text.rstrip() + "\n\n参考：\n" + "\n".join(f"- {u}" for u in urls[:4])
            else:
                text = (
                    text.rstrip()
                    + "\n\nSources:\n"
                    + "\n".join(f"- {u}" for u in urls[:4])
                )
        handoff = bool(_ADVISOR_HANDOFF_IN_ANSWER.search(text))
        return QuestionAnswer(
            language=lang,
            text=text,
            source_urls=urls,
            used_model=True,
            used_ukvi=research.used_ukvi,
            needs_advisor=_needs(handoff),
            confidence=research.confidence,
            used_web_search=research.used_web_search,
        )
    except DomainError:
        return QuestionAnswer(
            language=lang,
            text=_fallback_answer(language=lang, snippets=snippets),
            source_urls=urls,
            used_model=False,
            used_ukvi=research.used_ukvi,
            needs_advisor=_needs(),
            confidence=research.confidence,
            used_web_search=research.used_web_search,
        )


@dataclass(frozen=True, slots=True)
class MaterialLine:
    filename: str
    label: str
    issue: str | None = None


def build_status_reply(
    *,
    language: str,
    case: "CaseRecord",
    received: list[MaterialLine],
    issues: list[MaterialLine],
    missing: list[str],
    question_block: str | None = None,
    remediations_closed: list[str] | None = None,
) -> tuple[str, str]:
    """Monolingual materials-status email (optional Q&A block prepended)."""
    ref = case.id[:8]
    title = case.title or ("您的案件" if language == "zh" else "your case")
    if language == "zh":
        subject = f"回复：[签证助手] 案件进度 · {ref}"
        parts = [
            "您好，",
            "",
            f"我们已收到您关于「{title}」（{ref}）的来信。",
        ]
        if question_block:
            parts.extend(["", question_block.strip()])
        if remediations_closed:
            parts.extend(
                [
                    "",
                    "以下补件项已根据您新传的清晰材料关闭：",
                    *[f"- {x}" for x in remediations_closed],
                ]
            )
        if received:
            parts.extend(
                [
                    "",
                    "已归档材料：",
                    *[f"- {m.filename} → {m.label}" for m in received],
                ]
            )
        if issues:
            parts.extend(
                [
                    "",
                    "请重新上传（请标明对应材料；扫描需清晰、完整）：",
                    *[
                        f"- {m.filename}（{m.label}）：{m.issue or '需重传'}"
                        for m in issues
                    ],
                ]
            )
        if missing:
            parts.extend(["", "仍缺少：", *[f"- {x}" for x in missing[:8]]])
        elif not issues:
            parts.extend(
                [
                    "",
                    "清单项目前看起来已齐。最终材料包仍须顾问人工放行。",
                ]
            )
        parts.extend(["", "请直接回复本邮件继续提交材料。", ""])
        return subject, "\n".join(parts)

    subject = f"Re: [Visa Assist] Case update · {ref}"
    parts = [
        "Hello,",
        "",
        f"We received your email for 「{title}」 ({ref}).",
    ]
    if question_block:
        parts.extend(["", question_block.strip()])
    if remediations_closed:
        parts.extend(
            [
                "",
                "These remediation items are now closed after your clearer upload:",
                *[f"- {x}" for x in remediations_closed],
            ]
        )
    if received:
        parts.extend(
            [
                "",
                "Documents filed:",
                *[f"- {m.filename} → {m.label}" for m in received],
            ]
        )
    if issues:
        parts.extend(
            [
                "",
                "Please re-upload (clear, complete scan; name the document type):",
                *[
                    f"- {m.filename} ({m.label}): {m.issue or 'needs re-upload'}"
                    for m in issues
                ],
            ]
        )
    if missing:
        parts.extend(["", "Still missing:", *[f"- {x}" for x in missing[:8]]])
    elif not issues:
        parts.extend(
            [
                "",
                "Required checklist items appear present. "
                "Your advisor must still human-approve the final package.",
            ]
        )
    parts.extend(["", "Please reply to this thread with any remaining documents.", ""])
    return subject, "\n".join(parts)


def strip_model_noise(text: str) -> str:
    """Light cleanup if a model wraps the answer in fences."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:\w+)?\n?", "", t)
    t = re.sub(r"\n?```$", "", t)
    return t.strip()
