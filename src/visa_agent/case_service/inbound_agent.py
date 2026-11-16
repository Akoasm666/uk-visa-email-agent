"""Process attributed customer inbound: classify → quality → remediate → reply."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime

import psycopg

from visa_agent.ai_capabilities.attachment_classification import assess_attachment_photo, assess_attachment_text
from visa_agent.ai_capabilities.customer_reply import (
    QUESTION_FOLLOW_UP,
    MaterialLine,
    answer_customer_question,
    build_status_reply,
    render_reply_reference,
    requests_reference,
)
from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.ai_capabilities.visitor_trip import extract_visitor_trip
from visa_agent.case_service.delivery_events import acknowledge_delivery_from_reply
from visa_agent.case_service.itinerary import get_case_itinerary, prepare_itinerary_reply, record_itinerary_sent
from visa_agent.case_service.visitor_purpose import (
    PURPOSE_LABELS, get_stated_visitor_purpose, record_visitor_purpose, stated_visit_purpose,
)
from visa_agent.case_service.material_confirmation import (
    advance_material_confirmation,
    confirm_materials_from_reply,
    is_current_material_confirmation_reply,
)
from visa_agent.case_service.outreach import (
    _MATERIAL_LINES,
    _SEED_BY_ROUTE,
    try_auto_attribute_inbound_by_customer_email,
)
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
    FINISHED_CASE_STATUSES,
    get_case,
    mark_inbound_processed,
)
from visa_agent.config import Settings
from visa_agent.document_pipeline.classify import (
    classify_attachment,
    requirement_for_classification,
)
from visa_agent.document_pipeline.ocr import (
    LocalScanOcrProvider,
    SyntheticOcrProvider,
    default_ocr_provider,
)
from visa_agent.document_pipeline.quality import (
    PassportReading,
    passport_fields_conflict,
    read_passport,
    review_attachment_quality,
    scan_review,
)
from visa_agent.document_pipeline.service import (
    attach_evidence,
    latest_ocr_by_evidence,
    list_evidence,
    list_ocr_results,
    run_ocr_on_evidence,
    set_evidence_classification,
    set_evidence_quarantine,
)
from visa_agent.domain.enums import CaseStatus, ErrorCode, OutboundMessageKind, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.inbound import (
    InboundAttachment,
    attachments_from_payload,
)
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.email_adapter.outbox import (
    INBOUND_REPLY_OPERATION_PREFIX,
    OutboxRequest,
    enqueue_and_maybe_send,
)
from visa_agent.email_adapter.threading import new_reply_body, reply_references, single_message_id
from visa_agent.storage.files import LocalFileStore

_QUESTION_HINTS = re.compile(
    r"[?？]|请问|如何|怎么|什么时候|为什么|能否|是否|多久|几个月|多少钱|(?:吗|呢)(?:[。！!]|$)"
    r"|\bplease\s+advise\b"
    r"|(?:^|[.!]\s+)(?:why|how|when|what|where|which|who|"
    r"(?:can|could|should|may|would)\s+(?:i|we|you))\b",
    re.I | re.M,
)
_MATERIAL_STATUS_REQUEST = re.compile(
    r"还(?:差|缺)|需要.{0,8}(?:哪些|什么)材料|what.{0,24}(?:else|missing)|"
    r"what (?:documents|materials) (?:do|should|are)|when can i submit", re.I,
)
_MATERIAL_CORRECTION = re.compile(
    r"更正|修改|改成|不对|有误|不一致|弄错|换成|应为|需要改|不正确|实际.{0,12}(?:是|为)|"
    r"\b(?:correction|corrected|wrong|inaccurate|mistake|change[sd]?|instead)\b",
    re.I,
)
_ATTACHMENT_TYPE_QUESTION = re.compile(
    r"^(?:请问)?(?:这(?:个|份|张)|我(?:刚)?上传的|附件里).{0,24}(?:算什么|是什么|属于什么)"
    r"(?:类型|文件|材料)?[？?\s]*$",
)
_STOP_DELIVERY = re.compile(
    r"(?:请)?(?:先别|先不要|暂时别|暂时不要|不要再|(?<!不要)(?<!无需)(?<!不必)(?:暂缓|暂停|停止))"
    r"(?:给我)?(?:发送|发出|发信|发邮件|发材料|发[，。！!\s]*$|交付)"
    r"|先别发(?=[，。！!\s])"
    r"|(?:发|发送|寄)(?:最终|那份|这份)?(?:材料|文件|资料).{0,16}(?:先缓|缓一下|等我.{0,8}再说)"
    r"|\b(?:do not send|don['’]t send|stop sending|hold off on sending|please hold (?:the|my) (?:package|delivery))\b",
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
) -> tuple[str, str, str, tuple[InboundAttachment, ...], str | None, tuple[str, ...], str | None, datetime]:
    store = LocalFileStore(settings.file_storage_dir)
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            """
            SELECT from_addr, subject, body_text, raw_file_id, case_id,
                   attribution_status, processing_status,
                   rfc_message_id, in_reply_to, email_references, created_at
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
    (
        from_addr, subject, body_text, raw_file_id, case_id, attribution, processing,
        message_id, in_reply_to, references, received_at,
    ) = row
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
    parent_id = single_message_id(message_id)
    ancestry = (
        reply_references(
            parent_id=parent_id, references=references, parent_in_reply_to=in_reply_to
        )
        if parent_id else ()
    )
    return (
        str(from_addr or ""), str(subject or ""), str(body_text or ""), attachments,
        parent_id, ancestry, single_message_id(in_reply_to), received_at,
    )


