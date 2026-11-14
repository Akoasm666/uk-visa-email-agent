from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from smtplib import (
    SMTPDataError,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
)
from unittest.mock import MagicMock, patch

import psycopg
from fastapi.testclient import TestClient

from visa_agent.config import get_settings
from visa_agent.email_adapter.errors import SendStatusUnknownError
from visa_agent.email_adapter.inbound import InboundEmail, persist_inbound
from visa_agent.email_adapter.outbox import OutboxRequest, enqueue_and_maybe_send
from visa_agent.email_adapter.smtp_client import SendRejectedError, send_smtp
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"


def _base_env(tmp: Path, *, allowlist: str) -> Path:
    env_path = tmp / ".env"
    env_path.write_text(
        "\n".join(
            [
                "APP_HOST=127.0.0.1",
                "APP_PORT=8000",
                f"DATABASE_URL={DB}",
                f"FILE_STORAGE_DIR={tmp / 'files'}",
                "TENANT_ID=demo",
                "EMAIL_SMTP_HOST=smtp-relay.brevo.com",
                "EMAIL_SMTP_PORT=587",
                "EMAIL_SMTP_USER=user",
                "EMAIL_SMTP_PASSWORD=pass",
                "EMAIL_FROM=baroncpz666@gmail.com",
                f"EMAIL_OUTBOUND_ALLOWLIST={allowlist}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_path


class InboundDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_duplicate_provider_message_does_not_create_second_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            provider_id = f"test-{uuid.uuid4().hex}"
            mail = InboundEmail(
                from_addr="customer@example.com",
                to_addrs=("agent@example.com",),
                subject="hello",
                body_text="body",
                provider_message_id=provider_id,
            )
            first = persist_inbound(
                database_url=DB, tenant_id="demo", mail=mail, file_store=store
            )
            second = persist_inbound(
                database_url=DB, tenant_id="demo", mail=mail, file_store=store
            )
            self.assertFalse(first.duplicate)
            self.assertTrue(second.duplicate)
            self.assertEqual(first.email_event_id, second.email_event_id)

    def test_claimed_case_id_does_not_auto_attribute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            result = persist_inbound(
                database_url=DB,
                tenant_id="demo",
                mail=InboundEmail(
                    from_addr="a@b.com",
                    to_addrs=("c@d.com",),
                    subject="x",
                    body_text="y",
                    provider_message_id=f"claim-{uuid.uuid4().hex}",
                    claimed_case_id="nonexistent-case",
                ),
                file_store=store,
            )
            self.assertFalse(result.duplicate)
            self.assertEqual(result.attribution_status, "unattributed")

    def test_concurrent_inbound_single_event_and_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            provider_id = f"conc-{uuid.uuid4().hex}"
            mail = InboundEmail(
                from_addr="customer@example.com",
                to_addrs=("agent@example.com",),
                subject="concurrent",
                body_text="body",
                provider_message_id=provider_id,
            )
            results: list = []
            errors: list = []

            def worker() -> None:
                try:
                    results.append(
                        persist_inbound(
                            database_url=DB,
                            tenant_id="demo",
                            mail=mail,
                            file_store=store,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 12)
            ids = {r.email_event_id for r in results}
            self.assertEqual(len(ids), 1)
            self.assertEqual(sum(1 for r in results if not r.duplicate), 1)
            json_files = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(json_files), 1)

    def test_duplicate_with_different_body_does_not_overwrite_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            provider_id = f"ow-{uuid.uuid4().hex}"
            first = persist_inbound(
                database_url=DB,
                tenant_id="demo",
                mail=InboundEmail(
                    from_addr="customer@example.com",
                    to_addrs=("agent@example.com",),
                    subject="hello",
                    body_text="original-body",
                    provider_message_id=provider_id,
                ),
                file_store=store,
            )
            self.assertFalse(first.duplicate)
            self.assertIsNotNone(first.raw_file_id)
            path = Path(tmp) / f"{first.raw_file_id}.json"
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["body_text"],
                "original-body",
            )

            second = persist_inbound(
                database_url=DB,
                tenant_id="demo",
                mail=InboundEmail(
                    from_addr="customer@example.com",
                    to_addrs=("agent@example.com",),
                    subject="hello",
                    body_text="ATTACKER-OVERWRITE",
                    provider_message_id=provider_id,
                ),
                file_store=store,
            )
            self.assertTrue(second.duplicate)
            self.assertEqual(second.email_event_id, first.email_event_id)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["body_text"],
                "original-body",
            )
            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    "SELECT body_text FROM email_events WHERE id = %s",
                    (first.email_event_id,),
                ).fetchone()
            self.assertEqual(row[0], "original-body")

    def test_file_write_failure_leaves_no_row_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider_id = f"fw-{uuid.uuid4().hex}"
            mail = InboundEmail(
                from_addr="customer@example.com",
                to_addrs=("agent@example.com",),
                subject="hello",
                body_text="recover-me",
                provider_message_id=provider_id,
            )

            class BoomStore(LocalFileStore):
                def __init__(self, root: Path) -> None:
                    super().__init__(root)
                    self.boom = True

                def put_bytes_deterministic(  # type: ignore[override]
                    self,
                    key: str,
                    data: bytes,
                    *,
                    suffix: str = ".json",
                    overwrite: bool = False,
                ):
                    if self.boom:
                        self.boom = False
                        raise OSError("disk full")
                    return super().put_bytes_deterministic(
                        key, data, suffix=suffix, overwrite=overwrite
                    )

            boom = BoomStore(Path(tmp))
            with self.assertRaises(OSError):
                persist_inbound(
                    database_url=DB, tenant_id="demo", mail=mail, file_store=boom
                )
            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM email_events
                    WHERE tenant_id = 'demo' AND dedupe_key = %s
                    """,
                    (f"provider:{provider_id}",),
                ).fetchone()
            self.assertEqual(row[0], 0)

            ok_store = LocalFileStore(Path(tmp))
            result = persist_inbound(
                database_url=DB, tenant_id="demo", mail=mail, file_store=ok_store
            )
            self.assertFalse(result.duplicate)
            self.assertTrue(
                (Path(tmp) / f"{result.raw_file_id}.json").is_file()
            )

    def test_concurrent_divergent_bodies_db_and_file_match_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            provider_id = f"div-{uuid.uuid4().hex}"
            barrier = threading.Barrier(12)
            results: list = []
            errors: list = []

            def worker(body: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        (
                            body,
                            persist_inbound(
                                database_url=DB,
                                tenant_id="demo",
                                mail=InboundEmail(
                                    from_addr="customer@example.com",
                                    to_addrs=("agent@example.com",),
                                    subject="hello",
                                    body_text=body,
                                    provider_message_id=provider_id,
                                ),
                                file_store=store,
                            ),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            bodies = [f"body-{i}" for i in range(12)]
            threads = [
                threading.Thread(target=worker, args=(body,)) for body in bodies
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 12)
            winners = [pair for pair in results if not pair[1].duplicate]
            self.assertEqual(len(winners), 1)
            winner_body, winner = winners[0]
            event_ids = {pair[1].email_event_id for pair in results}
            self.assertEqual(len(event_ids), 1)

            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    "SELECT body_text, raw_file_id FROM email_events WHERE id = %s",
                    (winner.email_event_id,),
                ).fetchone()
            self.assertEqual(row[0], winner_body)
            path = Path(tmp) / f"{row[1]}.json"
            self.assertTrue(path.is_file())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["body_text"],
                winner_body,
            )
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["body_text"],
                row[0],
            )

    def test_retry_backfills_missing_original_after_orphaned_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            provider_id = f"orphan-{uuid.uuid4().hex}"
            event_id = uuid.uuid4().hex
            dedupe_key = f"provider:{provider_id}"
            with psycopg.connect(DB) as conn:
                conn.execute(
                    """
                    INSERT INTO email_events (
                        id, tenant_id, direction, provider_message_id,
                        dedupe_key, from_addr, to_addrs, subject, body_text,
                        raw_file_id, attribution_status
                    ) VALUES (
                        %s, 'demo', 'inbound', %s,
                        %s, 'customer@example.com', %s::jsonb, 'hello', 'original-body',
                        NULL, 'unattributed'
                    )
                    """,
                    (
                        event_id,
                        provider_id,
                        dedupe_key,
                        json.dumps(["agent@example.com"]),
                    ),
                )
                conn.commit()

            result = persist_inbound(
                database_url=DB,
                tenant_id="demo",
                mail=InboundEmail(
                    from_addr="customer@example.com",
                    to_addrs=("agent@example.com",),
                    subject="hello",
                    body_text="ATTACKER-BODY",
                    provider_message_id=provider_id,
                ),
                file_store=store,
            )
            self.assertEqual(result.email_event_id, event_id)
            self.assertFalse(result.duplicate)
            path = Path(tmp) / f"{result.raw_file_id}.json"
            self.assertTrue(path.is_file())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["body_text"],
                "original-body",
            )


class OutboxGateIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_blocks_recipient_not_on_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append((recipients, subject, body))
                return "fake-id"

            result = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("evil@example.com",),
                    subject="nope",
                    body_text="x",
                ),
                send_fn=fake_send,
            )
            self.assertEqual(result.status, "blocked")
            self.assertIn("OUTBOUND_NOT_ALLOWLISTED", result.block_reasons)
            self.assertEqual(sent, [])

    def test_rejects_non_ordinary_or_package_kinds_before_case_service(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            result = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="final_package",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="pkg",
                    body_text="x",
                ),
                send_fn=lambda *a, **k: "x",
            )
            self.assertEqual(result.status, "blocked")
            self.assertIn("VALIDATION_ERROR", result.block_reasons)

    def test_allowlisted_send_uses_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append((tuple(recipients), subject, body))
                return "fake-id"

            result = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject=f"ok-{uuid.uuid4().hex}",
                    body_text="hello",
                ),
                send_fn=fake_send,
            )
            self.assertEqual(result.status, "provider_accepted")
            self.assertEqual(len(sent), 1)

    def test_status_unknown_does_not_resend_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def flaky_send(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                raise SendStatusUnknownError("receipt lost after accept")

            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"unknown-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=f"op-{uuid.uuid4().hex}",
            )
            first = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=flaky_send
            )
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=flaky_send
            )
            self.assertEqual(first.status, "status_unknown")
            self.assertEqual(second.status, "status_unknown")
            self.assertTrue(second.resent_blocked)
            self.assertEqual(calls["n"], 1)

    def test_provider_ok_but_local_persist_fail_is_status_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def ok_send(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                return "provider-accepted-id"

            op = f"op-persist-{uuid.uuid4().hex}"
            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"persist-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=op,
            )
            first = enqueue_and_maybe_send(
                settings=settings,
                req=req,
                send_fn=ok_send,
                _force_persist_error=RuntimeError("disk write failed after accept"),
            )
            self.assertEqual(first.status, "status_unknown")
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=ok_send
            )
            self.assertEqual(second.status, "status_unknown")
            self.assertTrue(second.resent_blocked)
            self.assertEqual(calls["n"], 1)

    def test_rejects_multi_recipient_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(
                Path(tmp),
                allowlist="a@example.com,b@example.com",
            )
            settings = get_settings(env)
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append(list(recipients))
                return "fake-id"

            result = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("a@example.com", "b@example.com"),
                    subject=f"multi-{uuid.uuid4().hex}",
                    body_text="x",
                ),
                send_fn=fake_send,
            )
            self.assertEqual(result.status, "blocked")
            self.assertIn("VALIDATION_ERROR", result.block_reasons)
            self.assertEqual(sent, [])

    def test_smtp_explicit_reject_is_failed_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def reject_then_ok(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                if calls["n"] == 1:
                    raise SendRejectedError(
                        "SMTP rejected with 451: temporary local problem"
                    )
                return "fake-id"

            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"reject-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=f"op-reject-{uuid.uuid4().hex}",
            )
            first = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=reject_then_ok
            )
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=reject_then_ok
            )
            self.assertEqual(first.status, "failed")
            self.assertEqual(second.status, "provider_accepted")
            self.assertEqual(calls["n"], 2)

    def test_outbox_data_451_via_real_smtp_types_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def send_via_client(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                with patch(
                    "visa_agent.email_adapter.smtp_client.smtplib.SMTP"
                ) as mock_smtp_cls:
                    session = MagicMock()
                    mock_smtp_cls.return_value.__enter__.return_value = session
                    if calls["n"] == 1:
                        session.send_message.side_effect = SMTPDataError(
                            451, b"Try again later"
                        )
                    else:
                        session.send_message.return_value = {}
                    return send_smtp(
                        s, recipients=recipients, subject=subject, body_text=body
                    )

            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"data451-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=f"op-data451-{uuid.uuid4().hex}",
            )
            first = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            self.assertEqual(first.status, "failed")
            self.assertEqual(second.status, "provider_accepted")
            self.assertEqual(calls["n"], 2)

    def test_outbox_rcpt_450_via_real_smtp_types_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def send_via_client(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                with patch(
                    "visa_agent.email_adapter.smtp_client.smtplib.SMTP"
                ) as mock_smtp_cls:
                    session = MagicMock()
                    mock_smtp_cls.return_value.__enter__.return_value = session
                    if calls["n"] == 1:
                        session.send_message.side_effect = SMTPRecipientsRefused(
                            {recipients[0]: (450, b"Mailbox busy")}
                        )
                    else:
                        session.send_message.return_value = {}
                    return send_smtp(
                        s, recipients=recipients, subject=subject, body_text=body
                    )

            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"rcpt450-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=f"op-rcpt450-{uuid.uuid4().hex}",
            )
            first = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            self.assertEqual(first.status, "failed")
            self.assertEqual(second.status, "provider_accepted")
            self.assertEqual(calls["n"], 2)

    def test_quit_421_after_accept_keeps_accepted_no_resend(self) -> None:
        """Regression: SMTP accepted mail, then QUIT 421 must not become failed."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def send_via_client(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                with patch(
                    "visa_agent.email_adapter.smtp_client.smtplib.SMTP"
                ) as mock_smtp_cls:
                    session = MagicMock()
                    mock_smtp_cls.return_value.__enter__.return_value = session
                    session.send_message.return_value = {}
                    # SMTP.__exit__ raises when QUIT returns non-221 (e.g. 421).
                    mock_smtp_cls.return_value.__exit__.side_effect = (
                        SMTPResponseException(421, b"Service not available")
                    )
                    return send_smtp(
                        s, recipients=recipients, subject=subject, body_text=body
                    )

            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"quit421-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=f"op-quit421-{uuid.uuid4().hex}",
            )
            first = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            self.assertEqual(first.status, "provider_accepted")
            self.assertIn("quit:421:", first.provider_response or "")
            self.assertEqual(second.status, "provider_accepted")
            self.assertTrue(second.resent_blocked)
            self.assertEqual(calls["n"], 1)

    def test_outbox_disconnect_via_real_smtp_types_blocks_resend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            calls = {"n": 0}

            def send_via_client(s, recipients, subject, body):  # noqa: ANN001
                calls["n"] += 1
                with patch(
                    "visa_agent.email_adapter.smtp_client.smtplib.SMTP"
                ) as mock_smtp_cls:
                    session = MagicMock()
                    mock_smtp_cls.return_value.__enter__.return_value = session
                    session.send_message.side_effect = SMTPServerDisconnected(
                        "Connection unexpectedly closed"
                    )
                    return send_smtp(
                        s, recipients=recipients, subject=subject, body_text=body
                    )

            req = OutboxRequest(
                kind="ordinary_reply",
                recipients=("c-baron-app@hotmail.com",),
                subject=f"disc-{uuid.uuid4().hex}",
                body_text="hello",
                send_operation_id=f"op-disc-{uuid.uuid4().hex}",
            )
            first = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            second = enqueue_and_maybe_send(
                settings=settings, req=req, send_fn=send_via_client
            )
            self.assertEqual(first.status, "status_unknown")
            self.assertEqual(second.status, "status_unknown")
            self.assertTrue(second.resent_blocked)
            self.assertEqual(calls["n"], 1)

    def test_allowlist_revoke_restore_then_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _base_env(root, allowlist="c-baron-app@hotmail.com")
            op = f"op-restore-{uuid.uuid4().hex}"
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append(list(recipients))
                return "x"

            first = enqueue_and_maybe_send(
                settings=get_settings(env),
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                    send_now=False,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(first.status, "pending")

            env.write_text(
                env.read_text(encoding="utf-8").replace(
                    "EMAIL_OUTBOUND_ALLOWLIST=c-baron-app@hotmail.com",
                    "EMAIL_OUTBOUND_ALLOWLIST=",
                ),
                encoding="utf-8",
            )
            blocked = enqueue_and_maybe_send(
                settings=get_settings(env),
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                    send_now=True,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(blocked.status, "blocked")
            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    "SELECT status FROM outbox_messages WHERE id = %s",
                    (first.outbox_id,),
                ).fetchone()
            self.assertEqual(row[0], "blocked")

            env.write_text(
                env.read_text(encoding="utf-8").replace(
                    "EMAIL_OUTBOUND_ALLOWLIST=",
                    "EMAIL_OUTBOUND_ALLOWLIST=c-baron-app@hotmail.com",
                ),
                encoding="utf-8",
            )
            restored = enqueue_and_maybe_send(
                settings=get_settings(env),
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                    send_now=False,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(restored.status, "pending")
            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    "SELECT status, block_reasons FROM outbox_messages WHERE id = %s",
                    (first.outbox_id,),
                ).fetchone()
            self.assertEqual(row[0], "pending")
            self.assertEqual(list(row[1] or []), [])

            sent_ok = enqueue_and_maybe_send(
                settings=get_settings(env),
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                    send_now=True,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(sent_ok.status, "provider_accepted")
            self.assertEqual(len(sent), 1)

    def test_replay_kind_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            op = f"op-kind-{uuid.uuid4().hex}"
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append(body)
                return "fake-id"

            first = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(first.status, "provider_accepted")
            replay = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="final_package",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                ),
                send_fn=fake_send,
            )
            self.assertTrue(replay.resent_blocked)
            self.assertIn("VALIDATION_ERROR", replay.block_reasons)
            self.assertEqual(len(sent), 1)

    def test_pending_retry_after_allowlist_revoke_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _base_env(root, allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            op = f"op-revoke-{uuid.uuid4().hex}"
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append(recipients)
                return "x"

            first = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                    send_now=False,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(first.status, "pending")

            env.write_text(
                env.read_text(encoding="utf-8").replace(
                    "EMAIL_OUTBOUND_ALLOWLIST=c-baron-app@hotmail.com",
                    "EMAIL_OUTBOUND_ALLOWLIST=",
                ),
                encoding="utf-8",
            )
            revoked = get_settings(env)
            second = enqueue_and_maybe_send(
                settings=revoked,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="subj",
                    body_text="body",
                    send_operation_id=op,
                    send_now=True,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(second.status, "blocked")
            self.assertIn("OUTBOUND_NOT_ALLOWLISTED", second.block_reasons)
            self.assertEqual(sent, [])

    def test_same_operation_id_rejects_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), allowlist="c-baron-app@hotmail.com")
            settings = get_settings(env)
            sent: list = []
            op = f"op-freeze-{uuid.uuid4().hex}"

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append(body)
                return "fake-id"

            first = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="fixed-subject",
                    body_text="original-body",
                    send_operation_id=op,
                    send_now=False,
                ),
                send_fn=fake_send,
            )
            self.assertEqual(first.status, "pending")

            mismatched = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject="fixed-subject",
                    body_text="CHANGED-BODY",
                    send_operation_id=op,
                    send_now=True,
                ),
                send_fn=fake_send,
            )
            self.assertTrue(mismatched.resent_blocked)
            self.assertIn("VALIDATION_ERROR", mismatched.block_reasons)
            self.assertEqual(mismatched.outbox_id, first.outbox_id)
            self.assertEqual(mismatched.status, "pending")
            self.assertEqual(sent, [])

            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    "SELECT body_text, status FROM outbox_messages WHERE id = %s",
                    (first.outbox_id,),
                ).fetchone()
            self.assertEqual(row[0], "original-body")
            self.assertEqual(row[1], "pending")

    def test_clearing_allowlist_rejects_previous_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _base_env(root, allowlist="c-baron-app@hotmail.com")
            os.environ["EMAIL_OUTBOUND_ALLOWLIST"] = "c-baron-app@hotmail.com"
            env.write_text(
                env.read_text(encoding="utf-8").replace(
                    "EMAIL_OUTBOUND_ALLOWLIST=c-baron-app@hotmail.com",
                    "EMAIL_OUTBOUND_ALLOWLIST=",
                ),
                encoding="utf-8",
            )
            settings = get_settings(env)
            self.assertEqual(settings.outbound_allowlist, frozenset())
            sent: list = []

            def fake_send(s, recipients, subject, body):  # noqa: ANN001
                sent.append(recipients)
                return "x"

            result = enqueue_and_maybe_send(
                settings=settings,
                req=OutboxRequest(
                    kind="ordinary_reply",
                    recipients=("c-baron-app@hotmail.com",),
                    subject=f"stale-{uuid.uuid4().hex}",
                    body_text="x",
                ),
                send_fn=fake_send,
            )
            self.assertEqual(result.status, "blocked")
            self.assertIn("OUTBOUND_NOT_ALLOWLISTED", result.block_reasons)
            self.assertEqual(sent, [])

    def test_removing_env_file_ignores_process_environ_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _base_env(root, allowlist="c-baron-app@hotmail.com")
            os.environ["EMAIL_OUTBOUND_ALLOWLIST"] = "c-baron-app@hotmail.com"
            env.unlink()
            settings = get_settings(env)
            self.assertEqual(settings.outbound_allowlist, frozenset())
            self.assertNotIn("c-baron-app@hotmail.com", settings.outbound_allowlist)


