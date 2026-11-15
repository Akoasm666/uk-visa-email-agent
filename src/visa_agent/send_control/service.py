"""W10 send control — case-bound package outbound with W00 gates."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable

import psycopg

from visa_agent.case_service.service import get_case
from visa_agent.config import Settings
from visa_agent.delivery.service import get_package, verify_package_bytes
from visa_agent.domain.enums import CaseStatus, DeliveryKind, ErrorCode, OutboundMessageKind
from visa_agent.domain.errors import DomainError
from visa_agent.domain.models import FinalPackageApproval, OutboundGateInput, evaluate_outbound_gate
from visa_agent.email_adapter.allowlist import effective_outbound_allowlist
from visa_agent.email_adapter.errors import (
    SendPartialAcceptedError,
    SendStatusUnknownError,
)
from visa_agent.email_adapter.outbox import OutboxResult, build_send_operation_id
from visa_agent.email_adapter.smtp_client import send_smtp
from visa_agent.storage.files import LocalFileStore

_NO_RESEND_STATUSES = frozenset(
    {
        "processing",
        "provider_accepted",
        "status_unknown",
        "simulated",
        "partial_accepted",
    }
)

_FINAL_SEND_STATUSES = frozenset(
    {
        CaseStatus.READY_TO_DELIVER.value,
        CaseStatus.SENT_AWAITING_ACK.value,
    }
)

Attachment = tuple[str, bytes]
SendFn = Callable[[Settings, list[str], str, str, list[Attachment]], str]


@dataclass(frozen=True, slots=True)
class PackageSendRequest:
    package_id: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    send_operation_id: str | None = None
    send_now: bool = True


def _parse_recipients(raw: object) -> frozenset[str]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not raw:
        return frozenset()
    return frozenset(str(r).strip().lower() for r in raw)  # type: ignore[union-attr]


def _normalize_recipients(recipients: object) -> tuple[str, ...]:
    if isinstance(recipients, str):
        recipients = json.loads(recipients)
    return tuple(sorted(str(r).strip().lower() for r in recipients))  # type: ignore[union-attr]


def _content_matches(
    *,
    case_id: str,
    package_id: str,
    case_version: int,
    recipients: tuple[str, ...],
    subject: str,
    body_text: str,
    row: tuple,
) -> bool:
    # row: id, status, block_reasons, provider_response, email_event_id,
    #      send_operation_id, recipients, subject, body_text, kind,
    #      case_id, case_version, package_id
    return (
        (row[10] or "") == case_id
        and (row[12] or "") == package_id
        and int(row[11] or 0) == case_version
        and _normalize_recipients(row[6]) == recipients
        and (row[7] or "") == subject
        and (row[8] or "") == body_text
    )


def _select_sql() -> str:
    return """
        SELECT id, status, block_reasons, provider_response, email_event_id,
               send_operation_id, recipients, subject, body_text, kind,
               case_id, case_version, package_id
        FROM outbox_messages
        WHERE tenant_id = %s AND send_operation_id = %s
    """


def _row_to_result(
    row: tuple,
    *,
    resent_blocked: bool = False,
    block_reasons: tuple[str, ...] | None = None,
    status: str | None = None,
) -> OutboxResult:
    return OutboxResult(
        outbox_id=row[0],
        status=status if status is not None else row[1],
        block_reasons=block_reasons if block_reasons is not None else tuple(row[2] or []),
        provider_response=row[3],
        email_event_id=row[4],
        send_operation_id=row[5],
        resent_blocked=resent_blocked,
    )


def _load_final_package_approval(
    *,
    database_url: str,
    tenant_id: str,
    case_id: str,
    case_version: int,
    package_id: str,
    conn: psycopg.Connection | None = None,
) -> FinalPackageApproval | None:
    """Latest same-version decision wins for both case-level and package reviews.

    A later workbench/case reject (``package_id`` NULL) supersedes a prior
    package approve. Package send still requires a matching package-bound approve
    that is not superseded.
    """

    def _run(c: psycopg.Connection) -> FinalPackageApproval | None:
        # Single source of truth: latest same-version review (case or package).
        latest = c.execute(
            """
            SELECT decision, case_version, package_id, recipients
            FROM case_final_reviews
            WHERE tenant_id = %s AND case_id = %s
              AND case_version = %s
            ORDER BY decision_seq DESC
            LIMIT 1
            """,
            (tenant_id, case_id, case_version),
        ).fetchone()
        if latest is None or (latest[0] or "") != "approve":
            return None
        # Case-level approve alone cannot authorize a package send.
        if not latest[2]:
            return None
        recipients = _parse_recipients(latest[3])
        if not recipients:
            return None
        # Wrong package_id → gate yields FINAL_PACKAGE_APPROVAL_MISMATCH.
        return FinalPackageApproval(
            case_version=int(latest[1]),
            package_id=str(latest[2]),
            recipient_emails=recipients,
        )

    if conn is not None:
        return _run(conn)
    with psycopg.connect(database_url) as c:
        return _run(c)


def _has_customer_confirmation(
    conn: psycopg.Connection,
    *,
    tenant_id: str,
    case_id: str,
    case_version: int,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM case_customer_confirmations
        WHERE tenant_id = %s AND case_id = %s AND case_version = %s
        LIMIT 1
        """,
        (tenant_id, case_id, case_version),
    ).fetchone()
    return row is not None