def _looks_like_question(body: str) -> bool:
    return bool(_QUESTION_HINTS.search(body) or QUESTION_FOLLOW_UP.search(body) or requests_reference(body))


def _thread_history(
    settings: Settings, case_id: str, event_id: str, sender: str, references: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    if not references:
        return ()
    with psycopg.connect(settings.database_url) as conn:
        # Mail relays can rewrite IDs in References. Also follow the parents
        # recorded on the local copy of each earlier message.
        rows = conn.execute(
            """
            WITH RECURSIVE matching AS NOT MATERIALIZED (
                SELECT id, direction, body_text, created_at, rfc_message_id, in_reply_to
                FROM email_events
                WHERE tenant_id = %s AND case_id = %s AND id <> %s
                  AND processing_status = 'processed'
                  AND (direction = 'outbound' OR lower(trim(from_addr)) = %s)
            ), ancestors AS (
                SELECT id, in_reply_to, 1 AS depth FROM matching WHERE rfc_message_id = ANY(%s)
                UNION
                SELECT e.id, e.in_reply_to, a.depth + 1
                FROM ancestors a JOIN matching e ON e.rfc_message_id = a.in_reply_to
                WHERE a.depth < 8
            )
            SELECT direction, body_text FROM matching
            WHERE id IN (SELECT id FROM ancestors)
            ORDER BY created_at DESC, id DESC LIMIT 12
            """,
            (settings.tenant_id, case_id, event_id, normalize_email(sender), list(references)),
        ).fetchall()
    return tuple((direction, new_reply_body(text or "")[:650])
                 for direction, text in reversed(rows) if text)


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
    known_purpose: bool = False,
) -> list[str]:
    route = case.route.value if case.route else RouteCode.STANDARD_VISITOR.value
    seeds = _SEED_BY_ROUTE.get(route, ())
    present_reqs: set[str] = set()
    for ev in evidence_items:
        if (getattr(ev, "quarantine_status", "") or "") == "rejected":
            continue
        cls = (getattr(ev, "classification", "") or "").lower()
        key = requirement_for_classification(cls, route=case.route)
        if key and key not in open_keys:
            present_reqs.add(key)
    out: list[str] = []
    for req in seeds:
        if req.severity == "transfer_human":
            continue
        if route == RouteCode.STANDARD_VISITOR.value and req.key in {"translation_if_needed", "intention_to_leave"}:
            continue
        if known_purpose and req.key == "visit_purpose_allowed":
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


