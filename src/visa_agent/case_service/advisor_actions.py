"""Advisor workbench actions: classify evidence + relay answers via agent outbox."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.case_service.remediation import (
    ADVISOR_CUSTOMER_QUESTION,
    ADVISOR_UNCLASSIFIED,
    acknowledge_advisor_question,
    close_remediation,
    list_open_questions,
)
from visa_agent.case_service.service import CaseRecord, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode, OutboundMessageKind
from visa_agent.domain.errors import DomainError
from visa_agent.document_pipeline.classify import requirement_for_classification
from visa_agent.document_pipeline.service import (
    EvidenceRecord,
    list_evidence,
    set_evidence_classification,
)
from visa_agent.email_adapter.outbox import OutboxRequest, OutboxResult, enqueue_and_maybe_send
from visa_agent.rules import evaluate_case_requirements

# classification value → Chinese label for workbench select
CLASSIFY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("passport", "护照"),
    ("bank", "资金 / 银行流水"),
    ("funds", "资金证明"),
    ("sponsor", "担保人材料"),
    ("translation", "翻译件"),
    ("itinerary", "行程 / 离境意向"),
    ("employment", "在职 / 国内约束力"),
    ("purpose", "访问目的说明"),
    ("cas", "CAS"),
    ("maintenance", "学费 / 生活费资金"),
    ("course", "课程录取"),
    ("cos", "CoS 担保证书"),
    ("salary", "薪资证明"),
    ("skill", "职位 / 技能"),
    ("english", "英语能力"),
    ("tb", "TB 体检"),
    ("degree", "学历 / 学位"),
    ("graduation", "英国学业完成"),
    ("permission", "当前在英身份"),
)

_ALLOWED_CLASS = frozenset(c for c, _ in CLASSIFY_OPTIONS)


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    evidence: EvidenceRecord
    closed_unclassified: bool
    requirement_key: str | None


@dataclass(frozen=True, slots=True)
class AdvisorReplyResult:
    outbox: OutboxResult
    closed_question: bool
    language: str


def _is_unclassified_label(label: str | None) -> bool:
    return (label or "").strip().lower() in {
        "",
        "unclassified",
        "fuzzy",
        "unclear",
        "unknown",
    }


def count_unclassified_evidence(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
) -> int:
    items = list_evidence(
        database_url=database_url, tenant_id=tenant_id, case_id=case_id
    )
    return sum(1 for e in items if _is_unclassified_label(e.classification))


def advisor_classify_evidence(
    *,
    settings: Settings,
    case_id: str,
    evidence_id: str,
    classification: str,
    question_id: str | None = None,
) -> ClassifyResult:
    """Advisor assigns a checklist class; closes advisor_unclassified when done."""
    label = (classification or "").strip().lower()
    if label not in _ALLOWED_CLASS:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="请选择有效的材料类型",
            details=(classification,),
        )
    evidence = set_evidence_classification(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
        classification=label,
    )
    req_key = requirement_for_classification(label)
    closed = False
    remaining = count_unclassified_evidence(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    if remaining == 0:
        closed_q = close_remediation(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            requirement_key=ADVISOR_UNCLASSIFIED,
            closed_by_evidence_id=evidence_id,
        )
        closed = closed_q is not None
        if question_id and not closed:
            ack = acknowledge_advisor_question(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                question_id=question_id,
                actor="advisor_classify",
            )
            closed = ack is not None
    try:
        evaluate_case_requirements(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError:
        pass
    return ClassifyResult(
        evidence=evidence,
        closed_unclassified=closed,
        requirement_key=req_key,
    )


def _wrap_advisor_body(*, language: str, body: str) -> tuple[str, str]:
    text = (body or "").strip()
    if language == "zh":
        subject = "顾问回复（经签证材料助手转发）"
        wrapped = (
            "您好，顾问已就您的问题回复如下：\n\n"
            f"{text}\n\n"
            "如需继续补充材料或提问，直接回复本邮件即可。"
        )
    else:
        subject = "Advisor reply (via your visa materials assistant)"
        wrapped = (
            "Hello — your advisor’s reply is below:\n\n"
            f"{text}\n\n"
            "Reply to this email if you need to send more documents or ask follow-ups."
        )
    return subject, wrapped


def advisor_reply_via_agent(
    *,
    settings: Settings,
    case_id: str,
    body_text: str,
    question_id: str | None = None,
    language: str | None = None,
) -> AdvisorReplyResult:
    """Send advisor-authored answer through agent outbox (allowlist + audit).

    Prefer this over a direct SMTP side-channel so customer language framing,
    outbound allowlist, and outbox history stay consistent with the agent loop.
    """
    text = (body_text or "").strip()
    if len(text) < 4:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="回复内容太短",
        )
    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    email = (case.customer_email or "").strip().lower()
    if not email:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="案件缺少客户邮箱",
        )
    # Prefer language of the escalated question detail / customer email trail.
    lang = language or _infer_question_language(settings, case, question_id) or "zh"
    subject, wrapped = _wrap_advisor_body(language=lang, body=text)
    out = enqueue_and_maybe_send(
        settings=settings,
        req=OutboxRequest(
            kind=OutboundMessageKind.ORDINARY_REPLY.value,
            recipients=(email,),
            subject=subject,
            body_text=wrapped,
            send_operation_id=(
                f"advisor-relay:{settings.tenant_id}:{case_id}:{uuid.uuid4().hex[:12]}"
            ),
            send_now=True,
            case_id=case.id,
            case_version=case.case_version,
        ),
    )
    closed = False
    targets: list[str] = []
    if question_id:
        targets.append(question_id)
    else:
        for q in list_open_questions(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        ):
            if (
                q.requirement_key == ADVISOR_CUSTOMER_QUESTION
                and q.status == "escalated_human"
            ):
                targets.append(q.id)
    for qid in targets:
        ack = acknowledge_advisor_question(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            question_id=qid,
            actor="advisor_reply_via_agent",
        )
        if ack is not None:
            closed = True
    return AdvisorReplyResult(outbox=out, closed_question=closed, language=lang)


def _infer_question_language(
    settings: Settings,
    case: CaseRecord,
    question_id: str | None,
) -> str | None:
    texts: list[str] = []
    if question_id:
        for q in list_open_questions(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case.id,
            active_only=False,
        ):
            if q.id == question_id:
                texts.append(q.reason_detail or "")
                break
    texts.append(case.title or "")
    if not any(t.strip() for t in texts):
        return None
    return detect_reply_language(*texts)
