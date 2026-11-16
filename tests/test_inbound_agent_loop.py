"""Inbound agent loop: attach → classify → reply → mark processed."""

from __future__ import annotations

import base64
import email.mime.application
import email.mime.multipart
import email.mime.text
import tempfile
import unittest
import uuid
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import psycopg

from visa_agent.case_service.inbound_agent import process_customer_inbound
from visa_agent.case_service.outreach import send_opening_outreach
from visa_agent.case_service.service import (
    attribute_inbound_to_case,
    create_case,
    get_case,
)
from visa_agent.config import Settings
from visa_agent.document_pipeline.classify import classify_attachment
from visa_agent.document_pipeline.ocr import SyntheticOcrProvider
from visa_agent.document_pipeline.service import list_evidence
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.imap_poll import poll_imap_inbox
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
        email_from="agent@example.com",
        email_imap_host="imap.example.com",
        email_imap_port=993,
        email_imap_user="agent@example.com",
        email_imap_password="pass",
        outbound_allowlist=allowlist
        or frozenset({"client@example.com"}),
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


class FakeImapUidClient:
    def __init__(self, mails: dict[str, bytes]) -> None:
        self.mails = dict(mails)
        self.unseen = list(mails.keys())
        self.seen: list[str] = []

    def uid(self, command: str, *args):  # noqa: ANN001
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [" ".join(self.unseen).encode()])
        if cmd == "FETCH":
            uid = args[0]
            uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            raw = self.mails[uid_s]
            return ("OK", [(f"{uid_s} (BODY[])".encode(), raw)])
        if cmd == "STORE":
            uid = args[0]
            uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            self.seen.append(uid_s)
            if uid_s in self.unseen:
                self.unseen.remove(uid_s)
            return ("OK", [b""])
        raise AssertionError(f"unexpected UID command {command}")


class ClassifyTests(unittest.TestCase):
    def test_filename_hints(self) -> None:
        cls, req = classify_attachment(filename="my_passport_scan.pdf")
        self.assertEqual(cls, "passport")
        self.assertEqual(req, "passport_valid")

    def test_ocr_hints(self) -> None:
        cls, req = classify_attachment(
            filename="scan.pdf", ocr_text="Passport No: E1234567"
        )
        self.assertEqual(cls, "passport")
        self.assertEqual(req, "passport_valid")


class InboundAgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)
        self.enterContext(patch("visa_agent.case_service.inbound_agent.default_ocr_provider",
                                return_value=SyntheticOcrProvider()))

    def test_imap_thread_routes_between_two_cases_for_same_sender(self) -> None:
        customer = "customer@example.com"
        outsider = "outsider@example.com"
        settings = replace(
            _settings(allowlist=frozenset({customer, outsider})),
            tenant_id=uuid.uuid4().hex,
            email_smtp_host="smtp.example.com",
            email_smtp_user="agent@example.com",
            email_smtp_password="test",
        )
        primary, sibling, unrelated = [
            create_case(
                database_url=DB,
                tenant_id=settings.tenant_id,
                title=f"Thread case {index}",
                customer_email=sender,
                route=RouteCode.STANDARD_VISITOR,
            )
            for index, sender in enumerate((customer, customer, outsider))
        ]
        store = LocalFileStore(settings.file_storage_dir)
        with patch("visa_agent.email_adapter.smtp_client.smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            server.send_message.return_value = {}
            self.assertTrue(send_opening_outreach(settings=settings, case=primary).sent)
            self.assertTrue(send_opening_outreach(settings=settings, case=sibling).sent)
            first, second = [call.args[0] for call in server.send_message.call_args_list]
            primary_id = str(first["Message-ID"])
            sibling_id = str(second["Message-ID"])
            self.assertTrue(primary_id)
            self.assertNotEqual(primary_id, sibling_id)
            subject = f"Re: {first['Subject']}"

            def customer_reply(uid, sender, parent, references=None):  # noqa: ANN001
                mail = EmailMessage()
                mail["From"] = sender
                mail["To"] = settings.email_from
                mail["Subject"] = subject
                mail["Message-ID"] = f"<thread-{uid}-{settings.tenant_id}@example.com>"
                if parent:
                    mail["In-Reply-To"] = parent
                if references:
                    mail["References"] = references
                mail.set_content("Please find attached.")
                return mail.as_bytes()

            imap = FakeImapUidClient({
                "1": customer_reply("1", customer, primary_id),
                "2": customer_reply("2", outsider, primary_id),
                "3": customer_reply("3", customer, primary_id, sibling_id),
                "4": customer_reply("4", outsider, "<unknown@example.com>"),
                "5": customer_reply("5", customer, None),
                "6": customer_reply("6", customer, None, sibling_id),
            })
            result = poll_imap_inbox(
                settings=settings, file_store=store, client=imap, uidvalidity="1"
            )
            self.assertTrue(result.success)
            self.assertEqual(result.count, 6)
            self.assertEqual(server.send_message.call_count, 4)
            agent_reply = server.send_message.call_args_list[2].args[0]
            customer_id = f"<thread-1-{settings.tenant_id}@example.com>"
            self.assertEqual(str(agent_reply["In-Reply-To"]), customer_id)
            self.assertEqual(str(agent_reply["References"]), f"{primary_id} {customer_id}")
            self.assertEqual(str(agent_reply["Subject"]), subject)
            self.assertIsNotNone(agent_reply["Message-ID"])

            with psycopg.connect(DB) as conn:
                case_ids = [
                    conn.execute(
                        "SELECT case_id FROM email_events WHERE tenant_id = %s AND id = %s",
                        (settings.tenant_id, message.email_event_id),
                    ).fetchone()[0]
                    for message in result.messages
                ]
                reply_row = conn.execute(
                    """
                    SELECT case_id, in_reply_to, email_references
                    FROM email_events
                    WHERE tenant_id = %s AND rfc_message_id = %s
                    """,
                    (settings.tenant_id, str(agent_reply["Message-ID"])),
                ).fetchone()
            self.assertEqual(case_ids, [primary.id, None, None, None, None, sibling.id])
            self.assertEqual(reply_row, (primary.id, customer_id, f"{primary_id} {customer_id}"))
            self.assertNotIn(unrelated.id, case_ids)

    def test_attach_classify_reply_and_mark_processed(self) -> None:
        email_addr = f"loop-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        store = LocalFileStore(settings.file_storage_dir)
        store.root.mkdir(parents=True, exist_ok=True)
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="李四 · visitor",
            customer_email=email_addr,
            route=RouteCode.STANDARD_VISITOR,
        )
        inbound = persist_inbound(
            database_url=DB,
            tenant_id=TENANT,
            mail=InboundEmail(
                from_addr=email_addr,
                to_addrs=("agent@example.com",),
                subject="Re: materials",
                body_text="Please find attached my passport. When can I submit?",
                provider_message_id=f"loop-{uuid.uuid4().hex}",
                attachments=(
                    InboundAttachment(
                        filename="passport_bio.pdf",
                        content_type="application/pdf",
                        data=b"%PDF-1.4 passport bio-data page content demo",
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
            ) as send_mock,
            patch(
                "visa_agent.case_service.inbound_agent.answer_customer_question",
                return_value=type(
                    "QA",
                    (),
                    {
                        "language": "en",
                        "text": "You can submit once the checklist is complete.",
                        "source_urls": (),
                        "used_model": False,
                        "used_ukvi": False,
                        "needs_advisor": False,
                    },
                )(),
            ),
        ):
            result = process_customer_inbound(
                settings=settings,
                case_id=case.id,
                email_event_id=inbound.email_event_id,
                send_reply=True,
            )
        self.assertTrue(result.had_question)
        self.assertEqual(result.language, "en")
        self.assertEqual(len(result.attached), 1)
        self.assertEqual(result.attached[0].classification, "passport")
        self.assertTrue(result.reply_sent)
        self.assertEqual(result.conclusion, "material_change")
        self.assertTrue(send_mock.called)
        body = send_mock.call_args.kwargs["req"].body_text
        self.assertIn("passport", body.lower())
        self.assertIn("Still missing", body)
        self.assertIn("Documents filed", body)
        self.assertNotIn("仍缺少", body)
        self.assertNotIn("您好", body)

        evidence = list_evidence(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].classification, "passport")
        self.assertEqual(evidence[0].source_email_event_id, inbound.email_event_id)

        import psycopg

        with psycopg.connect(DB) as conn:
            row = conn.execute(
                """
                SELECT processing_status, processing_conclusion, processed_by
                FROM email_events WHERE id = %s
                """,
                (inbound.email_event_id,),
            ).fetchone()
        self.assertEqual(row[0], "processed")
        self.assertEqual(row[1], "material_change")
        self.assertEqual(row[2], "inbound_agent")

    def test_api_inbound_auto_attributes_and_processes(self) -> None:
        from fastapi.testclient import TestClient

        from visa_agent.fastapi_app import app, bootstrap

        settings = _settings()
        bootstrap(settings)
        email = f"api-loop-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email}))
        create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="API loop",
            customer_email=email,
            route=RouteCode.STANDARD_VISITOR,
        )
        payload = {
            "from_addr": email,
            "to_addrs": ["agent@example.com"],
            "subject": "bank statement",
            "body_text": "见附件流水",
            "provider_message_id": f"api-{uuid.uuid4().hex}",
            "attachments": [
                {
                    "filename": "bank_statement.txt",
                    "content_type": "text/plain",
                    "content_base64": base64.b64encode(
                        b"Account balance IBAN GB00 BANK STATEMENT"
                    ).decode(),
                }
            ],
        }
        with (
            patch("visa_agent.fastapi_app.get_settings", return_value=settings),
            patch(
                "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
                side_effect=_fake_outbox,
            ),
        ):
            client = TestClient(app)
            resp = client.post("/internal/email/inbound", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["attribution_status"], "attributed")
        self.assertIsNotNone(data["case_id"])
        self.assertIsNotNone(data["agent"])
        self.assertTrue(data["agent"]["reply_sent"])
        self.assertEqual(data["agent"]["attached"], 1)

        evidence = list_evidence(
            database_url=DB, tenant_id=TENANT, case_id=data["case_id"]
        )
        self.assertTrue(
            any(e.classification == "bank" for e in evidence),
            [e.classification for e in evidence],
        )

    def test_imap_extracts_attachment_and_runs_agent(self) -> None:
        email_addr = f"imap-loop-{uuid.uuid4().hex[:8]}@example.com"
        settings = _settings(allowlist=frozenset({email_addr}))
        store = LocalFileStore(settings.file_storage_dir)
        store.root.mkdir(parents=True, exist_ok=True)
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title="IMAP loop",
            customer_email=email_addr,
            route=RouteCode.STANDARD_VISITOR,
        )

        msg = email.mime.multipart.MIMEMultipart()
        msg["From"] = email_addr
        msg["To"] = "agent@example.com"
        msg["Subject"] = "passport attached"
        msg["Message-ID"] = f"<imap-{uuid.uuid4().hex}@example.com>"
        msg.attach(email.mime.text.MIMEText(
            "Please find attached the clearer scan of the passport bio-data page.\n\n"
            "On Monday, 16 November, Agent <agent@example.com> wrote:\n"
            "When can you send a clearer scan?\n"
            "> What else can we help with?\n",
            "plain",
        ))
        part = email.mime.application.MIMEApplication(
            b"%PDF-1.4 passport scan bytes", Name="passport.pdf"
        )
        part["Content-Disposition"] = 'attachment; filename="passport.pdf"'
        msg.attach(part)
        raw = msg.as_bytes()

        client = FakeImapUidClient({"501": raw})
        with (
            patch(
                "visa_agent.case_service.inbound_agent.enqueue_and_maybe_send",
                side_effect=_fake_outbox,
            ),
            patch("visa_agent.case_service.inbound_agent.answer_customer_question") as qa,
        ):
            result = poll_imap_inbox(
                settings=settings,
                file_store=store,
                limit=5,
                client=client,
                uidvalidity="1",
            )
        qa.assert_not_called()
        self.assertEqual(result.count, 1)
        self.assertTrue(result.success)
        evidence = list_evidence(
            database_url=DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].classification, "passport")
        live = get_case(database_url=DB, tenant_id=TENANT, case_id=case.id)
        self.assertGreaterEqual(live.case_version, 1)


if __name__ == "__main__":
    unittest.main()