def _review_attachment(
    *,
    settings: Settings,
    case: CaseRecord,
    store: LocalFileStore,
    email_event_id: str,
    attachment: InboundAttachment,
    language: str,
) -> tuple[AttachedItem, PassportReading | None]:
    filename = attachment.filename
    case_args = {
        "database_url": settings.database_url,
        "tenant_id": settings.tenant_id,
        "case_id": case.id,
    }
    live = get_case(**case_args)
    try:
        evidence = attach_evidence(
            **case_args,
            file_store=store,
            data=attachment.data,
            original_filename=filename,
            content_type=attachment.content_type or "application/octet-stream",
            source_email_event_id=email_event_id,
            expected_case_version=live.case_version,
            reuse_same_source=True,
        )
    except DomainError as exc:
        if exc.code != ErrorCode.ATTACHMENT_REJECTED:
            raise
        if "identical" in exc.message:
            return AttachedItem(filename, "unclassified", None, None, "isolated", "", None,
                                quality_code="duplicate"), None
        return AttachedItem(filename, "unclassified", None, None, "rejected", "", exc.message,
                            quality_code="rejected"), None
    if evidence.quarantine_status == "rejected":
        return AttachedItem(filename, "unclassified", None, evidence.id, "rejected", "",
                            evidence.rejection_reason or "file type not allowed",
                            quality_code="rejected"), None

    ocr_args = dict(**case_args, evidence_id=evidence.id)
    provider = default_ocr_provider()
    local_model = isinstance(provider, LocalScanOcrProvider)
    assessment = assess_attachment_photo(attachment.data) if local_model else None
    previous = [result for result in list_ocr_results(**ocr_args) if result.provider == provider.name]
    ocr = previous[0] if previous else run_ocr_on_evidence(
        **ocr_args,
        file_store=store,
        expected_case_version=get_case(**case_args).case_version,
        provider=provider,
    )
    passport = None
    if isinstance(provider, SyntheticOcrProvider):
        classification, _ = classify_attachment(filename=filename, ocr_text=ocr.extracted_text)
        finding = review_attachment_quality(
            filename=filename,
            data=attachment.data,
            ocr_text=ocr.extracted_text,
            ocr_status=ocr.status,
            classification=classification,
        )
    else:
        if local_model and (assessment is None or assessment.classification == "unclassified"):
            assessment = assess_attachment_text(ocr.extracted_text) or assessment
        classification, finding, reading = scan_review(
            filename=filename,
            ocr_text=ocr.extracted_text,
            ocr_status=ocr.status,
            ocr_detail=ocr.detail,
            agent_classification=assessment.classification if assessment else "unclassified",
            agent_readability=assessment.readability if assessment else None,
            visitor_passport_auto=(
                case.route == RouteCode.STANDARD_VISITOR
                and not live.human_takeover
                and ocr.provider == LocalScanOcrProvider.name
            ),
        )
        if classification == "passport":
            passport = reading
    if classification != evidence.classification or assessment:
        set_evidence_classification(
            **ocr_args, classification=classification, document_language=assessment.language if assessment else None,
        )

    issue = finding.detail_zh if language == "zh" else finding.detail_en
    return AttachedItem(
        filename=filename,
        classification=classification,
        requirement_key=requirement_for_classification(classification, route=case.route),
        evidence_id=evidence.id,
        quarantine_status=evidence.quarantine_status,
        ocr_status=ocr.status,
        issue=issue or None,
        quality_code=finding.code,
        quality_severity=finding.severity,
    ), passport


def process_customer_inbound(
    *,
    settings: Settings,
    case_id: str,
    email_event_id: str,
    send_reply: bool = True,
) -> InboundProcessResult | None:
    """Attach → classify → quality review → open/close补件 → monolingual reply."""
    with psycopg.connect(settings.database_url, autocommit=True) as lock:
        lock.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (f"customer-inbound:{settings.tenant_id}:{case_id}",),
        )
        row = lock.execute(
            """
            SELECT case_id, processing_status FROM email_events
            WHERE tenant_id = %s AND id = %s AND direction = 'inbound'
            """,
            (settings.tenant_id, email_event_id),
        ).fetchone()
        if row is None or row[0] != case_id:
            raise DomainError(ErrorCode.NOT_FOUND, "inbound email not found on case")
        if row[1] == "processed":
            return None
        return _process_customer_inbound(
            settings=settings, case_id=case_id, email_event_id=email_event_id,
            send_reply=send_reply,
        )


