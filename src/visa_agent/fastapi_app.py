"""FastAPI entrypoint for S1."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from visa_agent.ai_capabilities import generate_case_draft, list_case_drafts
from visa_agent.case_service import (
    attribute_inbound_to_case,
    create_case,
    get_case,
    list_cases,
    record_customer_confirmation,
    record_delivery_receipt,
    record_final_review,
    record_reopen_grant,
    set_takeover,
)
from visa_agent.case_service.service import bump_case_version
from visa_agent.config import get_settings
from visa_agent.domain.enums import CaseStatus, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter import (
    InboundEmail,
    OutboxRequest,
    enqueue_and_maybe_send,
    persist_inbound,
    poll_imap_inbox,
)
from visa_agent.http_app import build_health
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations
from visa_agent.workbench.pages import render_detail, render_list
from visa_agent.workflow.transitions import apply_transition, list_allowed_targets

boot = get_settings()
boot.file_storage_dir.mkdir(parents=True, exist_ok=True)
apply_migrations(boot.database_url)

app = FastAPI(title="visa-email-agent", version="0.4.0")


class InboundBody(BaseModel):
    from_addr: str
    to_addrs: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    rfc_message_id: str | None = None
    provider_message_id: str | None = None
    claimed_case_id: str | None = None


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


class ConfirmBody(BaseModel):
    expected_case_version: int


class AttributeBody(BaseModel):
    email_event_id: str
    force_reattribute: bool = False
    reason: str = ""


class PollBody(BaseModel):
    limit: int = 10


def _domain_http(exc: DomainError) -> HTTPException:
    if exc.code == ErrorCode.NOT_FOUND:
        status = 404
    elif exc.code == ErrorCode.CONFLICT_VERSION:
        status = 409
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


def _parse_expected_closure_seq(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="expected_closure_seq must be an integer",
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
            "stage": "S1.4",
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
        ),
        file_store=LocalFileStore(settings.file_storage_dir),
    )
    return {
        "success": True,
        "status_code_hint": 200 if result.duplicate else 201,
        "data": {
            "email_event_id": result.email_event_id,
            "dedupe_key": result.dedupe_key,
            "attribution_status": result.attribution_status,
            "duplicate": result.duplicate,
            "raw_file_id": result.raw_file_id,
        },
    }


@app.post("/internal/email/outbox")
def outbox_email(body: OutboxBody) -> dict[str, Any]:
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
def api_list_cases() -> dict[str, Any]:
    settings = get_settings()
    cases = list_cases(
        database_url=settings.database_url, tenant_id=settings.tenant_id
    )
    return {"success": True, "data": [_case_payload(c) for c in cases]}


@app.post("/cases")
def api_create_case(body: CreateCaseBody) -> dict[str, Any]:
    settings = get_settings()
    case = create_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        title=body.title,
        customer_email=body.customer_email,
    )
    return {"success": True, "data": _case_payload(case)}


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
def api_review(case_id: str, body: ReviewBody) -> dict[str, Any]:
    settings = get_settings()
    try:
        record_final_review(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            decision=body.decision,
            note=body.note,
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
    except DomainError as exc:
        raise _domain_http(exc) from exc
    return {
        "success": True,
        "data": {"case_id": case_id, "email_event_id": body.email_event_id},
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


@app.get("/workbench", response_class=HTMLResponse)
def workbench_list() -> HTMLResponse:
    settings = get_settings()
    cases = list_cases(
        database_url=settings.database_url, tenant_id=settings.tenant_id
    )
    return HTMLResponse(render_list(cases))


@app.post("/workbench/cases")
async def workbench_create(request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    case = create_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        title=form.get("title") or "",
        customer_email=form.get("customer_email") or None,
    )
    return RedirectResponse(url=f"/workbench/cases/{case.id}", status_code=303)


@app.get("/workbench/cases/{case_id}", response_class=HTMLResponse)
def workbench_detail(case_id: str, request: Request) -> HTMLResponse:
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
    drafts = list_case_drafts(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        limit=5,
    )
    msg = request.query_params.get("msg")
    return HTMLResponse(
        render_detail(
            case,
            allowed=allowed,
            message=msg,
            drafts=drafts,
        )
    )


@app.post("/workbench/cases/{case_id}/transition")
async def workbench_transition(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    target = form.get("target") or ""
    settings = get_settings()
    try:
        apply_transition(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            target=CaseStatus(target),
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = f"migrated to {target}"
    except (DomainError, ValueError) as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@app.post("/workbench/cases/{case_id}/takeover")
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


@app.post("/workbench/cases/{case_id}/confirm")
async def workbench_confirm(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        record_customer_confirmation(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = "customer confirmed"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@app.post("/workbench/cases/{case_id}/reviews")
async def workbench_review(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    decision = form.get("decision") or ""
    settings = get_settings()
    try:
        record_final_review(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            decision=decision,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
        )
        msg = f"review:{decision}"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@app.post("/workbench/cases/{case_id}/reopen-grant")
async def workbench_reopen(case_id: str, request: Request) -> RedirectResponse:
    form = await _form(request)
    settings = get_settings()
    try:
        record_reopen_grant(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            expected_case_version=_parse_expected_version(
                form.get("expected_case_version")
            ),
            expected_closure_seq=_parse_expected_closure_seq(
                form.get("expected_closure_seq")
            ),
        )
        msg = "reopen authorized"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@app.post("/workbench/cases/{case_id}/draft")
async def workbench_draft(case_id: str) -> RedirectResponse:
    settings = get_settings()
    try:
        draft = generate_case_draft(settings=settings, case_id=case_id)
        msg = f"draft ready ({draft.model_call_id[:8]})"
    except DomainError as exc:
        msg = str(exc)
    return _wb_redirect(case_id, msg)


@app.post("/workbench/cases/{case_id}/bump-version")
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


@app.post("/workbench/cases/{case_id}/receipt")
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
