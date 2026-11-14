from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

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
from visa_agent.domain.enums import CaseStatus, ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.inbound import InboundEmail, persist_inbound
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
        case = create_case(database_url=DB, tenant_id=TENANT, title="resend")
        _walk_to_ready(case.id)
        sent1 = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.SENT_AWAITING_ACK,
        )
        old_delivery = sent1.current_delivery_id
        assert old_delivery is not None
        # Resend: clear delivery then send again (simulate by bumping back via
        # ready -> collect is heavy; clear current_delivery and repair send).
        # Move back to ready_to_deliver then resend after clearing delivery id.
        apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.READY_TO_DELIVER,
        )
        import psycopg

        with psycopg.connect(DB) as conn:
            conn.execute(
                "UPDATE cases SET current_delivery_id = NULL WHERE id = %s",
                (case.id,),
            )
            conn.commit()
        sent2 = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.SENT_AWAITING_ACK,
        )
        self.assertNotEqual(sent2.current_delivery_id, old_delivery)
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

    def test_sent_creates_delivery_and_delivered_needs_receipt(self) -> None:
        case = create_case(database_url=DB, tenant_id=TENANT, title="deliver")
        _walk_to_ready(case.id)
        sent = apply_transition(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            target=CaseStatus.SENT_AWAITING_ACK,
        )
        self.assertIsNotNone(sent.current_delivery_id)
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
        apply_migrations(DB)
        from visa_agent.fastapi_app import app

        self.client = TestClient(app)

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

    def test_workbench_pages_render(self) -> None:
        created = self.client.post(
            "/cases", json={"title": "wb-page", "customer_email": "x@y.com"}
        )
        case_id = created.json()["data"]["case_id"]
        detail = self.client.get(f"/workbench/cases/{case_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('name="expected_case_version"', detail.text)
        self.assertIn('name="expected_delivery_id"', detail.text)
        self.assertIn('name="expected_closure_seq"', detail.text)

    def test_workbench_rejects_non_integer_version(self) -> None:
        created = self.client.post(
            "/cases", json={"title": "bad-ver", "customer_email": "x@y.com"}
        )
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
