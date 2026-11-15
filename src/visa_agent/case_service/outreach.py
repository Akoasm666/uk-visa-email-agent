"""Case opening outreach — fixed bilingual template; agent does not free-write."""

from __future__ import annotations

from dataclasses import dataclass

from visa_agent.case_service.service import CaseRecord, attribute_inbound_to_case, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus, ErrorCode, OutboundMessageKind, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.outbox import OutboxResult, OutboxRequest, enqueue_and_maybe_send
from visa_agent.rules.seed import (
    GRADUATE_REQUIREMENTS,
    SKILLED_WORKER_REQUIREMENTS,
    STUDENT_REQUIREMENTS,
    VISITOR_REQUIREMENTS,
)

_ROUTE_ZH: dict[str, str] = {
    RouteCode.STANDARD_VISITOR.value: "访问签证（Visitor）",
    RouteCode.STUDENT.value: "学生签证",
    RouteCode.SKILLED_WORKER.value: "技术工签",
    RouteCode.GRADUATE.value: "毕业生签证",
}

_ROUTE_EN: dict[str, str] = {
    RouteCode.STANDARD_VISITOR.value: "Standard Visitor visa",
    RouteCode.STUDENT.value: "Student visa",
    RouteCode.SKILLED_WORKER.value: "Skilled Worker visa",
    RouteCode.GRADUATE.value: "Graduate visa",
}

# Customer-facing checklist lines: (zh, en). Skip internal transfer triggers.
_MATERIAL_LINES: dict[str, tuple[str, str]] = {
    "passport_valid": (
        "护照资料页清晰彩色扫描件 / 照片（须在有效期内）",
        "Clear colour scan/photo of your passport bio-data page (must be valid)",
    ),
    "visit_purpose_allowed": (
        "访问目的说明（旅游 / 探亲 / 商务等，一两段即可）",
        "Brief visit purpose (tourism / family / business, a short paragraph is fine)",
    ),
    "intention_to_leave": (
        "行程或离境安排说明（机票意向、停留天数、回国计划）",
        "Travel / departure plans (intended stay, return plans; tickets if you have them)",
    ),
    "funds_for_stay": (
        "资金证明（近几个月银行流水或存款证明；勿发密码）",
        "Funds evidence (recent bank statements or savings proof; never send passwords)",
    ),
    "home_ties": (
        "国内约束力证明（在职证明 / 学籍 / 营业证明等，如有）",
        "Home ties (employment / study / business evidence, if available)",
    ),
    "sponsor_if_any": (
        "如有担保人：担保说明、关系证明、担保人资金与在英身份材料",
        "If sponsored: sponsor letter, relationship proof, sponsor funds & UK status",
    ),
    "translation_if_needed": (
        "非英文材料的完整翻译件（含译者姓名、签名、日期、联系方式）",
        "Full translation of non-English documents (translator name, signature, date, contact)",
    ),
    "cas_confirmation": (
        "CAS（Confirmation of Acceptance for Studies）确认函",
        "CAS (Confirmation of Acceptance for Studies)",
    ),
    "funds_maintenance": (
        "学费与生活费资金证明（按学生签证要求，勿套用访问签公式）",
        "Course fee & living-cost funds (Student route rules; not Visitor formulas)",
    ),
    "english_language": (
        "英语能力证明（如适用）",
        "English language evidence (if required)",
    ),
    "course_offer": (
        "课程录取 / 课程说明材料",
        "Course offer / academic course details",
    ),
    "tuberculosis_test_if_required": (
        "TB 体检证明（如您国籍/居住地需要）",
        "TB test certificate (if required for your nationality / residence)",
    ),
    "certificate_of_sponsorship": (
        "担保证书 CoS（Certificate of Sponsorship）",
        "Certificate of Sponsorship (CoS)",
    ),
    "job_skill_level": (
        "职位与技能等级相关说明 / 合同要点",
        "Job / skill-level evidence or contract summary",
    ),
    "salary_threshold": (
        "薪资达标证明（雇佣合同或工资单等）",
        "Salary evidence (contract or payslips meeting the threshold)",
    ),
    "uk_study_completed": (
        "英国学业已完成的证明",
        "Evidence that eligible UK study was completed",
    ),
    "qualification_confirmed": (
        "学历 / 学位确认材料",
        "Qualification / award confirmation",
    ),
    "current_permission_basis": (
        "当前在英身份 / 可转换依据说明",
        "Current UK permission / switching basis",
    ),
}

_SEED_BY_ROUTE = {
    RouteCode.STANDARD_VISITOR.value: VISITOR_REQUIREMENTS,
    RouteCode.STUDENT.value: STUDENT_REQUIREMENTS,
    RouteCode.SKILLED_WORKER.value: SKILLED_WORKER_REQUIREMENTS,
    RouteCode.GRADUATE.value: GRADUATE_REQUIREMENTS,
}


@dataclass(frozen=True, slots=True)
class OpeningOutreachResult:
    attempted: bool
    sent: bool
    skipped_reason: str | None
    outbox: OutboxResult | None


def _route_labels(case: CaseRecord) -> tuple[str, str]:
    if not case.route:
        return "签证办理", "visa application"
    code = case.route.value
    return (
        _ROUTE_ZH.get(code, code),
        _ROUTE_EN.get(code, code),
    )


