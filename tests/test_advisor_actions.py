"""Advisor classify + reply-via-agent workbench actions."""

from __future__ import annotations

import re
import tempfile
import unittest
import uuid
from dataclasses import replace
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
    get_case,
    set_takeover,
)
from visa_agent.config import Settings
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.inbound import (
    InboundAttachment,
    InboundEmail,
    persist_inbound,
)
from visa_agent.email_adapter.outbox import (
    INBOUND_REPLY_OPERATION_PREFIX, TAKEOVER_REPLY_OPERATION_PREFIX,
    OutboxRequest, OutboxResult, enqueue_and_maybe_send,
)
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

    def test_takeover_composer_sends_threaded_manual_reply_and_keeps_failed_draft(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.fastapi_app import app, bootstrap

        email = f"takeover-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email}))
        bootstrap(settings)
        keys = dict(database_url=DB, tenant_id=TENANT)
        case = create_case(**keys, title="人工沟通测试", customer_email=email)
        store = LocalFileStore(settings.file_storage_dir)
        for index in range(7):
            earlier = persist_inbound(
                **keys, file_store=store, mail=InboundEmail(
                    from_addr=email, to_addrs=(settings.email_from,), subject="补充材料",
                    body_text=f"早期来信 {index}", rfc_message_id=f"<{case.id}-{index}@example.com>",
                ),
            )
            attribute_inbound_to_case(**keys, email_event_id=earlier.email_event_id, case_id=case.id)
        inbound = persist_inbound(
            **keys, file_store=store,
            mail=InboundEmail(
                from_addr=email, to_addrs=(settings.email_from,), subject="补充材料",
                body_text="这是客户发来的消息。", rfc_message_id=f"<{case.id}@example.com>",
            ),
        )
        attribute_inbound_to_case(**keys, email_event_id=inbound.email_event_id, case_id=case.id)
        path = f"/workbench/cases/{case.id}"
        client = TestClient(app)
        with (patch("visa_agent.fastapi_app.get_settings", return_value=settings),
              patch("visa_agent.email_adapter.outbox.send_smtp", return_value="accepted") as smtp):
            self.assertNotIn('id="customer-reply"', client.get(path).text)
            set_takeover(**keys, case_id=case.id, active=True)
            page = client.get(path)
            self.assertIn('id="customer-reply"', page.text)
            conversation = page.text.split('id="customer-reply"', 1)[1].split("</section>", 1)[0]
            history, composer = conversation.split("<form ", 1)
            self.assertIn("<h2>客户对话</h2>", history)
            self.assertIn(f'<span class="muted case-email">{email}</span>', history)
            self.assertIn('class="customer-history-list"', history)
            self.assertNotIn("查看历史对话", conversation)
            self.assertIn("发送邮件</button>", composer)
            self.assertEqual(history.count('class="customer-message"'), 8)
            self.assertLess(history.index("早期来信 0"), history.index("这是客户发来的消息。"))
            self.assertIn(f'name="reply_event_id" value="{inbound.email_event_id}"', page.text)

            request_id = re.search(r'name="request_id" value="([a-f0-9]+)"', page.text)
            assert request_id is not None
            draft = "收到，我会核对你的材料。"
            form = dict(request_id=request_id[1], reply_event_id=inbound.email_event_id,
                        expected_case_version=str(case.case_version), body=draft)
            with patch("visa_agent.fastapi_app.get_settings", return_value=replace(settings, outbound_allowlist=frozenset())):
                unavailable = client.get(path)
                self.assertIn('type="submit" disabled>发送邮件</button>', unavailable.text)
                denied = client.post(f"{path}/takeover-reply", data=form)
            self.assertIn("客户邮箱不在允许发信的名单中", denied.text)
            self.assertIn(f">{draft}</textarea>", denied.text)
            smtp.assert_not_called()

            sent = client.post(f"{path}/takeover-reply", data=form)
            self.assertIn("邮件已发送", sent.text)
            self.assertIn('class="customer-message outbound"', sent.text)
            self.assertIn(f'<p class="customer-message-body">{draft}</p>', sent.text)
            replay = client.post(f"{path}/takeover-reply", data=form, follow_redirects=False)
            self.assertEqual(replay.status_code, 303)
            smtp.assert_called_once()
            self.assertEqual(smtp.call_args.kwargs["recipients"], [email])
            self.assertEqual(smtp.call_args.kwargs["body_text"], draft)
            self.assertEqual(smtp.call_args.kwargs["in_reply_to"], f"<{case.id}@example.com>")

            automatic = enqueue_and_maybe_send(settings=settings, req=OutboxRequest(
                kind="ordinary_reply", recipients=(email,), subject="自动回复", body_text="测试",
                case_id=case.id, case_version=case.case_version,
                send_operation_id=f"{INBOUND_REPLY_OPERATION_PREFIX}{case.id}:fixture",
            ))
            self.assertIn("TAKEOVER_ACTIVE", automatic.block_reasons)
            rejected = client.post("/internal/email/outbox", json={
                "recipients": [email], "subject": "测试", "body_text": "测试",
                "send_operation_id": f"{TAKEOVER_REPLY_OPERATION_PREFIX}fixture",
            })
            self.assertEqual(rejected.status_code, 400)
            set_takeover(**keys, case_id=case.id, active=False)
            self.assertNotIn('id="customer-reply"', client.get(path).text)
            self.assertIn("接管后发送顾问回复", client.post(f"{path}/takeover-reply", data=form).text)
            smtp.assert_called_once()

    def test_takeover_composer_accepts_draft_before_customer_email_is_saved(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.fastapi_app import app, bootstrap

        email = f"missing-then-added-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email}))
        bootstrap(settings)
        keys = dict(database_url=DB, tenant_id=TENANT)
        case = create_case(**keys, title="等待补填邮箱")
        set_takeover(**keys, case_id=case.id, active=True)
        path = f"/workbench/cases/{case.id}"
        client = TestClient(app)
        with (patch("visa_agent.fastapi_app.get_settings", return_value=settings),
              patch("visa_agent.email_adapter.outbox.send_smtp", return_value="accepted") as smtp):
            before = client.get(path).text
            self.assertIn('id="takeover-customer-email"', before)
            self.assertIn('id="takeover-reply-body" name="body" rows="2" required', before)
            self.assertIn('type="submit" disabled>发送邮件</button>', before)
            self.assertIn("先填写并保存收件人邮箱", before)
            draft = "我已收到材料。"
            form = dict(customer_email="无效邮箱", expected_case_version=str(case.case_version), body=draft)
            invalid = client.post(f"{path}/customer-email", data=form)
            self.assertIn("请输入有效的客户邮箱", invalid.text)
            self.assertIn(f">{draft}</textarea>", invalid.text)

            form["customer_email"] = email
            saved = client.post(f"{path}/customer-email", data=form)
            self.assertIn("客户邮箱已保存", saved.text)
            self.assertIn(f">{draft}</textarea>", saved.text)
            self.assertIn('type="submit">发送邮件</button>', saved.text)
            smtp.assert_not_called()
            changed = get_case(**keys, case_id=case.id)
            self.assertEqual((changed.customer_email, changed.case_version), (email, case.case_version + 1))
            request_id = re.search(r'name="request_id" value="([a-f0-9]+)"', saved.text)
            assert request_id is not None
            sent = client.post(f"{path}/takeover-reply", data={
                "request_id": request_id[1], "body": draft,
                "expected_case_version": str(changed.case_version),
            })
            self.assertIn("邮件已发送", sent.text)
            smtp.assert_called_once()
            self.assertEqual(smtp.call_args.kwargs["recipients"], [email])

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
            self.assertIn('id="advisor-work"', page.text)
            self.assertIn("确认归类", page.text)
            self.assertEqual(page.text.count("确认归类</button>"), 1)
            self.assertEqual(page.text.count('type="file"'), 1)
            self.assertIn('href="#unclassified-materials"', page.text)
            self.assertIn("选择材料类型", page.text)
            self.assertIn("/evidence/", page.text)
            self.assertIn("/classify", page.text)
            inbox = client.get("/workbench/inbox")
            self.assertNotIn("去案件处理", inbox.text)
            self.assertIn("#advisor-work'>处理</a>", inbox.text)

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

        with patch(
            "visa_agent.case_service.advisor_actions.enqueue_and_maybe_send",
            return_value=replace(_fake_outbox(), status="blocked", block_reasons=("TAKEOVER_ACTIVE",)),
        ):
            blocked = advisor_reply_via_agent(
                settings=settings,
                case_id=case.id,
                body_text="顾问测试答复",
                question_id=q.id,
            )
        self.assertFalse(blocked.closed_question)
        self.assertTrue(any(
            item.id == q.id and item.status == "escalated_human"
            for item in list_open_questions(database_url=DB, tenant_id=TENANT, case_id=case.id)
        ))

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
