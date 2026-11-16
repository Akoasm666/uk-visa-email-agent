"""Advisor workbench actions: classify evidence + relay answers via agent outbox."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg

from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.case_service.remediation import (
    ADVISOR_CUSTOMER_QUESTION,
    ADVISOR_UNCLASSIFIED,
    acknowledge_advisor_question,
    close_remediation,
    list_open_questions,
)
from visa_agent.case_service.service import (
    FINISHED_CASE_STATUSES, CaseRecord, _require_version, bump_case_version_in_conn, get_case,
)
from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode, OutboundMessageKind
from visa_agent.domain.errors import DomainError
from visa_agent.document_pipeline.classify import requirement_for_classification
from visa_agent.document_pipeline.service import (
    EvidenceRecord,
    list_evidence,
    set_evidence_classification,
)
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.email_adapter.outbox import (
    TAKEOVER_REPLY_OPERATION_PREFIX, OutboxRequest, OutboxResult, enqueue_and_maybe_send,
)
from visa_agent.email_adapter.threading import new_reply_body, reply_references, single_message_id
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


@dataclass(frozen=True, slots=True)
class CustomerMail:
    id: str
    direction: str
    subject: str
    body: str
    message_id: str | None
    in_reply_to: str | None
    references: str | None
    created_at: datetime


def list_customer_mail(
    settings: Settings, case: CaseRecord, *, event_id: str | None = None,
) -> list[CustomerMail]:
    email = normalize_email(case.customer_email)
    if not email:
        return []
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, direction, subject, body_text, rfc_message_id, in_reply_to, email_references, created_at
            FROM email_events
            WHERE tenant_id = %s AND case_id = %s AND attribution_status = 'attributed'
              AND (%s::text IS NULL OR id = %s)
              AND ((direction = 'inbound' AND lower(trim(from_addr)) = %s)
                OR (direction = 'outbound' AND to_addrs @> %s::jsonb))
            ORDER BY created_at DESC, id DESC
            """,
            (settings.tenant_id, case.id, event_id, event_id, email, json.dumps([email])),
        ).fetchall()
    return [
        CustomerMail(id, direction, subject, new_reply_body(body) if direction == "inbound" else body,
                     message_id, in_reply_to, references, created_at)
        for id, direction, subject, body, message_id, in_reply_to, references, created_at in rows
    ]


def takeover_reply_subject(mail: CustomerMail | None) -> str:
    subject = " ".join(mail.subject.split()) if mail else ""
    if not subject:
        return "签证案件沟通"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def fill_takeover_customer_email(
    *, settings: Settings, case_id: str, customer_email: str, expected_case_version: int | None,
) -> None:
    email = normalize_email(customer_email)
    if not email:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "请输入有效的客户邮箱")
    keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    with psycopg.connect(settings.database_url) as conn:
        case = get_case(**keys, conn=conn, for_update=True)
        if case.status in FINISHED_CASE_STATUSES or not case.human_takeover:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请在办理中的案件接管后填写客户邮箱")
        existing = normalize_email(case.customer_email)
        if existing == email:
            return
        _require_version(case, expected_case_version, required=True)
        if existing:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "案件已有客户邮箱")
        conn.execute(
            """UPDATE cases SET customer_email = %s WHERE tenant_id = %s AND id = %s""",
            (email, case.tenant_id, case.id),
        )
        bump_case_version_in_conn(**keys, conn=conn, expected_case_version=case.case_version)


def send_takeover_reply(
    *, settings: Settings, case_id: str, body: str, request_id: str,
    reply_event_id: str, expected_case_version: int | None,
) -> OutboxResult:
    if not body.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "请输入邮件正文")
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "回复表单已失效，请重新提交")
    case = get_case(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
    _require_version(case, expected_case_version, required=True)
    if case.status in FINISHED_CASE_STATUSES or not case.human_takeover:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "请在办理中的案件接管后发送顾问回复")
    email = normalize_email(case.customer_email)
    if not email:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "案件缺少客户邮箱")
    mail = None
    if reply_event_id:
        matches = list_customer_mail(settings, case, event_id=reply_event_id)
        if not matches:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "原邮件已变更，请按当前往来邮件重新核对")
        mail = matches[0]
    parent = single_message_id(mail.message_id) if mail else None
    return enqueue_and_maybe_send(
        settings=settings,
        req=OutboxRequest(
            kind=OutboundMessageKind.ORDINARY_REPLY,
            recipients=(email,),
            subject=takeover_reply_subject(mail),
            body_text=body,
            send_operation_id=f"{TAKEOVER_REPLY_OPERATION_PREFIX}{settings.tenant_id}:{case_id}:{request_id}",
            case_id=case_id,
            case_version=case.case_version,
            in_reply_to=parent,
            references=reply_references(
                parent_id=parent, references=mail.references, parent_in_reply_to=mail.in_reply_to,
            ) if parent and mail else (),
            advisor_authored=True,
        ),
    )


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
    if out.status != "provider_accepted" or out.block_reasons:
        return AdvisorReplyResult(outbox=out, closed_question=False, language=lang)
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
