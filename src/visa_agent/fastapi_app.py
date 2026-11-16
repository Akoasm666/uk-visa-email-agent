"""FastAPI entrypoint for S4."""

from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import parse_qs, quote

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from visa_agent.ai_capabilities import (
    context_from_stored_payload,
    generate_case_draft,
    groundedness_report,
    list_case_drafts,
)
from visa_agent.ai_capabilities.draft import load_latest_draft_audit
from visa_agent.case_service import (
    attribute_inbound_to_case,
    create_case,
    get_case,
    get_case_review_page,
    require_customer_email,
    send_opening_outreach,
    get_review_board,
    list_cases,
    mark_inbound_processed,
    process_inbound_after_attribution,
    record_customer_confirmation,
    record_delivery_receipt,
    record_final_review,
    record_reopen_grant,
    set_case_route,
    set_takeover,
)
from visa_agent.case_service.advisor_actions import (
    advisor_classify_evidence,
    advisor_reply_via_agent,
    fill_takeover_customer_email,
    list_customer_mail,
    send_takeover_reply,
)
from visa_agent.case_service.closure import (
    close_customer_case, closure_notice_status, required_materials_verified, termination_reasons,
)
from visa_agent.case_service.inbound_agent import process_customer_inbound, process_persisted_inbound
from visa_agent.case_service.itinerary import get_case_itinerary
from visa_agent.case_service.material_confirmation import material_confirmation_sent
from visa_agent.case_service.remediation import (
    acknowledge_advisor_question,
    count_pending_advisor_inbox,
    get_advisor_inbox,
    get_unassigned_mail,
    list_open_questions,
)
from visa_agent.case_service.service import (
    FINISHED_CASE_STATUSES, build_transition_context, bump_case_version, list_case_page,
)
from visa_agent.config import Settings, get_settings
from visa_agent.domain.enums import CaseStatus, ErrorCode, OutboundMessageKind, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.document_pipeline import (
    attach_evidence,
    get_evidence,
    latest_ocr_by_evidence,
    list_annotations,
    list_evidence,
    list_ocr_results,
    record_annotation,
    run_ocr_on_evidence,
    set_evidence_quarantine,
)
from visa_agent.document_pipeline.service import delete_evidence, mark_evidence_viewed
from visa_agent.email_adapter import (
    InboundEmail,
    OutboxRequest,
    enqueue_and_maybe_send,
    persist_inbound,
    poll_imap_inbox,
)
from visa_agent.email_adapter.allowlist import effective_outbound_allowlist, normalize_email
from visa_agent.http_app import build_health
from visa_agent.rules import (
    ensure_seed_route_packs,
    ensure_seed_visitor_pack,
    evaluate_case_requirements,
    get_rule_pack,
    impact_scan,
    list_case_requirements,
    list_rule_packs,
    publish_rule_pack,
    set_case_active_pack,
)
from visa_agent.rules.service import record_requirement_applicability, record_requirement_review
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations
from visa_agent.workbench.pages import (
    render_detail,
    render_evidence_view,
    render_inbox,
    render_list,
    render_unassigned_mail,
)
from visa_agent.workflow.transitions import apply_transition, list_allowed_targets

app = FastAPI(title="visa-email-agent", version="0.9.0")


def bootstrap(settings: Settings | None = None) -> Settings:
    """Migrate + seed against the given settings (or current get_settings())."""
    boot = settings or get_settings()
    boot.file_storage_dir.mkdir(parents=True, exist_ok=True)
    apply_migrations(boot.database_url)
    ensure_seed_route_packs(database_url=boot.database_url)
    return boot


def _parse_route_code(value: str | None) -> RouteCode | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return RouteCode(str(value).strip())
    except ValueError as exc:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="invalid route",
            details=(str(value),),
        ) from exc


# Tests set VISA_AGENT_SKIP_APP_BOOT=1 before import, then call bootstrap(test_settings).
if os.environ.get("VISA_AGENT_SKIP_APP_BOOT") != "1":
    bootstrap()


class InboundAttachmentBody(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    content_base64: str


class InboundBody(BaseModel):
    from_addr: str
    to_addrs: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    rfc_message_id: str | None = None
    provider_message_id: str | None = None
    claimed_case_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    attachments: list[InboundAttachmentBody] = Field(default_factory=list)


class OutboxBody(BaseModel):
    """S1.2 test send only — case/approval fields are forbidden."""

    model_config = ConfigDict(extra="forbid")

    kind: str = "ordinary_reply"
    recipients: list[str]
    subject: str
    body_text: str
    send_operation_id: str | None = None
    send_now: bool = True


class CreateCaseBody(BaseModel):
    title: str = ""
    customer_email: str | None = None
    route: str | None = RouteCode.STANDARD_VISITOR.value


class SetRouteBody(BaseModel):
    route: str
    expected_case_version: int


class EvidenceOcrBody(BaseModel):
    expected_case_version: int


class TransitionBody(BaseModel):
    target: str
    expected_case_version: int | None = None


class TakeoverBody(BaseModel):
    active: bool
    expected_case_version: int | None = None


class ReviewBody(BaseModel):
    decision: str
    note: str = ""
    expected_case_version: int
    package_id: str | None = None
    recipients: list[str] | None = None
    reviewer_id: str | None = None


class AssemblePackageBody(BaseModel):
    package_kind: str
    expected_case_version: int
    evidence_ids: list[str] | None = None


class PackageSendBody(BaseModel):
    recipients: list[str]
    subject: str
    body_text: str
    send_operation_id: str | None = None
    send_now: bool = True


class ConfirmBody(BaseModel):
    expected_case_version: int


class AttributeBody(BaseModel):
    email_event_id: str
    force_reattribute: bool = False
    reason: str = ""


class MarkInboundProcessedBody(BaseModel):
    email_event_id: str
    expected_case_version: int
    conclusion: str
    processed_by: str


class PollBody(BaseModel):
    limit: int = 10


class EvidenceAttachBody(BaseModel):
    """Base64 payload keeps workbench/API free of multipart requirements."""

    filename: str
    content_base64: str
    expected_case_version: int
    content_type: str = "application/octet-stream"
    classification: str = "unclassified"
    source_email_event_id: str | None = None


class EvidenceQuarantineBody(BaseModel):
    quarantine_status: str
    expected_case_version: int
    rejection_reason: str = ""


class EvidenceAnnotationBody(BaseModel):
    field_key: str
    field_value: str
    expected_case_version: int
    note: str = ""


class BindPackBody(BaseModel):
    pack_id: str
    pack_version: int


def _domain_http(exc: DomainError) -> HTTPException:
    if exc.code == ErrorCode.NOT_FOUND:
        status = 404
    elif exc.code == ErrorCode.CONFLICT_VERSION:
        status = 409
    elif exc.code == ErrorCode.UNAUTHORIZED:
        status = 403
    else:
        status = 400
    return HTTPException(
        status_code=status,
        detail={
            "success": False,
            "error": exc.code.value,
            "message": exc.message,
            "details": list(exc.details),
        },
    )


def _pack_payload(pack: Any) -> dict[str, Any]:
    return {
        "pack_id": pack.pack_id,
        "pack_version": pack.pack_version,
        "route": pack.route,
        "title": pack.title,
        "source_url": pack.source_url,
        "source_retrieved_on": pack.source_retrieved_on,
        "published": pack.published,
        "published_at": getattr(pack, "published_at", None),
        "requirements": [
            {
                "key": r.key,
                "title": r.title,
                "description": r.description,
                "severity": r.severity,
            }
            for r in pack.requirements
        ],
    }


def _req_state_payload(st: Any) -> dict[str, Any]:
    return {
        "requirement_key": st.requirement_key,
        "status": st.status,
        "reason": st.reason,
        "severity": st.severity,
        "pack_id": st.pack_id,
        "pack_version": st.pack_version,
        "case_version": st.case_version,
    }


def _parse_expected_version(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="expected_case_version must be an integer",
            details=(str(raw),),
        ) from exc


def _case_payload(case: Any) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "case_version": case.case_version,
        "status": case.status.value,
        "route": case.route.value if case.route else None,
        "resume_status": case.resume_status.value if case.resume_status else None,
        "customer_email": case.customer_email,
        "title": case.title,
        "human_takeover": case.human_takeover,
        "waiting_customer": case.waiting_customer,
        "system_blocked": case.system_blocked,
        "paused": case.paused,
        "cancelled": case.cancelled,
        "current_delivery_id": case.current_delivery_id,
        "closure_seq": case.closure_seq,
    }


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "service": "visa-email-agent",
            "stage": "S4",
            "workbench": "/workbench",
            "docs": "/health",
        },
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return build_health(get_settings())


