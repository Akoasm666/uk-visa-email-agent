"""Advisor classify + reply-via-agent workbench actions."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from visa_agent.case_service.advisor_actions import (
    advisor_classify_evidence,
    advisor_reply_via_agent,
)
from visa_agent.case_service.inbound_agent import process_customer_inbound
from visa_agent.case_service.remediation import (
    ADVISOR_UNCLASSIFIED,
    list_open_questions,
)
from visa_agent.case_service.service import (
    attribute_inbound_to_case,
    create_case,
)
from visa_agent.config import Settings
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.inbound import (
    InboundAttachment,
    InboundEmail,
    persist_inbound,
)
from visa_agent.email_adapter.outbox import OutboxResult
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _settings(*, allowlist: frozenset[str]) -> Settings:
    return Settings(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=DB,
        file_storage_dir=Path(tempfile.mkdtemp()),
        tenant_id=TENANT,
        siliconflow_api_key="",
        siliconflow_base_url="https://api.siliconflow.cn/v1",
        siliconflow_model="zai-org/GLM-5.3-Flash",
        email_smtp_host="",
        email_smtp_port=587,
        email_smtp_user="",
        email_smtp_password="",
        email_from="agent@example.com",
        email_imap_host="",
        email_imap_port=993,
        email_imap_user="",
        email_imap_password="",
        outbound_allowlist=allowlist,
        rule_publish_token="",
    )


def _fake_outbox(**_kwargs):  # noqa: ANN003
    return OutboxResult(
        outbox_id=uuid.uuid4().hex,
        status="provider_accepted",
        block_reasons=(),
        provider_response="mock",
        email_event_id=uuid.uuid4().hex,
        send_operation_id="mock-op",
    )


class AdvisorActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_classify_closes_unclassified_and_ui_has_controls(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.fastapi_app import app, bootstrap

        email_addr = f"cls-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        store = LocalFileStore(settings.file_storage_dir)
        store.root.mkdir(parents=True, exist_ok=True)
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title=f"归类-{uuid.uuid4().hex[:6]}",
            customer_email=email_addr,
            route=RouteCode.STANDARD_VISITOR,
        )
        inbound = persist_inbound(
            database_url=DB,
            tenant_id=TENANT,
            mail=InboundEmail(
                from_addr=email_addr,
                to_addrs=("agent@example.com",),
                subject="附件",
                body_text="请看附件",
                provider_message_id=f"cls-{uuid.uuid4().hex}",
                attachments=(
                    InboundAttachment(
                        filename="random_scan.pdf",
                        content_type="application/pdf",
                        data=b"%PDF-1.4 totally unknown blob xyz",
                    ),
                ),
            ),
            file_store=store,
        )
        attribute_inbound_to_case(
            database_url=DB,
            tenant_id=TENANT,
            email_event_id=inbound.email_event_id,
            case_id=case.id,
            reason="test",
        )
        with patch(
            "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
            side_effect=_fake_outbox,
        ):
            result = process_customer_inbound(
                settings=settings,
                case_id=case.id,
                email_event_id=inbound.email_event_id,
            )
        self.assertTrue(result.attached)
        evidence_id = result.attached[0].evidence_id
        self.assertIsNotNone(evidence_id)
        open_before = {
            q.requirement_key
            for q in list_open_questions(
                database_url=DB, tenant_id=TENANT, case_id=case.id
            )
        }
        self.assertIn(ADVISOR_UNCLASSIFIED, open_before)

        classified = advisor_classify_evidence(
            settings=settings,
            case_id=case.id,
            evidence_id=evidence_id or "",
            classification="passport",
        )
        self.assertEqual(classified.evidence.classification, "passport")
        self.assertTrue(classified.closed_unclassified)
        open_after = {
            q.requirement_key
            for q in list_open_questions(
                database_url=DB, tenant_id=TENANT, case_id=case.id
            )
            if q.status == "escalated_human"
        }
        self.assertNotIn(ADVISOR_UNCLASSIFIED, open_after)

        bootstrap(settings)
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            page = client.get(f"/workbench/cases/{case.id}")
            self.assertEqual(page.status_code, 200)
            # After classify, panel may be empty; create another case for UI check.
        email2 = f"ui-{uuid.uuid4().hex[:8]}@example.com"
        settings2 = _settings(allowlist=frozenset({email2}))
        case2 = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="待归类UI",
            customer_email=email2,
            route=RouteCode.STANDARD_VISITOR,
        )
        store2 = LocalFileStore(settings2.file_storage_dir)
        store2.root.mkdir(parents=True, exist_ok=True)
        inbound2 = persist_inbound(
            database_url=DB,
            tenant_id=TENANT,
            mail=InboundEmail(
                from_addr=email2,
                to_addrs=("agent@example.com",),
                subject="附件2",
                body_text="看这个",
                provider_message_id=f"ui-{uuid.uuid4().hex}",
                attachments=(
                    InboundAttachment(
                        filename="mystery.bin",
                        content_type="application/octet-stream",
                        data=b"nope-not-a-real-doc",
                    ),
                ),
            ),
            file_store=store2,
        )
        attribute_inbound_to_case(
            database_url=DB,
            tenant_id=TENANT,
            email_event_id=inbound2.email_event_id,
            case_id=case2.id,
            reason="test",
        )
        with patch(
            "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
            side_effect=_fake_outbox,
        ):
            process_customer_inbound(
                settings=settings2,
                case_id=case2.id,
                email_event_id=inbound2.email_event_id,
            )
        bootstrap(settings2)
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings2):
            client = TestClient(app)
            page = client.get(f"/workbench/cases/{case2.id}")
            self.assertEqual(page.status_code, 200)
            self.assertIn("顾问处理区", page.text)
            self.assertIn("确认归类", page.text)
            self.assertIn("选择材料类型", page.text)
            self.assertIn("/evidence/", page.text)
            self.assertIn("/classify", page.text)
            inbox = client.get("/workbench/inbox")
            self.assertIn("去案件处理", inbox.text)
            self.assertIn("#advisor-work", inbox.text)

    def test_advisor_reply_goes_through_agent_outbox(self) -> None:
        email_addr = f"reply-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="答疑",
            customer_email=email_addr,
            route=RouteCode.STANDARD_VISITOR,
        )
        from visa_agent.case_service.remediation import (
            ADVISOR_CUSTOMER_QUESTION,
            open_remediation,
        )

        q = open_remediation(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            requirement_key=ADVISOR_CUSTOMER_QUESTION,
            reason_code="customer_question_needs_advisor",
            reason_detail="客户问：我的余额够不够？",
            severity="complex_human",
            case_version=case.case_version,
        )
        self.assertEqual(q.status, "escalated_human")

        captured: dict = {}

        def _capture(**kwargs):  # noqa: ANN003
            captured["req"] = kwargs.get("req")
            return _fake_outbox()

        with patch(
            "visa_agent.case_service.advisor_actions.enqueue_and_maybe_send",
            side_effect=_capture,
        ):
            result = advisor_reply_via_agent(
                settings=settings,
                case_id=case.id,
                body_text="按官网资金要求准备流水即可，个案能否过签由顾问面谈确认。",
                question_id=q.id,
            )
        self.assertTrue(result.closed_question)
        self.assertIn("req", captured)
        req = captured["req"]
        self.assertEqual(req.recipients, (email_addr,))
        self.assertIn("顾问", req.body_text)
        self.assertIn("按官网资金要求", req.body_text)
        remaining = [
            x
            for x in list_open_questions(
                database_url=DB, tenant_id=TENANT, case_id=case.id
            )
            if x.requirement_key == ADVISOR_CUSTOMER_QUESTION
            and x.status == "escalated_human"
        ]
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
