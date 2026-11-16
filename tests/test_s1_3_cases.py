from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from visa_agent.case_service import (
    attribute_inbound_to_case,
    create_case,
    get_case,
    record_customer_confirmation,
    record_delivery_receipt,
    record_final_review,
    record_reopen_grant,
    set_takeover,
)
from visa_agent.case_service.service import bump_case_version
from visa_agent.config import Settings, get_settings
from visa_agent.delivery import assemble_package
from visa_agent.document_pipeline import attach_evidence, set_evidence_quarantine
from visa_agent.domain.enums import CaseStatus, DeliveryKind, ErrorCode, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.inbound import InboundEmail, persist_inbound
from visa_agent.rules.service import ensure_seed_route_packs, record_requirement_applicability, record_requirement_review
from visa_agent.send_control import PackageSendRequest, send_case_package
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations
from visa_agent.workflow.transitions import apply_transition, list_allowed_targets

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _ver(case_id: str) -> int:
    return get_case(database_url=DB, tenant_id=TENANT, case_id=case_id).case_version


def _confirm(case_id: str) -> None:
    record_customer_confirmation(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        expected_case_version=_ver(case_id),
    )


def _approve(case_id: str) -> None:
    record_final_review(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        decision="approve",
        expected_case_version=_ver(case_id),
    )


def _reject(case_id: str) -> None:
    record_final_review(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        decision="reject",
        expected_case_version=_ver(case_id),
    )


def _walk_to_customer_confirmation(case_id: str) -> None:
    for target in [
        CaseStatus.INTAKE_AUTHORIZATION,
        CaseStatus.ROUTE_SCOPE_CONFIRMED,
        CaseStatus.COLLECT_AND_VERIFY,
        CaseStatus.DRAFT_PREPARATION,
        CaseStatus.CUSTOMER_CONFIRMATION,
    ]:
        apply_transition(
            database_url=DB, tenant_id=TENANT, case_id=case_id, target=target
        )


def _walk_to_ready(case_id: str) -> None:
    _walk_to_customer_confirmation(case_id)
    _confirm(case_id)
    apply_transition(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        target=CaseStatus.FINAL_REVIEW,
    )
    _approve(case_id)
    apply_transition(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        target=CaseStatus.READY_TO_DELIVER,
    )


def _accept_evidence(case_id: str, store: LocalFileStore) -> list[str]:
    ensure_seed_route_packs(database_url=DB)
    keys = dict(database_url=DB, tenant_id=TENANT, case_id=case_id)
    ids = []
    for label, requirement in (
        ("passport", "passport_valid"), ("purpose", "visit_purpose_allowed"),
        ("itinerary", "intention_to_leave"), ("bank", "funds_for_stay"), ("employment", "home_ties"),
    ):
        ev = attach_evidence(
            **keys, file_store=store, data=f"%PDF-s13-{label}".encode(), original_filename=f"{label}.pdf",
            classification=label, expected_case_version=_ver(case_id),
        )
        record_requirement_review(**keys, requirement_key=requirement, expected_case_version=_ver(case_id))
        ids.append(ev.id)
    for requirement in ("sponsor_if_any", "translation_if_needed", "transfer_human_triggers"):
        record_requirement_applicability(**keys, requirement_key=requirement, decision="not_applicable",
                                         note="synthetic test case", expected_case_version=_ver(case_id))
    return ids


