from __future__ import annotations

import base64
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from visa_agent.case_service import (
    attribute_inbound_to_case,
    create_case,
    get_case,
    get_review_board,
    record_customer_confirmation,
    record_final_review,
)
from visa_agent.config import get_settings
from visa_agent.document_pipeline import (
    attach_evidence,
    list_evidence,
    record_annotation,
    set_evidence_quarantine,
)
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.inbound import InboundEmail, persist_inbound
from visa_agent.storage.files import LocalFileStore

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _ver(case_id: str) -> int:
    return get_case(database_url=DB, tenant_id=TENANT, case_id=case_id).case_version


class EvidencePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        from visa_agent import fastapi_app

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = replace(get_settings(root / ".env"), file_storage_dir=root)
        self.enterContext(patch.object(fastapi_app, "get_settings", return_value=settings))
        fastapi_app.bootstrap(settings)
        self.client = TestClient(fastapi_app.app)

    def test_attach_isolates_original_without_mutating_bytes(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-iso"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            raw = b"%PDF-synthetic-demo-bytes"
            ev = attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=raw,
                original_filename="passport.pdf",
                content_type="application/pdf",
                classification="passport",
                expected_case_version=1,
            )
            path = store.path_for_file_id(ev.file_id)
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(ev.quarantine_status, "isolated")
            self.assertEqual(ev.case_version, 2)
            self.assertEqual(_ver(case.id), 2)

    def test_rejects_empty_and_executable_extensions(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-rej"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            with self.assertRaises(DomainError) as empty:
                attach_evidence(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    file_store=store,
                    data=b"",
                    original_filename="empty.pdf",
                    expected_case_version=1,
                )
            self.assertEqual(empty.exception.code, ErrorCode.ATTACHMENT_REJECTED)

            bad = attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=b"MZ-fake",
                original_filename="tool.exe",
                expected_case_version=1,
            )
            self.assertEqual(bad.quarantine_status, "rejected")
            self.assertIn(".exe", bad.rejection_reason)

    def test_hard_rejected_cannot_accept_or_annotate(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-hard"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            bad = attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=b"MZ-fake",
                original_filename="tool.exe",
                expected_case_version=1,
            )
            with self.assertRaises(DomainError) as ctx:
                set_evidence_quarantine(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    evidence_id=bad.id,
                    quarantine_status="accepted_for_review",
                    expected_case_version=_ver(case.id),
                )
            self.assertEqual(ctx.exception.code, ErrorCode.ATTACHMENT_REJECTED)
            with self.assertRaises(DomainError) as ann:
                record_annotation(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    evidence_id=bad.id,
                    field_key="x",
                    field_value="y",
                    expected_case_version=_ver(case.id),
                )
            self.assertEqual(ann.exception.code, ErrorCode.ATTACHMENT_REJECTED)

    def test_duplicate_content_rejected_without_extra_files(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-dup"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            payload = b"same-bytes"
            first = attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=payload,
                original_filename="a.txt",
                expected_case_version=1,
            )
            files_after_first = list(store.root.glob("*"))
            with self.assertRaises(DomainError) as ctx:
                attach_evidence(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    file_store=store,
                    data=payload,
                    original_filename="b.txt",
                    expected_case_version=_ver(case.id),
                )
            self.assertEqual(ctx.exception.code, ErrorCode.ATTACHMENT_REJECTED)
            files_after_dup = [p for p in store.root.glob("*") if p.is_file()]
            self.assertEqual(len(files_after_dup), len(files_after_first))
            self.assertEqual(len(list_evidence(
                database_url=DB, tenant_id=TENANT, case_id=case.id
            )), 1)
            self.assertEqual(
                first.file_id, files_after_dup[0].name
            )

    def test_concurrent_identical_uploads_one_row_one_file(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-race"
        )
        payload = b"concurrent-identical-payload"
        errors: list[BaseException] = []
        oks: list[str] = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))

            def _worker() -> None:
                try:
                    ev = attach_evidence(
                        database_url=DB,
                        tenant_id=TENANT,
                        case_id=case.id,
                        file_store=store,
                        data=payload,
                        original_filename="race.txt",
                        expected_case_version=1,
                    )
                    with lock:
                        oks.append(ev.id)
                except DomainError as exc:
                    with lock:
                        errors.append(exc)
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=_worker) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(oks), 1, msg=f"oks={oks} errors={errors}")
            self.assertEqual(len(errors), 11, msg=repr(errors))
            self.assertTrue(
                all(
                    isinstance(e, DomainError)
                    and e.code
                    in {
                        ErrorCode.ATTACHMENT_REJECTED,
                        ErrorCode.CONFLICT_VERSION,
                    }
                    for e in errors
                ),
                msg=repr(errors),
            )
            files = [p for p in store.root.iterdir() if p.is_file()]
            self.assertEqual(len(files), 1)
            self.assertEqual(
                len(list_evidence(database_url=DB, tenant_id=TENANT, case_id=case.id)),
                1,
            )

    def test_material_change_invalidates_approval(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-stale-appr"
        )
        v = _ver(case.id)
        record_customer_confirmation(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=v,
        )
        record_final_review(
            database_url=DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=v,
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=b"new-material",
                original_filename="new.txt",
                expected_case_version=v,
            )
        after = get_case(database_url=DB, tenant_id=TENANT, case_id=case.id)
        self.assertEqual(after.case_version, v + 1)
        from visa_agent.case_service.service import build_transition_context

        ctx = build_transition_context(database_url=DB, case=after)
        self.assertIsNone(ctx.customer_confirmation_case_version)
        self.assertIsNone(ctx.final_review_approval_case_version)

    def test_stale_page_annotation_rejected_no_bump(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-stale-ann"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            ev = attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=b"bank",
                original_filename="bank.txt",
                expected_case_version=1,
            )
            before = _ver(case.id)
            with self.assertRaises(DomainError) as ctx:
                record_annotation(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case.id,
                    evidence_id=ev.id,
                    field_key="funds",
                    field_value="ok",
                    expected_case_version=1,  # stale after attach bump
                )
            self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT_VERSION)
            self.assertEqual(_ver(case.id), before)

    def test_source_email_must_be_attributed_to_same_case(self) -> None:
        case_a = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-src-a"
        )
        case_b = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-src-b"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            inbound = persist_inbound(
                database_url=DB,
                tenant_id=TENANT,
                mail=InboundEmail(
                    from_addr="c@example.com",
                    to_addrs=("agent@example.com",),
                    subject="src",
                    body_text="hi",
                    provider_message_id=f"src-{uuid.uuid4().hex}",
                ),
                file_store=store,
            )
            attribute_inbound_to_case(
                database_url=DB,
                tenant_id=TENANT,
                email_event_id=inbound.email_event_id,
                case_id=case_a.id,
            )
            with self.assertRaises(DomainError) as wrong:
                attach_evidence(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case_b.id,
                    file_store=store,
                    data=b"payload",
                    original_filename="x.txt",
                    source_email_event_id=inbound.email_event_id,
                    expected_case_version=1,
                )
            self.assertEqual(wrong.exception.code, ErrorCode.EMAIL_UNATTRIBUTED)

            with self.assertRaises(DomainError) as missing:
                attach_evidence(
                    database_url=DB,
                    tenant_id=TENANT,
                    case_id=case_a.id,
                    file_store=store,
                    data=b"payload2",
                    original_filename="y.txt",
                    source_email_event_id="does-not-exist",
                    expected_case_version=1,
                )
            self.assertEqual(missing.exception.code, ErrorCode.NOT_FOUND)

            ok = attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case_a.id,
                file_store=store,
                data=b"payload3",
                original_filename="z.txt",
                source_email_event_id=inbound.email_event_id,
                expected_case_version=1,
            )
            self.assertEqual(ok.source_email_event_id, inbound.email_event_id)

            with self.assertRaises(DomainError) as blocked:
                attribute_inbound_to_case(
                    database_url=DB,
                    tenant_id=TENANT,
                    email_event_id=inbound.email_event_id,
                    case_id=case_b.id,
                    force_reattribute=True,
                    reason="move to B",
                )
            self.assertEqual(blocked.exception.code, ErrorCode.VALIDATION_ERROR)
            self.assertIn("linked evidence", blocked.exception.message)

    def test_insert_failure_cleans_orphan_file_keeps_shared(self) -> None:
        import contextlib
        import hashlib
        from unittest.mock import patch

        import psycopg

        import visa_agent.document_pipeline.service as ev_svc

        case_a = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-orphan-a"
        )
        case_b = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-orphan-b"
        )
        shared = b"shared-content-bytes"
        unique_fail = b"unique-fail-content-bytes"
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            attach_evidence(
                database_url=DB,
                tenant_id=TENANT,
                case_id=case_a.id,
                file_store=store,
                data=shared,
                original_filename="shared.txt",
                expected_case_version=1,
            )
            shared_digest = hashlib.sha256(shared).hexdigest()
            shared_path = store.content_path(shared_digest, suffix=".txt")
            self.assertTrue(shared_path.is_file())

            fail_digest = hashlib.sha256(unique_fail).hexdigest()
            fail_path = store.content_path(fail_digest, suffix=".txt")

            real_connect = psycopg.connect

            @contextlib.contextmanager
            def boom_connect(dsn, **kwargs):  # noqa: ANN001
                with real_connect(dsn, **kwargs) as conn:
                    real_exec = conn.execute

                    def exec_wrap(query, params=None, **kw):  # noqa: ANN001
                        text = query if isinstance(query, str) else str(query)
                        if "INSERT INTO case_evidence" in text:
                            raise RuntimeError("simulated insert failure")
                        return real_exec(query, params, **kw)

                    conn.execute = exec_wrap  # type: ignore[method-assign]
                    yield conn

            with patch.object(ev_svc.psycopg, "connect", boom_connect):
                with self.assertRaises(RuntimeError):
                    attach_evidence(
                        database_url=DB,
                        tenant_id=TENANT,
                        case_id=case_b.id,
                        file_store=store,
                        data=unique_fail,
                        original_filename="fail.txt",
                        expected_case_version=1,
                    )

            self.assertFalse(
                fail_path.exists(),
                msg="orphan file from failed insert must be removed",
            )
            self.assertTrue(
                shared_path.is_file(),
                msg="files still referenced by other cases must remain",
            )
            self.assertEqual(_ver(case_b.id), 1)

            # Explicit cleanup keeps referenced shared path id.
            shared_file_id = LocalFileStore.file_id_for(
                shared_digest, suffix=".txt"
            )
            ev_svc._cleanup_orphan_evidence_file(
                database_url=DB,
                file_store=store,
                file_id=shared_file_id,
                path=shared_path,
            )
            self.assertTrue(shared_path.is_file())

    def test_concurrent_attach_and_reattribute_fixed_order(self) -> None:
        """Attach holds email FOR UPDATE until commit; reattribute cannot sneak in."""
        import visa_agent.document_pipeline.service as ev_svc

        case_a = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-race-attr-a"
        )
        case_b = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-race-attr-b"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            inbound = persist_inbound(
                database_url=DB,
                tenant_id=TENANT,
                mail=InboundEmail(
                    from_addr="c@example.com",
                    to_addrs=("agent@example.com",),
                    subject="race",
                    body_text="body",
                    provider_message_id=f"race-{uuid.uuid4().hex}",
                ),
                file_store=store,
            )
            attribute_inbound_to_case(
                database_url=DB,
                tenant_id=TENANT,
                email_event_id=inbound.email_event_id,
                case_id=case_a.id,
            )

            email_locked = threading.Event()
            attach_may_finish = threading.Event()
            attach_done = threading.Event()
            reattr_result: list[BaseException | str] = []

            def after_email_lock() -> None:
                email_locked.set()
                self.assertTrue(
                    attach_may_finish.wait(timeout=5),
                    msg="attach stuck before release",
                )

            def attach_worker() -> None:
                try:
                    attach_evidence(
                        database_url=DB,
                        tenant_id=TENANT,
                        case_id=case_a.id,
                        file_store=store,
                        data=b"race-attach-payload",
                        original_filename="from-mail.txt",
                        source_email_event_id=inbound.email_event_id,
                        expected_case_version=1,
                    )
                    reattr_result.append("attach-ok")
                except BaseException as exc:  # noqa: BLE001
                    reattr_result.append(exc)
                finally:
                    attach_done.set()

            def reattr_worker() -> None:
                try:
                    attribute_inbound_to_case(
                        database_url=DB,
                        tenant_id=TENANT,
                        email_event_id=inbound.email_event_id,
                        case_id=case_b.id,
                        force_reattribute=True,
                        reason="concurrent move",
                    )
                    reattr_result.append("reattr-ok")
                except BaseException as exc:  # noqa: BLE001
                    reattr_result.append(exc)

            ev_svc._TEST_BARRIERS["after_email_lock"] = after_email_lock
            try:
                t_attach = threading.Thread(target=attach_worker)
                t_attach.start()
                self.assertTrue(email_locked.wait(timeout=5))

                t_reattr = threading.Thread(target=reattr_worker)
                t_reattr.start()
                # Reattribute must block on email row lock while attach holds it.
                t_reattr.join(timeout=0.3)
                self.assertTrue(
                    t_reattr.is_alive(),
                    msg="reattribute should still be waiting on email FOR UPDATE",
                )

                attach_may_finish.set()
                self.assertTrue(attach_done.wait(timeout=5))
                t_reattr.join(timeout=5)
                self.assertFalse(t_reattr.is_alive())
            finally:
                ev_svc._TEST_BARRIERS.pop("after_email_lock", None)

            self.assertIn("attach-ok", reattr_result)
            errors = [e for e in reattr_result if isinstance(e, DomainError)]
            self.assertTrue(
                any(e.code == ErrorCode.VALIDATION_ERROR for e in errors),
                msg=repr(reattr_result),
            )
            # Evidence remains on A; email still attributed to A.
            items = list_evidence(
                database_url=DB, tenant_id=TENANT, case_id=case_a.id
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(
                items[0].source_email_event_id, inbound.email_event_id
            )
            import psycopg

            with psycopg.connect(DB) as conn:
                row = conn.execute(
                    "SELECT case_id FROM email_events WHERE id = %s",
                    (inbound.email_event_id,),
                ).fetchone()
            self.assertEqual(row[0], case_a.id)

    def test_concurrent_fail_cleanup_keeps_successful_peer_file(self) -> None:
        """Failing attach must not delete a peer's committed path under the same lock."""
        import hashlib

        import visa_agent.document_pipeline.service as ev_svc

        case_fail = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-clean-fail"
        )
        case_ok = create_case(
            database_url=DB, tenant_id=TENANT, title="ev-clean-ok"
        )
        payload = b"shared-concurrent-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        file_id = LocalFileStore.file_id_for(digest, suffix=".txt")

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            path = store.path_for_file_id(file_id)

            wrote = threading.Event()
            fail_may_continue = threading.Event()
            cleanup_may_lock = threading.Event()
            ok_done = threading.Event()
            fail_exc: list[BaseException] = []
            ok_ids: list[str] = []

            def after_file_write() -> None:
                wrote.set()
                self.assertTrue(fail_may_continue.wait(timeout=5))

            def cleanup_before_lock() -> None:
                # Let the successful peer commit before orphan cleanup runs.
                self.assertTrue(
                    ok_done.wait(timeout=5),
                    msg="ok attach did not finish before cleanup",
                )
                cleanup_may_lock.set()

            def fail_worker() -> None:
                import contextlib
                from unittest.mock import patch

                import psycopg

                real_connect = psycopg.connect
                fail_ident = threading.get_ident()

                @contextlib.contextmanager
                def boom_connect(dsn, **kwargs):  # noqa: ANN001
                    with real_connect(dsn, **kwargs) as conn:
                        real_exec = conn.execute

                        def exec_wrap(query, params=None, **kw):  # noqa: ANN001
                            text = (
                                query if isinstance(query, str) else str(query)
                            )
                            # Only the failing worker's attach insert blows up;
                            # peer attaches and cleanup must keep working.
                            if (
                                "INSERT INTO case_evidence" in text
                                and threading.get_ident() == fail_ident
                                and params
                                and case_fail.id in params
                            ):
                                raise RuntimeError("simulated insert failure")
                            return real_exec(query, params, **kw)

                        conn.execute = exec_wrap  # type: ignore[method-assign]
                        yield conn

                try:
                    with patch.object(ev_svc.psycopg, "connect", boom_connect):
                        attach_evidence(
                            database_url=DB,
                            tenant_id=TENANT,
                            case_id=case_fail.id,
                            file_store=store,
                            data=payload,
                            original_filename="same.txt",
                            expected_case_version=1,
                        )
                except BaseException as exc:  # noqa: BLE001
                    fail_exc.append(exc)

            def ok_worker() -> None:
                try:
                    # Wait until fail side has the file lock + wrote bytes.
                    self.assertTrue(wrote.wait(timeout=5))
                    # Fail side still in txn holding advisory lock — we block
                    # until it aborts, then we should commit successfully.
                    # Release fail to proceed to insert failure.
                    fail_may_continue.set()
                    ev = attach_evidence(
                        database_url=DB,
                        tenant_id=TENANT,
                        case_id=case_ok.id,
                        file_store=store,
                        data=payload,
                        original_filename="same.txt",
                        expected_case_version=1,
                    )
                    ok_ids.append(ev.id)
                finally:
                    ok_done.set()

            ev_svc._TEST_BARRIERS["after_file_write"] = after_file_write
            ev_svc._TEST_BARRIERS["cleanup_before_lock"] = cleanup_before_lock
            try:
                t_fail = threading.Thread(target=fail_worker)
                t_ok = threading.Thread(target=ok_worker)
                t_fail.start()
                t_ok.start()
                t_fail.join(timeout=10)
                t_ok.join(timeout=10)
            finally:
                ev_svc._TEST_BARRIERS.pop("after_file_write", None)
                ev_svc._TEST_BARRIERS.pop("cleanup_before_lock", None)
                fail_may_continue.set()
                ok_done.set()

            self.assertTrue(fail_exc, msg="fail worker should error")
            self.assertEqual(len(ok_ids), 1, msg=f"ok_ids={ok_ids}")
            self.assertTrue(
                path.is_file(),
                msg="successful peer file must survive failed cleanup",
            )
            self.assertEqual(
                len(
                    list_evidence(
                        database_url=DB, tenant_id=TENANT, case_id=case_ok.id
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    list_evidence(
                        database_url=DB, tenant_id=TENANT, case_id=case_fail.id
                    )
                ),
                0,
            )
    def test_api_attach_and_annotate(self) -> None:
        created = self.client.post(
            "/cases", json={"title": "api-ev", "customer_email": "x@y.com"}
        )
        case_id = created.json()["data"]["case_id"]
        payload = base64.b64encode(b"hello-evidence").decode()
        resp = self.client.post(
            f"/cases/{case_id}/evidence",
            json={
                "filename": "note.txt",
                "content_base64": payload,
                "content_type": "text/plain",
                "expected_case_version": 1,
            },
        )
        self.assertEqual(resp.status_code, 200)
        evidence_id = resp.json()["data"]["evidence_id"]
        self.assertEqual(resp.json()["data"]["quarantine_status"], "isolated")
        self.assertEqual(resp.json()["data"]["case_version"], 2)

        ann = self.client.post(
            f"/cases/{case_id}/evidence/{evidence_id}/annotations",
            json={
                "field_key": "applicant_name_alias",
                "field_value": "SYN-DEMO",
                "expected_case_version": 2,
            },
        )
        self.assertEqual(ann.status_code, 200)
        self.assertEqual(ann.json()["data"]["case_version"], 3)

        stale = self.client.post(
            f"/cases/{case_id}/evidence/{evidence_id}/annotations",
            json={
                "field_key": "x",
                "field_value": "y",
                "expected_case_version": 2,
            },
        )
        self.assertEqual(stale.status_code, 409)

        page = self.client.get(f"/workbench/cases/{case_id}")
        self.assertIn("材料清单", page.text)
        self.assertIn("note.txt", page.text)
        self.assertIn("查看", page.text)
        self.assertIn("下载", page.text)

    def test_workbench_multipart_upload_and_view(self) -> None:
        created = self.client.post(
            "/cases", json={"title": "wb-upload", "customer_email": "u@y.com"}
        )
        case_id = created.json()["data"]["case_id"]
        case = self.client.get(f"/cases/{case_id}").json()["data"]
        version = case["case_version"]
        payload = b"real local passport bytes for demo"
        resp = self.client.post(
            f"/workbench/cases/{case_id}/evidence",
            data={
                "expected_case_version": str(version),
                "classification": "passport",
            },
            files={
                "file": ("passport.pdf", payload, "application/pdf"),
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        items = self.client.get(f"/cases/{case_id}/evidence").json()["data"]
        self.assertTrue(items)
        evidence_id = items[0]["evidence_id"]
        detail_url = f"/workbench/cases/{case_id}"
        file_url = f"{detail_url}/evidence/{evidence_id}/file"
        self.assertNotIn(">已查看</span>", self.client.get(detail_url).text)
        self.assertEqual(self.client.get(f"{file_url}?download=1").content, payload)
        self.assertNotIn(">已查看</span>", self.client.get(detail_url).text)
        version = _ver(case_id)
        view = self.client.get(
            f"{detail_url}/evidence/{evidence_id}/view"
        )
        self.assertEqual(view.status_code, 200)
        self.assertIn("passport.pdf", view.text)
        self.assertIn(">已查看</span>", self.client.get(detail_url).text)
        self.assertEqual(_ver(case_id), version)
        [evidence] = list_evidence(database_url=DB, tenant_id=TENANT, case_id=case_id)
        self.assertIsNotNone(evidence.viewed_at)
        self.assertEqual(evidence.quarantine_status, "isolated")
        raw = self.client.get(file_url)
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.content, payload)

    def test_workbench_delete_removes_material_and_invalidates_confirmation(self) -> None:
        created = self.client.post("/cases", json={"title": "wb-delete"})
        case_id = created.json()["data"]["case_id"]
        path = f"/workbench/cases/{case_id}/evidence"
        upload = dict(data={"classification": "passport", "expected_case_version": "1"},
                      files={"file": ("delete-test.pdf", b"test document", "application/pdf")})
        self.assertEqual(self.client.post(path, **upload, follow_redirects=False).status_code, 303)
        items = self.client.get(f"/cases/{case_id}/evidence").json()["data"]
        evidence_id = items[0]["evidence_id"]
        old_version = _ver(case_id)
        record_customer_confirmation(database_url=DB, tenant_id=TENANT, case_id=case_id,
                                     expected_case_version=old_version)

        stale = self.client.post(f"{path}/{evidence_id}/delete",
                                 data={"expected_case_version": str(old_version - 1)})
        self.assertIn("CONFLICT_VERSION", stale.text)
        self.assertEqual(_ver(case_id), old_version)
        self.assertEqual(self.client.get(f"{path}/{evidence_id}/file").status_code, 200)

        result = self.client.post(f"{path}/{evidence_id}/delete",
                                  data={"expected_case_version": str(old_version)})
        self.assertIn("材料已从案件移除", result.text)
        self.assertNotIn("delete-test.pdf", result.text)
        self.assertEqual(_ver(case_id), old_version + 1)
        self.assertEqual(self.client.get(f"{path}/{evidence_id}/file").status_code, 404)
        self.assertEqual(self.client.get(f"/cases/{case_id}/evidence").json()["data"], [])
        self.assertFalse(get_review_board(database_url=DB, tenant_id=TENANT,
                                          case_id=case_id).customer_confirmation_effective)
        upload["data"]["expected_case_version"] = str(_ver(case_id))
        self.assertEqual(self.client.post(path, **upload, follow_redirects=False).status_code, 303)
        new_id = self.client.get(f"/cases/{case_id}/evidence").json()["data"][0]["evidence_id"]
        self.assertNotEqual(new_id, evidence_id)