def _process_customer_inbound(
    *, settings: Settings, case_id: str, email_event_id: str, send_reply: bool,
) -> InboundProcessResult:
    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    if case.status == CaseStatus.CLOSED:
        mark_inbound_processed(
            database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id,
            email_event_id=email_event_id, conclusion="acknowledged", processed_by="inbound_agent",
            expected_case_version=case.case_version,
        )
        return InboundProcessResult(
            case_id=case_id, email_event_id=email_event_id, had_question=False, language="en",
            attached=(), missing_titles_zh=(), missing_titles_en=(), reply_sent=False,
            reply_skipped_reason="case_closed", conclusion="acknowledged",
        )
    sender, subject, body, attachments, parent_message_id, references, reply_to, received_at = _load_inbound_payload(
        settings=settings, email_event_id=email_event_id
    )
    body = new_reply_body(body)
    language = detect_reply_language(body, subject, *(a.filename for a in attachments))
    had_question = _looks_like_question(body)
    material_correction = bool(
        _MATERIAL_CORRECTION.search(body)
        and is_current_material_confirmation_reply(settings, case, sender=sender, reply_to=reply_to)
    )
    is_customer = bool(case.customer_email and normalize_email(sender) == normalize_email(case.customer_email))
    stop_delivery = is_customer and bool(_STOP_DELIVERY.search(body))
    operation_id = f"{INBOUND_REPLY_OPERATION_PREFIX}{settings.tenant_id}:{email_event_id}"
    frozen = _saved_reply(settings, case_id, operation_id) if send_reply and not stop_delivery else None
    if frozen:
        reply_sent, reply_skip = _dispatch_reply(settings, frozen, email_event_id)
        conclusion = "material_change" if attachments or had_question or material_correction else "acknowledged"
        _finish_inbound(settings, case_id, email_event_id, conclusion)
        advance_material_confirmation(settings, case_id)
        return InboundProcessResult(
            case_id=case_id, email_event_id=email_event_id, had_question=had_question,
            language=language, attached=(), missing_titles_zh=(), missing_titles_en=(),
            reply_sent=reply_sent, reply_skipped_reason=reply_skip, conclusion=conclusion,
        )
    trip = None
    received_on = received_at.astimezone().date()
    visitor = case.route == RouteCode.STANDARD_VISITOR
    if is_customer and not stop_delivery and not case.human_takeover:
        current_itinerary = (
            get_case_itinerary(settings=settings, case_id=case_id)
            if visitor and settings.model_configured else None
        )
        trip = extract_visitor_trip(
            settings=settings, body=body, received_on=received_on,
            awaiting_travel_details=bool(current_itinerary and current_itinerary.awaiting_details),
            is_visitor=visitor,
        )
        stop_delivery = bool(trip and trip.stop_delivery)
        if trip is None and body.strip() and settings.model_configured and case.status == CaseStatus.READY_TO_DELIVER:
            # Retain the unprocessed inbound gate until this email can be
            # understood or an advisor records its conclusion.
            raise DomainError(ErrorCode.SYSTEM_BLOCKED, "customer delivery reply awaits interpretation")
    if stop_delivery:
        _finish_inbound(settings, case_id, email_event_id, "stop_delivery")
        return InboundProcessResult(
            case_id=case_id, email_event_id=email_event_id, had_question=False, language=language,
            attached=(), missing_titles_zh=(), missing_titles_en=(), reply_sent=False,
            reply_skipped_reason="stop_delivery", conclusion="stop_delivery",
        )
    had_question = had_question or bool(trip and trip.question)
    reply_keys = dict(event_id=email_event_id, sender=sender, body=body, reply_to=reply_to)
    if not attachments and (
        acknowledge_delivery_from_reply(settings, case_id, **reply_keys,
                                        semantic_receipt=bool(trip and trip.delivery_receipt))
        or (not material_correction and confirm_materials_from_reply(
            settings, case_id, **reply_keys, semantic_confirmation=bool(trip and trip.material_confirmation)))
    ):
        return InboundProcessResult(
            case_id=case_id, email_event_id=email_event_id, had_question=False,
            language=language, attached=(), missing_titles_zh=(), missing_titles_en=(),
            reply_sent=False, reply_skipped_reason=None, conclusion="acknowledged",
        )
    purpose = None
    if visitor:
        if case.status not in FINISHED_CASE_STATUSES and not case.human_takeover and is_customer:
            stated = trip.purpose if trip is not None else stated_visit_purpose(body)
            record_visitor_purpose(settings, case_id, email_event_id, stated)
        with psycopg.connect(settings.database_url) as conn:
            purpose = get_stated_visitor_purpose(conn, settings.tenant_id, case_id)
    store = LocalFileStore(settings.file_storage_dir)
    attached: list[AttachedItem] = []
    opened_keys: list[str] = []
    closed_labels: list[str] = []

    reviewed = []
    seen_evidence = set()
    for attachment in attachments:
        item, reading = _review_attachment(
            settings=settings, case=case, store=store, email_event_id=email_event_id,
            attachment=attachment, language=language,
        )
        if item.evidence_id:
            if item.evidence_id in seen_evidence:
                continue
            seen_evidence.add(item.evidence_id)
        reviewed.append((item, reading))
    question_args = {
        "database_url": settings.database_url,
        "tenant_id": settings.tenant_id,
        "case_id": case_id,
    }
    active = {q.requirement_key: q for q in list_open_questions(**question_args)}
    passport_question = active.get("passport_valid")
    prior_scan = None
    if any(passport for _, passport in reviewed):
        latest_ocr = latest_ocr_by_evidence(**question_args)
        if passport_question:
            prior = latest_ocr.get(passport_question.evidence_id)
            if prior and prior.provider == LocalScanOcrProvider.name and prior.status == "completed":
                prior_scan = read_passport(prior.extracted_text)
        accepted_passports = [
            read_passport(latest_ocr[ev.id].extracted_text)
            for ev in list_evidence(**question_args)
            if ev.classification == "passport" and ev.quarantine_status == "accepted_for_review"
            and ev.id in latest_ocr and latest_ocr[ev.id].provider == LocalScanOcrProvider.name
            and latest_ocr[ev.id].status == "completed"
        ]
    else:
        accepted_passports = []

    for item, passport in reviewed:
        remed_key = item.requirement_key
        if not remed_key and item.quality_severity == "complex_human":
            remed_key = ADVISOR_UNCLASSIFIED
        current_question = active.get(remed_key)
        if passport:
            other_passports = [other for other_item, other in reviewed
                               if other and other_item.evidence_id != item.evidence_id]
            compared = [*accepted_passports, *other_passports]
            if prior_scan:
                compared.append(prior_scan)
            if any(passport_fields_conflict(passport, other) for other in compared):
                item = replace(
                    item, quality_code="passport_data_conflict", quality_severity="complex_human",
                    issue=("与案件中另一份护照的身份字段不一致。" if language == "zh"
                           else "Passport fields disagree with another scan in this case."),
                )
            elif item.quality_code == "ok" and current_question and not (
                prior_scan and prior_scan.fields.get("number") == passport.fields.get("number")
            ):
                item = replace(
                    item, quality_code="passport_identity_uncertain", quality_severity="complex_human",
                    issue=("无法确认与待补件为同一证件。" if language == "zh"
                           else "The replacement passport could not be matched to the earlier scan."),
                )
        if current_question and current_question.severity == "complex_human":
            item = replace(
                item, quality_code=current_question.reason_code, quality_severity="complex_human",
                issue=current_question.reason_detail,
            )
        if remed_key and item.issue and item.quality_code not in {"ok", "duplicate"}:
            if not current_question or current_question.severity != "complex_human":
                live = get_case(**question_args)
                active[remed_key] = open_remediation(
                    **question_args, requirement_key=remed_key, reason_code=item.quality_code,
                    reason_detail=item.issue, severity=item.quality_severity,
                    evidence_id=item.evidence_id, source_email_event_id=email_event_id,
                    case_version=live.case_version,
                )
                opened_keys.append(remed_key)
        elif remed_key and item.evidence_id and item.quality_code == "ok":
            if item.ocr_status == "completed" and item.quarantine_status == "isolated":
                accepted = set_evidence_quarantine(
                    **question_args, evidence_id=item.evidence_id,
                    quarantine_status="accepted_for_review",
                    expected_case_version=get_case(**question_args).case_version,
                )
                item = replace(item, quarantine_status=accepted.quarantine_status)
            closed = close_remediation(
                **question_args, requirement_key=remed_key,
                closed_by_evidence_id=item.evidence_id, automatic_only=True,
            )
            if closed:
                active.pop(remed_key, None)
                closed_labels.append(_label_for(remed_key, item.classification, language))
        attached.append(item)

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
    history = _thread_history(settings, case_id, email_event_id, sender, references)
    known_purpose = purpose in PURPOSE_LABELS
    missing = _missing_materials(
        case, evidence_items, open_keys=open_keys, language=language, known_purpose=known_purpose,
    )

    itinerary = prepare_itinerary_reply(
        settings=settings, case=case, email_event_id=email_event_id, sender=sender,
        body=body, language=language, references=references, reply_to=reply_to,
        tourism=purpose == "tourism", trip=trip, received_on=received_on,
    )
    if itinerary:
        # Creating a different draft advances the case version.
        case = get_case(**question_args)
    unanswered_body = itinerary.remaining_body if itinerary else body
    if attached:
        unanswered_body = "".join(
            part for part in re.split(r"(?<=[？?。\n])", unanswered_body)
            if not _ATTACHMENT_TYPE_QUESTION.fullmatch(part.strip())
        )
    had_question = had_question or itinerary is not None
    question_block = itinerary.text if itinerary else None
    question_answered = itinerary is not None
    reply_reference = None
    if material_correction and not itinerary:
        open_remediation(
            **question_args, requirement_key=ADVISOR_CUSTOMER_QUESTION,
            reason_code="customer_material_correction",
            reason_detail=unanswered_body[:400], severity="complex_human",
            source_email_event_id=email_event_id, case_version=case.case_version,
        )
        opened_keys.append(ADVISOR_CUSTOMER_QUESTION)
        question_block = ("已收到您对材料的更正，顾问会核对后处理。"
                          if language == "zh" else
                          "I received your correction. Your advisor will check it before review continues.")
    elif _looks_like_question(unanswered_body) or (trip and trip.question and trip.question in unanswered_body):
        saved_itinerary = get_case_itinerary(settings=settings, case_id=case_id) if visitor else None
        qa = answer_customer_question(
            settings=settings,
            case=case,
            question=unanswered_body,
            language=language,
            thread_history=history,
            latest_email=body,
            trip_days=trip.days if trip and trip.days else saved_itinerary.days if saved_itinerary else None,
        )
        question_block = "\n\n".join(
            text for text in (qa.text, question_block) if text
        )
        reply_reference = getattr(qa, "reference", None)
        question_answered = True
        if qa.needs_advisor:
            detail = (
                f"客户提问需顾问跟进：{unanswered_body[:400]}"
                if language == "zh"
                else f"Customer question needs advisor: {unanswered_body[:400]}"
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
    advisor_review = any(item.quality_severity == "complex_human" for item in attached)
    for item in attached:
        if item.quality_code == "duplicate" or item.quality_severity == "complex_human":
            continue
        label = _label_for(item.requirement_key, item.classification, language)
        if item.quality_code == "optional_identity_card":
            label = "身份证（额外材料，已留档）" if language == "zh" else "Identity card (extra material on file)"
        if item.issue or item.quarantine_status == "rejected":
            issue_lines.append(MaterialLine(filename=item.filename, label=label, issue=item.issue))
        else:
            received_lines.append(MaterialLine(filename=item.filename, label=label))

    conclusion = "material_change" if any(item.quality_code != "duplicate" for item in attached) else "acknowledged"
    if opened_keys or closed_labels:
        conclusion = "material_change"

    reply_sent = False
    reply_skip: str | None = None
    if send_reply:
        email = normalize_email(case.customer_email)
        if not email:
            reply_skip = "missing_customer_email"
        elif normalize_email(sender) != email:
            reply_skip = "sender_not_case_customer"
        elif attached and all(item.quality_code == "duplicate" for item in attached) and not had_question:
            reply_skip = "duplicate_materials"
        else:
            subject_out, reply_body = build_status_reply(
                language=language,
                case=case,
                received=received_lines,
                issues=issue_lines,
                missing=missing if not advisor_review and (
                    _MATERIAL_STATUS_REQUEST.search(body) or (received_lines and not (issue_lines or had_question))
                ) else [],
                question_block=question_block,
                remediations_closed=closed_labels or None,
                advisor_review=advisor_review,
            )
            thread_subject = re.sub(r"[\r\n]+\s*", " ", subject).strip()
            if parent_message_id and thread_subject:
                subject_out = (
                    thread_subject if thread_subject.lower().startswith("re:")
                    else f"Re: {thread_subject}"
                )
            body_html = None
            if reply_reference:
                reply_body, body_html = render_reply_reference(reply_body, reply_reference)
            reply = OutboxRequest(
                kind=OutboundMessageKind.ORDINARY_REPLY.value,
                recipients=(email,),
                subject=subject_out,
                body_text=reply_body,
                send_operation_id=operation_id,
                case_id=case.id,
                case_version=case.case_version,
                in_reply_to=parent_message_id,
                references=references,
                attachment_evidence_id=itinerary.attachment_id if itinerary else None,
                body_html=body_html,
            )
            reply_sent, reply_skip = _dispatch_reply(settings, reply, email_event_id)

    case = _finish_inbound(settings, case_id, email_event_id, conclusion)
    if send_reply:
        advance_material_confirmation(settings, case_id)

    missing_other_lang = _missing_materials(
        case,
        evidence_items,
        open_keys=open_keys,
        language="en" if language == "zh" else "zh",
        known_purpose=known_purpose,
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


def _saved_reply(settings: Settings, case_id: str, operation_id: str) -> OutboxRequest | None:
    with psycopg.connect(settings.database_url) as conn:
        # The case lock excludes a live sender for this operation. An interrupted
        # SMTP call may have completed externally, so keep it for reconciliation.
        conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'status_unknown', updated_at = NOW()
            WHERE tenant_id = %s AND case_id = %s AND send_operation_id = %s
              AND status = 'processing'
            """,
            (settings.tenant_id, case_id, operation_id),
        )
        row = conn.execute(
            """
            SELECT kind, recipients, subject, body_text, case_version, in_reply_to,
                   email_references, attachment_evidence_id, body_html
            FROM outbox_messages
            WHERE tenant_id = %s AND case_id = %s AND send_operation_id = %s
            """,
            (settings.tenant_id, case_id, operation_id),
        ).fetchone()
    if row is None:
        return None
    return OutboxRequest(
        kind=row[0], recipients=tuple(row[1]), subject=row[2], body_text=row[3],
        send_operation_id=operation_id, case_id=case_id, case_version=row[4],
        in_reply_to=row[5], references=tuple((row[6] or "").split()),
        attachment_evidence_id=row[7], body_html=row[8],
    )


def _dispatch_reply(
    settings: Settings, reply: OutboxRequest, email_event_id: str,
) -> tuple[bool, str | None]:
    result = enqueue_and_maybe_send(settings=settings, req=reply)
    if result.status == "provider_accepted" and not result.block_reasons:
        if reply.case_id and reply.send_operation_id:
            record_itinerary_sent(
                settings=settings, case_id=reply.case_id, inbound_event_id=email_event_id,
                send_operation_id=reply.send_operation_id,
            )
        return True, None
    if result.status in {"failed", "pending"}:
        raise DomainError(ErrorCode.SYSTEM_BLOCKED, "automatic reply pending retry")

    reason = ",".join(result.block_reasons) or result.status
    if reply.case_id and reply.case_version is not None:
        open_remediation(
            database_url=settings.database_url, tenant_id=settings.tenant_id,
            case_id=reply.case_id, requirement_key=f"advisor_reply_delivery:{email_event_id}",
            reason_code="automatic_reply_delivery", reason_detail=f"Outbound reply: {reason}",
            severity="complex_human", source_email_event_id=email_event_id,
            case_version=reply.case_version,
        )
    return False, reason


def _finish_inbound(
    settings: Settings, case_id: str, email_event_id: str, conclusion: str,
) -> CaseRecord:
    if conclusion != "stop_delivery":
        _try_evaluate(settings, case_id)
    case_args = dict(
        database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id,
    )
    case = get_case(**case_args)
    return mark_inbound_processed(
        **case_args, email_event_id=email_event_id, conclusion=conclusion,
        processed_by="inbound_agent", expected_case_version=case.case_version,
    )


def process_inbound_after_attribution(
    *,
    settings: Settings,
    case_id: str,
    email_event_id: str,
) -> InboundProcessResult | None:
    return process_customer_inbound(
        settings=settings, case_id=case_id, email_event_id=email_event_id,
    )


def process_persisted_inbound(
    *, settings: Settings, email_event_id: str,
) -> tuple[str | None, InboundProcessResult | None]:
    """A duplicate delivery resumes unfinished work on the original event."""
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            """
            SELECT case_id, from_addr FROM email_events
            WHERE tenant_id = %s AND id = %s AND direction = 'inbound'
            """,
            (settings.tenant_id, email_event_id),
        ).fetchone()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "inbound email event not found")
    case_id, sender = row
    if not case_id:
        case_id = try_auto_attribute_inbound_by_customer_email(
            settings=settings, email_event_id=email_event_id, from_addr=sender,
        )
    if not case_id:
        return None, None
    return case_id, process_inbound_after_attribution(
        settings=settings, case_id=case_id, email_event_id=email_event_id,
    )
