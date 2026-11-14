"""FastAPI entrypoint for S1."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from visa_agent.config import get_settings
from visa_agent.email_adapter import (
    InboundEmail,
    OutboxRequest,
    enqueue_and_maybe_send,
    persist_inbound,
)
from visa_agent.http_app import build_health
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations

boot = get_settings()
boot.file_storage_dir.mkdir(parents=True, exist_ok=True)
apply_migrations(boot.database_url)

app = FastAPI(title="visa-email-agent", version="0.2.2")


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


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "service": "visa-email-agent",
            "stage": "S1.2",
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
    # Content / kind mismatch on replay is a hard client error.
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
    # Gate rejection (including live re-check on retry) must not look successful.
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