def _material_checklist(case: CaseRecord) -> tuple[list[str], list[str]]:
    route = case.route.value if case.route else RouteCode.STANDARD_VISITOR.value
    seeds = _SEED_BY_ROUTE.get(route, VISITOR_REQUIREMENTS)
    zh_lines: list[str] = []
    en_lines: list[str] = []
    for req in seeds:
        if req.severity == "transfer_human" or req.key not in _MATERIAL_LINES:
            continue
        zh, en = _MATERIAL_LINES[req.key]
        zh_lines.append(f"  {len(zh_lines) + 1}. {zh}")
        en_lines.append(f"  {len(en_lines) + 1}. {en}")
    return zh_lines, en_lines


def build_opening_email(case: CaseRecord) -> tuple[str, str]:
    """Fixed bilingual template (EN then ZH). Not model-generated."""
    title = (case.title or "您的签证办理 / your visa case").strip()
    visa_zh, visa_en = _route_labels(case)
    ref = case.id[:8]
    zh_items, en_items = _material_checklist(case)
    zh_list = "\n".join(zh_items) if zh_items else "  （清单稍后由顾问补充）"
    en_list = "\n".join(en_items) if en_items else "  (checklist to follow from your advisor)"

    subject = f"[Visa Assist] Case opened · {title} · {ref}"
    body = f"""Hello,

We have opened your case 「{title}」 for a {visa_en}.

Please reply to this email (keep the same thread) and attach clear scans/photos of the documents below. You may send them in several replies.

Documents we need:
{en_list}

How to reply:
- Questions only — just write in the email body.
- Documents only — attach files and name them clearly (e.g. passport.jpg, bank_statement.pdf).
- Questions + documents — both in the same reply is fine.

Case reference: {ref}
Please reply to this message and do not change the recipient.

---

您好，

我们已为您开通案件「{title}」（{visa_zh}）。

请直接回复本邮件（保持同一邮件线程），并按下列清单附上清晰扫描件/照片。材料可以分多封发送。

请您准备并提交：
{zh_list}

回复方式：
- 只问问题：直接写在邮件正文即可。
- 只交材料：添加附件，文件名尽量写清楚（如 passport.jpg、银行流水.pdf）。
- 问题 + 材料：同一封回复即可。

案件编号：{ref}
请直接回复本邮件，勿更改收件人。
"""
    return subject, body.strip() + "\n"


def send_opening_outreach(
    *,
    settings: Settings,
    case: CaseRecord,
    send_now: bool = True,
) -> OpeningOutreachResult:
    """Send the first agent→customer email after case create.

    Idempotent per case via stable ``send_operation_id``. Missing email or
    allowlist/SMTP blocks are reported; they do not roll back case creation.
    """
    email = (case.customer_email or "").strip().lower()
    if not email:
        return OpeningOutreachResult(
            attempted=False,
            sent=False,
            skipped_reason="missing_customer_email",
            outbox=None,
        )

    subject, body = build_opening_email(case)
    op_id = f"case-opening:{settings.tenant_id}:{case.id}"
    result = enqueue_and_maybe_send(
        settings=settings,
        req=OutboxRequest(
            kind=OutboundMessageKind.ORDINARY_REPLY.value,
            recipients=(email,),
            subject=subject,
            body_text=body,
            send_operation_id=op_id,
            send_now=send_now,
            case_id=case.id,
            case_version=case.case_version,
        ),
    )
    sent = result.status in {"provider_accepted", "pending", "status_unknown"} and not (
        result.block_reasons
    )
    if sent:
        _mark_waiting_customer(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case.id,
        )
        return OpeningOutreachResult(
            attempted=True, sent=True, skipped_reason=None, outbox=result
        )

    reason = ",".join(result.block_reasons) if result.block_reasons else result.status
    return OpeningOutreachResult(
        attempted=True,
        sent=False,
        skipped_reason=reason or "send_blocked",
        outbox=result,
    )


def _mark_waiting_customer(
    *, database_url: str, tenant_id: str, case_id: str
) -> None:
    import psycopg

    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            UPDATE cases
            SET waiting_customer = TRUE, updated_at = NOW()
            WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, case_id),
        )
        conn.commit()


def try_auto_attribute_inbound_by_customer_email(
    *,
    settings: Settings,
    email_event_id: str,
    from_addr: str,
) -> str | None:
    """If exactly one open case matches the sender email, bind the inbound.

    Returns attributed case_id, or None when ambiguous / unmatched.
    """
    import psycopg

    sender = (from_addr or "").strip().lower()
    if not sender or "@" not in sender:
        return None
    # Strip display-name wrappers: "Name <a@b.com>"
    if "<" in sender and ">" in sender:
        sender = sender[sender.rfind("<") + 1 : sender.rfind(">")].strip()

    closed = {CaseStatus.CLOSED.value, CaseStatus.DELIVERED.value}
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT id FROM cases
            WHERE tenant_id = %s
              AND lower(trim(customer_email)) = %s
              AND status <> ALL(%s)
            ORDER BY updated_at DESC
            LIMIT 3
            """,
            (settings.tenant_id, sender, list(closed)),
        ).fetchall()

    if len(rows) != 1:
        return None
    case_id = rows[0][0]
    try:
        attribute_inbound_to_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            email_event_id=email_event_id,
            case_id=case_id,
            reason="auto_match_customer_email",
        )
    except DomainError as exc:
        if exc.code == ErrorCode.NOT_FOUND:
            return None
        # Already attributed elsewhere / conflict — leave for human.
        return None
    return case_id


def require_customer_email(value: str | None) -> str:
    email = (value or "").strip()
    if not email or "@" not in email:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="开案必须填写客户邮箱，以便系统先发信联系客户",
        )
    return email