def _has_unprocessed_inbound(
    conn: psycopg.Connection,
    *,
    tenant_id: str,
    case_id: str,
    package_id: str | None = None,
) -> bool:
    """Any attributed inbound still marked unprocessed blocks send.

    Only an explicit mark_inbound_processed clears the gate — assemble does not.
    ``package_id`` is unused but kept for call-site compatibility.
    """
    del package_id  # gate is case-scoped processing status, not package timestamp
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM email_events e
            WHERE e.tenant_id = %s
              AND e.case_id = %s
              AND e.direction = 'inbound'
              AND e.attribution_status = 'attributed'
              AND e.processing_status = 'unprocessed'
        )
        """,
        (tenant_id, case_id),
    ).fetchone()
    return bool(row and row[0])


def _case_readiness_reasons(
    *,
    case_status: str,
    package_kind: str,
    has_confirmation: bool,
    has_unprocessed_inbound: bool,
) -> list[str]:
    reasons: list[str] = []
    if has_unprocessed_inbound:
        reasons.append("DELIVERY_GATE_FAILED")
    if package_kind == DeliveryKind.FINAL_PACKAGE.value:
        if case_status not in _FINAL_SEND_STATUSES:
            reasons.append("DELIVERY_GATE_FAILED")
        if not has_confirmation:
            reasons.append("CUSTOMER_CONFIRMATION_REQUIRED")
    return reasons


def _evaluate_gates(
    *,
    settings: Settings,
    case: object,
    package: object,
    recipients: tuple[str, ...],
    kind: str,
    conn: psycopg.Connection,
) -> tuple[str, ...]:
    case_id = case.id  # type: ignore[attr-defined]
    case_version = int(case.case_version)  # type: ignore[attr-defined]
    package_id = package.package_id  # type: ignore[attr-defined]
    package_kind = package.package_kind  # type: ignore[attr-defined]

    if int(package.case_version) != case_version:  # type: ignore[attr-defined]
        return ("CONFLICT_VERSION",)

    approval = None
    if package_kind == DeliveryKind.FINAL_PACKAGE.value:
        approval = _load_final_package_approval(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
            case_id=case_id,
            case_version=case_version,
            package_id=package_id,
            conn=conn,
        )

    has_conf = _has_customer_confirmation(
        conn,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        case_version=case_version,
    )
    unprocessed = _has_unprocessed_inbound(
        conn,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        package_id=package_id,
    )
    readiness = _case_readiness_reasons(
        case_status=case.status.value,  # type: ignore[attr-defined]
        package_kind=package_kind,
        has_confirmation=has_conf,
        has_unprocessed_inbound=unprocessed,
    )

    gate = OutboundGateInput(
        kind=kind,
        recipient_emails=frozenset(recipients),
        allowlist=effective_outbound_allowlist(settings),
        human_takeover=bool(case.human_takeover),  # type: ignore[attr-defined]
        human_send_approved=False,
        case_version=case_version,
        outbox_case_version=case_version,
        has_unprocessed_inbound=unprocessed,
        system_blocked=bool(case.system_blocked),  # type: ignore[attr-defined]
        paused=bool(case.paused),  # type: ignore[attr-defined]
        cancelled=bool(case.cancelled),  # type: ignore[attr-defined]
        package_id=package_id,
        package_kind=package_kind,
        final_package_approval=approval,
    )
    return tuple(dict.fromkeys([*evaluate_outbound_gate(gate), *readiness]))


def _mark_blocked(
    *,
    settings: Settings,
    outbox_id: str,
    reasons: tuple[str, ...],
) -> None:
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'blocked',
                block_reasons = %s::jsonb,
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'failed', 'blocked')
            """,
            (json.dumps(list(reasons)), outbox_id),
        )
        conn.commit()


