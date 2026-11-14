from __future__ import annotations

import os

os.environ["VISA_AGENT_SKIP_APP_BOOT"] = "1"

import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg
import uuid
from fastapi.testclient import TestClient

from tests.db_isolation import TEST_DB, TEST_PUBLISH_TOKEN, reset_test_database
from visa_agent.case_service import (
    create_case,
    get_case_review_page,
    get_review_board,
    record_customer_confirmation,
    record_final_review,
)
from visa_agent.case_service.service import bump_case_version
from visa_agent.config import Settings
from visa_agent.storage.migrate import apply_migrations

TENANT = "demo"


def _settings() -> Settings:
    return Settings(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=TEST_DB,
        file_storage_dir=Path("data/files"),
        tenant_id=TENANT,
        siliconflow_api_key="",
        siliconflow_base_url="https://api.siliconflow.cn/v1",
        siliconflow_model="zai-org/GLM-5.3-Flash",
        email_smtp_host="",
        email_smtp_port=587,
        email_smtp_user="",
        email_smtp_password="",
        email_from="",
        email_imap_host="",
        email_imap_port=993,
        email_imap_user="",
        email_imap_password="",
        outbound_allowlist=frozenset(),
        rule_publish_token=TEST_PUBLISH_TOKEN,
    )


class ReviewBoardUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        apply_migrations(TEST_DB)

    def test_stale_approval_visible_after_version_bump(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="rb-stale"
        )
        record_customer_confirmation(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=1,
        )
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=1,
        )
        before = get_review_board(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertTrue(before.customer_confirmation_effective)
        self.assertTrue(before.final_approval_effective)
        self.assertEqual(before.current_final_decision, "approve")

        bump_case_version(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        after = get_review_board(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(after.case_version, 2)
        self.assertFalse(after.customer_confirmation_effective)
        self.assertFalse(after.final_approval_effective)
        self.assertIsNone(after.current_final_decision)
        self.assertFalse(after.confirmations[0].effective)
        self.assertFalse(after.reviews[0].effective)

    def test_snapshot_consistent_under_concurrent_bump(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="rb-race"
        )
        record_customer_confirmation(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=1,
        )
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            note="v1-ok",
            expected_case_version=1,
        )
        barriers = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                barriers.wait(timeout=5)
                snap_case, board = get_case_review_page(
                    database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
                )
                results.append((snap_case, board))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def bumper() -> None:
            try:
                barriers.wait(timeout=5)
                bump_case_version(
                    database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=bumper),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        snap_case, board = results[0]
        self.assertEqual(snap_case.case_version, board.case_version)
        for c in board.confirmations:
            self.assertEqual(
                c.effective, c.case_version == board.case_version
            )
        for r in board.reviews:
            if r.effective:
                self.assertEqual(r.case_version, board.case_version)
                self.assertFalse(r.superseded)
            elif r.superseded:
                self.assertEqual(r.case_version, board.case_version)
            else:
                self.assertNotEqual(r.case_version, board.case_version)
        if board.final_approval_effective:
            self.assertEqual(board.current_final_decision, "approve")
            self.assertTrue(
                any(r.effective and r.decision == "approve" for r in board.reviews)
            )
        elif board.current_final_decision == "reject":
            self.assertFalse(board.final_approval_effective)
        else:
            self.assertFalse(board.final_approval_effective)
            self.assertTrue(
                all(not r.effective for r in board.reviews)
                or board.case_version > 1
            )

    def test_same_version_decisions_append_gate_uses_latest(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="rb-hist"
        )
        bump_case_version(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )  # v2
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="reject",
            note="first-reject",
            expected_case_version=2,
        )
        mid = get_review_board(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(mid.current_final_decision, "reject")
        self.assertFalse(mid.final_approval_effective)
        self.assertEqual(len(mid.reviews), 1)

        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            note="later-approve",
            expected_case_version=2,
        )
        after = get_review_board(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(len(after.reviews), 2)
        notes = {r.note for r in after.reviews}
        self.assertEqual(notes, {"first-reject", "later-approve"})
        self.assertEqual(after.current_final_decision, "approve")
        self.assertTrue(after.final_approval_effective)
        latest = next(r for r in after.reviews if r.effective)
        self.assertEqual(latest.note, "later-approve")
        superseded = next(r for r in after.reviews if r.superseded)
        self.assertEqual(superseded.note, "first-reject")
        self.assertGreater(latest.decision_seq, superseded.decision_seq)

    def test_decision_seq_beats_created_at_txn_order(self) -> None:
        """Later lock-allocated seq wins even if created_at is earlier (txn NOW())."""
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="rb-seq"
        )
        with psycopg.connect(TEST_DB) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE cases SET final_review_seq = 2
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (TENANT, case.id),
                )
                # Approve: higher wall-clock created_at, lower decision_seq.
                conn.execute(
                    """
                    INSERT INTO case_final_reviews (
                        id, tenant_id, case_id, case_version, decision, note,
                        decision_seq, created_at
                    ) VALUES (
                        %s, %s, %s, 1, 'approve', 'approve-late-clock',
                        1, NOW() + INTERVAL '1 hour'
                    )
                    """,
                    (uuid.uuid4().hex, TENANT, case.id),
                )
                # Reject: earlier created_at, higher decision_seq (written under lock later).
                conn.execute(
                    """
                    INSERT INTO case_final_reviews (
                        id, tenant_id, case_id, case_version, decision, note,
                        decision_seq, created_at
                    ) VALUES (
                        %s, %s, %s, 1, 'reject', 'reject-early-clock',
                        2, NOW() - INTERVAL '1 hour'
                    )
                    """,
                    (uuid.uuid4().hex, TENANT, case.id),
                )
        board = get_review_board(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(board.current_final_decision, "reject")
        self.assertFalse(board.final_approval_effective)
        effective = next(r for r in board.reviews if r.effective)
        self.assertEqual(effective.note, "reject-early-clock")
        self.assertEqual(effective.decision_seq, 2)

    def test_repeatable_read_snapshot_excludes_later_version_rows(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="rb-rr"
        )
        record_customer_confirmation(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=1,
        )
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=1,
        )

        with psycopg.connect(TEST_DB) as reader:
            reader.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with reader.transaction():
                snap = reader.execute(
                    """
                    SELECT case_version FROM cases
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (TENANT, case.id),
                ).fetchone()
                self.assertEqual(snap[0], 1)

                # Concurrent writer advances version and adds v2 history.
                bump_case_version(
                    database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
                )
                record_customer_confirmation(
                    database_url=TEST_DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    note="v2-conf",
                    expected_case_version=2,
                )
                record_final_review(
                    database_url=TEST_DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    decision="reject",
                    note="v2-rej",
                    expected_case_version=2,
                )

                confs = reader.execute(
                    """
                    SELECT case_version FROM case_customer_confirmations
                    WHERE tenant_id = %s AND case_id = %s
                    ORDER BY case_version
                    """,
                    (TENANT, case.id),
                ).fetchall()
                revs = reader.execute(
                    """
                    SELECT case_version, decision FROM case_final_reviews
                    WHERE tenant_id = %s AND case_id = %s
                    ORDER BY decision_seq
                    """,
                    (TENANT, case.id),
                ).fetchall()
                # Snapshot still v1 — must not observe v2 rows.
                self.assertEqual(snap[0], 1)
                self.assertEqual({r[0] for r in confs}, {1})
                self.assertEqual({r[0] for r in revs}, {1})

        # Public API uses the same isolation: consistent page after bump.
        snap_case, board = get_case_review_page(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(snap_case.case_version, board.case_version)
        for c in board.confirmations:
            self.assertLessEqual(c.case_version, board.case_version)
        for r in board.reviews:
            self.assertLessEqual(r.case_version, board.case_version)


class ReviewBoardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        apply_migrations(TEST_DB)
        self.settings = _settings()
        from visa_agent import fastapi_app as fa

        fa.bootstrap(self.settings)
        self.fa = fa
        self._patcher = patch.object(fa, "get_settings", return_value=self.settings)
        self._patcher.start()
        self.client = TestClient(fa.app)

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_api_and_workbench_mark_stale_and_reject_summary(self) -> None:
        created = self.client.post("/cases", json={"title": "rb-api"})
        case_id = created.json()["data"]["case_id"]
        self.client.post(
            f"/cases/{case_id}/confirm",
            json={"expected_case_version": 1},
        )
        self.client.post(
            f"/cases/{case_id}/reviews",
            json={
                "decision": "approve",
                "expected_case_version": 1,
                "note": "v1",
            },
        )
        board = self.client.get(f"/cases/{case_id}/review-board")
        self.assertEqual(board.status_code, 200)
        data = board.json()["data"]
        self.assertTrue(data["final_approval_effective"])
        self.assertEqual(data["current_final_decision"], "approve")

        self.client.post(f"/workbench/cases/{case_id}/bump-version")
        board2 = self.client.get(f"/cases/{case_id}/review-board")
        data2 = board2.json()["data"]
        self.assertEqual(data2["case_version"], 2)
        self.assertFalse(data2["final_approval_effective"])
        self.assertTrue(data2["reviews"][0]["stale"])

        # Current-version reject must show 已驳回 in workbench gate summary.
        self.client.post(
            f"/cases/{case_id}/reviews",
            json={
                "decision": "reject",
                "expected_case_version": 2,
                "note": "nope",
            },
        )
        page = self.client.get(f"/workbench/cases/{case_id}")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("已驳回", html)
        self.assertIn("当前版本 · 驳回", html)
        self.assertIn("v2", html)

        # Re-approve keeps reject history.
        self.client.post(
            f"/cases/{case_id}/reviews",
            json={
                "decision": "approve",
                "expected_case_version": 2,
                "note": "ok-now",
            },
        )
        board3 = self.client.get(f"/cases/{case_id}/review-board")
        data3 = board3.json()["data"]
        self.assertEqual(len(data3["reviews"]), 3)
        notes = {r["note"] for r in data3["reviews"]}
        self.assertIn("nope", notes)
        self.assertIn("ok-now", notes)
        self.assertEqual(data3["current_final_decision"], "approve")
        self.assertTrue(data3["final_approval_effective"])

        root = self.client.get("/")
        self.assertEqual(root.json()["data"]["stage"], "S2.3")


if __name__ == "__main__":
    unittest.main()
