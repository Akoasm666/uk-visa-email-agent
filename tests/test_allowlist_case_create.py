"""New case customer email is auto-added to outbound allowlist."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from visa_agent.case_service.outreach import send_opening_outreach
from visa_agent.case_service.service import create_case
from visa_agent.config import Settings
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.allowlist import (
    effective_outbound_allowlist,
    list_db_allowlist,
)
from visa_agent.email_adapter.outbox import OutboxResult
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _settings(*, allowlist: frozenset[str] = frozenset()) -> Settings:
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


class CaseCreateAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_create_case_adds_customer_email_to_db_allowlist(self) -> None:
        email = f"new-client-{uuid.uuid4().hex[:8]}@example.com"
        # .env / settings allowlist intentionally empty.
        settings = _settings(allowlist=frozenset())
        self.assertNotIn(email, settings.outbound_allowlist)

        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="白名单自动加入",
            customer_email=email,
            route=RouteCode.STANDARD_VISITOR,
        )
        db = list_db_allowlist(database_url=DB, tenant_id=TENANT)
        self.assertIn(email, db)
        effective = effective_outbound_allowlist(settings)
        self.assertIn(email, effective)

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
        ) as send:
            result = send_opening_outreach(settings=settings, case=case)
        self.assertTrue(result.sent)
        # Gate uses effective allowlist inside enqueue; here we only assert outreach runs.
        self.assertTrue(send.called)

    def test_outbox_accepts_db_allowlisted_recipient(self) -> None:
        from visa_agent.email_adapter.outbox import OutboxRequest, enqueue_and_maybe_send

        email = f"db-allow-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset())
        create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="outbox-db-allow",
            customer_email=email,
            route=RouteCode.STANDARD_VISITOR,
        )

        def _fake_send(settings, recipients, subject, body_text):  # noqa: ANN001
            return "250 mock"

        with patch(
            "visa_agent.email_adapter.outbox.send_smtp", side_effect=_fake_send
        ):
            # Settings still has empty static allowlist; DB entry should pass gate.
            out = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=(email,),
                    subject="hello",
                    body_text="body",
                    send_operation_id=f"allow-test:{uuid.uuid4().hex}",
                    send_now=True,
                ),
            )
        self.assertEqual(out.status, "provider_accepted")
        self.assertEqual(out.block_reasons, ())


if __name__ == "__main__":
    unittest.main()