def _reopen_pending(*, settings: Settings, outbox_id: str) -> None:
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'pending',
                block_reasons = '[]'::jsonb,
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'failed', 'blocked')
            """,
            (outbox_id,),
        )
        conn.commit()


def _link_case_delivery(
    conn: psycopg.Connection,
    *,
    tenant_id: str,
    case_id: str,
    case_version: int,
    package_id: str,
    outbox_id: str,
    simulated: bool,
    package_kind: str,
) -> str:
    """Persist send history; only final packages may become current delivery.

    If the case version advanced during SMTP, keep history but do not replace
    ``current_delivery_id`` on the newer version.
    """
    delivery_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO case_deliveries (
            id, tenant_id, case_id, case_version,
            sent_at, simulated_send, package_id, outbox_id
        ) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s)
        """,
        (
            delivery_id,
            tenant_id,
            case_id,
            case_version,
            simulated,
            package_id,
            outbox_id,
        ),
    )
    if package_kind == DeliveryKind.FINAL_PACKAGE.value:
        conn.execute(
            """
            UPDATE cases
            SET current_delivery_id = %s, updated_at = NOW()
            WHERE tenant_id = %s AND id = %s AND case_version = %s
            """,
            (delivery_id, tenant_id, case_id, case_version),
        )
    return delivery_id


