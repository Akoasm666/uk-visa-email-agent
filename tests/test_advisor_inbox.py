"""Advisor inbox + case list search/filter."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from visa_agent.case_service.inbound_agent import process_customer_inbound
from visa_agent.case_service.remediation import (
    ADVISOR_CUSTOMER_QUESTION,
    ADVISOR_UNCLASSIFIED,
    UnassignedMail,
    list_advisor_alerts,
    list_open_questions,
    open_remediation,
)
from visa_agent.case_service.service import (
    CaseRecord,
    attribute_inbound_to_case,
    create_case,
    get_case,
    list_cases,
    set_takeover,
)
from visa_agent.config import Settings
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


class AdvisorInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_close_case_queues_threaded_notice_and_reuses_it_after_failure(self) -> None:
        from fastapi.testclient import TestClient
        from visa_agent.case_service.closure import resume_closure_notices
        from visa_agent.document_pipeline.service import attach_evidence
        from visa_agent.fastapi_app import app, bootstrap
        from visa_agent.rules.service import record_requirement_applicability, record_requirement_review

        address = f"close-{uuid.uuid4().hex[:8]}@example.com"
        settings = replace(_settings(allowlist=frozenset({address})), tenant_id=uuid.uuid4().hex)
        keys = dict(database_url=DB, tenant_id=settings.tenant_id)
        bootstrap(settings)
        case = create_case(**keys, customer_email=address, status=CaseStatus.READY_TO_DELIVER,
                           route=RouteCode.STANDARD_VISITOR)
        case_keys = dict(**keys, case_id=case.id)
        store = LocalFileStore(settings.file_storage_dir)
        for requirement, classification in (
            ("passport_valid", "passport"), ("visit_purpose_allowed", "purpose"),
            ("intention_to_leave", "itinerary"), ("funds_for_stay", "bank"), ("home_ties", "employment"),
        ):
            attach_evidence(
                **case_keys, file_store=store, original_filename=f"{classification}.pdf",
                data=f"%PDF-1.4 synthetic {classification}".encode(), content_type="application/pdf",
                classification=classification, expected_case_version=get_case(**case_keys).case_version,
            )
            if requirement != "home_ties":
                record_requirement_review(
                    **case_keys, requirement_key=requirement, expected_case_version=get_case(**case_keys).case_version,
                )
        for requirement in ("sponsor_if_any", "translation_if_needed"):
            record_requirement_applicability(
                **case_keys, requirement_key=requirement, decision="not_applicable", note="合成测试材料",
                expected_case_version=get_case(**case_keys).case_version,
            )
        inbound = persist_inbound(
            **keys, file_store=store, mail=InboundEmail(
                from_addr=address, to_addrs=("agent@example.com",), subject="英国签证",
                body_text="已经收到材料，谢谢！", rfc_message_id="<customer-close@example.com>",
            ),
        )
        attribute_inbound_to_case(**keys, case_id=case.id, email_event_id=inbound.email_event_id)
        case = get_case(**case_keys)
        url = f"/workbench/cases/{case.id}"
        form = {"expected_case_version": str(case.case_version)}
        with (
            patch("visa_agent.fastapi_app.get_settings", return_value=settings),
            patch("visa_agent.email_adapter.outbox.send_smtp",
                  side_effect=[OSError("temporary failure"), "accepted"]) as send,
        ):
            client = TestClient(app)
            initial = client.get(url).text
            self.assertIn(">结束案件</button>", initial)
            self.assertIn('data-case-dialog disabled>结束案件</button>', initial)
            self.assertIn("确认结束案件", initial)
            self.assertNotIn("是否需转人工（敏感情形）", initial)
            self.assertNotIn('id="judge-transfer_human_triggers"', initial)
            refused = client.post(f"{url}/close", data=form, follow_redirects=True)
            self.assertIn("必需材料集齐并核对通过后才能结束案件", refused.text)
            send.assert_not_called()
            record_requirement_review(
                **case_keys, requirement_key="home_ties", expected_case_version=case.case_version,
            )
            with psycopg.connect(DB) as conn:
                conn.execute(
                    "UPDATE cases SET status = 'collect_and_verify' WHERE tenant_id = %s AND id = %s",
                    (settings.tenant_id, case.id),
                )
            self.assertIn('data-case-dialog disabled>结束案件</button>', client.get(url).text)
            refused = client.post(f"{url}/close", data=form, follow_redirects=True)
            self.assertIn("只有交付中的案件才能结束", refused.text)
            send.assert_not_called()
            with psycopg.connect(DB) as conn:
                conn.execute(
                    "UPDATE cases SET status = 'ready_to_deliver' WHERE tenant_id = %s AND id = %s",
                    (settings.tenant_id, case.id),
                )
            self.assertIn('data-case-dialog>结束案件</button>', client.get(url).text)

            failed = client.post(f"{url}/close", data=form, follow_redirects=True)
            self.assertIn("案件已结束", failed.text)
            self.assertIn("服务结束通知发送失败。", failed.text)
            self.assertIn(">重试发送通知</button>", failed.text)
            closed = get_case(**keys, case_id=case.id)
            self.assertEqual(closed.status, CaseStatus.CLOSED)
            self.assertEqual(closed.closure_seq, case.closure_seq + 1)

            with psycopg.connect(DB) as conn:
                conn.execute(
                    "UPDATE outbox_messages SET updated_at = NOW() - INTERVAL '6 minutes' "
                    "WHERE tenant_id = %s AND case_id = %s",
                    (settings.tenant_id, case.id),
                )
            resume_closure_notices(settings)
            sent = client.get(url)
            self.assertIn("服务结束通知已发送。", sent.text)
            self.assertNotIn(">结束案件</button>", sent.text)
            client.post(f"{url}/close", data=form)
            self.assertEqual(send.call_count, 2)
            original, retry = (call.kwargs for call in send.call_args_list)
            self.assertEqual(original["message_id"], retry["message_id"])
            self.assertEqual(retry["recipients"], [address])
            self.assertEqual(retry["in_reply_to"], "<customer-close@example.com>")
            self.assertIn("本次签证服务已结束", retry["body_text"])

    def test_terminate_partial_case_stores_optional_reason_without_email(self) -> None:
        from fastapi.testclient import TestClient
        from visa_agent.case_service.closure import resume_closure_notices, termination_reasons
        from visa_agent.fastapi_app import app, bootstrap

        address = f"terminate-{uuid.uuid4().hex[:8]}@example.com"
        settings = replace(_settings(allowlist=frozenset({address})), tenant_id=uuid.uuid4().hex)
        bootstrap(settings)
        keys = dict(database_url=DB, tenant_id=settings.tenant_id)
        with (patch("visa_agent.fastapi_app.get_settings", return_value=settings),
              patch("visa_agent.email_adapter.outbox.send_smtp") as send):
            client = TestClient(app)
            for reason in ("客户决定取消", ""):
                with self.subTest(reason=reason):
                    case = create_case(**keys, customer_email=address, route=RouteCode.STANDARD_VISITOR)
                    url = f"/workbench/cases/{case.id}"
                    self.assertIn('id="terminate-case-dialog"', client.get(url).text)
                    form = {"expected_case_version": str(case.case_version), "operation": "terminate"}
                    terminated = client.post(f"{url}/close", data={**form, "reason": reason}, follow_redirects=True)
                    self.assertIn("案件已终止", terminated.text)
                    self.assertNotIn(">终止案件</button>", terminated.text)
                    self.assertEqual(termination_reasons(settings, [case.id]), {case.id: reason})
                    client.post(f"{url}/close", data={**form, "reason": "重复点击理由"})
                    self.assertEqual(termination_reasons(settings, [case.id]), {case.id: reason})
                    client.post(f"{url}/close", data={"expected_case_version": str(case.case_version)})
            resume_closure_notices(settings)
            send.assert_not_called()
        with psycopg.connect(DB) as conn:
            notices, = conn.execute(
                "SELECT COUNT(*) FROM outbox_messages WHERE tenant_id = %s AND kind = 'case_closure_notice'",
                (settings.tenant_id,),
            ).fetchone()
        self.assertEqual(notices, 0)

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
        with (
            patch(
                "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
                side_effect=_fake_outbox,
            ),
            patch("visa_agent.ai_capabilities.research.lookup_ukvi_snippets", return_value=()),
            patch("visa_agent.ai_capabilities.research.duckduckgo_search", return_value=()),
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
            self.assertNotIn("<h2>顾问收件箱</h2>", home.text)
            inbox = client.get("/workbench/inbox")
            self.assertEqual(inbox.status_code, 200)
            self.assertIn("<h2>顾问收件箱</h2>", inbox.text)
            filtered = client.get(
                "/workbench", params={"q": "护照", "route": "student", "waiting": "takeover"}
            )
            self.assertEqual(filtered.status_code, 200)
            search, rest = filtered.text.split('<dialog class="case-dialog" id="case-filter-dialog"', 1)
            self.assertNotIn("<select", search)
            self.assertIn('name="waiting" value="takeover"', search)
            self.assertIn("筛选 · 2", search)
            dialog = rest.split("</dialog>", 1)[0]
            self.assertIn('type="hidden" name="q" value="护照"', dialog)
            self.assertIn('<option value="student" selected>', dialog)
            self.assertIn('<option value="takeover" selected>', dialog)

    def test_workbench_stage_filters_group_steps_and_separate_terminated_cases(self) -> None:
        import re

        from fastapi.testclient import TestClient
        from visa_agent.fastapi_app import app, bootstrap

        settings = replace(_settings(allowlist=frozenset()), tenant_id=uuid.uuid4().hex)
        bootstrap(settings)
        keys = dict(database_url=DB, tenant_id=settings.tenant_id)
        cases = {status: create_case(**keys, status=status) for status in CaseStatus}
        normal_end = cases[CaseStatus.CLOSED]
        terminated = create_case(**keys, title="提前终止案件", status=CaseStatus.CLOSED)
        with psycopg.connect(DB) as conn:
            conn.execute("UPDATE cases SET closure_seq = 2 WHERE id = %s", (normal_end.id,))
            conn.execute(
                """
                INSERT INTO case_terminations (tenant_id, case_id, closure_seq, reason)
                VALUES (%s, %s, 1, ''), (%s, %s, 0, '')
                """,
                (settings.tenant_id, normal_end.id, settings.tenant_id, terminated.id),
            )

        groups = {
            "lead": {CaseStatus.LEAD, CaseStatus.INTAKE_AUTHORIZATION, CaseStatus.ROUTE_SCOPE_CONFIRMED},
            "collect_and_verify": {CaseStatus.COLLECT_AND_VERIFY},
            "customer_confirmation": {CaseStatus.DRAFT_PREPARATION, CaseStatus.CUSTOMER_CONFIRMATION},
            "final_review": {CaseStatus.FINAL_REVIEW},
            "ready_to_deliver": {CaseStatus.READY_TO_DELIVER, CaseStatus.SENT_AWAITING_ACK},
            "closed": {CaseStatus.CLOSED, CaseStatus.DELIVERED},
            "terminated": set(),
        }
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            home = client.get("/workbench")
            select = home.text.split('<select id="status"', 1)[1].split("</select>", 1)[0]
            self.assertEqual(re.findall(r'<option value="([^"]*)"', select), ["", *groups])
            self.assertIn('value="terminated">案件已终止</option>', select)

            for stage, statuses in groups.items():
                with self.subTest(stage=stage):
                    response = client.get("/workbench", params={"status": stage})
                    self.assertEqual(response.status_code, 200)
                    ids = set(re.findall(r"class='case-link' href='/workbench/cases/([^']+)'", response.text))
                    expected = {terminated.id} if stage == "terminated" else {cases[s].id for s in statuses}
                    self.assertEqual(ids, expected)
                    self.assertIn(f'value="{stage}" selected', response.text)
            terminated_with_nonmatching_query = client.get(
                "/workbench", params={"status": "terminated", "q": "不存在的案件", "page": 2},
            )
            self.assertIn("没有符合条件的案件", terminated_with_nonmatching_query.text)

    def test_workbench_case_pagination_preserves_filters(self) -> None:
        import re
        from html import unescape
        from urllib.parse import parse_qs, urlsplit

        from fastapi.testclient import TestClient
        from visa_agent.fastapi_app import app, bootstrap

        settings = replace(_settings(allowlist=frozenset()), tenant_id=uuid.uuid4().hex)
        bootstrap(settings)
        keys = dict(database_url=DB, tenant_id=settings.tenant_id)
        cases = [
            create_case(**keys, title=f"分页 & 案件 {i}", route=RouteCode.STUDENT)
            for i in range(43)
        ]
        create_case(**keys, title="分页 & 访问签证", route=RouteCode.STANDARD_VISITOR)
        with psycopg.connect(DB) as conn:
            conn.execute("UPDATE cases SET waiting_customer = TRUE WHERE tenant_id = %s", (settings.tenant_id,))
        filters = {"q": "分页 &", "route": "student", "status": "lead", "waiting": "customer"}

        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            first = client.get("/workbench", params=filters)
            self.assertEqual(first.status_code, 200)
            first_ids = re.findall(r"class='case-link' href='/workbench/cases/([^']+)'", first.text)
            self.assertEqual(len(first_ids), 20)
            self.assertNotIn('rel="prev"', first.text)
            self.assertIn("第 1 页 / 共 3 页", first.text)

            next_url = unescape(re.search(r'href="([^"]+)" rel="next"', first.text)[1])
            self.assertEqual(parse_qs(urlsplit(next_url).query),
                             {key: [value] for key, value in {**filters, "page": "2"}.items()})
            second = client.get(next_url)
            self.assertEqual(second.status_code, 200)
            second_ids = re.findall(r"class='case-link' href='/workbench/cases/([^']+)'", second.text)
            self.assertEqual(len(second_ids), 20)
            self.assertIn('rel="prev"', second.text)
            self.assertIn("第 2 页 / 共 3 页", second.text)

            jump_form = first.text.split('<form class="case-page-jump"', 1)[1].split("</form>", 1)[0]
            jump_filters = {
                key: unescape(value)
                for key, value in re.findall(r'type="hidden" name="([^"]+)" value="([^"]*)"', jump_form)
            }
            self.assertEqual(jump_filters, filters)
            self.assertIn('name="page" min="1" max="3"', jump_form)
            third = client.get("/workbench", params={**jump_filters, "page": "3"})
            self.assertEqual(third.status_code, 200)
            third_ids = re.findall(r"class='case-link' href='/workbench/cases/([^']+)'", third.text)
            self.assertEqual(len(third_ids), 3)
            self.assertEqual(set(first_ids + second_ids + third_ids), {case.id for case in cases})
            self.assertIn("第 3 页 / 共 3 页", third.text)
            self.assertNotIn('rel="next"', third.text)

    def test_list_keeps_one_case_link_and_puts_create_form_in_dialog(self) -> None:
        from visa_agent.workbench.pages import render_inbox, render_list

        case = create_case(database_url=DB, tenant_id=TENANT, title="精简列表")
        home = render_list([replace(case, human_takeover=True, waiting_customer=True)], alert_count=8)
        self.assertEqual(home.count(f"/workbench/cases/{case.id}"), 1)
        self.assertNotIn(f"{case.id[:10]}…", home)
        self.assertIn('<div class="brand-mark">英国签证助手工作台</div>', home)
        self.assertNotIn('<div class="brand-sub">', home)
        self.assertIn(">案件列表</a>", home)
        self.assertIn('>收件箱<span class="inbox-count" aria-hidden="true">8</span></a>', home)
        self.assertIn("<th>办理阶段</th>", home)
        row = home.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertIn("刚开案", row)
        self.assertEqual(row.count("class='badge "), 1)
        self.assertNotIn("人工接管中", row)
        self.assertNotIn("等客户", row)
        self.assertIn('aria-haspopup="dialog" aria-controls="create-case-dialog"', home)
        dialog = home.split('<dialog class="case-dialog" id="create-case-dialog"', 1)[1]
        dialog = dialog.split("</dialog>", 1)[0]
        self.assertNotIn(' open', dialog.split('>', 1)[0])
        self.assertIn('<form method="post" action="/workbench/cases">', dialog)
        self.assertIn("新建并发送首封邮件", dialog)
        empty = render_list([])
        self.assertIn("还没有案件", empty)
        self.assertIn(">收件箱</a>", empty)
        self.assertIn("没有符合条件的案件", render_list([], filters={"q": "护照"}))

        inbox = render_inbox(unassigned_mail=[UnassignedMail("mail-id", "client@example.com", "材料")])
        self.assertNotIn("收件记录 ID", inbox)
        self.assertIn("<summary>排查编号</summary><code>mail-id</code>", inbox)

    def test_inbox_badges_decrease_only_when_items_are_processed(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.fastapi_app import app, bootstrap

        settings = replace(_settings(allowlist=frozenset()), tenant_id=uuid.uuid4().hex)
        bootstrap(settings)
        keys = {"database_url": DB, "tenant_id": settings.tenant_id}
        case = create_case(**keys, title="待核对案件")
        takeover = create_case(**keys, title="人工跟进案件")
        closed = create_case(**keys, title="已结案的测试案件", status=CaseStatus.CLOSED)
        set_takeover(**keys, case_id=case.id, active=True)
        set_takeover(**keys, case_id=takeover.id, active=True)
        question_input = dict(
            **keys, case_id=case.id, requirement_key=ADVISOR_CUSTOMER_QUESTION,
            reason_code="customer_question_needs_advisor", severity="complex_human",
        )
        question = open_remediation(**question_input, reason_detail="请顾问核对")

        def inbound(subject: str) -> str:
            return persist_inbound(
                **keys, file_store=LocalFileStore(settings.file_storage_dir),
                mail=InboundEmail(from_addr="unknown@example.com", subject=subject,
                                  to_addrs=("agent@example.com",), body_text="测试来信"),
            ).email_event_id

        mail_id = inbound("待归属来信一")
        second_mail = inbound("待归属来信二")
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)

            def expect_badges(count: int) -> str:
                home, inbox = (client.get(path) for path in ("/workbench", "/workbench/inbox"))
                for page in (home, inbox):
                    self.assertEqual(page.status_code, 200)
                    if count:
                        self.assertIn(f'aria-label="收件箱，{count} 项待处理"', page.text)
                    else:
                        self.assertNotIn('<span class="inbox-count"', page.text)
                return inbox.text

            inbox = expect_badges(5)
            self.assertIn(f"/workbench/cases/{case.id}#advisor-work'>处理</a>", inbox)
            self.assertIn(f"/workbench/inbox/mail/{mail_id}'>处理</a>", inbox)
            self.assertNotIn("标为已读", inbox)
            self.assertEqual(client.get(f"/workbench/cases/{case.id}").status_code, 200)
            review = client.get(f"/workbench/inbox/mail/{mail_id}")
            self.assertIn("测试来信", review.text)
            self.assertNotIn(f'value="{closed.id}"', review.text)
            expect_badges(5)

            client.post(f"/workbench/cases/{case.id}/advisor-ack", data={"question_id": question.id})
            expect_badges(4)
            with patch("visa_agent.case_service.inbound_agent.enqueue_and_maybe_send") as send:
                assigned = client.post(f"/workbench/inbox/mail/{mail_id}", data={"case_id": case.id})
                send.assert_not_called()
            self.assertIn("来信已归到案件", assigned.text)
            expect_badges(3)
            self.assertEqual(client.get(f"/workbench/inbox/mail/{mail_id}").status_code, 404)
            failed = client.post(f"/workbench/inbox/mail/{second_mail}", data={"case_id": closed.id})
            self.assertIn("案件已结束", failed.text)
            expect_badges(3)

            client.post(f"/workbench/cases/{takeover.id}/takeover", data={"active": "false"})
            expect_badges(2)
            client.post(f"/workbench/cases/{case.id}/takeover", data={"active": "false"})
            expect_badges(1)
            client.post(f"/workbench/inbox/mail/{second_mail}", data={"case_id": case.id})
            expect_badges(0)
            inbound("新来信")
            expect_badges(1)

    def test_material_applicability_is_saved_for_an_unbound_case(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.domain.errors import DomainError
        from visa_agent.fastapi_app import app, bootstrap
        from visa_agent.rules.service import evaluate_case_requirements

        settings = _settings(allowlist=frozenset())
        bootstrap(settings)
        case = create_case(database_url=DB, tenant_id=TENANT, title="材料适用性")
        path = f"/workbench/cases/{case.id}"
        sponsor_path = f"{path}/requirements/sponsor_if_any/applicability"
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            page = client.get(path)
            self.assertEqual(page.status_code, 200)
            passport = page.text.split('class="material-title">护照', 1)[1].split("</li>", 1)[0]
            self.assertIn(">还缺</span>", passport)
            self.assertNotIn("data-open-material-dialog", passport)
            self.assertIn('aria-controls="judge-sponsor_if_any"', page.text)

            saved = client.post(sponsor_path, data={
                "decision": "not_applicable", "note": "客户自费", "expected_case_version": 1,
            })
            self.assertEqual(saved.status_code, 200)
            sponsor = saved.text.split('class="material-title">担保人材料（如有）', 1)[1]
            sponsor = sponsor.split("</li>", 1)[0]
            self.assertIn(">不适用</span>", sponsor)
            self.assertIn("客户自费", sponsor)
            self.assertIn("修改</button>", sponsor)

            stale = client.post(sponsor_path, data={
                "decision": "applies", "expected_case_version": 1,
            })
            self.assertIn("案件有更新", stale.text)
            applicable = client.post(sponsor_path, data={
                "decision": "applies", "expected_case_version": 2,
            })
            sponsor = applicable.text.split('class="material-title">担保人材料（如有）', 1)[1]
            self.assertIn(">还缺</span>", sponsor.split("</li>", 1)[0])
            reset = client.post(sponsor_path, data={
                "decision": "", "expected_case_version": 3,
            })
            self.assertIn("材料判断已保存", reset.text)
            sponsor = reset.text.split('class="material-title">担保人材料（如有）', 1)[1]
            self.assertIn(">待判断</span>", sponsor.split("</li>", 1)[0])

            required = client.post(f"{path}/requirements/passport_valid/applicability", data={
                "decision": "not_applicable", "expected_case_version": 4,
            })
            self.assertIn("此项目无需手动判断", required.text)
        with self.assertRaises(DomainError):
            evaluate_case_requirements(database_url=DB, tenant_id=TENANT, case_id=case.id)

    def test_reviewing_material_clears_pending_until_new_file_is_added(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.document_pipeline import attach_evidence
        from visa_agent.fastapi_app import app, bootstrap

        settings = _settings(allowlist=frozenset())
        bootstrap(settings)
        case = create_case(database_url=DB, tenant_id=TENANT, title="材料核对")
        keys = dict(database_url=DB, tenant_id=TENANT, case_id=case.id)
        store = LocalFileStore(settings.file_storage_dir)
        path = f"/workbench/cases/{case.id}"
        review = f"{path}/requirements/passport_valid/review"

        def add_file(name: str, classification: str) -> int:
            return attach_evidence(
                **keys, file_store=store, original_filename=name, classification=classification,
                content_type="text/plain", data=name.encode(),
                expected_case_version=get_case(**keys).case_version,
            ).case_version

        add_file("行程单.txt", "itinerary")
        version = add_file("护照.txt", "passport")
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            client = TestClient(app)
            before = client.get(path)
            self.assertIn('aria-controls="review-passport_valid"', before.text)
            self.assertIn('aria-controls="review-intention_to_leave"', before.text)
            self.assertIn("核对通过</button>", before.text)
            saved = client.post(review, data={"expected_case_version": version})
            self.assertIn("材料核对已保存", saved.text)
            self.assertNotIn('aria-controls="review-passport_valid"', saved.text)
            self.assertIn('aria-controls="review-intention_to_leave"', saved.text)
            passport_file_row = saved.text.split("护照.txt</div>", 1)[1].split("</li>", 1)[0]
            self.assertIn(">已核对</span>", passport_file_row)
            self.assertNotIn("data-evidence-badge", passport_file_row)
            self.assertEqual(get_case(**keys).case_version, version)

            add_file("补充护照.txt", "passport")
            stale = client.post(review, data={"expected_case_version": version})
            self.assertIn("材料有更新", stale.text)
            self.assertIn('aria-controls="review-passport_valid"', stale.text)


class CaseDetailRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = CaseRecord(
            id="case-ui",
            tenant_id="ui-fixture",
            case_version=7,
            status=CaseStatus.COLLECT_AND_VERIFY,
            route=RouteCode.STANDARD_VISITOR,
            resume_status=None,
            customer_email="client@example.com",
            title="测试案件",
            waiting_customer=False,
            waiting_third_party=False,
            human_takeover=False,
            system_blocked=False,
            paused=False,
            cancelled=False,
            current_delivery_id=None,
        )

    def test_itinerary_only_shows_current_draft_and_human_readable_status(self) -> None:
        from visa_agent.workbench.pages import render_detail

        self.assertNotIn('id="itinerary"', render_detail(self.case))
        plan = SimpleNamespace(draft_text=None, version=0, status="awaiting_details")
        waiting = render_detail(self.case, itinerary=plan)
        self.assertIn("等客户补充旅行日期或停留天数", waiting)
        self.assertNotIn("第 0 版", waiting)
        plan = SimpleNamespace(draft_text="第一天\n<London>", version=2,
                               status="awaiting_confirmation", evidence_id="itinerary-2")
        document = SimpleNamespace(id=plan.evidence_id, original_filename="行程单-v02.pdf",
                                   classification="itinerary", quarantine_status="isolated")
        draft = render_detail(self.case, itinerary=plan, evidence_items=[document])
        row = draft.split('id="itinerary"', 1)[1].split("</li>", 1)[0]
        self.assertIn("行程单-v02.pdf", row)
        self.assertIn("/itinerary-2/file?download=1", row)
        self.assertNotIn("第 2 版", row)
        self.assertNotIn("等客户确认", row)
        self.assertIn("行程单待核对", draft)
        self.assertIn('aria-controls="review-intention_to_leave"', draft)
        self.assertNotIn("第一天\n&lt;London&gt;", draft)
        plan.status = "draft_ready"
        unsent = render_detail(self.case, itinerary=plan)
        self.assertIn('class="material-note">草案待发送</p>', unsent)
        plan.status = "confirmed"
        verified = render_detail(self.case, itinerary=plan, evidence_items=[document])
        row = verified.split('id="itinerary"', 1)[1].split("</li>", 1)[0]
        self.assertIn("行程单-v02.pdf", row)
        self.assertNotIn("客户已确认", row)
        self.assertNotIn("行程单待核对", verified)

    def test_visit_purpose_displays_its_category(self) -> None:
        from visa_agent.workbench.pages import render_detail

        purpose = SimpleNamespace(
            requirement_key="visit_purpose_allowed", status="received_pending_review",
            severity="required", reason="旅游（来自客户邮件）",
        )
        page = render_detail(self.case, requirement_states=[purpose])
        self.assertIn('class="material-title">访问目的</span>', page)
        self.assertIn('class="material-note">旅游</p>', page)
        self.assertNotIn("来自客户邮件", page)

    def test_evidence_removal_needs_explicit_post_and_history_is_read_only(self) -> None:
        from visa_agent.workbench.pages import render_detail, render_evidence_view

        evidence = SimpleNamespace(
            id="scan-ui", original_filename="证件.pdf", classification="passport",
            quarantine_status="accepted_for_review",
        )
        href = f"/workbench/cases/{self.case.id}/evidence/{evidence.id}/delete"
        current = render_detail(self.case, evidence_items=[evidence])
        self.assertIn('aria-controls="delete-file-scan-ui" data-open-material-dialog>删除</button>', current)
        confirmation = current.split('<dialog class="case-dialog file-delete-dialog"', 1)[1]
        confirmation = confirmation.split("</dialog>", 1)[0]
        self.assertIn('id="delete-file-scan-ui"', confirmation)
        self.assertIn("将从此案件移除「证件.pdf」。", confirmation)
        self.assertIn(f'<form method="post" action="{href}">', confirmation)
        self.assertIn('name="expected_case_version" value="7"', confirmation)
        self.assertIn('data-close-material-dialog autofocus>取消</button>', confirmation)
        self.assertIn('<button class="btn file-delete-confirm" type="submit">删除</button>', confirmation)
        self.assertIn('class="text-action" type="submit">人工接管</button>', current)
        preview = render_evidence_view(self.case, evidence=evidence, text_preview=None, can_inline_frame=True)
        self.assertNotIn(href, preview)
        for status in (CaseStatus.SENT_AWAITING_ACK, CaseStatus.DELIVERED, CaseStatus.CLOSED):
            with self.subTest(status=status):
                history = replace(self.case, status=status)
                self.assertNotIn(href, render_detail(history, evidence_items=[evidence]))

    def test_finished_case_with_unclassified_files_and_open_tasks_is_read_only(self) -> None:
        from visa_agent.workbench.pages import render_detail

        evidence = SimpleNamespace(id="scan-ui", original_filename="文件.pdf", classification="unclassified")
        question = SimpleNamespace(id="question-ui", status="escalated_human", requirement_key=ADVISOR_CUSTOMER_QUESTION)
        for status in (CaseStatus.DELIVERED, CaseStatus.CLOSED):
            with self.subTest(status=status):
                case = replace(self.case, status=status, closure_seq=1, human_takeover=True)
                page = render_detail(
                    case, evidence_items=[evidence], advisor_items=[question],
                )
                if status == CaseStatus.DELIVERED:
                    self.assertEqual(page.count("<form"), 2)
                    self.assertIn(f"/workbench/cases/{case.id}/close", page)
                else:
                    self.assertNotIn("<form", page)
                self.assertNotIn('id="advisor-work"', page)
                self.assertIn("/evidence/scan-ui/view", page)
                self.assertIn("/evidence/scan-ui/file?download=1", page)


if __name__ == "__main__":
    unittest.main()
