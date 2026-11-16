"""Acceptance: blurry passport → auto remediate email → clear scan closes item."""

from __future__ import annotations

import tempfile
import unittest
import uuid
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import psycopg

from visa_agent.ai_capabilities.language import detect_reply_language
from visa_agent.ai_capabilities.ukvi_lookup import select_ukvi_urls
from visa_agent.case_service.inbound_agent import process_customer_inbound
from visa_agent.case_service.itinerary import attach_existing_itinerary_pdfs, get_case_itinerary
from visa_agent.case_service.remediation import list_open_questions
from visa_agent.case_service.service import (
    attribute_inbound_to_case,
    create_case,
    get_case,
    get_review_board,
)
from visa_agent.config import Settings
from visa_agent.document_pipeline.ocr import SyntheticOcrProvider
from visa_agent.document_pipeline.quality import review_attachment_quality
from visa_agent.domain.enums import CaseStatus, RouteCode
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
    def test_budget_totals_and_required_amount_claims_are_checked(self) -> None:
        from visa_agent.ai_capabilities.customer_reply import _has_practical_funds_guidance, _trip_budget

        facts = "我们两人去旅游 11 天，住10晚，往返机票每人预算 £550。"
        budget = _trip_budget(facts, (), trip_days=None)
        self.assertEqual((budget.people, budget.nights, budget.total), (2, 10, 3300))
        question = "银行流水要几个月？存款要准备多少钱？"
        self.assertTrue(_has_practical_funds_guidance("流水建议 6 个月，行程预算 £3,300。", question, budget))
        self.assertFalse(_has_practical_funds_guidance("流水建议 6 个月，行程预算 £5,000。", question, budget))
        self.assertFalse(_has_practical_funds_guidance("流水必须 6 个月，存款必须有 £3,300。", question, budget))

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
        self.assertIn("3–6 个月", qa.text)
        self.assertIn("£1,360", qa.text)
        self.assertIn("资金来源", qa.text)
        self.assertNotIn("https://", qa.text)
        with patch("visa_agent.ai_capabilities.customer_reply.SiliconFlowClient") as model:
            model.complete.return_value = {"choices": [{"message": {
                "content": "请准备能说明旅行资金来源和用途的材料。如需核对个人情况，可以联系顾问。",
            }}]}
            reply = answer_customer_question(
                settings=settings, case=case, question=q, language="zh", chat=model,
                ukvi_snippets=(UkviSnippet(
                    url="https://www.gov.uk/standard-visitor",
                    title="Standard Visitor", text="You may need to show you can support yourself.",
                ),),
                web_hits=(),
            )
        self.assertTrue(reply.used_model)
        self.assertFalse(reply.needs_advisor)


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
        self.enterContext(patch("visa_agent.case_service.inbound_agent.default_ocr_provider",
                                return_value=SyntheticOcrProvider()))

    def test_customer_can_stop_sending(self) -> None:
        address = "stop-delivery@example.com"
        settings = _settings(allowlist=frozenset({address}))
        args = dict(database_url=DB, tenant_id=TENANT)
        case = create_case(**args, customer_email=address, route=RouteCode.STANDARD_VISITOR)
        event = persist_inbound(**args, file_store=LocalFileStore(settings.file_storage_dir), mail=InboundEmail(
            from_addr=address, to_addrs=(settings.email_from,), subject="稍等",
            body_text="请先别发，我还没看完。", rfc_message_id=f"<{uuid.uuid4().hex}@example.com>",
        ))
        attribute_inbound_to_case(**args, case_id=case.id, email_event_id=event.email_event_id)
        before = get_case(**args, case_id=case.id)
        with patch("visa_agent.case_service.inbound_agent.enqueue_and_maybe_send") as send:
            result = process_customer_inbound(settings=settings, case_id=case.id, email_event_id=event.email_event_id)
            send.assert_not_called()
        self.assertEqual(result.conclusion, "stop_delivery")
        live = get_case(**args, case_id=case.id)
        self.assertTrue(live.human_takeover)
        self.assertGreater(live.case_version, before.case_version)

    def test_answers_both_questions_and_follows_rewritten_references(self) -> None:
        address = "follow-up@example.com"
        settings = replace(
            _settings(allowlist=frozenset({address})), email_smtp_host="smtp.example.com",
            email_smtp_user="agent@example.com", email_smtp_password="test", siliconflow_api_key="mock",
        )
        args = dict(database_url=DB, tenant_id=TENANT)
        store = LocalFileStore(settings.file_storage_dir)
        case = create_case(**args, customer_email=address, route=RouteCode.STANDARD_VISITOR)

        def reply(body, parent=None, *, attachments=()):
            event = persist_inbound(**args, file_store=store, mail=InboundEmail(
                from_addr=address, to_addrs=(settings.email_from,), subject="英国行程",
                body_text=body, rfc_message_id=f"<{uuid.uuid4().hex}@example.com>",
                in_reply_to=parent, references="<rewritten@example.net>" if parent else None,
                attachments=attachments,
            ))
            attribute_inbound_to_case(**args, case_id=case.id, email_event_id=event.email_event_id)
            result = process_customer_inbound(settings=settings, case_id=case.id, email_event_id=event.email_event_id)
            self.assertTrue(result.reply_sent)
            return result

        with (
            patch("visa_agent.email_adapter.smtp_client.smtplib.SMTP") as smtp,
            patch("visa_agent.ai_capabilities.customer_reply.SiliconFlowClient") as model,
            patch("visa_agent.ai_capabilities.visitor_trip.SiliconFlowClient") as extractor,
            patch("visa_agent.case_service.itinerary.draft_itinerary", return_value=None),
        ):
            server = smtp.return_value.__enter__.return_value
            server.send_message.return_value = {}
            prepared = (
                "流水可以准备最近 3–6 个月，方便的话就给 6 个月。\n\n"
                "您去 7 天，按 6 晚算，境内开支大致 £660。假如往返机票 £700，"
                "全程预算约 £1,360，再按实际开销调整。"
            )
            model.from_settings.return_value.complete.side_effect = [
                {"choices": [{"message": {"content": prepared}}]},
                {"choices": [{"message": {"content": prepared}}]},
                {"choices": [{"message": {"content": "流水没有统一规定，存款没有固定最低金额。"}}]},
            ]
            def extract(*, messages, **_):
                payload = json.loads(messages[-1]["content"])
                report = dict(purpose=None, departure=None, duration=None)
                received = date.fromisoformat(payload["received_on"])
                year, month = divmod(received.year * 12 + received.month, 12)
                fifteenth = date(year, month + 1, 15).isoformat()
                if "下个月中旬动身" in payload["email"]:
                    report.update(
                        purpose={"value": "tourism", "quote": "我想去英国玩一趟"},
                        departure={"date": fifteenth, "quote": "下个月中旬动身"},
                        duration={"days": 7, "approximate": True, "quote": "待上一周左右"},
                    )
                elif "下个月十五号" in payload["email"]:
                    report["departure"] = {"date": fifteenth, "quote": "下个月十五号"}
                return {"choices": [{"message": {"content": json.dumps(report, ensure_ascii=False)}}]}

            extractor.from_settings.return_value.complete.side_effect = extract
            reply("我想去英国玩一趟，下个月中旬动身，待上一周左右。")
            pending = get_case_itinerary(settings=settings, case_id=case.id)
            self.assertIsNone(pending.start)
            self.assertIsNone(pending.evidence_id)
            self.assertEqual(pending.departure_window, "下个月中旬动身")
            self.assertEqual(pending.days, 7)
            self.assertTrue(pending.duration_approximate)
            clarification = server.send_message.call_args.args[0]
            self.assertIn("提供行程开始日期", clarification.get_body(preferencelist=("plain",)).get_content())
            reply(
                "就定下个月十五号吧。顺便问一下银行流水需要多长时间的？存款需要准备多少钱？",
                str(clarification["Message-ID"]),
            )
            first = server.send_message.call_args.args[0]
            plain = first.get_body(preferencelist=("plain",)).get_content()
            self.assertTrue(plain.startswith("流水可以准备最近 3–6 个月"))
            self.assertIn("£1,360", plain)
            self.assertNotIn("https://", plain)
            self.assertEqual(next(first.iter_attachments()).get_content_type(), "application/pdf")
            plan = get_case_itinerary(settings=settings, case_id=case.id)
            self.assertEqual((plan.start.day, plan.days), (15, 7))
            self.assertTrue(plan.duration_approximate)
            self.assertEqual(plan.departure_window, "")
            self.assertIn("约 7 天", plain)
            messages = model.from_settings.return_value.complete.call_args.kwargs["messages"]
            self.assertIn("preferably 6", messages[0]["content"])
            self.assertIn("trip budget, not", messages[0]["content"])
            first_prompt = json.loads(messages[1]["content"])
            self.assertIn("下个月十五号", first_prompt["latest_email"])
            with psycopg.connect(DB) as conn:
                from visa_agent.case_service.visitor_purpose import get_stated_visitor_purpose
                self.assertEqual(get_stated_visitor_purpose(conn, TENANT, case.id), "tourism")
            self.assertEqual(first_prompt["visitor_spending_benchmark"]["in_uk_gbp_per_person_per_night"], 110)

            result = reply("你没直接回答我的问题啊，2026年11月1日的流水还要吗？我的材料在下面。",
                           str(first["Message-ID"]), attachments=(
                               InboundAttachment("passport.pdf", "application/pdf",
                                                 b"%PDF-1.4 CLEAR_SCAN passport bio-data page sharp"),
                           ))
            second = server.send_message.call_args.args[0]
            plain = second.get_body(preferencelist=("plain",)).get_content()
            self.assertTrue(plain.startswith("流水可以准备最近 3–6 个月"))
            self.assertIn("£1,360", plain)
            self.assertNotIn("https://", plain)
            self.assertEqual(result.remediations_opened, ())

            reply("可以发我官方链接吗？", str(second["Message-ID"]))
            third = server.send_message.call_args.args[0]
            html = third.get_body(preferencelist=("html",)).get_content()
            self.assertIn('<a href="https://www.gov.uk/', html)
            self.assertIn('">英国政府签证指南</a>', html)
            self.assertIn("£1,360", html)
            self.assertNotIn("https://", third.get_body(preferencelist=("plain",)).get_content())

    def test_agent_requests_material_confirmation_and_moves_only_current_reply_to_final_review(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.case_service.material_confirmation import resume_material_confirmations
        from visa_agent.document_pipeline.service import attach_evidence, set_evidence_classification
        from visa_agent.email_adapter.outbox import MATERIAL_CONFIRMATION_OPERATION_PREFIX
        from visa_agent.fastapi_app import app
        from visa_agent.rules.service import ensure_seed_route_packs, record_requirement_applicability

        address = f"confirm-{uuid.uuid4().hex}@example.com"
        settings = replace(
            _settings(allowlist=frozenset({address})), email_smtp_host="smtp.example.com",
            email_smtp_user="agent@example.com", email_smtp_password="test",
        )
        store = LocalFileStore(settings.file_storage_dir)
        args = dict(database_url=DB, tenant_id=TENANT)
        ensure_seed_route_packs(database_url=DB)
        case = create_case(**args, customer_email=address, status=CaseStatus.DRAFT_PREPARATION)
        keys = dict(**args, case_id=case.id)

        def receive(text, parent=None):
            mail = InboundEmail(
                from_addr=address, to_addrs=("agent@example.com",), body_text=text,
                subject="访英材料", in_reply_to=parent,
                rfc_message_id=f"<{uuid.uuid4().hex}@example.com>",
            )
            event = persist_inbound(**args, file_store=store, mail=mail)
            attribute_inbound_to_case(**keys, email_event_id=event.email_event_id)
            return process_customer_inbound(
                settings=settings, case_id=case.id, email_event_id=event.email_event_id,
            )

        def add_file(classification, filename, data=None, content_type="application/pdf"):
            live = get_case(**keys)
            evidence = attach_evidence(
                **keys, file_store=store, data=data or f"%PDF-1.4 {filename}".encode(),
                original_filename=filename, content_type=content_type,
                expected_case_version=live.case_version,
            )
            return set_evidence_classification(**keys, evidence_id=evidence.id, classification=classification)

        def requests():
            with psycopg.connect(DB) as conn:
                return conn.execute(
                    """
                    SELECT rfc_message_id, body_text FROM outbox_messages
                    WHERE case_id = %s AND send_operation_id LIKE 'case-material-confirmation:%%'
                      AND status = 'provider_accepted'
                    ORDER BY case_version
                    """,
                    (case.id,),
                ).fetchall()

        with patch("visa_agent.email_adapter.smtp_client.smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            server.send_message.return_value = {}
            receive("我在准备资料。")
            self.assertEqual(requests(), [])
            self.assertEqual(get_case(**keys).status, CaseStatus.COLLECT_AND_VERIFY)

            passport_bytes = b"\xff\xd8\xff synthetic passport photo"
            add_file("passport", "护照.jpeg", passport_bytes, "image/jpeg")
            for classification in ("purpose", "bank", "employment"):
                add_file(classification, f"{classification}.pdf")
            itinerary = add_file("itinerary", "itinerary.pdf")
            with psycopg.connect(DB) as conn:
                conn.execute(
                    """
                    INSERT INTO case_itinerary_requests (tenant_id, case_id, awaiting_details,
                        source_email_event_id, draft_text, draft_version, draft_evidence_id)
                    VALUES (%s, %s, FALSE, 'itinerary-event', 'Current itinerary', 1, %s)
                    """, (TENANT, case.id, itinerary.id),
                )
            for requirement in ("sponsor_if_any", "translation_if_needed", "transfer_human_triggers"):
                live = get_case(**keys)
                record_requirement_applicability(
                    **keys, requirement_key=requirement, decision="not_applicable",
                    note="已评估", expected_case_version=live.case_version,
                )

            resume_material_confirmations(settings)
            self.assertEqual(requests(), [])
            with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
                reviewed = TestClient(app).post(
                    f"/workbench/cases/{case.id}/requirements/intention_to_leave/review",
                    data={"expected_case_version": get_case(**keys).case_version},
                )
                self.assertIn("材料核对已保存", reviewed.text)
            plan = get_case_itinerary(settings=settings, case_id=case.id)
            self.assertTrue(plan.advisor_reviewed)
            self.assertFalse(plan.confirmed)
            from visa_agent.delivery import assemble_package
            from visa_agent.domain.enums import DeliveryKind

            package = assemble_package(
                **keys, package_kind=DeliveryKind.FINAL_PACKAGE.value, file_store=store,
                evidence_ids=[itinerary.id], expected_case_version=get_case(**keys).case_version,
            )
            self.assertEqual(package.items[0].evidence_id, itinerary.id)

            case_version = get_case(**keys).case_version
            legacy_message_id = f"<legacy-{case.id}@example.com>"
            with psycopg.connect(DB) as conn:
                conn.execute(
                    """
                    INSERT INTO outbox_messages (id, tenant_id, case_id, case_version, send_operation_id,
                        kind, recipients, subject, body_text, rfc_message_id)
                    VALUES (%s, %s, %s, %s, %s, 'ordinary_reply', %s::jsonb, '旧邮件', '请确认第 16 版材料', %s)
                    """,
                    (uuid.uuid4().hex, TENANT, case.id, case_version,
                     f"{MATERIAL_CONFIRMATION_OPERATION_PREFIX}{TENANT}:{case.id}:{case_version}",
                     json.dumps([address]), legacy_message_id),
                )
            resume_material_confirmations(settings)
            [(old_message, summary)] = requests()
            self.assertEqual(old_message, legacy_message_id)
            self.assertIn("确认材料", summary)
            self.assertNotIn("版", summary)
            message = server.send_message.call_args.args[0]
            attachments = {part.get_filename(): part for part in message.iter_attachments()}
            self.assertEqual(set(attachments), {"护照.jpeg", "purpose.pdf", "bank.pdf", "employment.pdf", "itinerary.pdf"})
            self.assertTrue(all(name in summary for name in attachments))
            self.assertEqual(attachments["护照.jpeg"].get_content_type(), "image/jpeg")
            self.assertEqual(attachments["护照.jpeg"].get_payload(decode=True), passport_bytes)
            self.assertEqual(attachments["bank.pdf"].get_payload(decode=True), b"%PDF-1.4 bank.pdf")
            self.assertEqual(get_case(**keys).status, CaseStatus.CUSTOMER_CONFIRMATION)

            with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
                page = TestClient(app).get(f"/workbench/cases/{case.id}").text
            self.assertIn("等客户确认材料", page)
            self.assertNotIn("生成客户说明", page)
            self.assertNotIn("进入客户确认阶段", page)
            self.assertNotIn(f"/workbench/cases/{case.id}/confirm", page)

            send_count = server.send_message.call_count
            resume_material_confirmations(settings)
            self.assertEqual(server.send_message.call_count, send_count)
            receive("收到，谢谢。", old_message)
            self.assertEqual(get_case(**keys).status, CaseStatus.CUSTOMER_CONFIRMATION)

            add_file("bank", "updated-bank.pdf")
            receive("确认本版材料", old_message)
            self.assertEqual(get_case(**keys).status, CaseStatus.CUSTOMER_CONFIRMATION)
            self.assertFalse(get_review_board(**keys).customer_confirmation_effective)
            [_, (current_message, new_summary)] = requests()
            self.assertIn("updated-bank.pdf", new_summary)
            self.assertNotIn("版", new_summary)
            self.assertIn("updated-bank.pdf", {
                part.get_filename() for part in server.send_message.call_args.args[0].iter_attachments()
            })

            receive(f"确认材料。\n\nOn Tuesday, Agent wrote:\n> {new_summary}", current_message)
            self.assertEqual(get_case(**keys).status, CaseStatus.FINAL_REVIEW)
            board = get_review_board(**keys)
            self.assertTrue(board.customer_confirmation_effective)
            self.assertFalse(board.final_approval_effective)

    def test_existing_text_itinerary_becomes_a_pdf_without_needing_another_mail(self) -> None:
        settings = _settings(allowlist=frozenset())
        args = dict(database_url=DB, tenant_id=TENANT)
        case = create_case(**args, customer_email="legacy@example.com")
        draft = "已发原稿\n2026-12-15  抵达伦敦\n2026-12-16  市区步行"
        expected = "Previously sent itinerary\n2026-12-15  Arrive in London\n2026-12-16  Walk in the city centre"
        with psycopg.connect(DB) as conn:
            conn.execute(
                """
                INSERT INTO case_itinerary_requests
                    (tenant_id, case_id, start_date, duration_days, awaiting_details, source_email_event_id,
                     draft_text, draft_version, draft_message_id)
                VALUES (%s, %s, '2026-12-15', 2, FALSE, 'legacy-event', %s, 1, '<legacy@example.com>')
                """,
                (TENANT, case.id, draft),
            )
        with (
            patch("visa_agent.email_adapter.outbox.send_smtp") as smtp,
            patch("visa_agent.ai_capabilities.visitor_itinerary.translate_ocr_pages", return_value=[[
                ("已发原稿", "Previously sent itinerary"),
                ("抵达伦敦", "Arrive in London"),
                ("市区步行", "Walk in the city centre"),
            ]]),
        ):
            self.assertEqual(attach_existing_itinerary_pdfs(settings), 1)
            self.assertEqual(attach_existing_itinerary_pdfs(settings), 0)
            smtp.assert_not_called()
        plan = get_case_itinerary(settings=settings, case_id=case.id)
        self.assertEqual((plan.version, plan.status, plan.draft_text), (1, "awaiting_confirmation", expected))
        from visa_agent.document_pipeline.service import get_evidence

        evidence = get_evidence(**args, case_id=case.id, evidence_id=plan.evidence_id)
        self.assertEqual((evidence.classification, evidence.content_type), ("itinerary", "application/pdf"))

    def test_itinerary_asks_for_missing_date_then_uses_days_from_previous_mail(self) -> None:
        email_addr = f"itinerary-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        store = LocalFileStore(settings.file_storage_dir)
        case_args = dict(database_url=DB, tenant_id=TENANT)
        case = create_case(**case_args, customer_email=email_addr, route=RouteCode.STANDARD_VISITOR)
        with patch("visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
                   side_effect=_fake_outbox) as send, patch(
            "visa_agent.case_service.inbound_agent.answer_customer_question",
        ) as generic:
            for seq, body in enumerate(("我去英国7天，能帮我做个行程吗？", "从2027年1月29日开始。")):
                received = persist_inbound(**case_args, file_store=store, mail=InboundEmail(
                    from_addr=email_addr, to_addrs=("agent@example.com",),
                    subject="行程", body_text=body, provider_message_id=f"itinerary-{case.id}-{seq}",
                ))
                attribute_inbound_to_case(**case_args, case_id=case.id,
                                          email_event_id=received.email_event_id)
                result = process_customer_inbound(
                    settings=settings, case_id=case.id, email_event_id=received.email_event_id,
                )
                self.assertTrue(result and result.reply_sent)
                reply = send.call_args.kwargs["req"].body_text
                if seq == 0:
                    self.assertIn("提供行程开始日期（含年份）", reply)
                    self.assertNotIn("计划停留的天数", reply)
                else:
                    self.assertIn("7 天，2027-01-29 至 2027-02-04", reply)
            generic.assert_not_called()
        self.assertEqual(list_open_questions(**case_args, case_id=case.id), [])

    def test_tourism_reply_fills_purpose_and_builds_itinerary_without_a_separate_request(self) -> None:
        from datetime import date, timedelta

        from fastapi.testclient import TestClient
        from visa_agent.fastapi_app import app
        from visa_agent.rules.service import ensure_seed_route_packs

        address = f"tourism-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({address}))
        args = dict(database_url=DB, tenant_id=TENANT)
        ensure_seed_route_packs(database_url=DB)
        case = create_case(**args, customer_email=address, route=RouteCode.STANDARD_VISITOR)
        departure = date.today() + timedelta(days=40)
        body = f"访问目的是旅游。我从上海出发去爱丁堡，计划从{departure}起停留 5 天。"
        event = persist_inbound(**args, file_store=LocalFileStore(settings.file_storage_dir), mail=InboundEmail(
            from_addr=address, to_addrs=("agent@example.com",), subject="英国旅行",
            body_text=body + "\n\nOn Monday, Agent wrote:\n> 目的是商务。",
            provider_message_id=f"tourism-{case.id}",
        ))
        attribute_inbound_to_case(**args, case_id=case.id, email_event_id=event.email_event_id)
        with patch("visa_agent.email_adapter.outbox.send_smtp", return_value="accepted") as smtp:
            for _ in range(2):
                process_customer_inbound(settings=settings, case_id=case.id, email_event_id=event.email_event_id)
            self.assertEqual(smtp.call_count, 1)
            self.assertNotIn("访问目的说明", smtp.call_args.kwargs["body_text"])
            self.assertTrue(smtp.call_args.kwargs["attachments"][0][1].startswith(b"%PDF-"))
        plan = get_case_itinerary(settings=settings, case_id=case.id)
        self.assertEqual((plan.destination_cities, plan.start), (("Edinburgh",), departure))
        self.assertIn("Edinburgh Castle", plan.draft_text)
        self.assertIn("09:30–12:30", plan.draft_text)
        self.assertNotIn("Tower of London", plan.draft_text)
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            page = TestClient(app).get(f"/workbench/cases/{case.id}")
            self.assertEqual(page.status_code, 200)
            self.assertIn("已记录", page.text)
            self.assertIn("旅游", page.text)
            self.assertIn("Itinerary-v01.pdf", page.text)
            self.assertNotIn("机票与住宿预订参考", page.text)
        self.assertEqual(list_open_questions(**args, case_id=case.id), [])

    def test_non_english_source_yields_translation_for_advisor_review(self) -> None:
        from fastapi.testclient import TestClient
        from visa_agent.case_service.translations import prepare_pending_translations
        from visa_agent.document_pipeline.service import (
            attach_evidence, delete_evidence, list_evidence, set_evidence_quarantine,
        )
        from visa_agent.delivery.service import assemble_package
        from visa_agent.domain.enums import DeliveryKind
        from visa_agent.domain.errors import DomainError
        from visa_agent.fastapi_app import app
        from visa_agent.rules.service import ensure_seed_route_packs, evaluate_case_requirements

        settings = _settings(allowlist=frozenset())
        args = dict(database_url=DB, tenant_id=TENANT)
        ensure_seed_route_packs(database_url=DB)
        case = create_case(**args, customer_email="draft-translation@example.com", route=RouteCode.STANDARD_VISITOR)
        keys = dict(**args, case_id=case.id)
        source = attach_evidence(
            **keys, file_store=LocalFileStore(settings.file_storage_dir), original_filename="statement.pdf",
            content_type="application/pdf", data=b"%PDF-1.4 synthetic original", classification="bank",
            expected_case_version=case.case_version,
        )
        extracted = "银行存款证明\n账户余额为 1234 元"
        with psycopg.connect(DB) as conn:
            conn.execute(
                """
                INSERT INTO evidence_ocr_results (id, tenant_id, case_id, evidence_id, case_version,
                    provider, status, extracted_text, source_content_sha256)
                VALUES (%s, %s, %s, %s, %s, 'vision_local_v1', 'completed', %s, %s)
                """,
                (uuid.uuid4().hex, TENANT, case.id, source.id, source.case_version, extracted, source.content_sha256),
            )
        with patch("visa_agent.case_service.translations.translate_ocr_pages",
                   return_value=[[("银行存款证明", "Bank deposit certificate"), ("账户余额为 1234 元", "Account balance: 1234 yuan")]]) as model:
            self.assertEqual(prepare_pending_translations(settings), 1)
            self.assertEqual(prepare_pending_translations(settings), 0)
            model.assert_called_once_with(extracted)
        draft = next(ev for ev in list_evidence(**keys) if ev.classification == "translation_draft")
        self.assertEqual(draft.quarantine_status, "isolated")
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            page = client.get(f"/workbench/cases/{case.id}")
            self.assertIn('aria-controls="review-translation_if_needed"', page.text)
            self.assertIn(f'aria-controls="signed-translation-{draft.id}"', page.text)
            translation_section = page.text.split('id="translation-materials"', 1)[1].split("</li>\n", 1)[0]
            self.assertIn(f"/evidence/{draft.id}/file?download=1", translation_section)
            file = client.get(f"/workbench/cases/{case.id}/evidence/{draft.id}/file?download=1")
            self.assertEqual(file.headers["content-type"], "application/pdf")
            self.assertTrue(file.content.startswith(b"%PDF-"))
        version = get_case(**keys).case_version
        with self.assertRaises(DomainError):
            set_evidence_quarantine(**keys, evidence_id=draft.id, expected_case_version=version,
                                    quarantine_status="accepted_for_review")
        with self.assertRaisesRegex(DomainError, "unreviewed translation"):
            assemble_package(**keys, package_kind=next(iter(DeliveryKind)).value,
                             file_store=LocalFileStore(settings.file_storage_dir),
                             evidence_ids=[draft.id], expected_case_version=version)
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            url = f"/workbench/cases/{case.id}"
            form = {"classification": "translation", "translation_draft_id": draft.id,
                    "expected_case_version": str(version)}
            duplicate = client.post(f"{url}/evidence", data=form,
                                    files={"file": ("draft.pdf", file.content, "application/pdf")},
                                    follow_redirects=True)
            self.assertIn('aria-controls="review-translation_if_needed"', duplicate.text)
            uploaded = client.post(f"{url}/evidence", data=form, files={
                "file": ("signed.pdf", b"%PDF-1.4 synthetic signed translation", "application/pdf"),
            }, follow_redirects=True)
            self.assertIn('aria-controls="review-translation_if_needed"', uploaded.text)
            self.assertNotIn("待译者核签", uploaded.text)
            formal = next(ev for ev in list_evidence(**keys) if ev.classification == "translation")
            self.assertEqual(formal.translation_source_id, source.id)
            self.assertEqual(formal.quarantine_status, "isolated")
            reviewed = client.post(
                f"{url}/requirements/translation_if_needed/review",
                data={"expected_case_version": str(get_case(**keys).case_version)}, follow_redirects=True,
            )
            translation_section = reviewed.text.split('id="translation-materials"', 1)[1].split("</li>\n", 1)[0]
            self.assertIn("已核对", translation_section)
            self.assertNotIn(f"data-evidence-badge='{formal.id}'", translation_section)
            self.assertNotIn("待译者核签", translation_section)
        current = evaluate_case_requirements(**keys, preview_unbound=True, persist_states=False)
        status = next(s.status for s in current.states if s.requirement_key == "translation_if_needed")
        self.assertEqual(status, "evidence_accepted")
        package = assemble_package(**keys, package_kind=next(iter(DeliveryKind)).value,
                                   file_store=LocalFileStore(settings.file_storage_dir),
                                   evidence_ids=[formal.id], expected_case_version=get_case(**keys).case_version)
        self.assertEqual([item.evidence_id for item in package.items], [formal.id])
        another = attach_evidence(
            **keys, file_store=LocalFileStore(settings.file_storage_dir), original_filename="employment.pdf",
            content_type="application/pdf", data=b"%PDF-1.4 second synthetic original", classification="employment",
            expected_case_version=get_case(**keys).case_version,
        )
        with psycopg.connect(DB) as conn:
            conn.execute(
                """
                INSERT INTO evidence_ocr_results (id, tenant_id, case_id, evidence_id, case_version,
                    provider, status, extracted_text, source_content_sha256)
                VALUES (%s, %s, %s, %s, %s, 'vision_local_v1', 'completed', %s, %s)
                """,
                (uuid.uuid4().hex, TENANT, case.id, another.id, another.case_version,
                 "示例公司全职在职证明", another.content_sha256),
            )
        current = evaluate_case_requirements(**keys, preview_unbound=True, persist_states=False)
        status = next(s.status for s in current.states if s.requirement_key == "translation_if_needed")
        self.assertEqual(status, "applicable_missing")
        with patch("visa_agent.case_service.translations.translate_ocr_pages",
                   return_value=[[("示例公司全职在职证明", "Full-time employment certificate")]]):
            self.assertEqual(prepare_pending_translations(settings), 1)
        new_draft = next(ev for ev in list_evidence(**keys) if ev.classification == "translation_draft")
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            reviewed = client.post(
                f"{url}/requirements/translation_if_needed/review",
                data={"expected_case_version": get_case(**keys).case_version}, follow_redirects=True,
            )
            self.assertIn("材料核对已保存", reviewed.text)
            self.assertIn(f'aria-controls="signed-translation-{new_draft.id}"', reviewed.text)
        current = evaluate_case_requirements(**keys, preview_unbound=True, persist_states=False)
        status = next(s.status for s in current.states if s.requirement_key == "translation_if_needed")
        self.assertEqual(status, "applicable_missing")
        with self.assertRaisesRegex(DomainError, "unreviewed translation"):
            assemble_package(
                **keys, package_kind=DeliveryKind.FINAL_PACKAGE.value,
                file_store=LocalFileStore(settings.file_storage_dir),
                evidence_ids=[new_draft.id], expected_case_version=get_case(**keys).case_version,
            )
        delete_evidence(**keys, evidence_id=another.id, expected_case_version=get_case(**keys).case_version)
        delete_evidence(**keys, evidence_id=source.id, expected_case_version=get_case(**keys).case_version)
        self.assertEqual(list_evidence(**keys), [])

    def test_itinerary_follows_thread_revises_and_confirms_only_the_current_version(self) -> None:
        from visa_agent.rules.service import ensure_seed_route_packs

        address = f"itinerary-roundtrip-{uuid.uuid4().hex[:8]}@example.com"
        settings = replace(_settings(allowlist=frozenset({address})), siliconflow_api_key="mock")
        store = LocalFileStore(settings.file_storage_dir)
        args = dict(database_url=DB, tenant_id=TENANT)
        ensure_seed_route_packs(database_url=DB)
        case = create_case(**args, customer_email=address, route=RouteCode.STANDARD_VISITOR)
        first_id = f"<first-{case.id}@example.com>"

        def reply(text, parent=None, *, message_id=None):
            event = persist_inbound(**args, file_store=store, mail=InboundEmail(
                from_addr=address, to_addrs=("agent@example.com",), subject="英国行程",
                body_text=text, rfc_message_id=message_id or f"<{uuid.uuid4().hex}@example.com>",
                in_reply_to=parent, references=f"{first_id} {parent}" if parent else None,
            ))
            attribute_inbound_to_case(**args, case_id=case.id, email_event_id=event.email_event_id)
            with psycopg.connect(DB) as conn:
                conn.execute("UPDATE email_events SET created_at = '2026-11-16T08:00:00Z' WHERE id = %s",
                             (event.email_event_id,))
            outcome = process_customer_inbound(settings=settings, case_id=case.id,
                                               email_event_id=event.email_event_id)
            self.assertTrue(outcome and outcome.reply_sent)
            return get_case_itinerary(settings=settings, case_id=case.id)

        def revise(*, messages, **_):
            payload = json.loads(messages[-1]["content"])
            if "dates" in payload:
                from visa_agent.ai_capabilities.visitor_itinerary import compose_itinerary

                draft = compose_itinerary(date.fromisoformat(payload["dates"][0]), len(payload["dates"]))
                activities = [row.split("  ", 1)[1] for row in draft.splitlines()[2:]]
                return {"choices": [{"message": {"content": json.dumps({"activities": activities})}}]}
            activities = [line.split("  ", 1)[1] for line in payload["current_rows"]]
            self.assertEqual(payload["language"], "en")
            activities[1] = "Visit Windsor Castle"
            return {"choices": [{"message": {"content": json.dumps({"activities": activities})}}]}

        with patch("visa_agent.case_service.inbound_agent.extract_visitor_trip", return_value=None), patch(
            "visa_agent.email_adapter.outbox.send_smtp", return_value="mock accepted"
        ) as smtp, patch(
            "visa_agent.ai_capabilities.visitor_itinerary.SiliconFlowClient"
        ) as model, patch("visa_agent.case_service.inbound_agent.answer_customer_question") as generic:
            model.from_settings.return_value.complete.side_effect = revise
            pending = reply("访问目的是旅游。我想玩7天，请帮我做一份行程。", message_id=first_id)
            self.assertEqual(pending.status, "awaiting_details")
            first = reply("预计下个月15号出发", smtp.call_args.kwargs["message_id"])
            self.assertEqual((first.version, first.status), (1, "awaiting_confirmation"))
            self.assertIn("2026-12-15 to 2026-12-21", first.draft_text)
            self.assertIn("PDF 行程单", smtp.call_args.kwargs["body_text"])
            self.assertNotIn("请回复「确认行程」", smtp.call_args.kwargs["body_text"])
            filename, content = smtp.call_args.kwargs["attachments"][0]
            self.assertEqual(filename, "Itinerary-v01.pdf")
            self.assertTrue(content.startswith(b"%PDF-"))
            self.assertIn("British Museum", first.draft_text)
            self.assertIn("Windsor Castle", first.draft_text)
            self.assertNotIn("仍缺少", smtp.call_args.kwargs["body_text"])
            from fastapi.testclient import TestClient
            from visa_agent.fastapi_app import app

            evidence_url = f"/workbench/cases/{case.id}/evidence"
            page_url = f"/workbench/cases/{case.id}"
            with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
                client = TestClient(app)
                page = client.get(page_url)
                self.assertEqual(page.status_code, 200)
                self.assertIn("Itinerary-v01.pdf", page.text)
                downloaded = client.get(f"{evidence_url}/{first.evidence_id}/file?download=1")
                self.assertEqual(downloaded.content, content)
                self.assertEqual(downloaded.headers["content-type"], "application/pdf")
                reviewed = client.post(
                    f"{page_url}/requirements/intention_to_leave/review",
                    data={"expected_case_version": get_case(**args, case_id=case.id).case_version},
                )
                self.assertIn("材料核对已保存", reviewed.text)
            self.assertTrue(get_case_itinerary(settings=settings, case_id=case.id).advisor_reviewed)
            second = reply("把行程改到2026年12月18日。", first.message_id)
            self.assertEqual((second.version, second.status), (2, "awaiting_confirmation"))
            self.assertFalse(second.advisor_reviewed)
            self.assertIn("2026-12-18 to 2026-12-24", second.draft_text)
            self.assertNotIn("2026-12-15", second.draft_text)
            self.assertEqual(smtp.call_args.kwargs["attachments"][0][0], "Itinerary-v02.pdf")
            from visa_agent.document_pipeline.service import list_evidence

            current_plan_files = list_evidence(**args, case_id=case.id)
            self.assertEqual([item.id for item in current_plan_files], [second.evidence_id])
            with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
                self.assertEqual(client.get(f"{evidence_url}/{first.evidence_id}/file").status_code, 404)
                page = client.get(page_url)
                self.assertIn("Itinerary-v02.pdf", page.text)
                self.assertNotIn("Itinerary-v01.pdf", page.text)
            stale = reply("确认行程", first.message_id)
            self.assertEqual((stale.version, stale.status), (2, "awaiting_confirmation"))
            third = reply("请把第二天改成参观温莎城堡。", second.message_id)
            self.assertEqual((third.version, third.status), (3, "awaiting_confirmation"))
            self.assertIn("2026-12-19  Visit Windsor Castle", third.draft_text)
            confirmed = reply("确认行程", third.message_id)
            self.assertEqual((confirmed.version, confirmed.status), (3, "confirmed"))
            self.assertEqual(list_evidence(**args, case_id=case.id)[0].quarantine_status,
                             "accepted_for_review")
            generic.assert_not_called()
            generic.return_value = type("Reply", (), {"text": "Test", "needs_advisor": False})()
            reply("可以用这个代替吗？", third.message_id)
            self.assertIn(("inbound", "访问目的是旅游。我想玩7天，请帮我做一份行程。"),
                          generic.call_args.kwargs["thread_history"])

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
        self.assertIn("请重传", body1)
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
        self.assertIn("补件完成", body2)
        remaining = list_open_questions(
            database_url=DB, tenant_id=TENANT, case_id=case.id, active_only=True
        )
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