def send_case_package(
    *,
    settings: Settings,
    case_id: str,
    req: PackageSendRequest,
    file_store: LocalFileStore | None = None,
    send_fn: SendFn | None = None,
) -> OutboxResult:
    """Enqueue (and optionally send) a delivery package under W00 hard gates."""
    store = file_store or LocalFileStore(settings.file_storage_dir)
    case = get_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
    )
    package = get_package(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        case_id=case_id,
        package_id=req.package_id,
    )
    if package.case_version != case.case_version:
        raise DomainError(
            code=ErrorCode.CONFLICT_VERSION,
            message="package was assembled for a different case_version",
            details=(str(package.case_version), str(case.case_version)),
        )

    if package.package_kind == DeliveryKind.FINAL_PACKAGE.value:
        kind = OutboundMessageKind.FINAL_PACKAGE.value
    else:
        kind = OutboundMessageKind.PROGRESS_PACKAGE.value

    recipients = tuple(sorted(r.strip().lower() for r in req.recipients if r.strip()))
    if not recipients:
        raise DomainError(
            code=ErrorCode.VALIDATION_ERROR,
            message="recipients required",
        )

    # Fail-fast byte check before enqueue (also re-checked under claim).
    verify_package_bytes(file_store=store, package=package)

    op_id = build_send_operation_id(
        tenant_id=settings.tenant_id,
        kind=kind,
        recipients=recipients,
        subject=req.subject,
        body_text=req.body_text,
        explicit=req.send_operation_id,
        case_id=case_id,
        case_version=case.case_version,
        package_id=package.package_id,
    )

    with psycopg.connect(settings.database_url) as conn:
        existing = conn.execute(
            _select_sql(), (settings.tenant_id, op_id)
        ).fetchone()

    if existing is not None:
        if not _content_matches(
            case_id=case_id,
            package_id=package.package_id,
            case_version=case.case_version,
            recipients=recipients,
            subject=req.subject,
            body_text=req.body_text,
            row=existing,
        ):
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    "send_operation_id already bound to a different "
                    "case/package/version/recipients/content"
                ),
                details=(op_id,),
            )
        if existing[1] in _NO_RESEND_STATUSES:
            return _row_to_result(existing, resent_blocked=True)

        with psycopg.connect(settings.database_url) as conn:
            case = get_case(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            package = get_package(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                package_id=req.package_id,
            )
            reasons = _evaluate_gates(
                settings=settings,
                case=case,
                package=package,
                recipients=recipients,
                kind=kind,
                conn=conn,
            )
        if reasons:
            _mark_blocked(settings=settings, outbox_id=existing[0], reasons=reasons)
            return OutboxResult(
                outbox_id=existing[0],
                status="blocked",
                block_reasons=reasons,
                provider_response=existing[3],
                email_event_id=existing[4],
                send_operation_id=existing[5],
                resent_blocked=True,
            )
        _reopen_pending(settings=settings, outbox_id=existing[0])
        outbox_id = existing[0]
    else:
        with psycopg.connect(settings.database_url) as conn:
            with conn.transaction():
                case = get_case(
                    database_url=settings.database_url,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    conn=conn,
                    for_update=True,
                )
                package = get_package(
                    database_url=settings.database_url,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    package_id=req.package_id,
                )
                reasons = _evaluate_gates(
                    settings=settings,
                    case=case,
                    package=package,
                    recipients=recipients,
                    kind=kind,
                    conn=conn,
                )
                outbox_id = uuid.uuid4().hex
                status = "blocked" if reasons else "pending"
                conn.execute(
                    """
                    INSERT INTO outbox_messages (
                        id, tenant_id, send_operation_id, case_id, case_version,
                        kind, recipients, subject, body_text,
                        package_id, package_kind, status, block_reasons
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s::jsonb, %s, %s,
                        %s, %s, %s, %s::jsonb
                    )
                    ON CONFLICT (tenant_id, send_operation_id) DO NOTHING
                    """,
                    (
                        outbox_id,
                        settings.tenant_id,
                        op_id,
                        case_id,
                        case.case_version,
                        kind,
                        json.dumps(list(recipients)),
                        req.subject,
                        req.body_text,
                        package.package_id,
                        package.package_kind,
                        status,
                        json.dumps(list(reasons)),
                    ),
                )
                row = conn.execute(
                    _select_sql(), (settings.tenant_id, op_id)
                ).fetchone()
                assert row is not None
                if not _content_matches(
                    case_id=case_id,
                    package_id=package.package_id,
                    case_version=case.case_version,
                    recipients=recipients,
                    subject=req.subject,
                    body_text=req.body_text,
                    row=row,
                ):
                    raise DomainError(
                        code=ErrorCode.VALIDATION_ERROR,
                        message=(
                            "send_operation_id already bound to a different "
                            "case/package/version/recipients/content"
                        ),
                        details=(op_id,),
                    )
                outbox_id = row[0]
                status = row[1]
                reasons = tuple(row[2] or [])
                if status in _NO_RESEND_STATUSES:
                    return _row_to_result(row, resent_blocked=True)

        if reasons or status == "blocked":
            return OutboxResult(
                outbox_id=outbox_id,
                status="blocked",
                block_reasons=tuple(reasons),
                provider_response=None,
                email_event_id=None,
                send_operation_id=op_id,
            )

    if not req.send_now:
        return OutboxResult(
            outbox_id=outbox_id,
            status="pending",
            block_reasons=(),
            provider_response=None,
            email_event_id=None,
            send_operation_id=op_id,
        )

    # --- Atomic claim + live gate re-check + byte verify ---
    with psycopg.connect(settings.database_url) as conn:
        with conn.transaction():
            case = get_case(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                conn=conn,
                for_update=True,
            )
            package = get_package(
                database_url=settings.database_url,
                tenant_id=settings.tenant_id,
                case_id=case_id,
                package_id=req.package_id,
            )
            frozen = conn.execute(
                """
                SELECT recipients, subject, body_text, case_id, case_version, package_id
                FROM outbox_messages
                WHERE id = %s
                FOR UPDATE
                """,
                (outbox_id,),
            ).fetchone()
            assert frozen is not None
            frozen_recipients = _normalize_recipients(frozen[0])
            frozen_subject = frozen[1]
            frozen_body = frozen[2]
            if (
                (frozen[3] or "") != case_id
                or (frozen[5] or "") != package.package_id
                or int(frozen[4] or 0) != case.case_version
                or frozen_recipients != recipients
                or frozen_subject != req.subject
                or frozen_body != req.body_text
            ):
                raise DomainError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="outbox frozen content does not match send request",
                    details=(op_id,),
                )

            reasons = _evaluate_gates(
                settings=settings,
                case=case,
                package=package,
                recipients=frozen_recipients,
                kind=kind,
                conn=conn,
            )
            if reasons:
                conn.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'blocked',
                        block_reasons = %s::jsonb,
                        updated_at = NOW()
                    WHERE id = %s
                      AND status IN ('pending', 'failed', 'blocked')
                    """,
                    (json.dumps(list(reasons)), outbox_id),
                )
                return OutboxResult(
                    outbox_id=outbox_id,
                    status="blocked",
                    block_reasons=reasons,
                    provider_response=None,
                    email_event_id=None,
                    send_operation_id=op_id,
                    resent_blocked=True,
                )

            attachments = verify_package_bytes(file_store=store, package=package)

            claimed = conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'processing',
                    block_reasons = '[]'::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                  AND status IN ('pending', 'failed', 'blocked')
                RETURNING id
                """,
                (outbox_id,),
            ).fetchone()
            if claimed is None:
                row = conn.execute(
                    _select_sql(), (settings.tenant_id, op_id)
                ).fetchone()
                assert row is not None
                return _row_to_result(row, resent_blocked=True)

            claim_case_version = case.case_version
            claim_package_id = package.package_id
            claim_package_kind = package.package_kind

    dispatcher: SendFn
    if send_fn is not None:
        dispatcher = send_fn
    elif settings.email_configured:
        def _smtp_dispatch(
            s: Settings,
            recips: list[str],
            subject: str,
            body: str,
            atts: list[Attachment],
        ) -> str:
            return send_smtp(
                s,
                recipients=recips,
                subject=subject,
                body_text=body,
                attachments=atts,
            )

        dispatcher = _smtp_dispatch
    else:
        dispatcher = None  # type: ignore[assignment]

    # Simulated path — no SMTP configured; never report provider_accepted.
    if dispatcher is None:
        with psycopg.connect(settings.database_url) as conn:
            with conn.transaction():
                case = get_case(
                    database_url=settings.database_url,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    conn=conn,
                    for_update=True,
                )
                if _has_unprocessed_inbound(
                    conn, tenant_id=settings.tenant_id, case_id=case_id
                ):
                    conn.execute(
                        """
                        UPDATE outbox_messages
                        SET status = 'blocked',
                            block_reasons = %s::jsonb,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (json.dumps(["DELIVERY_GATE_FAILED"]), outbox_id),
                    )
                    return OutboxResult(
                        outbox_id=outbox_id,
                        status="blocked",
                        block_reasons=("DELIVERY_GATE_FAILED",),
                        provider_response=None,
                        email_event_id=None,
                        send_operation_id=op_id,
                        resent_blocked=True,
                    )
                _link_case_delivery(
                    conn,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    case_version=claim_case_version,
                    package_id=claim_package_id,
                    outbox_id=outbox_id,
                    simulated=True,
                    package_kind=claim_package_kind,
                )
                conn.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'simulated',
                        provider_response = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        f"simulated-send;attachments={len(attachments)}",
                        outbox_id,
                    ),
                )
        return OutboxResult(
            outbox_id=outbox_id,
            status="simulated",
            block_reasons=(),
            provider_response=f"simulated-send;attachments={len(attachments)}",
            email_event_id=None,
            send_operation_id=op_id,
        )

    try:
        provider_response = dispatcher(
            settings,
            list(frozen_recipients),
            frozen_subject,
            frozen_body,
            attachments,
        )
    except SendPartialAcceptedError as exc:
        note = (exc.provider_response or str(exc))[:500]
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'partial_accepted',
                    provider_response = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (note, outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status="partial_accepted",
            block_reasons=(),
            provider_response=note,
            email_event_id=None,
            send_operation_id=op_id,
        )
    except SendStatusUnknownError as exc:
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'status_unknown',
                    provider_response = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(exc)[:500], outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status="status_unknown",
            block_reasons=(),
            provider_response=str(exc)[:500],
            email_event_id=None,
            send_operation_id=op_id,
        )
    except Exception as exc:  # noqa: BLE001
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'failed',
                    provider_response = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(exc)[:500], outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status="failed",
            block_reasons=(),
            provider_response=str(exc)[:500],
            email_event_id=None,
            send_operation_id=op_id,
        )

    email_event_id = uuid.uuid4().hex
    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.transaction():
                get_case(
                    database_url=settings.database_url,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    conn=conn,
                    for_update=True,
                )
                if _has_unprocessed_inbound(
                    conn, tenant_id=settings.tenant_id, case_id=case_id
                ):
                    # Provider may have accepted; local policy fails current delivery link.
                    conn.execute(
                        """
                        UPDATE outbox_messages
                        SET status = 'status_unknown',
                            provider_response = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            f"{provider_response};unprocessed_inbound_after_send",
                            outbox_id,
                        ),
                    )
                    return OutboxResult(
                        outbox_id=outbox_id,
                        status="status_unknown",
                        block_reasons=(),
                        provider_response=(
                            f"{provider_response};unprocessed_inbound_after_send"
                        ),
                        email_event_id=None,
                        send_operation_id=op_id,
                    )
                conn.execute(
                    """
                    INSERT INTO email_events (
                        id, tenant_id, direction, provider_message_id,
                        dedupe_key, from_addr, to_addrs, subject, body_text,
                        case_id, attribution_status, processing_status
                    ) VALUES (
                        %s, %s, 'outbound', %s,
                        %s, %s, %s::jsonb, %s, %s,
                        %s, 'attributed', 'processed'
                    )
                    """,
                    (
                        email_event_id,
                        settings.tenant_id,
                        provider_response,
                        f"outbox:{outbox_id}",
                        settings.email_from,
                        json.dumps(list(frozen_recipients)),
                        frozen_subject,
                        frozen_body,
                        case_id,
                    ),
                )
                _link_case_delivery(
                    conn,
                    tenant_id=settings.tenant_id,
                    case_id=case_id,
                    case_version=claim_case_version,
                    package_id=claim_package_id,
                    outbox_id=outbox_id,
                    simulated=False,
                    package_kind=claim_package_kind,
                )
                conn.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'provider_accepted',
                        provider_response = %s,
                        email_event_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (provider_response, email_event_id, outbox_id),
                )
        return OutboxResult(
            outbox_id=outbox_id,
            status="provider_accepted",
            block_reasons=(),
            provider_response=provider_response,
            email_event_id=email_event_id,
            send_operation_id=op_id,
        )
    except Exception as exc:  # noqa: BLE001 — provider may have accepted
        note = f"provider_accepted_locally_unconfirmed:{exc}"
        with psycopg.connect(settings.database_url) as conn:
            conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'status_unknown',
                    provider_response = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (note[:500], outbox_id),
            )
            conn.commit()
        return OutboxResult(
            outbox_id=outbox_id,
            status="status_unknown",
            block_reasons=(),
            provider_response=note[:500],
            email_event_id=None,
            send_operation_id=op_id,
        )