@app.post("/internal/email/inbound")
def inbound_email(body: InboundBody) -> dict[str, Any]:
    settings = get_settings()
    from visa_agent.email_adapter.inbound import InboundAttachment

    attachments = tuple(
        InboundAttachment(
            filename=a.filename,
            content_type=a.content_type or "application/octet-stream",
            data=base64.b64decode(a.content_base64, validate=False),
        )
        for a in body.attachments
        if a.content_base64
    )
    result = persist_inbound(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        mail=InboundEmail(
            from_addr=body.from_addr,
            to_addrs=tuple(body.to_addrs),
            subject=body.subject,
            body_text=body.body_text,
            rfc_message_id=body.rfc_message_id,
            provider_message_id=body.provider_message_id,
            claimed_case_id=body.claimed_case_id,
            attachments=attachments,
            in_reply_to=body.in_reply_to,
            references=body.references,
        ),
        file_store=LocalFileStore(settings.file_storage_dir),
    )
    try:
        attributed_case_id, agent_result = process_persisted_inbound(
            settings=settings, email_event_id=result.email_event_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "status_code_hint": 200 if result.duplicate else 201,
        "data": {
            "email_event_id": result.email_event_id,
            "dedupe_key": result.dedupe_key,
            "attribution_status": (
                "attributed" if attributed_case_id else result.attribution_status
            ),
            "case_id": attributed_case_id,
            "duplicate": result.duplicate,
            "raw_file_id": result.raw_file_id,
            "agent": (
                None
                if agent_result is None
                else {
                    "reply_sent": agent_result.reply_sent,
                    "had_question": agent_result.had_question,
                    "language": agent_result.language,
                    "attached": len(agent_result.attached),
                    "missing_en": list(agent_result.missing_titles_en),
                    "conclusion": agent_result.conclusion,
                    "reply_skipped_reason": agent_result.reply_skipped_reason,
                    "remediations_opened": list(agent_result.remediations_opened),
                    "remediations_closed": list(agent_result.remediations_closed),
                    "question_answered": agent_result.question_answered,
                }
            ),
        },
    }


@app.post("/internal/email/outbox")
def outbox_email(body: OutboxBody) -> dict[str, Any]:
    from visa_agent.email_adapter.outbox import (
        CLOSURE_NOTICE_OPERATION_PREFIX, INBOUND_REPLY_OPERATION_PREFIX, MATERIAL_CONFIRMATION_OPERATION_PREFIX,
        TAKEOVER_REPLY_OPERATION_PREFIX,
    )

    if body.kind == OutboundMessageKind.CASE_CLOSURE_NOTICE or (body.send_operation_id or "").strip().startswith((
        CLOSURE_NOTICE_OPERATION_PREFIX, INBOUND_REPLY_OPERATION_PREFIX, MATERIAL_CONFIRMATION_OPERATION_PREFIX,
        TAKEOVER_REPLY_OPERATION_PREFIX,
    )):
        raise HTTPException(status_code=400, detail="reserved reply operation ID")
    live = get_settings()
    result = enqueue_and_maybe_send(
        settings=live,
        req=OutboxRequest(
            kind=body.kind,
            recipients=tuple(body.recipients),
            subject=body.subject,
            body_text=body.body_text,
            send_operation_id=body.send_operation_id,
            send_now=body.send_now,
        ),
    )
    if result.resent_blocked and "VALIDATION_ERROR" in result.block_reasons:
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "error": "VALIDATION_ERROR",
                "message": "send_operation_id payload does not match original outbox content",
                "data": {
                    "outbox_id": result.outbox_id,
                    "send_operation_id": result.send_operation_id,
                    "block_reasons": list(result.block_reasons),
                },
            },
        )
    ok = (
        result.status in {"provider_accepted", "pending", "status_unknown"}
        and not result.block_reasons
    )
    return {
        "success": ok,
        "data": {
            "outbox_id": result.outbox_id,
            "status": result.status,
            "block_reasons": list(result.block_reasons),
            "provider_response": result.provider_response,
            "email_event_id": result.email_event_id,
            "send_operation_id": result.send_operation_id,
            "resent_blocked": result.resent_blocked,
        },
    }


# ----- Case JSON API (S1.3) -----