def _send_final_package(
    case_id: str,
    evidence_ids: list[str],
    store: LocalFileStore,
    *,
    recipient: str = "allow@example.com",
) -> str:
    """Assemble + approve + simulate-send a final package; returns delivery id.

    Caller must already be on READY_TO_DELIVER for the current case_version.
    """
    case = get_case(database_url=DB, tenant_id=TENANT, case_id=case_id)
    pkg = assemble_package(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        package_kind=DeliveryKind.FINAL_PACKAGE.value,
        file_store=store,
        evidence_ids=evidence_ids,
        expected_case_version=case.case_version,
    )
    record_final_review(
        database_url=DB,
        tenant_id=TENANT,
        case_id=case_id,
        decision="approve",
        expected_case_version=_ver(case_id),
        package_id=pkg.package_id,
        recipients=[recipient],
        reviewer_id="s13-reviewer",
    )
    settings = Settings(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=DB,
        file_storage_dir=store.root,
        tenant_id=TENANT,
        siliconflow_api_key="",
        siliconflow_base_url="https://api.siliconflow.cn/v1",
        siliconflow_model="zai-org/GLM-5.3-Flash",
        email_smtp_host="",
        email_smtp_port=587,
        email_smtp_user="",
        email_smtp_password="",
        email_from="",
        email_imap_host="",
        email_imap_port=993,
        email_imap_user="",
        email_imap_password="",
        outbound_allowlist=frozenset({recipient}),
        rule_publish_token="test-token",
    )
    result = send_case_package(
        settings=settings,
        case_id=case_id,
        file_store=store,
        req=PackageSendRequest(
            package_id=pkg.package_id,
            recipients=(recipient,),
            subject="final package",
            body_text="attached",
            send_operation_id=f"s13-{uuid.uuid4().hex[:12]}",
        ),
    )
    assert result.status == "simulated", result
    case = get_case(database_url=DB, tenant_id=TENANT, case_id=case_id)
    assert case.current_delivery_id
    return case.current_delivery_id


class CaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_create_and_happy_path_to_intake(self) -> None:
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title=f"t-{uuid.uuid4().hex[:8]}",
            customer_email="c-baron-app@hotmail.com",
        )
        updated = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.INTAKE_AUTHORIZATION,
        )
        self.assertEqual(updated.status, CaseStatus.INTAKE_AUTHORIZATION)

    def test_issue_resolution_captures_and_clears_resume(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="issue-resume"
        )
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.INTAKE_AUTHORIZATION,
        )
        into_issue = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.ISSUE_RESOLUTION,
        )
        self.assertEqual(into_issue.resume_status, CaseStatus.INTAKE_AUTHORIZATION)
        back = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.INTAKE_AUTHORIZATION,
        )
        self.assertIsNone(back.resume_status)

    def test_stale_page_confirm_rejected_after_version_bump(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="stale-page")
        seen = case.case_version
        bump_case_version(database_url=DB, tenant_id=TENANT, case_id=case.id)
        with self.assertRaises(DomainError) as ctx:
            record_customer_confirmation(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                expected_case_version=seen,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT_VERSION)

    def test_confirm_requires_expected_case_version(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="need-ver")
        with self.assertRaises(DomainError) as ctx:
            record_customer_confirmation(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                expected_case_version=None,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)
        with self.assertRaises(DomainError) as ctx2:
            record_final_review(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                decision="approve",
                expected_case_version=None,
            )
        self.assertEqual(ctx2.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_transition_rejects_stale_expected_version(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="race")
        seen = case.case_version
        bump_case_version(database_url=DB, tenant_id=TENANT, case_id=case.id)
        with self.assertRaises(DomainError) as ctx:
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                target=CaseStatus.INTAKE_AUTHORIZATION,
                expected_case_version=seen,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT_VERSION)

    def test_bump_or_reject_blocks_send_and_delivered(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="no-send")
        _walk_to_ready(case.id)
        bump_case_version(database_url=DB, tenant_id=TENANT, case_id=case.id)
        with self.assertRaises(DomainError) as ctx:
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                target=CaseStatus.SENT_AWAITING_ACK,
            )
        self.assertIn(
            ctx.exception.code,
            {
                ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED,
                ErrorCode.REVIEW_APPROVAL_REQUIRED,
            },
        )

        case2 = create_case(database_url=DB, tenant_id=TENANT, title="reject-send")
        _walk_to_ready(case2.id)
        _reject(case2.id)
        with self.assertRaises(DomainError) as ctx2:
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case2.id,
                target=CaseStatus.SENT_AWAITING_ACK,
            )
        self.assertEqual(ctx2.exception.code, ErrorCode.REVIEW_APPROVAL_REQUIRED)

    def test_stale_delivery_id_cannot_confirm_new_round(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="resend", route=RouteCode.STANDARD_VISITOR)
        store = LocalFileStore(Path(tempfile.mkdtemp()) / "files")
        eid = _accept_evidence(case.id, store)
        _walk_to_ready(case.id)
        old_delivery = _send_final_package(case.id, eid, store)
        sent1 = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.SENT_AWAITING_ACK,
        )
        self.assertEqual(sent1.current_delivery_id, old_delivery)
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.READY_TO_DELIVER,
        )
        # Second final send replaces current delivery (same package re-send needs new op).
        new_delivery = _send_final_package(
            case.id, eid, store, recipient="allow@example.com"
        )
        self.assertNotEqual(new_delivery, old_delivery)
        sent2 = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.SENT_AWAITING_ACK,
        )
        self.assertEqual(sent2.current_delivery_id, new_delivery)
        with self.assertRaises(DomainError) as ctx:
            record_delivery_receipt(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                expected_delivery_id=old_delivery,
                expected_case_version=_ver(case.id),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT_VERSION)
        record_delivery_receipt(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_delivery_id=sent2.current_delivery_id,
            expected_case_version=_ver(case.id),
        )

    def test_sent_requires_final_package_and_delivered_needs_receipt(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="deliver", route=RouteCode.STANDARD_VISITOR)
        store = LocalFileStore(Path(tempfile.mkdtemp()) / "files")
        eid = _accept_evidence(case.id, store)
        _walk_to_ready(case.id)
        with self.assertRaises(DomainError) as bare:
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                target=CaseStatus.SENT_AWAITING_ACK,
            )
        self.assertEqual(bare.exception.code, ErrorCode.DELIVERY_GATE_FAILED)
        delivery_id = _send_final_package(case.id, eid, store)
        sent = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.SENT_AWAITING_ACK,
        )
        self.assertEqual(sent.current_delivery_id, delivery_id)
        with self.assertRaises(DomainError) as ctx:
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                target=CaseStatus.DELIVERED,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.DELIVERY_RECEIPT_REQUIRED)
        record_delivery_receipt(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_delivery_id=sent.current_delivery_id,
            expected_case_version=_ver(case.id),
        )
        delivered = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.DELIVERED,
        )
        self.assertEqual(delivered.status, CaseStatus.DELIVERED)
        self.assertGreaterEqual(delivered.closure_seq, 1)

    def test_reopen_grant_one_per_closure_cycle(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="reopen")
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.CLOSED,
        )
        closed = get_case(database_url=DB, tenant_id=TENANT, case_id=case.id)
        self.assertEqual(closed.closure_seq, 1)
        record_reopen_grant(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=closed.case_version,
            expected_closure_seq=closed.closure_seq,
        )
        record_reopen_grant(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=closed.case_version,
            expected_closure_seq=closed.closure_seq,
        )
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.LEAD,
        )
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.CLOSED,
        )
        closed2 = get_case(database_url=DB, tenant_id=TENANT, case_id=case.id)
        self.assertEqual(closed2.closure_seq, 2)
        # Stale page still holding closure_seq=1 must not authorize the new cycle.
        with self.assertRaises(DomainError) as stale:
            record_reopen_grant(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                expected_case_version=closed2.case_version,
                expected_closure_seq=1,
            )
        self.assertEqual(stale.exception.code, ErrorCode.CONFLICT_VERSION)
        with self.assertRaises(DomainError) as ctx:
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                target=CaseStatus.LEAD,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.REOPEN_UNAUTHORIZED)

    def test_reopen_grant_after_bump_during_closed_updates_version_binding(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="bump-closed")
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.CLOSED,
        )
        closed = get_case(database_url=DB, tenant_id=TENANT, case_id=case.id)
        record_reopen_grant(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=closed.case_version,
            expected_closure_seq=closed.closure_seq,
        )
        bumped = bump_case_version(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        # Old grant is version-stale until refreshed for the same closure_seq.
        with self.assertRaises(DomainError):
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                target=CaseStatus.LEAD,
            )
        record_reopen_grant(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=bumped.case_version,
            expected_closure_seq=bumped.closure_seq,
        )
        import psycopg

        with psycopg.connect(DB) as conn:
            row = conn.execute(
                """
                SELECT bound_case_version FROM case_reopen_grants
                WHERE case_id = %s AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (case.id,),
            ).fetchone()
        self.assertEqual(row[0], bumped.case_version)
        reopened = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.LEAD,
        )
        self.assertEqual(reopened.status, CaseStatus.LEAD)

    def test_allowed_targets_hide_gated_without_preconditions(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="dropdown")
        _walk_to_customer_confirmation(case.id)
        allowed = list_allowed_targets(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertNotIn(CaseStatus.FINAL_REVIEW, allowed)
        _confirm(case.id)
        allowed2 = list_allowed_targets(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertIn(CaseStatus.FINAL_REVIEW, allowed2)

    def test_attribute_rejects_cross_case_without_force(self) -> None:
        a = create_case(database_url=DB, tenant_id=TENANT, title="cust-a")
        b = create_case(database_url=DB, tenant_id=TENANT, title="cust-b")
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            inbound = persist_inbound(
                database_url=DB,
                tenant_id=TENANT,
                mail=InboundEmail(
                    from_addr="x@y.com",
                    to_addrs=("agent@example.com",),
                    subject="hi",
                    body_text="body",
                    provider_message_id=f"attr-{uuid.uuid4().hex}",
                ),
                file_store=store,
            )
        attribute_inbound_to_case(
            database_url=DB,
            tenant_id=TENANT,
            email_event_id=inbound.email_event_id,
            case_id=a.id,
        )
        with self.assertRaises(DomainError) as ctx:
            attribute_inbound_to_case(
                database_url=DB,
                tenant_id=TENANT,
                email_event_id=inbound.email_event_id,
                case_id=b.id,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT_VERSION)

    def test_takeover_flag_persists(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="takeover")
        updated = set_takeover(
            database_url=DB, tenant_id=TENANT, case_id=case.id, active=True
        )
        self.assertTrue(updated.human_takeover)


class CaseHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        from visa_agent import fastapi_app

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = replace(get_settings(root / ".env"), file_storage_dir=root)
        self.enterContext(patch.object(fastapi_app, "get_settings", return_value=settings))
        fastapi_app.bootstrap(settings)
        self.client = TestClient(fastapi_app.app)

    def test_api_confirm_requires_version_field(self) -> None:
        created = self.client.post(
            "/cases",
            json={"title": f"http-{uuid.uuid4().hex[:6]}"},
        )
        case_id = created.json()["data"]["case_id"]
        missing = self.client.post(f"/cases/{case_id}/confirm", json={})
        self.assertEqual(missing.status_code, 422)
        missing_review = self.client.post(
            f"/cases/{case_id}/reviews",
            json={"decision": "approve"},
        )
        self.assertEqual(missing_review.status_code, 422)
        ok = self.client.post(
            f"/cases/{case_id}/confirm",
            json={"expected_case_version": 1},
        )
        self.assertEqual(ok.status_code, 200)
        bump_case_version(database_url=DB, tenant_id=TENANT, case_id=case_id)
        rejected = self.client.post(
            f"/cases/{case_id}/confirm",
            json={"expected_case_version": 1},
        )
        self.assertEqual(rejected.status_code, 409)

    def test_workbench_approval_and_email_events_move_delivery_forward(self) -> None:
        from visa_agent.fastapi_app import get_settings as app_settings

        recipient = f"receipt-{uuid.uuid4().hex}@example.com"
        keys = dict(database_url=DB, tenant_id=TENANT)
        case = create_case(**keys, customer_email=recipient, status=CaseStatus.FINAL_REVIEW,
                           route=RouteCode.STANDARD_VISITOR)
        path = f"/workbench/cases/{case.id}"
        self.assertNotIn(f"{path}/reviews", self.client.get(path).text)
        settings = replace(
            app_settings(), email_smtp_host="smtp.example.com", email_smtp_user="agent@example.com",
            email_smtp_password="test-only", email_from="agent@example.com", outbound_allowlist=frozenset({recipient}),
        )
        store = LocalFileStore(settings.file_storage_dir)
        evidence_id = _accept_evidence(case.id, store)
        _confirm(case.id)
        final_review = self.client.get(path).text
        self.assertIn("终审通过", final_review)
        self.assertIn("核对客户已确认的材料", final_review)
        review = self.client.post(
            f"{path}/reviews", data={"decision": "approve", "expected_case_version": _ver(case.id)},
        )
        self.assertIn("进度：<strong>交付中</strong>", review.text)
        self.assertNotIn('id="advisor-work"', review.text)
        self.assertEqual(get_case(**keys, case_id=case.id).status, CaseStatus.READY_TO_DELIVER)
        package = assemble_package(
            **keys, case_id=case.id, package_kind=DeliveryKind.FINAL_PACKAGE.value, file_store=store,
            evidence_ids=evidence_id, expected_case_version=_ver(case.id),
        )
        request = PackageSendRequest(
            package_id=package.package_id, recipients=(recipient,), subject="Materials", body_text="Attached.",
        )
        with patch("visa_agent.send_control.service.send_smtp", return_value="250 accepted") as smtp:
            self.assertEqual(send_case_package(settings=settings, case_id=case.id, req=request).status, "blocked")
            smtp.assert_not_called()
            record_final_review(
                **keys, case_id=case.id, decision="approve", expected_case_version=_ver(case.id),
                package_id=package.package_id, recipients=[recipient], reviewer_id="test-reviewer",
            )
            self.assertEqual(send_case_package(settings=settings, case_id=case.id, req=request).status,
                             "provider_accepted")
            message_id = smtp.call_args.kwargs["message_id"]
        self.assertEqual(get_case(**keys, case_id=case.id).status, CaseStatus.SENT_AWAITING_ACK)
        self.assertIn("客户直接回复交付邮件「确认收件」", self.client.get(path).text)

        def reply(body, parent):
            response = self.client.post(
                "/internal/email/inbound",
                json={"from_addr": recipient, "to_addrs": ["agent@example.com"], "subject": "Re: Materials",
                      "body_text": body, "rfc_message_id": f"<{uuid.uuid4().hex}@example.com>",
                      "in_reply_to": parent},
            )
            self.assertEqual(response.status_code, 200)

        reply("确认收件", None)
        reply("谢谢", message_id)
        self.assertEqual(get_case(**keys, case_id=case.id).status, CaseStatus.SENT_AWAITING_ACK)
        reply("确认收件", message_id)
        self.assertEqual(get_case(**keys, case_id=case.id).status, CaseStatus.DELIVERED)
        self.assertIn("已办完", self.client.get(path).text)

    def test_workbench_pages_render(self) -> None:
        created = self.client.post("/cases", json={"title": "wb-page"})
        case_id = created.json()["data"]["case_id"]
        detail = self.client.get(f"/workbench/cases/{case_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('name="expected_case_version"', detail.text)
        self.assertNotIn('name="expected_delivery_id"', detail.text)
        self.assertNotIn('name="expected_closure_seq"', detail.text)

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            evidence_id = _accept_evidence(case_id, store)
            _walk_to_ready(case_id)
            ready = self.client.get(f"/workbench/cases/{case_id}")
            self.assertIn("进度：<strong>交付中</strong>", ready.text)
            self.assertNotIn('id="advisor-work"', ready.text)
            self.assertNotIn(f"/workbench/cases/{case_id}/transition", ready.text)
            _send_final_package(case_id, evidence_id, store)
            sent = self.client.get(f"/workbench/cases/{case_id}")
            self.assertNotIn("等待客户确认收件", sent.text)
            apply_transition(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case_id,
                target=CaseStatus.SENT_AWAITING_ACK,
            )
            detail = self.client.get(f"/workbench/cases/{case_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("等待客户确认收件", detail.text)
            self.assertNotIn(f"/workbench/cases/{case_id}/receipt", detail.text)
            self.assertNotIn('name="expected_closure_seq"', detail.text)

        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case_id,
            target=CaseStatus.CLOSED,
        )
        closed = self.client.get(f"/workbench/cases/{case_id}")
        self.assertEqual(closed.status_code, 200)
        self.assertIn("案件已结束", closed.text)
        self.assertNotIn("<form", closed.text)
        self.assertIn(f"/evidence/{evidence_id[0]}/file?download=1", closed.text)
        for action in ("takeover", "transition", "bump-version", "evidence", "advisor-reply"):
            with self.subTest(action=action):
                stale = self.client.post(f"/workbench/cases/{case_id}/{action}")
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(stale.json()["detail"], "案件已结束，仅可查看")
        reopen = self.client.post(f"/workbench/cases/{case_id}/reopen-grant")
        self.assertEqual(reopen.status_code, 404)

    def test_workbench_rejects_non_integer_version(self) -> None:
        created = self.client.post("/cases", json={"title": "bad-ver"})
        case_id = created.json()["data"]["case_id"]
        resp = self.client.post(
            f"/workbench/cases/{case_id}/confirm",
            data={"expected_case_version": "not-a-number"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("expected_case_version must be an integer", resp.text)


if __name__ == "__main__":
    unittest.main()
