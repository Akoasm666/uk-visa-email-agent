from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from visa_agent.config import get_settings
from visa_agent.email_adapter.imap_poll import (
    _case_thread_uids,
    poll_imap_inbox,
    stable_imap_provider_id,
)
from visa_agent.email_adapter.inbound import InboundEmail, persist_inbound
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"


def _raw(*, subject: str, body: str, message_id: str | None) -> bytes:
    lines = [
        b"From: customer@example.com",
        b"To: agent@example.com",
        f"Subject: {subject}".encode(),
    ]
    if message_id:
        lines.append(f"Message-ID: {message_id}".encode())
    lines.extend(
        [
            b"Content-Type: text/plain; charset=utf-8",
            b"",
            body.encode(),
        ]
    )
    return b"\r\n".join(lines)


class FakeImapUidClient:
    """UID SEARCH/FETCH/STORE double; sequence numbers intentionally reused."""

    def __init__(self, mails: dict[str, bytes]) -> None:
        self.mails = dict(mails)
        self.unseen = list(mails.keys())
        self.seen: list[str] = []
        self.fetch_parts: list[str] = []

    def uid(self, command: str, *args):  # noqa: ANN001
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [" ".join(self.unseen).encode()])
        if cmd == "FETCH":
            uid = args[0]
            uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            parts = args[1]
            part_s = (
                parts.decode() if isinstance(parts, (bytes, bytearray)) else str(parts)
            )
            self.fetch_parts.append(part_s)
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


class CaseThreadFilterTests(unittest.TestCase):
    def test_only_expected_sender_and_thread_are_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(get_settings(Path(tmp) / ".env"),
                               outbound_allowlist=frozenset({"customer@example.com"}))
        known = b"In-Reply-To: <opening@example.com>\r\n"
        client = FakeImapUidClient({
            "1": b"From: Customer <customer@example.com>\r\n" + known + b"\r\n",
            "2": b"From: customer@example.com\r\nReferences: <personal@example.com>\r\n\r\n",
            "3": b"From: customer@example.com.other.test\r\n" + known + b"\r\n",
        })
        with patch("visa_agent.email_adapter.imap_poll.psycopg.connect") as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchall.return_value = [
                ("customer@example.com", "<opening@example.com>"),
            ]
            self.assertEqual(_case_thread_uids(client, settings, 10), [b"1"])
        self.assertTrue(all("HEADER.FIELDS" in parts for parts in client.fetch_parts))
        self.assertEqual(client.seen, [])


class ImapPollTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def _settings(self, tmp: Path):
        env = tmp / ".env"
        env.write_text(
            "\n".join(
                [
                    f"DATABASE_URL={DB}",
                    f"FILE_STORAGE_DIR={tmp / 'files'}",
                    "TENANT_ID=demo",
                    "EMAIL_IMAP_HOST=imap.example.com",
                    "EMAIL_IMAP_PORT=993",
                    "EMAIL_IMAP_USER=agent@example.com",
                    "EMAIL_IMAP_PASSWORD=pass",
                    "EMAIL_FROM=agent@example.com",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return get_settings(env)

    def test_stable_provider_id_uses_account_mailbox_uidvalidity_uid(self) -> None:
        pid = stable_imap_provider_id(
            imap_user="Agent@Example.com",
            mailbox="INBOX",
            uidvalidity="99",
            uid="42",
        )
        self.assertEqual(pid, "imap:agent@example.com:INBOX:99:42")

    def test_two_mails_without_message_id_are_not_deduped_by_reuseable_seq(self) -> None:
        """Regression: sequence '1' reused must not collide across UIDs."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            store = LocalFileStore(Path(tmp) / "files")
            store.root.mkdir(parents=True, exist_ok=True)

            # First poll: UID 101, no Message-ID
            client1 = FakeImapUidClient(
                {
                    "101": _raw(
                        subject="first", body="aaa", message_id=None
                    )
                }
            )
            r1 = poll_imap_inbox(
                settings=settings,
                file_store=store,
                client=client1,
                uidvalidity="777",
            )
            self.assertTrue(r1.success)
            self.assertEqual(r1.count, 1)
            self.assertEqual(
                r1.messages[0].provider_message_id,
                "imap:agent@example.com:INBOX:777:101",
            )

            # Second poll: different UID 102 (sequence slot may look like '1' elsewhere)
            client2 = FakeImapUidClient(
                {
                    "102": _raw(
                        subject="second", body="bbb", message_id=None
                    )
                }
            )
            r2 = poll_imap_inbox(
                settings=settings,
                file_store=store,
                client=client2,
                uidvalidity="777",
            )
            self.assertTrue(r2.success)
            self.assertEqual(r2.count, 1)
            self.assertFalse(r2.messages[0].duplicate)
            self.assertEqual(
                r2.messages[0].provider_message_id,
                "imap:agent@example.com:INBOX:777:102",
            )
            self.assertNotEqual(
                r1.messages[0].email_event_id, r2.messages[0].email_event_id
            )

    def test_persist_failure_reports_failure_keeps_unseen_then_retries(self) -> None:
        mailbox = FakeImapUidClient(
            {
                "55": _raw(
                    subject="retry-me",
                    body="hello",
                    message_id="<retry-imap-1@example.com>",
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            store = LocalFileStore(Path(tmp) / "files")
            store.root.mkdir(parents=True, exist_ok=True)

            with patch(
                "visa_agent.email_adapter.imap_poll.persist_inbound",
                side_effect=RuntimeError("db down"),
            ):
                first = poll_imap_inbox(
                    settings=settings,
                    file_store=store,
                    client=mailbox,
                    uidvalidity="1",
                )
            self.assertFalse(first.success)
            self.assertEqual(first.count, 0)
            self.assertEqual(first.failed_count, 1)
            self.assertIn("db down", first.failures[0].error)
            self.assertEqual(mailbox.seen, [])
            self.assertTrue(
                any("PEEK" in part for part in mailbox.fetch_parts),
                msg="fetch must use BODY.PEEK to avoid auto-Seen",
            )

            second = poll_imap_inbox(
                settings=settings,
                file_store=store,
                client=mailbox,
                uidvalidity="1",
            )
            self.assertTrue(second.success)
            self.assertEqual(second.count, 1)
            self.assertEqual(second.failed_count, 0)
            self.assertEqual(mailbox.seen, ["55"])

    def test_partial_failure_when_one_of_two_persists(self) -> None:
        client = FakeImapUidClient(
            {
                "1": _raw(subject="ok", body="a", message_id="<a@x>"),
                "2": _raw(subject="bad", body="b", message_id="<b@x>"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            store = LocalFileStore(Path(tmp) / "files")
            store.root.mkdir(parents=True, exist_ok=True)

            def _persist(**kwargs):  # noqa: ANN003
                mail: InboundEmail = kwargs["mail"]
                if mail.subject == "bad":
                    raise RuntimeError("boom")
                return persist_inbound(**kwargs)

            with patch(
                "visa_agent.email_adapter.imap_poll.persist_inbound",
                side_effect=_persist,
            ):
                result = poll_imap_inbox(
                    settings=settings,
                    file_store=store,
                    client=client,
                    uidvalidity="9",
                )
            self.assertFalse(result.success)
            self.assertTrue(result.partial)
            self.assertEqual(result.count, 1)
            self.assertEqual(result.failed_count, 1)
            self.assertEqual(client.seen, ["1"])
            self.assertIn("2", client.unseen)
