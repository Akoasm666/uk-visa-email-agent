"""The send gate requires an explicit allowlist entry."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from visa_agent.case_service.service import create_case
from visa_agent.config import Settings
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.allowlist import (
    add_outbound_allowlist_email,
    effective_outbound_allowlist,
    list_db_allowlist,
)
from visa_agent.email_adapter.outbox import OutboxRequest, enqueue_and_maybe_send
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _settings(*, allowlist: frozenset[str] = frozenset(), tenant_id: str = TENANT) -> Settings:
    return Settings(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=DB,
        file_storage_dir=Path(tempfile.mkdtemp()),
        tenant_id=tenant_id,
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

    def test_case_creation_and_legacy_case_entries_do_not_authorize_send(self) -> None:
        email = f"new-client-{uuid.uuid4().hex[:8]}@example.com"
        # .env / settings allowlist intentionally empty.
        settings = _settings(allowlist=frozenset())
        self.assertNotIn(email, settings.outbound_allowlist)

        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="白名单验收",
            customer_email=email,
            route=RouteCode.STANDARD_VISITOR,
        )
        self.assertNotIn(email, list_db_allowlist(database_url=DB, tenant_id=TENANT))
        add_outbound_allowlist_email(
            database_url=DB, tenant_id=TENANT, email=email,
            case_id=case.id, source="case_create",
        )
        self.assertNotIn(email, effective_outbound_allowlist(settings))
        with patch("visa_agent.email_adapter.outbox.send_smtp") as send:
            result = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(kind="ordinary_reply", recipients=(email,),
                                  subject="test", body_text="test"),
            )
        self.assertIn("OUTBOUND_NOT_ALLOWLISTED", result.block_reasons)
        send.assert_not_called()

    def test_outbox_accepts_db_allowlisted_recipient(self) -> None:
        email = f"db-allow-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset(), tenant_id=f"allow-{uuid.uuid4().hex}")
        add_outbound_allowlist_email(database_url=DB, tenant_id=settings.tenant_id, email=email)

        def _fake_send(settings, **kwargs):  # noqa: ANN001
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
