"""OCR derived artifacts — never mutate quarantine originals."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["VISA_AGENT_SKIP_APP_BOOT"] = "1"

from fastapi.testclient import TestClient

from tests.db_isolation import TEST_DB, TEST_PUBLISH_TOKEN, reset_test_database
from visa_agent.case_service import create_case, get_case
from visa_agent.config import Settings
from visa_agent.document_pipeline import (
    SyntheticOcrProvider,
    attach_evidence,
    list_annotations,
    list_ocr_results,
    run_ocr_on_evidence,
    set_evidence_quarantine,
)
from visa_agent.document_pipeline.ocr import OcrExtraction
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.storage.files import LocalFileStore

TENANT = "demo"


def _settings(**overrides: object) -> Settings:
    base = dict(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=TEST_DB,
        file_storage_dir=Path("data/files"),
        tenant_id=TENANT,
        siliconflow_api_key="test-key",
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
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _MutatingProvider:
    """Hostile provider that tries to rewrite the buffer it receives."""

    name = "mutating_test_provider"

    def extract(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> OcrExtraction:
        # Attempt in-place mutation if a bytearray sneaks through.
        if isinstance(data, bytearray):
            data[:] = b"MUTATED" + data[7:]
        return OcrExtraction(
            text="hostile",
            status="completed",
            provider=self.name,
            detail="mutated_buffer_attempt",
        )


class OcrIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalFileStore(Path(self.tmp.name))
        self.settings = _settings(file_storage_dir=Path(self.tmp.name))
        from visa_agent import fastapi_app as fa

        fa.bootstrap(self.settings)
        self._patcher = patch.object(fa, "get_settings", return_value=self.settings)
        self._patcher.start()
        self.client = TestClient(fa.app)

    def tearDown(self) -> None:
        self._patcher.stop()
        self.tmp.cleanup()

    def test_synthetic_provider_extracts_text_without_touching_file(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="ocr-iso")
        raw = b"PASSPORT NO: X1234567\nName: Demo Applicant\n"
        ev = attach_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            file_store=self.store,
            data=raw,
            original_filename="passport.txt",
            content_type="text/plain",
            classification="passport",
            expected_case_version=1,
        )
        path = self.store.path_for_file_id(ev.file_id)
        before = path.read_bytes()
        self.assertEqual(before, raw)

        ocr = run_ocr_on_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            evidence_id=ev.id,
            file_store=self.store,
            expected_case_version=ev.case_version,
            provider=SyntheticOcrProvider(),
        )
        self.assertEqual(ocr.status, "completed")
        self.assertIn("X1234567", ocr.extracted_text)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(ocr.source_content_sha256, ev.content_sha256)
        # OCR must not invent human field annotations.
        self.assertEqual(
            list_annotations(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                evidence_id=ev.id,
            ),
            [],
        )
        case_after = get_case(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(case_after.case_version, ev.case_version + 1)

    def test_provider_cannot_mutate_original_even_if_hostile(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="ocr-hostile"
        )
        raw = b"immutable-original-bytes-abcdef"
        ev = attach_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            file_store=self.store,
            data=raw,
            original_filename="note.bin",
            content_type="application/octet-stream",
            expected_case_version=1,
        )
        path = self.store.path_for_file_id(ev.file_id)
        ocr = run_ocr_on_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            evidence_id=ev.id,
            file_store=self.store,
            expected_case_version=ev.case_version,
            provider=_MutatingProvider(),
        )
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(ocr.provider, "mutating_test_provider")

    def test_rejected_evidence_cannot_be_ocrd(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="ocr-rej"
        )
        bad = attach_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            file_store=self.store,
            data=b"MZ-fake",
            original_filename="tool.exe",
            expected_case_version=1,
        )
        self.assertEqual(bad.quarantine_status, "rejected")
        with self.assertRaises(DomainError) as ctx:
            run_ocr_on_evidence(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                evidence_id=bad.id,
                file_store=self.store,
                expected_case_version=bad.case_version,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.ATTACHMENT_REJECTED)

    def test_api_ocr_and_list(self) -> None:
        created = self.client.post("/cases", json={"title": "api-ocr"})
        case_id = created.json()["data"]["case_id"]
        payload = base64.b64encode(b"OCR API DEMO TEXT").decode()
        attach = self.client.post(
            f"/cases/{case_id}/evidence",
            json={
                "filename": "note.txt",
                "content_base64": payload,
                "content_type": "text/plain",
                "expected_case_version": 1,
            },
        )
        self.assertEqual(attach.status_code, 200, attach.text)
        evidence_id = attach.json()["data"]["evidence_id"]
        ver = attach.json()["data"]["case_version"]

        run = self.client.post(
            f"/cases/{case_id}/evidence/{evidence_id}/ocr",
            json={"expected_case_version": ver},
        )
        self.assertEqual(run.status_code, 200, run.text)
        body = run.json()["data"]
        self.assertEqual(body["status"], "completed")
        self.assertIn("OCR API DEMO TEXT", body["extracted_text"])
        self.assertEqual(body["case_version"], ver + 1)

        listed = self.client.get(f"/cases/{case_id}/ocr")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["data"]), 1)

        by_ev = self.client.get(f"/cases/{case_id}/evidence/{evidence_id}/ocr")
        self.assertEqual(by_ev.status_code, 200)
        self.assertEqual(len(by_ev.json()["data"]), 1)

        # Stale version rejected.
        stale = self.client.post(
            f"/cases/{case_id}/evidence/{evidence_id}/ocr",
            json={"expected_case_version": ver},
        )
        self.assertEqual(stale.status_code, 409)

    def test_workbench_ocr_form_stores_derived_row(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="wb-ocr"
        )
        ev = attach_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            file_store=self.store,
            data=b"workbench ocr text",
            original_filename="wb.txt",
            content_type="text/plain",
            expected_case_version=1,
        )
        resp = self.client.post(
            f"/workbench/cases/{case.id}/evidence/{ev.id}/ocr",
            data={"expected_case_version": str(ev.case_version)},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        rows = list_ocr_results(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("workbench ocr text", rows[0].extracted_text)
        path = self.store.path_for_file_id(ev.file_id)
        self.assertEqual(path.read_bytes(), b"workbench ocr text")

    def test_human_reject_then_cannot_ocr(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="ocr-soft-rej"
        )
        ev = attach_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            file_store=self.store,
            data=b"later rejected",
            original_filename="later.txt",
            content_type="text/plain",
            expected_case_version=1,
        )
        set_evidence_quarantine(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            evidence_id=ev.id,
            quarantine_status="rejected",
            rejection_reason="blurry",
            expected_case_version=ev.case_version,
        )
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        with self.assertRaises(DomainError) as ctx:
            run_ocr_on_evidence(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                evidence_id=ev.id,
                file_store=self.store,
                expected_case_version=case.case_version,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.ATTACHMENT_REJECTED)


if __name__ == "__main__":
    unittest.main()