class OutboxHttpContractTests(unittest.TestCase):
    def test_rejects_case_fields_on_outbox_request(self) -> None:
        from visa_agent.fastapi_app import app, bootstrap

        bootstrap()
        client = TestClient(app)
        resp = client.post(
            "/internal/email/outbox",
            json={
                "kind": "ordinary_reply",
                "recipients": ["c-baron-app@hotmail.com"],
                "subject": "x",
                "body_text": "y",
                "case_id": "should-be-rejected",
            },
        )
        self.assertEqual(resp.status_code, 422)

    def test_rejects_approval_fields_on_outbox_request(self) -> None:
        from visa_agent.fastapi_app import app, bootstrap

        bootstrap()
        client = TestClient(app)
        resp = client.post(
            "/internal/email/outbox",
            json={
                "kind": "ordinary_reply",
                "recipients": ["c-baron-app@hotmail.com"],
                "subject": "x",
                "body_text": "y",
                "human_send_approved": True,
            },
        )
        self.assertEqual(resp.status_code, 422)

    def test_gate_rejection_returns_success_false(self) -> None:
        from visa_agent.fastapi_app import app, bootstrap

        bootstrap()
        client = TestClient(app)
        resp = client.post(
            "/internal/email/outbox",
            json={
                "kind": "ordinary_reply",
                "recipients": ["not-on-allowlist@example.com"],
                "subject": f"http-block-{uuid.uuid4().hex}",
                "body_text": "y",
                "send_now": False,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["data"]["status"], "blocked")
        self.assertIn("OUTBOUND_NOT_ALLOWLISTED", body["data"]["block_reasons"])


class ConfigFileBackedTests(unittest.TestCase):
    def test_env_file_removed_does_not_use_os_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "EMAIL_OUTBOUND_ALLOWLIST=alive@example.com\n",
                encoding="utf-8",
            )
            os.environ["EMAIL_OUTBOUND_ALLOWLIST"] = "stale@example.com"
            self.assertIn("alive@example.com", get_settings(env_path).outbound_allowlist)
            env_path.unlink()
            settings = get_settings(env_path)
            self.assertEqual(settings.outbound_allowlist, frozenset())
            self.assertNotIn("stale@example.com", settings.outbound_allowlist)


if __name__ == "__main__":
    unittest.main()