@app.get("/cases")
def api_list_cases(
    q: str | None = None,
    status: str | None = None,
    route: str | None = None,
    waiting: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    settings = get_settings()
    cases = list_cases(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        q=q,
        status=status,
        route=route,
        waiting=waiting,
        limit=limit,
    )
    return {"success": True, "data": [_case_payload(c) for c in cases]}


@app.post("/cases")
def api_create_case(body: CreateCaseBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        route = _parse_route_code(body.route)
        if route is None:
            route = RouteCode.STANDARD_VISITOR
        email = (
            require_customer_email(body.customer_email)
            if body.customer_email
            else None
        )
        case = create_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            title=body.title,
            customer_email=email,
            route=route,
        )
        outreach = (
            send_opening_outreach(settings=settings, case=case)
            if email
            else None
        )
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case.id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    payload = _case_payload(case)
    if outreach is not None:
        payload["opening_outreach"] = {
            "attempted": outreach.attempted,
            "sent": outreach.sent,
            "skipped_reason": outreach.skipped_reason,
            "outbox_id": outreach.outbox.outbox_id if outreach.outbox else None,
            "outbox_status": outreach.outbox.status if outreach.outbox else None,
        }
    return {"success": True, "data": payload}


@app.post("/cases/{case_id}/route")
def api_set_case_route(case_id: str, body: SetRouteBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        route = _parse_route_code(body.route)
        if route is None:
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="route is required",
            )
        case = set_case_route(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            route=route,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _case_payload(case)}


@app.get("/routes")
def api_list_routes() -> dict[str, Any]:
    """Supported UK visa routes for case create / rebind."""
    return {
        "success": True,
        "data": [
            {
                "code": RouteCode.STANDARD_VISITOR.value,
                "label": "Standard Visitor",
            },
            {"code": RouteCode.STUDENT.value, "label": "Student"},
            {
                "code": RouteCode.SKILLED_WORKER.value,
                "label": "Skilled Worker",
            },
            {"code": RouteCode.GRADUATE.value, "label": "Graduate"},
        ],
    }


@app.get("/cases/{case_id}")
def api_get_case(case_id: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    allowed = list_allowed_targets(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    payload = _case_payload(case)
    payload["allowed_targets"] = [s.value for s in allowed]
    return {"success": True, "data": payload}


@app.post("/cases/{case_id}/transition")
def api_transition(case_id: str, body: TransitionBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        target = CaseStatus(body.target)
        case = apply_transition(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            target=target,
            expected_case_version=body.expected_case_version,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": "VALIDATION_ERROR",
                "message": f"unknown status: {body.target}",
            },
        ) from exc
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _case_payload(case)}


@app.post("/cases/{case_id}/takeover")
def api_takeover(case_id: str, body: TakeoverBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        case = set_takeover(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            active=body.active,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _case_payload(case)}


@app.post("/cases/{case_id}/confirm")
def api_confirm(case_id: str, body: ConfirmBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        record_customer_confirmation(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            expected_case_version=body.expected_case_version,
        )
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _case_payload(case)}


@app.post("/cases/{case_id}/reviews")
def api_review(case_id: str, body: ReviewBody, request: Request) -> dict[str, Any]:
    settings = get_settings()
    if body.package_id:
        provided = request.headers.get("X-Rule-Publish-Token", "")
        auth = request.headers.get("Authorization", "")
        if not provided and auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if not settings.rule_publish_enabled or provided != settings.rule_publish_token:
            raise _domain_http(
                DomainError(
                    code=ErrorCode.UNAUTHORIZED,
                    message="package review requires a valid review/publish token",
                )
            )
    try:
        record_final_review(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            decision=body.decision,
            note=body.note,
            expected_case_version=body.expected_case_version,
            package_id=body.package_id,
            recipients=body.recipients,
            reviewer_id=body.reviewer_id,
        )
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _case_payload(case)}


@app.get("/cases/{case_id}/review-board")
def api_review_board(case_id: str) -> dict[str, Any]:
    """Confirmations + final reviews with stale (non-current version) marked."""
    settings = get_settings()
    try:
        board = get_review_board(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "case_id": board.case_id,
            "case_version": board.case_version,
            "customer_confirmation_effective": board.customer_confirmation_effective,
            "final_approval_effective": board.final_approval_effective,
            "current_final_decision": board.current_final_decision,
            "confirmations": [
                {
                    "id": c.id,
                    "case_version": c.case_version,
                    "note": c.note,
                    "created_at": c.created_at,
                    "effective": c.effective,
                    "stale": not c.effective,
                }
                for c in board.confirmations
            ],
            "reviews": [
                {
                    "id": r.id,
                    "case_version": r.case_version,
                    "decision": r.decision,
                    "note": r.note,
                    "created_at": r.created_at,
                    "decision_seq": r.decision_seq,
                    "effective": r.effective,
                    "superseded": r.superseded,
                    "stale": (not r.effective) and (not r.superseded),
                }
                for r in board.reviews
            ],
        },
    }


@app.post("/cases/{case_id}/attribute-inbound")
def api_attribute(case_id: str, body: AttributeBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        attribute_inbound_to_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            email_event_id=body.email_event_id,
            case_id=case_id,
            force_reattribute=body.force_reattribute,
            reason=body.reason,
        )
        agent_result = process_inbound_after_attribution(
            settings=settings, case_id=case_id, email_event_id=body.email_event_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "case_id": case_id,
            "email_event_id": body.email_event_id,
            "agent": (
                None
                if agent_result is None
                else {
                    "reply_sent": agent_result.reply_sent,
                    "had_question": agent_result.had_question,
                    "attached": len(agent_result.attached),
                    "conclusion": agent_result.conclusion,
                }
            ),
        },
    }


@app.post("/cases/{case_id}/inbound/mark-processed")
def api_mark_inbound_processed(
    case_id: str, body: MarkInboundProcessedBody, request: Request
) -> dict[str, Any]:
    """Authorized action that clears the unprocessed-inbound outbound gate."""
    settings = get_settings()
    provided = request.headers.get("X-Rule-Publish-Token", "")
    auth = request.headers.get("Authorization", "")
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not settings.rule_publish_enabled or provided != settings.rule_publish_token:
        raise _domain_http(
            DomainError(
                code=ErrorCode.UNAUTHORIZED,
                message="mark-processed requires a valid review/publish token",
            )
        )
    try:
        case = mark_inbound_processed(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            email_event_id=body.email_event_id,
            conclusion=body.conclusion,
            processed_by=body.processed_by,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "case_id": case_id,
            "email_event_id": body.email_event_id,
            "processing_status": "processed",
            "processing_conclusion": body.conclusion,
            "processed_by": body.processed_by,
            "case_version": case.case_version,
        },
    }


def _draft_payload(draft: Any) -> dict[str, Any]:
    return {
        "model_call_id": draft.model_call_id,
        "case_id": draft.case_id,
        "case_version": draft.case_version,
        "model": draft.model,
        "draft_text": draft.draft_text,
        "candidate_facts": list(draft.candidate_facts),
        "suggested_actions": list(draft.suggested_actions),
        "prompt_digest": draft.prompt_digest,
    }


def _evidence_payload(ev: Any) -> dict[str, Any]:
    return {
        "evidence_id": ev.id,
        "case_id": ev.case_id,
        "case_version": ev.case_version,
        "file_id": ev.file_id,
        "content_sha256": ev.content_sha256,
        "original_filename": ev.original_filename,
        "content_type": ev.content_type,
        "byte_size": ev.byte_size,
        "classification": ev.classification,
        "quarantine_status": ev.quarantine_status,
        "rejection_reason": ev.rejection_reason,
        "source_email_event_id": ev.source_email_event_id,
    }


def _ocr_payload(ocr: Any) -> dict[str, Any]:
    return {
        "ocr_id": ocr.id,
        "evidence_id": ocr.evidence_id,
        "case_id": ocr.case_id,
        "case_version": ocr.case_version,
        "provider": ocr.provider,
        "status": ocr.status,
        "extracted_text": ocr.extracted_text,
        "source_content_sha256": ocr.source_content_sha256,
        "detail": ocr.detail,
    }


