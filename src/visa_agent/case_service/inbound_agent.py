"""Process attributed customer inbound: classify → quality → remediate → reply."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import psycopg

from visa_agent.ai_capabilities.customer_reply import (
    MaterialLine,
    answer_customer_question,
    build_status_reply,
    strip_model_noise,
)
from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.case_service.outreach import _MATERIAL_LINES, _SEED_BY_ROUTE
from visa_agent.case_service.remediation import (
    ADVISOR_CUSTOMER_QUESTION,
    ADVISOR_UNCLASSIFIED,
    close_remediation,
    list_open_questions,
    open_remediation,
    open_requirement_keys,
)
from visa_agent.case_service.service import (
    CaseRecord,
    get_case,
    mark_inbound_processed,
)
from visa_agent.config import Settings
from visa_agent.document_pipeline.classify import (
    classify_attachment,
    requirement_for_classification,
)
from visa_agent.document_pipeline.quality import review_attachment_quality
from visa_agent.document_pipeline.service import (
    attach_evidence,
    list_evidence,
    run_ocr_on_evidence,
    set_evidence_classification,
)
from visa_agent.domain.enums import ErrorCode, OutboundMessageKind, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.inbound import (
    InboundAttachment,
    attachments_from_payload,
)
from visa_agent.email_adapter.outbox import OutboxRequest, enqueue_and_maybe_send
from visa_agent.storage.files import LocalFileStore

_QUESTION_HINTS = re.compile(
    r"(\?|？|吗|呢|请问|如何|怎么|什么时候|能否|可以吗|why|how|when|what|please\s+advise)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class AttachedItem:
    filename: str
    classification: str
    requirement_key: str | None
    evidence_id: str | None
    quarantine_status: str
    ocr_status: str
    issue: str | None
    quality_code: str = "ok"
    quality_severity: str = "simple_auto"


@dataclass(frozen=True, slots=True)
class InboundProcessResult:
    case_id: str
    email_event_id: str
    had_question: bool
    language: str
    attached: tuple[AttachedItem, ...]
    missing_titles_zh: tuple[str, ...]
    missing_titles_en: tuple[str, ...]
    reply_sent: bool
    reply_skipped_reason: str | None
    conclusion: str
    remediations_opened: tuple[str, ...] = ()
    remediations_closed: tuple[str, ...] = ()
    question_answered: bool = False


def _load_inbound_payload(
    *,
    settings: Settings,
    email_event_id: str,
) -> tuple[str, str, str, tuple[InboundAttachment, ...]]:
    store = LocalFileStore(settings.file_storage_dir)
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            """
            SELECT from_addr, subject, body_text, raw_file_id, case_id,
                   attribution_status, processing_status
            FROM email_events
            WHERE tenant_id = %s AND id = %s AND direction = 'inbound'
            """,
            (settings.tenant_id, email_event_id),
        ).fetchone()
    if row is None:
        raise DomainError(
            code=ErrorCode.NOT_FOUND,
            message="inbound email event not found",
            details=(email_event_id,),
        )
    from_addr, subject, body_text, raw_file_id, case_id, attribution, processing = row
    if (attribution or "") != "attributed" or not case_id:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="inbound must be attributed before agent processing",
            details=(attribution or "",),
        )
    if (processing or "") == "processed":
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="inbound already processed",
            details=(email_event_id,),
        )

    attachments: tuple[InboundAttachment, ...] = ()
    if raw_file_id:
        path = store.root / f"{raw_file_id}.json"
        if path.is_file():
            blob = json.loads(path.read_text(encoding="utf-8"))
            attachments = attachments_from_payload(blob.get("attachments"))
            body_text = body_text or blob.get("body_text") or ""
            subject = subject or blob.get("subject") or ""
            from_addr = from_addr or blob.get("from_addr") or ""
    return str(from_addr or ""), str(subject or ""), str(body_text or ""), attachments


def _looks_like_question(body: str) -> bool:
    text = (body or "").strip()
    if len(text) < 2:
        return False
    if re.fullmatch(
        r"(please\s+find\s+attach(ed)?\.?|见附件|附件请查收|请查收附件)\s*",
        text,
        flags=re.I,
    ):
        return False
    return bool(_QUESTION_HINTS.search(text)) or len(text) >= 40


def _label_for(req_key: str | None, classification: str, language: str) -> str:
    pair = _MATERIAL_LINES.get(req_key or "")
    if pair:
        return pair[0] if language == "zh" else pair[1]
    return classification


def _missing_materials(
    case: CaseRecord,
    evidence_items: list,
    *,
    open_keys: set[str],
    language: str,
) -> list[str]:
    route = case.route.value if case.route else RouteCode.STANDARD_VISITOR.value
    seeds = _SEED_BY_ROUTE.get(route, ())
    present_reqs: set[str] = set()
    for ev in evidence_items:
        if (getattr(ev, "quarantine_status", "") or "") == "rejected":
            continue
        cls = (getattr(ev, "classification", "") or "").lower()
        key = requirement_for_classification(cls)
        if key and key not in open_keys:
            present_reqs.add(key)
    out: list[str] = []
    for req in seeds:
        if req.severity == "transfer_human":
            continue
        if req.key in present_reqs:
            continue
        # Open remediations are "issues", not "missing".
        if req.key in open_keys:
            continue
        pair = _MATERIAL_LINES.get(req.key)
        if not pair:
            continue
        out.append(pair[0] if language == "zh" else pair[1])
    return out


def _try_evaluate(settings: Settings, case_id: str) -> None:
    try:
        from visa_agent.rules.service import evaluate_case_requirements

        evaluate_case_requirements(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError:
        # No active pack / route mismatch — checklist still tracked via remediations.
        return


def process_customer_inbound(
    *,
    settings: Settings,
    case_id: str,
    email_event_id: str,
    send_reply: bool = True,
) -> InboundProcessResult:
    """Attach → classify → quality review → open/close补件 → monolingual reply."""
    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    _from, subject, body, attachments = _load_inbound_payload(
        settings=settings, email_event_id=email_event_id
    )
    language = detect_reply_language(body, subject, *(a.filename for a in attachments))
    had_question = _looks_like_question(body)
    store = LocalFileStore(settings.file_storage_dir)
    attached: list[AttachedItem] = []
    opened_keys: list[str] = []
    closed_labels: list[str] = []

    for att in attachments:
        cls, req_key = classify_attachment(filename=att.filename, ocr_text="")
        issue: str | None = None
        evidence_id: str | None = None
        q_status = "isolated"
        ocr_status = ""
        ocr_text = ""
        quality_code = "ok"
        quality_severity = "simple_auto"
        try:
            live = get_case(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
            )
            ev = attach_evidence(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                file_store=store,
                data=att.data,
                original_filename=att.filename,
                content_type=att.content_type or "application/octet-stream",
                classification=cls,
                source_email_event_id=email_event_id,
                expected_case_version=live.case_version,
            )
            evidence_id = ev.id
            q_status = ev.quarantine_status
            if q_status == "rejected":
                issue = ev.rejection_reason or "file type not allowed"
                quality_code = "rejected"
            else:
                try:
                    ocr = run_ocr_on_evidence(
                        database_url=settings.database_url,
                        tenant_id=settings.tenant_id,
                        case_id=case_id,
                        evidence_id=ev.id,
                        file_store=store,
                        expected_case_version=ev.case_version,
                    )
                    ocr_status = ocr.status
                    ocr_text = ocr.extracted_text or ""
                    if cls == "unclassified" and ocr_text:
                        new_cls, new_req = classify_attachment(
                            filename=att.filename,
                            ocr_text=ocr_text,
                        )
                        if new_cls != "unclassified":
                            cls, req_key = new_cls, new_req
                            set_evidence_classification(
                                database_url=settings.database_url,
                                tenant_id=settings.tenant_id,
                                case_id=case_id,
                                evidence_id=ev.id,
                                classification=cls,
                            )
                except DomainError as exc:
                    ocr_status = "failed"
                    issue = str(exc.message)

                finding = review_attachment_quality(
                    filename=att.filename,
                    data=att.data,
                    ocr_text=ocr_text,
                    ocr_status=ocr_status,
                    classification=cls,
                )
                quality_code = finding.code
                quality_severity = finding.severity
                if finding.code != "ok":
                    issue = (
                        finding.detail_zh if language == "zh" else finding.detail_en
                    )
        except DomainError as exc:
            if exc.code == ErrorCode.ATTACHMENT_REJECTED and "identical" in (
                exc.message or ""
            ):
                issue = None
                q_status = "isolated"
                cls, req_key = classify_attachment(
                    filename=att.filename, ocr_text=""
                )
            else:
                issue = exc.message
                q_status = "rejected"
                quality_code = "rejected"

        # Open / close remediation against checklist slot (or advisor triage).
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
        remed_key = req_key
        if quality_severity == "complex_human" and not remed_key:
            remed_key = ADVISOR_UNCLASSIFIED
        if (
            remed_key
            and evidence_id
            and quality_code not in {"ok", "rejected"}
            and issue
        ):
            open_remediation(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                requirement_key=remed_key,
                reason_code=quality_code,
                reason_detail=issue,
                severity=quality_severity,
                evidence_id=evidence_id,
                source_email_event_id=email_event_id,
                case_version=case.case_version,
            )
            opened_keys.append(remed_key)
        elif (
            req_key
            and evidence_id
            and quality_code == "ok"
            and q_status != "rejected"
        ):
            closed = close_remediation(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                requirement_key=req_key,
                closed_by_evidence_id=evidence_id,
            )
            if closed:
                closed_labels.append(
                    _label_for(req_key, cls, language)
                )

        attached.append(
            AttachedItem(
                filename=att.filename,
                classification=cls,
                requirement_key=req_key,
                evidence_id=evidence_id,
                quarantine_status=q_status,
                ocr_status=ocr_status,
                issue=issue,
                quality_code=quality_code,
                quality_severity=quality_severity,
            )
        )

    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    open_keys = open_requirement_keys(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    evidence_items = list_evidence(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    missing = _missing_materials(
        case, evidence_items, open_keys=open_keys, language=language
    )

    question_block: str | None = None
    question_answered = False
    if had_question:
        qa = answer_customer_question(
            settings=settings,
            case=case,
            question=body,
            language=language,
        )
        question_block = strip_model_noise(qa.text)
        question_answered = True
        if qa.needs_advisor:
            detail = (
                f"客户提问需顾问跟进：{(body or '')[:400]}"
                if language == "zh"
                else f"Customer question needs advisor: {(body or '')[:400]}"
            )
            open_remediation(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                requirement_key=ADVISOR_CUSTOMER_QUESTION,
                reason_code="customer_question_needs_advisor",
                reason_detail=detail,
                severity="complex_human",
                source_email_event_id=email_event_id,
                case_version=case.case_version,
            )
            opened_keys.append(ADVISOR_CUSTOMER_QUESTION)

    received_lines: list[MaterialLine] = []
    issue_lines: list[MaterialLine] = []
    for item in attached:
        label = _label_for(item.requirement_key, item.classification, language)
        if item.issue or item.quarantine_status == "rejected":
            issue_lines.append(
                MaterialLine(
                    filename=item.filename, label=label, issue=item.issue
                )
            )
        else:
            received_lines.append(MaterialLine(filename=item.filename, label=label))

    # Surface still-open remediations not covered by this mail's attachments.
    for q in list_open_questions(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        active_only=True,
    ):
        if q.requirement_key in {i.requirement_key for i in attached if i.issue}:
            continue
        if q.severity == "complex_human":
            continue
        label = _label_for(q.requirement_key, q.requirement_key, language)
        issue_lines.append(
            MaterialLine(
                filename=label,
                label=label,
                issue=q.reason_detail or q.reason_code,
            )
        )

    conclusion = "material_change" if attachments else "acknowledged"
    if opened_keys or closed_labels:
        conclusion = "material_change"

    reply_sent = False
    reply_skip: str | None = None
    if send_reply:
        email = (case.customer_email or "").strip().lower()
        if not email:
            reply_skip = "missing_customer_email"
        else:
            subject_out, reply_body = build_status_reply(
                language=language,
                case=case,
                received=received_lines,
                issues=issue_lines,
                missing=missing,
                question_block=question_block,
                remediations_closed=closed_labels or None,
            )
            out = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind=OutboundMessageKind.ORDINARY_REPLY.value,
                    recipients=(email,),
                    subject=subject_out,
                    body_text=reply_body,
                    send_operation_id=(
                        f"inbound-reply:{settings.tenant_id}:{email_event_id}"
                    ),
                    send_now=True,
                    case_id=case.id,
                    case_version=case.case_version,
                ),
            )
            reply_sent = out.status in {
                "provider_accepted",
                "pending",
                "status_unknown",
            } and not out.block_reasons
            if not reply_sent:
                reply_skip = (
                    ",".join(out.block_reasons) if out.block_reasons else out.status
                )

    _try_evaluate(settings, case_id)

    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    mark_inbound_processed(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        email_event_id=email_event_id,
        conclusion=conclusion,
        processed_by="inbound_agent",
        expected_case_version=case.case_version,
    )

    missing_other_lang = _missing_materials(
        case,
        evidence_items,
        open_keys=open_keys,
        language="en" if language == "zh" else "zh",
    )

    return InboundProcessResult(
        case_id=case_id,
        email_event_id=email_event_id,
        had_question=had_question,
        language=language,
        attached=tuple(attached),
        missing_titles_zh=tuple(missing if language == "zh" else missing_other_lang),
        missing_titles_en=tuple(missing if language == "en" else missing_other_lang),
        reply_sent=reply_sent,
        reply_skipped_reason=reply_skip,
        conclusion=conclusion,
        remediations_opened=tuple(dict.fromkeys(opened_keys)),
        remediations_closed=tuple(closed_labels),
        question_answered=question_answered,
    )


def process_inbound_after_attribution(
    *,
    settings: Settings,
    case_id: str,
    email_event_id: str,
) -> InboundProcessResult | None:
    """Best-effort agent loop after auto/manual attribution. Never raises to callers."""
    try:
        return process_customer_inbound(
            settings=settings,
            case_id=case_id,
            email_event_id=email_event_id,
            send_reply=True,
        )
    except DomainError:
        return None
