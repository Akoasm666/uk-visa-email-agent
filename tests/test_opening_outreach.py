"""Opening outreach: agent emails the customer first after case create."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from fastapi.testclient import TestClient

from visa_agent.case_service.outreach import (
    build_opening_email,
    send_opening_outreach,
)
from visa_agent.case_service.service import create_case, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.outbox import OutboxResult
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _settings(*, allowlist: frozenset[str] | None = None) -> Settings:
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
        email_from="",
        email_imap_host="",
        email_imap_port=993,
        email_imap_user="",
        email_imap_password="",
        outbound_allowlist=allowlist or frozenset(),
        rule_publish_token="",
    )


class OpeningOutreachTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)
        from visa_agent.fastapi_app import app, bootstrap

        bootstrap()
        self.client = TestClient(app)

    def test_opening_email_is_human_readable(self) -> None:
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="张三 · 访英",
            customer_email="client@example.com",
            route=RouteCode.STANDARD_VISITOR,
        )
        subject, body = build_opening_email(case)
        self.assertIn("张三", subject)
        self.assertIn("Standard Visitor", body)
        self.assertIn("访问签证", body)
        self.assertIn("护照资料页", body)
        self.assertIn("passport bio-data", body.lower())
        self.assertLess(body.find("Hello,"), body.find("您好"))
        self.assertIn("请直接回复本邮件", body)
        self.assertNotIn("case_version", body)
        self.assertNotIn("applicant_alias", body)
        self.assertNotIn("transfer_human", body)

    def test_send_opening_sets_waiting_customer(self) -> None:
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="outreach-wait",
            customer_email="allowlisted@example.com",
            route=RouteCode.STANDARD_VISITOR,
        )
        fake = OutboxResult(
            outbox_id="ob1",
            status="provider_accepted",
            block_reasons=(),
            provider_response="250 ok",
            email_event_id="ee1",
            send_operation_id=f"case-opening:{TENANT}:{case.id}",
        )
        with patch(
            "visa_agent.case_service.outreach.enqueue_and_maybe_send",
            return_value=fake,
        ):
            result = send_opening_outreach(
                settings=_settings(allowlist=frozenset({"allowlisted@example.com"})),
                case=case,
            )
        self.assertTrue(result.sent)
        after = get_case(database_url=DB, tenant_id=TENANT, case_id=case.id)
        self.assertTrue(after.waiting_customer)

    def test_workbench_create_requires_email_and_triggers_outreach(self) -> None:
        missing = self.client.post(
            "/workbench/cases",
            data={"title": "no-mail", "route": "standard_visitor"},
            follow_redirects=False,
        )
        self.assertEqual(missing.status_code, 303)
        loc = unquote(missing.headers["location"])
        self.assertIn("客户邮箱", loc)

        fake_email = f"wb-test-{uuid.uuid4().hex[:8]}@example.com"
        with patch(
            "visa_agent.fastapi_app.send_opening_outreach",
            return_value=type(
                "R",
                (),
                {
                    "attempted": True,
                    "sent": True,
                    "skipped_reason": None,
                    "outbox": None,
                },
            )(),
        ):
            ok = self.client.post(
                "/workbench/cases",
                data={
                    "title": f"wb-outreach-{uuid.uuid4().hex[:8]}",
                    "customer_email": fake_email,
                    "route": "standard_visitor",
                },
                follow_redirects=False,
            )
        self.assertEqual(ok.status_code, 303)
        self.assertIn("首封邮件", unquote(ok.headers["location"]))

    def test_inbound_auto_attributes_by_customer_email(self) -> None:
        unique = f"reply-{uuid.uuid4().hex[:10]}@example.com"
        created = self.client.post(
            "/cases",
            json={
                "title": "auto-attr",
                "customer_email": unique,
                "route": "standard_visitor",
            },
        )
        self.assertEqual(created.status_code, 200)
        case_id = created.json()["data"]["case_id"]

        inbound = self.client.post(
            "/internal/email/inbound",
            json={
                "from_addr": f"Reply Me <{unique}>",
                "to_addrs": ["agent@example.com"],
                "subject": "Re: 我的护照",
                "body_text": "附件是护照",
                "rfc_message_id": f"<auto-{uuid.uuid4().hex}@test>",
            },
        )
        self.assertEqual(inbound.status_code, 200)
        data = inbound.json()["data"]
        self.assertEqual(data["attribution_status"], "attributed")
        self.assertEqual(data["case_id"], case_id)

        # Second open case with same email → ambiguous, leave unattributed.
        self.client.post(
            "/cases",
            json={
                "title": "auto-attr-2",
                "customer_email": unique,
                "route": "student",
            },
        )
        second = self.client.post(
            "/internal/email/inbound",
            json={
                "from_addr": unique,
                "to_addrs": ["agent@example.com"],
                "subject": "第二封",
                "body_text": "又来一封",
                "rfc_message_id": f"<auto2-{uuid.uuid4().hex}@test>",
            },
        )
        self.assertEqual(second.json()["data"]["attribution_status"], "unattributed")


if __name__ == "__main__":
    unittest.main()