@app.post("/cases/{case_id}/evidence")
def api_attach_evidence(case_id: str, body: EvidenceAttachBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        raw = base64.b64decode(body.content_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise _domain_http(
            DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="content_base64 is invalid",
                details=(str(exc),),
            )
        ) from exc
    try:
        evidence = attach_evidence(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            file_store=LocalFileStore(settings.file_storage_dir),
            data=raw,
            original_filename=body.filename,
            content_type=body.content_type,
            classification=body.classification,
            source_email_event_id=body.source_email_event_id,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _evidence_payload(evidence)}


@app.get("/cases/{case_id}/evidence")
def api_list_evidence(case_id: str) -> dict[str, Any]:
    settings = get_settings()
    items = list_evidence(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    return {"success": True, "data": [_evidence_payload(e) for e in items]}


@app.post("/cases/{case_id}/evidence/{evidence_id}/quarantine")
def api_quarantine(
    case_id: str, evidence_id: str, body: EvidenceQuarantineBody
) -> dict[str, Any]:
    settings = get_settings()
    try:
        evidence = set_evidence_quarantine(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            evidence_id=evidence_id,
            quarantine_status=body.quarantine_status,
            rejection_reason=body.rejection_reason,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _evidence_payload(evidence)}


@app.post("/cases/{case_id}/evidence/{evidence_id}/annotations")
def api_annotate(
    case_id: str, evidence_id: str, body: EvidenceAnnotationBody
) -> dict[str, Any]:
    settings = get_settings()
    try:
        ann = record_annotation(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            evidence_id=evidence_id,
            field_key=body.field_key,
            field_value=body.field_value,
            note=body.note,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "annotation_id": ann.id,
            "evidence_id": ann.evidence_id,
            "case_id": ann.case_id,
            "case_version": ann.case_version,
            "field_key": ann.field_key,
            "field_value": ann.field_value,
            "note": ann.note,
        },
    }


@app.get("/cases/{case_id}/evidence/{evidence_id}/annotations")
def api_list_annotations(case_id: str, evidence_id: str) -> dict[str, Any]:
    settings = get_settings()
    items = list_annotations(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )
    return {
        "success": True,
        "data": [
            {
                "annotation_id": a.id,
                "evidence_id": a.evidence_id,
                "case_version": a.case_version,
                "field_key": a.field_key,
                "field_value": a.field_value,
                "note": a.note,
            }
            for a in items
        ],
    }


@app.post("/cases/{case_id}/evidence/{evidence_id}/ocr")
def api_run_ocr(case_id: str, evidence_id: str, body: EvidenceOcrBody) -> dict[str, Any]:
    """Run derived OCR; never mutates quarantine original bytes or auto-confirms fields."""
    settings = get_settings()
    try:
        ocr = run_ocr_on_evidence(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            evidence_id=evidence_id,
            file_store=LocalFileStore(settings.file_storage_dir),
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _ocr_payload(ocr)}


@app.get("/cases/{case_id}/ocr")
def api_list_case_ocr(case_id: str, evidence_id: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    items = list_ocr_results(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )
    return {"success": True, "data": [_ocr_payload(o) for o in items]}


@app.get("/cases/{case_id}/evidence/{evidence_id}/ocr")
def api_list_evidence_ocr(case_id: str, evidence_id: str) -> dict[str, Any]:
    settings = get_settings()
    items = list_ocr_results(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )
    return {"success": True, "data": [_ocr_payload(o) for o in items]}


@app.get("/rule-packs")
def api_list_rule_packs() -> dict[str, Any]:
    settings = get_settings()
    packs = list_rule_packs(database_url=settings.database_url)
    return {"success": True, "data": [_pack_payload(p) for p in packs]}


@app.get("/rule-packs/{pack_id}/impact")
def api_impact_scan(
    pack_id: str,
    from_version: int,
    to_version: int | None = None,
) -> dict[str, Any]:
    """Registered before /{pack_version} so 'impact' is not parsed as an int."""
    settings = get_settings()
    try:
        rows = impact_scan(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            pack_id=pack_id,
            from_version=from_version,
            to_version=to_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": rows}


@app.get("/rule-packs/{pack_id}/{pack_version}")
def api_get_rule_pack(pack_id: str, pack_version: int) -> dict[str, Any]:
    settings = get_settings()
    try:
        pack = get_rule_pack(
            database_url=settings.database_url,
            pack_id=pack_id,
            pack_version=pack_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _pack_payload(pack)}


@app.post("/rule-packs/{pack_id}/{pack_version}/publish")
def api_publish_rule_pack(pack_id: str, pack_version: int, request: Request) -> dict[str, Any]:
    """Publish requires server-configured RULE_PUBLISH_TOKEN (disabled when empty)."""
    settings = get_settings()
    provided = request.headers.get("X-Rule-Publish-Token", "")
    auth = request.headers.get("Authorization", "")
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    try:
        pack = publish_rule_pack(
            database_url=settings.database_url,
            pack_id=pack_id,
            pack_version=pack_version,
            provided_token=provided,
            configured_token=settings.rule_publish_token,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _pack_payload(pack)}


@app.post("/cases/{case_id}/active-pack")
def api_bind_active_pack(case_id: str, body: BindPackBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        case = set_case_active_pack(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            pack_id=body.pack_id,
            pack_version=body.pack_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "case_id": case_id,
            "case_version": case.case_version,
            "active_pack_id": body.pack_id,
            "active_pack_version": body.pack_version,
        },
    }


class AnnotationPairBody(BaseModel):
    key: str
    value: str = ""


class VisitorScenarioBody(BaseModel):
    scenario_id: str = "standard_adult_tourist"
    classifications: list[str] = []
    annotations: list[AnnotationPairBody] = []
    annotation_keys: list[str] = []
    annotation_values: list[str] = []


def _visitor_scenario_payload(body: VisitorScenarioBody) -> dict[str, Any]:
    from visa_agent.rules.visitor_pack import AnnotationPair, run_visitor_scenario

    # Keep parallel-list order and duplicate values (do not set()-dedupe).
    result = run_visitor_scenario(
        scenario_id=body.scenario_id,
        classifications=list(body.classifications),
        annotations=[
            AnnotationPair(key=a.key, value=a.value) for a in body.annotations
        ],
        annotation_keys=list(body.annotation_keys),
        annotation_values=list(body.annotation_values),
    )
    return {
        "scenario_id": result.scenario_id,
        "title": result.title,
        "transfer_human_required": result.transfer_human_required,
        "transfer_polarity": result.transfer_polarity,
        "student_funds_rejected": result.student_funds_rejected,
        "student_funds_hits": list(result.student_funds_hits),
        "notes": list(result.notes),
        "pack_hint": {
            "pack_id": "standard_visitor",
            "pack_version_for_s3_gates": 2,
            "note": "Bind published v2 for formal evaluate polarity/funds gates",
        },
    }


@app.get("/visitor-scenarios")
def api_list_visitor_scenarios() -> dict[str, Any]:
    """W06-V canned scenario ids for demo / TL acceptance."""
    from visa_agent.rules.visitor_pack import SCENARIO_IDS

    return {
        "success": True,
        "data": {
            "route": "standard_visitor",
            "scenario_ids": list(SCENARIO_IDS),
            "notes": (
                "Canned scenarios are Standard Visitor only. "
                "Student / Skilled Worker / Graduate use seeded route packs "
                "via /routes + /cases/{id}/active-pack. "
                "Outcomes follow shared signal assessment (not scenario-id hardcoding). "
                "Formal evaluate S3 gates require published standard_visitor v2."
            ),
        },
    }


@app.post("/visitor-scenarios")
def api_post_visitor_scenarios(body: VisitorScenarioBody) -> dict[str, Any]:
    """Canonical run endpoint (contract)."""
    return {"success": True, "data": _visitor_scenario_payload(body)}


@app.post("/visitor-scenarios/run")
def api_run_visitor_scenario(body: VisitorScenarioBody) -> dict[str, Any]:
    """Alias of POST /visitor-scenarios."""
    return {"success": True, "data": _visitor_scenario_payload(body)}


@app.post("/cases/{case_id}/requirements/evaluate")
def api_evaluate_requirements(case_id: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        result = evaluate_case_requirements(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "pack_id": result.pack_id,
            "pack_version": result.pack_version,
            "case_id": result.case_id,
            "case_version": result.case_version,
            "blocking_keys": list(result.blocking_keys),
            "all_required_clear": result.all_required_clear,
            "states": [_req_state_payload(s) for s in result.states],
        },
    }


@app.post("/cases/{case_id}/packages/assemble")
def api_assemble_package(case_id: str, body: AssemblePackageBody) -> dict[str, Any]:
    """W09: snapshot accepted evidence into an immutable delivery package."""
    from visa_agent.delivery import assemble_package

    settings = get_settings()
    try:
        pkg = assemble_package(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            package_kind=body.package_kind,
            file_store=LocalFileStore(settings.file_storage_dir),
            evidence_ids=body.evidence_ids,
            expected_case_version=body.expected_case_version,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "package_id": pkg.package_id,
            "case_id": pkg.case_id,
            "case_version": pkg.case_version,
            "package_kind": pkg.package_kind,
            "manifest_digest": pkg.manifest_digest,
            "items": pkg.manifest_payload(),
        },
    }


@app.get("/cases/{case_id}/packages/{package_id}")
def api_get_package(case_id: str, package_id: str) -> dict[str, Any]:
    from visa_agent.delivery import get_package

    settings = get_settings()
    try:
        pkg = get_package(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            package_id=package_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "package_id": pkg.package_id,
            "case_id": pkg.case_id,
            "case_version": pkg.case_version,
            "package_kind": pkg.package_kind,
            "manifest_digest": pkg.manifest_digest,
            "items": pkg.manifest_payload(),
        },
    }


@app.post("/cases/{case_id}/packages/{package_id}/send")
def api_send_package(case_id: str, package_id: str, body: PackageSendBody) -> dict[str, Any]:
    """W10: send a delivery package; kind/hashes come from the saved package."""
    from visa_agent.send_control import PackageSendRequest, send_case_package

    settings = get_settings()
    try:
        result = send_case_package(
            settings=settings,
            case_id=case_id,
            file_store=LocalFileStore(settings.file_storage_dir),
            req=PackageSendRequest(
                package_id=package_id,
                recipients=tuple(body.recipients),
                subject=body.subject,
                body_text=body.body_text,
                send_operation_id=body.send_operation_id,
                send_now=body.send_now,
            ),
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {
            "outbox_id": result.outbox_id,
            "status": result.status,
            "block_reasons": list(result.block_reasons),
            "provider_response": result.provider_response,
            "send_operation_id": result.send_operation_id,
            "package_id": package_id,
        },
    }


@app.get("/cases/{case_id}/requirements")
def api_list_requirements(case_id: str) -> dict[str, Any]:
    settings = get_settings()
    items = list_case_requirements(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    return {"success": True, "data": [_req_state_payload(s) for s in items]}


@app.post("/cases/{case_id}/draft")
def api_generate_draft(case_id: str) -> dict[str, Any]:
    """Generate a model draft from synthetic context. No send / approve / status change."""
    settings = get_settings()
    try:
        draft = generate_case_draft(settings=settings, case_id=case_id)
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {"success": True, "data": _draft_payload(draft)}


@app.get("/cases/{case_id}/drafts")
def api_list_drafts(case_id: str) -> dict[str, Any]:
    settings = get_settings()
    drafts = list_case_drafts(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    return {"success": True, "data": [_draft_payload(d) for d in drafts]}


@app.get("/cases/{case_id}/draft/groundedness")
def api_draft_groundedness(case_id: str) -> dict[str, Any]:
    """W07 diagnostic bound to generation-time context; marks stale drafts."""
    import json as _json

    settings = get_settings()
    try:
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    loaded = load_latest_draft_audit(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    if loaded is None:
        report = groundedness_report(
            context=None,
            facts=[],
            has_draft=False,
            current_case_version=case.case_version,
        )
        return {
            "success": True,
            "data": {
                "case_id": case_id,
                "model_call_id": None,
                "report": report,
            },
        }
    draft, audit = loaded
    synthetic_payload: dict[str, Any] | None = None
    messages = audit.get("messages") if isinstance(audit, dict) else None
    if isinstance(messages, list) and len(messages) >= 2:
        try:
            user_blob = _json.loads(messages[1].get("content") or "{}")
            if isinstance(user_blob, dict):
                raw = user_blob.get("synthetic_context")
                if isinstance(raw, dict):
                    synthetic_payload = raw
        except (_json.JSONDecodeError, TypeError, AttributeError):
            synthetic_payload = None
    context = context_from_stored_payload(synthetic_payload=synthetic_payload)
    report = groundedness_report(
        context=context,
        facts=list(draft.candidate_facts),
        draft_text=draft.draft_text,
        actions=list(draft.suggested_actions),
        draft_case_version=draft.case_version,
        current_case_version=case.case_version,
        has_draft=True,
        generation_context_missing=context is None,
    )
    return {
        "success": True,
        "data": {
            "case_id": case_id,
            "model_call_id": draft.model_call_id,
            "report": report,
        },
    }


@app.post("/internal/email/poll")
def poll_email(body: PollBody = PollBody()) -> dict[str, Any]:
    settings = get_settings()
    limit = body.limit
    try:
        result = poll_imap_inbox(
            settings=settings,
            file_store=LocalFileStore(settings.file_storage_dir),
            limit=limit,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": result.success,
        "data": {
            "count": result.count,
            "failed_count": result.failed_count,
            "partial": result.partial,
            "mailbox": result.mailbox,
            "uidvalidity": result.uidvalidity,
            "messages": [
                {
                    "email_event_id": p.email_event_id,
                    "subject": p.subject,
                    "from_addr": p.from_addr,
                    "duplicate": p.duplicate,
                    "provider_message_id": p.provider_message_id,
                    "uid": p.uid,
                    "uidvalidity": p.uidvalidity,
                }
                for p in result.messages
            ],
            "failures": [
                {
                    "uid": f.uid,
                    "uidvalidity": f.uidvalidity,
                    "provider_message_id": f.provider_message_id,
                    "error": f.error,
                    "subject": f.subject,
                    "from_addr": f.from_addr,
                }
                for f in result.failures
            ],
        },
    }


# ----- Workbench HTML (S1.3) -----


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _wb_redirect(case_id: str, msg: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/workbench/cases/{case_id}?msg={quote(msg)}", status_code=303
    )


@app.post("/workbench/cases/{case_id}/close")
async def workbench_close_case(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    operation = form.get("operation", "complete")
    if operation not in {"complete", "terminate"}:
        return _wb_redirect(case_id, "请选择有效的案件状态操作")
    terminate = operation == "terminate"
    try:
        result = await run_in_threadpool(
            close_customer_case, settings=get_settings(), case_id=case_id,
            expected_case_version=_parse_expected_version(form.get("expected_case_version")),
            terminate=terminate, reason=form.get("reason", ""),
        )
    except (DomainError, ValueError) as exc:
        return _wb_redirect(case_id, str(exc))
    message = "案件已终止" if terminate else "案件已结束"
    if result and result.status == "provider_accepted":
        message += "，通知邮件已发出"
    elif result and "OUTBOUND_NOT_ALLOWLISTED" in result.block_reasons:
        message += "，客户邮箱不在允许发信的名单中"
    return _wb_redirect(case_id, message)


def _require_editable_workbench_case(case_id: str) -> None:
    settings = get_settings()
    try:
        case = get_case(
            database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    if case.status in FINISHED_CASE_STATUSES:
        raise HTTPException(status_code=409, detail="案件已结束，仅可查看")


_workbench_case_actions = APIRouter(
    prefix="/workbench/cases/{case_id}",
    dependencies=[Depends(_require_editable_workbench_case)],
)


_WORKBENCH_PAGE_SIZE = 20


@app.get("/workbench", response_class=HTMLResponse)
def workbench_list(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    route: str | None = None,
    waiting: str | None = None,
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    settings = get_settings()
    cases, page, total_pages = list_case_page(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        q=q,
        status=status,
        route=route,
        waiting=waiting,
        page=page,
        page_size=_WORKBENCH_PAGE_SIZE,
    )
    pending_count = count_pending_advisor_inbox(
        database_url=settings.database_url, tenant_id=settings.tenant_id,
    )
    return HTMLResponse(
        render_list(
            cases,
            alert_count=pending_count,
            page=page,
            total_pages=total_pages,
            terminated_case_ids=set(termination_reasons(
                settings, [case.id for case in cases if case.status == CaseStatus.CLOSED],
            )),
            filters={
                "q": q or "",
                "status": status or "",
                "route": route or "",
                "waiting": waiting or "",
            },
            message=request.query_params.get("msg"),
        )
    )


@app.get("/workbench/inbox", response_class=HTMLResponse)
def workbench_inbox(request: Request) -> HTMLResponse:
    """Advisor reminder page — separate from the case list home."""
    settings = get_settings()
    inbox = get_advisor_inbox(
        database_url=settings.database_url, tenant_id=settings.tenant_id,
    )
    return HTMLResponse(
        render_inbox(
            inbox.alerts, message=request.query_params.get("msg"),
            unassigned_mail=inbox.unassigned_mail, pending_count=inbox.pending_count,
        )
    )


@app.get("/workbench/inbox/mail/{email_event_id}", response_class=HTMLResponse)
def workbench_review_unassigned_mail(
    email_event_id: str, request: Request, q: str = "",
) -> HTMLResponse:
    settings = get_settings()
    keys = {"database_url": settings.database_url, "tenant_id": settings.tenant_id}
    mail = get_unassigned_mail(**keys, email_event_id=email_event_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="来信已归属或不存在")
    return HTMLResponse(render_unassigned_mail(
        mail, list_cases(**keys, q=q, active_only=True, limit=100), q=q,
        pending_count=count_pending_advisor_inbox(**keys), message=request.query_params.get("msg"),
    ))


@app.post("/workbench/inbox/mail/{email_event_id}")
async def workbench_assign_mail(email_event_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    keys = {"database_url": settings.database_url, "tenant_id": settings.tenant_id}
    if get_unassigned_mail(**keys, email_event_id=email_event_id) is None:
        return RedirectResponse("/workbench/inbox", status_code=303)
    case_id = form.get("case_id", "").strip()
    try:
        if not case_id:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请选择案件")
        attribute_inbound_to_case(
            **keys, email_event_id=email_event_id, case_id=case_id,
            reason="workbench_inbox", active_case_only=True,
        )
    except DomainError as exc:
        msg = quote(f"处理失败：{exc.message}")
        return RedirectResponse(f"/workbench/inbox/mail/{quote(email_event_id)}?msg={msg}", status_code=303)

    msg = "来信已归到案件"
    try:
        process_customer_inbound(
            settings=settings, case_id=case_id, email_event_id=email_event_id, send_reply=False,
        )
    except DomainError as exc:
        msg += f"，材料处理失败：{exc.message}"
    return RedirectResponse(f"/workbench/inbox?msg={quote(msg)}", status_code=303)


@_workbench_case_actions.post("/advisor-ack")
async def workbench_advisor_ack(case_id: str, request: Request) -> RedirectResponse:
    """Advisor marks an escalated inbox item as handled (no forced takeover)."""
    form = await _form(request)
    settings = get_settings()
    qid = (form.get("question_id") or "").strip()
    next_to = (form.get("next") or "").strip().lower()

    def _after(msg: str) -> RedirectResponse:
        qmsg = quote(msg)
        if next_to == "case":
            return RedirectResponse(
                url=f"/workbench/cases/{case_id}?msg={qmsg}#advisor-work",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/workbench/inbox?msg={qmsg}", status_code=303
        )

    if not qid:
        return _after("缺少待办 id")
    closed = acknowledge_advisor_question(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        question_id=qid,
        actor="advisor",
    )
    if closed is None:
        return _after("待办不存在或已处理")
    return _after("已标记处理")


@_workbench_case_actions.post("/evidence/{evidence_id}/classify")
async def workbench_classify_evidence(
    case_id: str, evidence_id: str, request: Request
) -> RedirectResponse:
    """Advisor assigns a material type on unclassified evidence."""
    form = await _form(request)
    settings = get_settings()
    try:
        result = advisor_classify_evidence(
            settings=settings,
            case_id=case_id,
            evidence_id=evidence_id,
            classification=str(form.get("classification") or ""),
            question_id=(form.get("question_id") or None),
        )
        label = result.evidence.classification
        msg = f"已归类为 {label}"
        if result.closed_unclassified:
            msg += "，顾问分类待办已关闭"
    except DomainError as exc:
        msg = str(exc)
    return RedirectResponse(
        url=f"/workbench/cases/{case_id}?msg={quote(msg)}#unclassified-materials",
        status_code=303,
    )


@_workbench_case_actions.post("/advisor-reply")
async def workbench_advisor_reply(case_id: str, request: Request) -> RedirectResponse:
    """Advisor answer → agent outbox → customer (not a side-channel SMTP)."""
    form = await _form(request)
    settings = get_settings()
    try:
        result = advisor_reply_via_agent(
            settings=settings,
            case_id=case_id,
            body_text=str(form.get("body") or ""),
            question_id=(form.get("question_id") or None) or None,
        )
        if result.outbox.block_reasons:
            msg = "出箱被拦住：" + ",".join(result.outbox.block_reasons)
        elif result.outbox.status == "provider_accepted":
            msg = "已由 Agent 发给客户"
            if result.closed_question:
                msg += "，提问待办已关闭"
        elif result.outbox.status in {"pending", "status_unknown"}:
            msg = "发送结果待确认"
        else:
            msg = f"发送状态：{result.outbox.status}"
    except DomainError as exc:
        msg = str(exc)
    return RedirectResponse(
        url=f"/workbench/cases/{case_id}?msg={quote(msg)}#advisor-work",
        status_code=303,
    )


@app.post("/workbench/cases")
async def workbench_create(request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        route = _parse_route_code(form.get("route"))
        if route is None:
            route = RouteCode.STANDARD_VISITOR
        email = require_customer_email(form.get("customer_email"))
        case = create_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            title=form.get("title") or "",
            customer_email=email,
            route=route,
        )
        outreach = send_opening_outreach(settings=settings, case=case)
    except DomainError as exc:
        return RedirectResponse(
            url=f"/workbench?msg={quote(str(exc))}", status_code=303
        )
    if outreach.sent:
        msg = "案件已创建，已向客户发出首封邮件"
    elif outreach.skipped_reason:
        msg = f"案件已创建，但首封邮件未发出：{outreach.skipped_reason}"
    else:
        msg = "案件已创建"
    return RedirectResponse(
        url=f"/workbench/cases/{case.id}?msg={quote(msg)}", status_code=303
    )


@app.get("/workbench/cases/{case_id}", response_class=HTMLResponse)
def workbench_detail(case_id: str, request: Request) -> HTMLResponse:
    return _render_workbench_detail(case_id, request)


def _render_workbench_detail(
    case_id: str, request: Request, *, reply_form: dict[str, str] | None = None,
    reply_message: str | None = None,
) -> HTMLResponse:
    settings = get_settings()
    try:
        case, board = get_case_review_page(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc
    can_edit = case.status not in FINISHED_CASE_STATUSES
    drafts = list_case_drafts(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        limit=5,
    )
    evidence_items = list_evidence(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    ocr_map = latest_ocr_by_evidence(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    requirement_states: list[Any] = []
    requirement_titles: dict[str, str] = {}
    completion_ready = False
    try:
        evaluated = evaluate_case_requirements(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            preview_unbound=True,
            persist_states=can_edit,
        )
        requirement_states = list(evaluated.states)
        completion_ready = (
            case.status == CaseStatus.READY_TO_DELIVER
            and evaluated.case_version == case.case_version
            and required_materials_verified(evaluated.states)
        )
        pack = get_rule_pack(
            database_url=settings.database_url,
            pack_id=evaluated.pack_id,
            pack_version=evaluated.pack_version,
        )
        requirement_titles = {r.key: r.title for r in pack.requirements}
    except DomainError:
        requirement_states = list(
            list_case_requirements(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
            )
        )
    advisor_items = []
    if can_edit:
        advisor_items = list_open_questions(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            active_only=True,
        )
    termination_reason = (
        termination_reasons(settings, [case.id]).get(case.id) if case.status == CaseStatus.CLOSED else None
    )
    msg = request.query_params.get("msg")
    reply_active = can_edit and case.human_takeover
    return HTMLResponse(
        render_detail(
            case,
            message=msg,
            drafts=drafts,
            evidence_items=evidence_items,
            review_board=board,
            ocr_by_evidence=ocr_map,
            requirement_states=requirement_states,
            requirement_titles=requirement_titles or None,
            advisor_items=advisor_items,
            itinerary=get_case_itinerary(settings=settings, case_id=case_id),
            confirmation_sent=material_confirmation_sent(settings, case),
            closure_status=closure_notice_status(settings, case)
            if case.status == CaseStatus.CLOSED and termination_reason is None else None,
            completion_ready=completion_ready,
            termination_reason=termination_reason,
            materials_message=request.query_params.get("materials_msg"),
            customer_mail=list_customer_mail(settings, case) if reply_active else None,
            reply_allowed=reply_active and normalize_email(case.customer_email) in effective_outbound_allowlist(settings),
            reply_form=reply_form,
            reply_message=reply_message or request.query_params.get("reply_msg"),
        )
    )


@_workbench_case_actions.post("/customer-email", response_class=HTMLResponse)
async def workbench_fill_customer_email(case_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    try:
        await run_in_threadpool(
            fill_takeover_customer_email,
            settings=get_settings(), case_id=case_id, customer_email=form.get("customer_email", ""),
            expected_case_version=_parse_expected_version(form.get("expected_case_version")),
        )
        message = "客户邮箱已保存"
    except DomainError as exc:
        message = f"保存失败：{exc.message}"
    return await run_in_threadpool(_render_workbench_detail, case_id, request, reply_form=form, reply_message=message)


@_workbench_case_actions.post("/takeover-reply", response_model=None)
async def workbench_takeover_reply(case_id: str, request: Request) -> RedirectResponse | HTMLResponse:
    form = await _form(request)
    try:
        result = await run_in_threadpool(
            send_takeover_reply,
            settings=get_settings(), case_id=case_id, body=form.get("body", ""),
            request_id=form.get("request_id", ""), reply_event_id=form.get("reply_event_id", ""),
            expected_case_version=_parse_expected_version(form.get("expected_case_version")),
        )
    except DomainError as exc:
        message = f"发送失败：{exc.message}"
        form.pop("request_id", None)
    else:
        if result.status == "provider_accepted" and not result.block_reasons:
            message = quote("邮件已发送")
            return RedirectResponse(f"/workbench/cases/{case_id}?reply_msg={message}#customer-reply", status_code=303)
        if result.block_reasons or result.status in {"blocked", "failed"}:
            detail = "请检查案件状态或发信设置后重试"
            if "OUTBOUND_NOT_ALLOWLISTED" in result.block_reasons:
                detail = "客户邮箱不在允许发信的名单中"
            elif "CONFLICT_VERSION" in result.block_reasons:
                detail = "案件信息有更新，请核对后重试"
            message = f"发送失败：{detail}"
            if result.status in {"blocked", "failed"}:
                form.pop("request_id", None)
        else:
            message = "发送结果待确认，请先核对发件记录"
    return await run_in_threadpool(_render_workbench_detail, case_id, request, reply_form=form, reply_message=message)


@_workbench_case_actions.post("/requirements/{requirement_key}/applicability")
async def workbench_requirement_applicability(
    case_id: str, requirement_key: str, request: Request,
) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        record_requirement_applicability(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            requirement_key=requirement_key,
            decision=form.get("decision", "invalid"),
            note=form.get("note", ""),
            expected_case_version=_parse_expected_version(form.get("expected_case_version")),
        )
        msg = "材料判断已保存"
    except DomainError as exc:
        detail = "案件有更新，请根据当前清单重新判断" if exc.code == ErrorCode.CONFLICT_VERSION else exc.message
        msg = f"保存失败：{detail}"
    return RedirectResponse(
        url=f"/workbench/cases/{case_id}?materials_msg={quote(msg)}#materials", status_code=303,
    )


@_workbench_case_actions.post("/requirements/{requirement_key}/review")
async def workbench_review_requirement(
    case_id: str, requirement_key: str, request: Request,
) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        record_requirement_review(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            requirement_key=requirement_key,
            expected_case_version=_parse_expected_version(form.get("expected_case_version")),
        )
        msg = "材料核对已保存"
    except DomainError as exc:
        detail = "材料有更新，请刷新后重新核对" if exc.code == ErrorCode.CONFLICT_VERSION else exc.message
        msg = f"保存失败：{detail}"
    return RedirectResponse(
        url=f"/workbench/cases/{case_id}?materials_msg={quote(msg)}#materials", status_code=303,
    )


@_workbench_case_actions.post("/route")
async def workbench_set_route(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        route = _parse_route_code(form.get("route"))
        if route is None:
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="route is required",
            )
        case = set_case_route(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            route=route,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = f"route set to {case.route.value if case.route else '?'}"
    except (DomainError, ValueError) as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/transition")
async def workbench_transition(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    target = form.get("target") or ""
    settings = get_settings()
    try:
        target_status = CaseStatus(target)
        if target_status == CaseStatus.CLOSED:
            return RedirectResponse(url=f"/workbench/cases/{case_id}/close", status_code=307)
        case = get_case(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
        if case.route == RouteCode.STANDARD_VISITOR and (case.status, target_status) in {
            (CaseStatus.COLLECT_AND_VERIFY, CaseStatus.DRAFT_PREPARATION),
            (CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION),
            (CaseStatus.CUSTOMER_CONFIRMATION, CaseStatus.FINAL_REVIEW),
        }:
            return _wb_redirect(case_id, "此步骤由 Agent 根据材料和客户邮件自动处理")
        apply_transition(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            target=target_status,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = f"migrated to {target}"
    except (DomainError, ValueError) as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/takeover")
async def workbench_takeover(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        set_takeover(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            active=(form.get("active") or "").lower() == "true",
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = "takeover updated"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/confirm")
async def workbench_confirm(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        version = _parse_expected_version(form.get("expected_case_version"))
        case = get_case(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
        if case.route == RouteCode.STANDARD_VISITOR and case.status in {
            CaseStatus.COLLECT_AND_VERIFY, CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION,
        }:
            return _wb_redirect(case_id, "Agent 会在收到客户确认材料的邮件后自动送终审")
        record_customer_confirmation(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            expected_case_version=version,
        )
        msg = "customer confirmed"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/reviews")
async def workbench_review(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    decision = form.get("decision") or ""
    settings = get_settings()
    try:
        keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
        version = _parse_expected_version(form.get("expected_case_version"))
        with psycopg.connect(settings.database_url) as conn:
            case = get_case(**keys, conn=conn, for_update=True)
            if case.status != CaseStatus.FINAL_REVIEW:
                raise DomainError(ErrorCode.VALIDATION_ERROR, "案件当前不在终审阶段")
            context = build_transition_context(case=case, conn=conn)
            if context.customer_confirmation_case_version != case.case_version:
                raise DomainError(ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED, "请等客户确认当前材料")
            record_final_review(**keys, decision=decision, expected_case_version=version, conn=conn)
            if decision == "approve":
                apply_transition(
                    **keys, target=CaseStatus.READY_TO_DELIVER, expected_case_version=version, conn=conn,
                )
        msg = f"review:{decision}"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/draft")
async def workbench_draft(case_id: str) -> RedirectResponse:
    settings = get_settings()
    try:
        case = get_case(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
        if case.route == RouteCode.STANDARD_VISITOR:
            return _wb_redirect(case_id, "材料核对后由 Agent 自动生成并发出确认邮件")
        draft = generate_case_draft(settings=settings, case_id=case_id)
        msg = f"draft ready ({draft.model_call_id[:8]})"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


def _workbench_evidence_path(case_id: str, evidence_id: str):
    settings = get_settings()
    evidence = get_evidence(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        evidence_id=evidence_id,
    )
    path = LocalFileStore(settings.file_storage_dir).path_for_file_id(
        evidence.file_id
    )
    if not path.is_file():
        raise DomainError(
            code=ErrorCode.NOT_FOUND,
            message="evidence file missing on disk",
            details=(evidence_id, evidence.file_id),
        )
    return settings, evidence, path


@_workbench_case_actions.post("/evidence/{evidence_id}/delete")
async def workbench_delete_evidence(case_id: str, evidence_id: str, request: Request) -> RedirectResponse:
    settings = get_settings()
    form = await _form(request)
    try:
        delete_evidence(
            database_url=settings.database_url, tenant_id=settings.tenant_id,
            case_id=case_id, evidence_id=evidence_id,
            expected_case_version=_parse_expected_version(form.get("expected_case_version")),
        )
        message = "材料已从案件移除，原审批需重新确认"
    except DomainError as exc:
        message = str(exc)
    return _wb_redirect(case_id, message)


@app.get("/workbench/cases/{case_id}/evidence/{evidence_id}/file")
def workbench_evidence_file(
    case_id: str, evidence_id: str, download: int = 0
) -> FileResponse:
    """Serve original bytes for in-browser view or forced download."""
    try:
        _settings, evidence, path = _workbench_evidence_path(case_id, evidence_id)
    except DomainError as exc:
        raise _domain_http(exc) from exc
    headers = {}
    filename = evidence.original_filename or "evidence.bin"
    return FileResponse(
        path,
        media_type=evidence.content_type or "application/octet-stream",
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
        headers=headers,
    )


@app.get(
    "/workbench/cases/{case_id}/evidence/{evidence_id}/view",
    response_class=HTMLResponse,
)
def workbench_evidence_view(case_id: str, evidence_id: str) -> HTMLResponse:
    """Open material in workbench chrome — text inline, else iframe/download."""
    settings = get_settings()
    try:
        case = get_case(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
        )
        evidence = get_evidence(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            evidence_id=evidence_id,
        )
    except DomainError as exc:
        raise _domain_http(exc) from exc

    path = LocalFileStore(settings.file_storage_dir).path_for_file_id(
        evidence.file_id
    )
    if not path.is_file():
        # Stale DB row / moved storage — don't dump a bare 404 JSON page.
        missing = "这份材料的原件在服务器上找不到了。"
        if case.status not in FINISHED_CASE_STATUSES:
            missing += "请回到案件，在对应清单项里重新从本地上传。"
        return HTMLResponse(
            render_evidence_view(
                case,
                evidence=evidence,
                text_preview=missing,
                can_inline_frame=False,
            ),
            status_code=404,
        )

    ctype = (evidence.content_type or "").lower()
    name = (evidence.original_filename or "").lower()
    text_preview: str | None = None
    can_inline = False
    if (
        ctype.startswith("text/")
        or name.endswith((".txt", ".csv", ".md", ".json", ".xml", ".html", ".htm"))
        or ctype in {"application/json", "application/xml"}
    ):
        text_preview = path.read_text(encoding="utf-8", errors="replace")
    elif ctype.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp")
    ):
        can_inline = True
    elif ctype == "application/pdf" or name.endswith(".pdf"):
        can_inline = True
    viewed = evidence.quarantine_status != "rejected" and (text_preview is not None or can_inline)
    if viewed and not evidence.viewed_at:
        mark_evidence_viewed(
            database_url=settings.database_url, tenant_id=settings.tenant_id,
            case_id=case_id, evidence_id=evidence_id,
        )
    return HTMLResponse(
        render_evidence_view(
            case,
            evidence=evidence,
            text_preview=text_preview,
            can_inline_frame=can_inline,
            viewed=viewed,
        ),
        headers={"Cache-Control": "no-store"},
    )


@_workbench_case_actions.post("/evidence")
async def workbench_attach_evidence(
    case_id: str, request: Request
) -> RedirectResponse:
    """Accept a real local file upload (multipart) into quarantine storage."""
    form = await request.form()
    settings = get_settings()
    try:
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="请选择要上传的本地文件",
            )
        data = await upload.read()  # type: ignore[misc]
        filename = (getattr(upload, "filename", None) or "").strip() or "upload.bin"
        translation_draft_id = str(form.get("translation_draft_id") or "").strip() or None
        content_type = (
            getattr(upload, "content_type", None) or "application/octet-stream"
        )
        store = LocalFileStore(settings.file_storage_dir)
        evidence = attach_evidence(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            file_store=store,
            data=data,
            original_filename=filename,
            content_type=str(content_type),
            classification=str(form.get("classification") or "unclassified"),
            expected_case_version=_parse_expected_version(
                str(form.get("expected_case_version") or "") or None
            ),
            translation_draft_id=translation_draft_id,
        )
        # Best-effort OCR so「查看」旁状态能更新；失败不挡上传。
        if not translation_draft_id:
            try:
                run_ocr_on_evidence(
                    database_url=settings.database_url,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    evidence_id=evidence.id,
                    file_store=store,
                    expected_case_version=evidence.case_version,
                )
            except DomainError:
                pass
        msg = f"已上传：{filename}"
    except DomainError as exc:
        msg = str(exc)
    except Exception as exc:  # noqa: BLE001 — surface upload IO errors in flash
        msg = f"上传失败：{exc}"
    if form.get("translation_draft_id"):
        return RedirectResponse(
            url=f"/workbench/cases/{case_id}?materials_msg={quote(msg)}#translation-materials", status_code=303,
        )
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/evidence/{evidence_id}/annotations")
async def workbench_annotate(
    case_id: str, evidence_id: str, request: Request
) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        ann = record_annotation(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            evidence_id=evidence_id,
            field_key=form.get("field_key") or "",
            field_value=form.get("field_value") or "",
            note=form.get("note") or "",
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = f"annotated {ann.field_key} @ v{ann.case_version}"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/evidence/{evidence_id}/ocr")
async def workbench_run_ocr(
    case_id: str, evidence_id: str, request: Request
) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        ocr = run_ocr_on_evidence(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            evidence_id=evidence_id,
            file_store=LocalFileStore(settings.file_storage_dir),
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        preview = (ocr.extracted_text or "")[:80].replace("\n", " ")
        msg = (
            f"OCR {ocr.status} via {ocr.provider} @ v{ocr.case_version}"
            + (f": {preview}" if preview else "")
        )
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@_workbench_case_actions.post("/bump-version")
async def workbench_bump(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        case = bump_case_version(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        return _wb_redirect(case.id, "version bumped")
    except DomainError as exc:
        return _wb_redirect(case_id, str(exc))


@_workbench_case_actions.post("/receipt")
async def workbench_receipt(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        record_delivery_receipt(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
            expected_delivery_id=form.get("expected_delivery_id") or None,
        )
        msg = "receipt recorded"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


app.include_router(_workbench_case_actions)
