"""Advisor inbox + case list search/filter."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from visa_agent.case_service.inbound_agent import process_customer_inbound
from visa_agent.case_service.remediation import (
    ADVISOR_CUSTOMER_QUESTION,
    ADVISOR_UNCLASSIFIED,
    list_advisor_alerts,
    list_open_questions,
)
from visa_agent.case_service.service import (
    attribute_inbound_to_case,
    create_case,
    list_cases,
    set_takeover,
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


class AdvisorInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_unclassified_and_question_land_in_inbox(self) -> None:
        email_addr = f"adv-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        store = LocalFileStore(settings.file_storage_dir)
        store.root.mkdir(parents=True, exist_ok=True)
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title=f"顾问提醒-{uuid.uuid4().hex[:6]}",
            customer_email=email_addr,
            route=RouteCode.STANDARD_VISITOR,
        )
        inbound = persist_inbound(
            database_url=DB,
            tenant_id=TENANT,
            mail=InboundEmail(
                from_addr=email_addr,
                to_addrs=("agent@example.com",),
                subject="问一下",
                body_text="这个材料到底算什么？我该怎么准备资金证明呢，能否详细说明一下政策？",
                provider_message_id=f"adv-{uuid.uuid4().hex}",
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
            # Unknown file → unclassified escalate; basic funds/policy Q → agent answers.
            process_customer_inbound(
                settings=settings,
                case_id=case.id,
                email_event_id=inbound.email_event_id,
            )
        open_qs = list_open_questions(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        keys = {q.requirement_key for q in open_qs}
        # Unknown attachment → advisor classify. Basic funds/policy Q → agent answers,
        # does NOT open advisor_customer_question.
        self.assertIn(ADVISOR_UNCLASSIFIED, keys)
        self.assertNotIn(ADVISOR_CUSTOMER_QUESTION, keys)
        self.assertTrue(
            any(q.status == "escalated_human" for q in open_qs)
        )
        alerts = list_advisor_alerts(database_url=DB, tenant_id=TENANT)
        self.assertTrue(any(a.case_id == case.id for a in alerts))

    def test_list_cases_search_and_filter(self) -> None:
        token = uuid.uuid4().hex[:8]
        email = f"search-{token}@example.com"
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title=f"可搜索案件-{token}",
            customer_email=email,
            route=RouteCode.STUDENT,
        )
        set_takeover(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            active=True,
        )
        found = list_cases(
            database_url=DB, tenant_id=TENANT, q=token, limit=20
        )
        self.assertTrue(any(c.id == case.id for c in found))
        by_route = list_cases(
            database_url=DB,
            tenant_id=TENANT,
            route=RouteCode.STUDENT.value,
            waiting="takeover",
            limit=50,
        )
        self.assertTrue(any(c.id == case.id for c in by_route))
        miss = list_cases(
            database_url=DB, tenant_id=TENANT, q="zzz-no-such-case", limit=20
        )
        self.assertFalse(any(c.id == case.id for c in miss))

    def test_workbench_home_and_inbox_are_separate(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.fastapi_app import app, bootstrap

        settings = _settings(allowlist=frozenset({"wb@example.com"}))
        bootstrap(settings)
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            home = client.get("/workbench")
            self.assertEqual(home.status_code, 200)
            self.assertIn("全部案件", home.text)
            self.assertIn('name="q"', home.text)
            self.assertIn("/workbench/inbox", home.text)
            self.assertNotIn("需要你处理的事", home.text)
            inbox = client.get("/workbench/inbox")
            self.assertEqual(inbox.status_code, 200)
            self.assertIn("顾问收件箱", inbox.text)
            self.assertIn("需要你处理的事", inbox.text)
            filtered = client.get("/workbench", params={"waiting": "takeover"})
            self.assertEqual(filtered.status_code, 200)


if __name__ == "__main__":
    unittest.main()
