"""Acceptance: blurry passport → auto remediate email → clear scan closes item."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.ai_capabilities.ukvi_lookup import select_ukvi_urls
from visa_agent.case_service.inbound_agent import process_customer_inbound
from visa_agent.case_service.remediation import list_open_questions
from visa_agent.case_service.service import attribute_inbound_to_case, create_case
from visa_agent.config import Settings
from visa_agent.document_pipeline.quality import review_attachment_quality
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


class LanguageAndUkviTests(unittest.TestCase):
    def test_detect_zh_from_customer_body(self) -> None:
        self.assertEqual(detect_reply_language("请问护照要彩色吗？"), "zh")
        self.assertEqual(detect_reply_language("When can I submit?"), "en")

    def test_ukvi_urls_are_gov_uk_only(self) -> None:
        urls = select_ukvi_urls(
            question="护照扫描件要求是什么？",
            route=RouteCode.STANDARD_VISITOR.value,
        )
        self.assertTrue(urls)
        self.assertTrue(all("gov.uk" in u for u in urls))

    def test_basic_funds_question_does_not_need_advisor(self) -> None:
        from visa_agent.ai_capabilities.customer_reply import (
            answer_customer_question,
            question_needs_human_judgement,
        )
        from visa_agent.ai_capabilities.ukvi_lookup import UkviSnippet
        from visa_agent.case_service.service import CaseRecord
        from visa_agent.domain.enums import CaseStatus, RouteCode

        q = (
            "这个材料到底算什么？我该怎么准备资金证明呢，"
            "能否详细说明一下政策？"
        )
        self.assertFalse(question_needs_human_judgement(q))
        case = CaseRecord(
            id="c" * 32,
            tenant_id=TENANT,
            case_version=1,
            status=CaseStatus.COLLECT_AND_VERIFY,
            route=RouteCode.STANDARD_VISITOR,
            resume_status=None,
            customer_email="a@b.com",
            title="t",
            waiting_customer=False,
            waiting_third_party=False,
            human_takeover=False,
            system_blocked=False,
            paused=False,
            cancelled=False,
            current_delivery_id=None,
        )
        settings = _settings(allowlist=frozenset({"a@b.com"}))
        # No model key → template path; snippets present → must not escalate.
        qa = answer_customer_question(
            settings=settings,
            case=case,
            question=q,
            language="zh",
            ukvi_snippets=(
                UkviSnippet(
                    url="https://www.gov.uk/standard-visitor",
                    title="Standard Visitor",
                    text="You may need to show you can support yourself.",
                ),
            ),
            web_hits=(),
            enable_web_search=True,
        )
        self.assertFalse(qa.needs_advisor)
        self.assertGreaterEqual(qa.confidence, 0.6)
        self.assertIn("gov.uk", qa.text.lower())


class QualityTests(unittest.TestCase):
    def test_blurry_marker(self) -> None:
        finding = review_attachment_quality(
            filename="passport.pdf",
            data=b"%PDF-1.4 BLURRY passport bio-data",
            ocr_text="BLURRY passport",
            ocr_status="completed",
            classification="passport",
        )
        self.assertEqual(finding.code, "blurry")
        self.assertEqual(finding.severity, "simple_auto")

    def test_clear_scan_ok(self) -> None:
        finding = review_attachment_quality(
            filename="passport.pdf",
            data=b"%PDF-1.4 CLEAR_SCAN passport bio-data page",
            ocr_text="CLEAR_SCAN passport",
            ocr_status="completed",
            classification="passport",
        )
        self.assertEqual(finding.code, "ok")


class RemediationAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_blurry_passport_opens_and_clear_closes(self) -> None:
        email_addr = f"remed-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        store = LocalFileStore(settings.file_storage_dir)
        store.root.mkdir(parents=True, exist_ok=True)
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="补件验收",
            customer_email=email_addr,
            route=RouteCode.STANDARD_VISITOR,
        )

        blurry = persist_inbound(
            database_url=DB,
            tenant_id=TENANT,
            mail=InboundEmail(
                from_addr=email_addr,
                to_addrs=("agent@example.com",),
                subject="护照",
                body_text="见附件，这是护照扫描件",
                provider_message_id=f"blur-{uuid.uuid4().hex}",
                attachments=(
                    InboundAttachment(
                        filename="passport_scan.pdf",
                        content_type="application/pdf",
                        data=b"%PDF-1.4 BLURRY passport bio-data page",
                    ),
                ),
            ),
            file_store=store,
        )
        attribute_inbound_to_case(
            database_url=DB,
            tenant_id=TENANT,
            email_event_id=blurry.email_event_id,
            case_id=case.id,
            reason="test",
        )
        with patch(
            "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
            side_effect=_fake_outbox,
        ) as send1:
            r1 = process_customer_inbound(
                settings=settings,
                case_id=case.id,
                email_event_id=blurry.email_event_id,
            )
        self.assertEqual(r1.language, "zh")
        self.assertIn("passport_valid", r1.remediations_opened)
        self.assertTrue(r1.reply_sent)
        body1 = send1.call_args.kwargs["req"].body_text
        self.assertIn("请重新上传", body1)
        self.assertIn("模糊", body1)
        self.assertNotIn("Hello,", body1)
        open_qs = list_open_questions(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(len(open_qs), 1)
        self.assertEqual(open_qs[0].requirement_key, "passport_valid")
        self.assertEqual(open_qs[0].status, "waiting_customer")

        clear = persist_inbound(
            database_url=DB,
            tenant_id=TENANT,
            mail=InboundEmail(
                from_addr=email_addr,
                to_addrs=("agent@example.com",),
                subject="重传护照",
                body_text="这是清晰版护照",
                provider_message_id=f"clear-{uuid.uuid4().hex}",
                attachments=(
                    InboundAttachment(
                        filename="passport_clear.pdf",
                        content_type="application/pdf",
                        data=b"%PDF-1.4 CLEAR_SCAN passport bio-data page sharp",
                    ),
                ),
            ),
            file_store=store,
        )
        attribute_inbound_to_case(
            database_url=DB,
            tenant_id=TENANT,
            email_event_id=clear.email_event_id,
            case_id=case.id,
            reason="test",
        )
        with patch(
            "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
            side_effect=_fake_outbox,
        ) as send2:
            r2 = process_customer_inbound(
                settings=settings,
                case_id=case.id,
                email_event_id=clear.email_event_id,
            )
        self.assertTrue(r2.remediations_closed)
        self.assertEqual(r2.remediations_opened, ())
        body2 = send2.call_args.kwargs["req"].body_text
        self.assertIn("已根据您新传的清晰材料关闭", body2)
        remaining = list_open_questions(
            database_url=DB, tenant_id=TENANT, case_id=case.id, active_only=True
        )
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
