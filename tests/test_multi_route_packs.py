"""Additional UK routes beyond Standard Visitor (Student / SW / Graduate)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

os.environ["VISA_AGENT_SKIP_APP_BOOT"] = "1"

from fastapi.testclient import TestClient

from tests.db_isolation import TEST_DB, TEST_PUBLISH_TOKEN, reset_test_database
from visa_agent.case_service import create_case, get_case, set_case_route
from visa_agent.case_service.outreach import OpeningOutreachResult
from visa_agent.config import Settings
from visa_agent.domain.enums import CaseStatus, ErrorCode, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.document_pipeline import attach_evidence, record_annotation
from visa_agent.rules import (
    default_pack_for_route,
    ensure_seed_route_packs,
    evaluate_case_requirements,
    publish_rule_pack,
    run_visitor_scenario,
    set_case_active_pack,
)
from visa_agent.rules.seed import (
    GRADUATE_PACK_ID,
    GRADUATE_PACK_VERSION,
    SKILLED_WORKER_PACK_ID,
    SKILLED_WORKER_PACK_VERSION,
    STUDENT_PACK_ID,
    STUDENT_PACK_VERSION,
    VISITOR_PACK_ID,
    VISITOR_PACK_VERSION,
    VISITOR_PACK_VERSION_S3,
)
from visa_agent.storage.files import LocalFileStore
from visa_agent.workflow.transitions import apply_transition

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


def _publish(pack_id: str, pack_version: int) -> None:
    publish_rule_pack(
        database_url=TEST_DB,
        pack_id=pack_id,
        pack_version=pack_version,
        provided_token=TEST_PUBLISH_TOKEN,
        configured_token=TEST_PUBLISH_TOKEN,
    )


class MultiRoutePackTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        ensure_seed_route_packs(database_url=TEST_DB)
        self.settings = _settings(file_storage_dir=Path(tempfile.mkdtemp()))
        from visa_agent import fastapi_app as fa

        fa.bootstrap(self.settings)
        self._patcher = patch.object(fa, "get_settings", return_value=self.settings)
        self._patcher.start()
        self.client = TestClient(fa.app)

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_seeded_packs_and_default_pack_for_each_route(self) -> None:
        expected = {
            RouteCode.STANDARD_VISITOR: (VISITOR_PACK_ID, VISITOR_PACK_VERSION),
            RouteCode.STUDENT: (STUDENT_PACK_ID, STUDENT_PACK_VERSION),
            RouteCode.SKILLED_WORKER: (
                SKILLED_WORKER_PACK_ID,
                SKILLED_WORKER_PACK_VERSION,
            ),
            RouteCode.GRADUATE: (GRADUATE_PACK_ID, GRADUATE_PACK_VERSION),
        }
        for route, pack in expected.items():
            self.assertEqual(default_pack_for_route(route), pack)

        listed = self.client.get("/routes").json()["data"]
        codes = {r["code"] for r in listed}
        self.assertEqual(
            codes,
            {
                RouteCode.STANDARD_VISITOR.value,
                RouteCode.STUDENT.value,
                RouteCode.SKILLED_WORKER.value,
                RouteCode.GRADUATE.value,
            },
        )

    def test_create_bind_and_evaluate_student_and_worker(self) -> None:
        for route, pack_id, pack_version in (
            (RouteCode.STUDENT, STUDENT_PACK_ID, STUDENT_PACK_VERSION),
            (RouteCode.SKILLED_WORKER, SKILLED_WORKER_PACK_ID, SKILLED_WORKER_PACK_VERSION),
            (RouteCode.GRADUATE, GRADUATE_PACK_ID, GRADUATE_PACK_VERSION),
        ):
            _publish(pack_id, pack_version)
            created = self.client.post(
                "/cases",
                json={"title": f"{route.value}-case", "route": route.value},
            )
            self.assertEqual(created.status_code, 200, created.text)
            case_id = created.json()["data"]["case_id"]
            self.assertEqual(created.json()["data"]["route"], route.value)

            bound = self.client.post(
                f"/cases/{case_id}/active-pack",
                json={"pack_id": pack_id, "pack_version": pack_version},
            )
            self.assertEqual(bound.status_code, 200, bound.text)

            with tempfile.TemporaryDirectory() as tmp:
                store = LocalFileStore(Path(tmp))
                case = get_case(
                    database_url=TEST_DB, tenant_id=TENANT, case_id=case_id
                )
                ev = attach_evidence(
                    database_url=TEST_DB,
                    tenant_id=TENANT,
                    case_id=case_id,
                    file_store=store,
                    data=b"passport bytes",
                    original_filename="passport.pdf",
                    content_type="application/pdf",
                    classification="passport",
                    expected_case_version=case.case_version,
                )
                case = get_case(
                    database_url=TEST_DB, tenant_id=TENANT, case_id=case_id
                )
                record_annotation(
                    database_url=TEST_DB,
                    tenant_id=TENANT,
                    case_id=case_id,
                    evidence_id=ev.id,
                    field_key="passport_valid",
                    field_value="yes",
                    expected_case_version=case.case_version,
                )

            evaluated = self.client.post(f"/cases/{case_id}/requirements/evaluate")
            self.assertEqual(evaluated.status_code, 200, evaluated.text)
            data = evaluated.json()["data"]
            self.assertEqual(data["pack_id"], pack_id)
            keys = {r["requirement_key"] for r in data["states"]}
            self.assertIn("passport_valid", keys)
            # Visitor-only funds gate must not appear on non-visitor packs.
            self.assertNotIn("funds_for_stay", keys)

    def _active_pack(self, case_id: str) -> tuple[str | None, int | None]:
        import psycopg

        with psycopg.connect(TEST_DB) as conn:
            row = conn.execute(
                """
                SELECT active_pack_id, active_pack_version
                FROM cases WHERE tenant_id = %s AND id = %s
                """,
                (TENANT, case_id),
            ).fetchone()
        assert row is not None
        return row[0], row[1]

    def test_route_rebind_clears_pack_and_blocks_late_change(self) -> None:
        _publish(STUDENT_PACK_ID, STUDENT_PACK_VERSION)
        case = create_case(
            database_url=TEST_DB,
            tenant_id=TENANT,
            title="rebind",
            route=RouteCode.STUDENT,
        )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=STUDENT_PACK_ID,
            pack_version=STUDENT_PACK_VERSION,
        )
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pack_id, _ = self._active_pack(case.id)
        self.assertEqual(pack_id, STUDENT_PACK_ID)

        after = set_case_route(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            route=RouteCode.GRADUATE,
            expected_case_version=case.case_version,
        )
        self.assertEqual(after.route, RouteCode.GRADUATE)
        cleared_id, cleared_ver = self._active_pack(after.id)
        self.assertIsNone(cleared_id)
        self.assertIsNone(cleared_ver)
        self.assertGreater(after.case_version, case.case_version)

        # Advance past route_scope_confirmed — route changes must be rejected.
        apply_transition(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=after.id,
            target=CaseStatus.INTAKE_AUTHORIZATION,
            expected_case_version=after.case_version,
        )
        after = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=after.id)
        apply_transition(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=after.id,
            target=CaseStatus.ROUTE_SCOPE_CONFIRMED,
            expected_case_version=after.case_version,
        )
        after = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=after.id)
        apply_transition(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=after.id,
            target=CaseStatus.COLLECT_AND_VERIFY,
            expected_case_version=after.case_version,
        )
        after = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=after.id)
        with self.assertRaises(DomainError) as ctx:
            set_case_route(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=after.id,
                route=RouteCode.STUDENT,
                expected_case_version=after.case_version,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_visitor_scenarios_still_pass_alongside_other_routes(self) -> None:
        _publish(VISITOR_PACK_ID, VISITOR_PACK_VERSION_S3)
        result = run_visitor_scenario(
            scenario_id="standard_adult_tourist",
            classifications=["passport", "bank"],
            annotation_keys=["intention_to_leave"],
        )
        self.assertEqual(result.scenario_id, "standard_adult_tourist")
        self.assertFalse(result.transfer_human_required)

        created = self.client.post(
            "/cases",
            json={
                "title": "visitor-still",
                "route": RouteCode.STANDARD_VISITOR.value,
            },
        )
        case_id = created.json()["data"]["case_id"]
        bound = self.client.post(
            f"/cases/{case_id}/active-pack",
            json={
                "pack_id": VISITOR_PACK_ID,
                "pack_version": VISITOR_PACK_VERSION_S3,
            },
        )
        self.assertEqual(bound.status_code, 200, bound.text)
        evaluated = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case_id
        )
        self.assertEqual(evaluated.pack_id, VISITOR_PACK_ID)
        self.assertEqual(evaluated.pack_version, VISITOR_PACK_VERSION_S3)

    def test_workbench_create_honours_route_picker(self) -> None:
        outcome = OpeningOutreachResult(
            attempted=True, sent=False, skipped_reason="blocked", outbox=None
        )
        with patch(
            "visa_agent.fastapi_app.send_opening_outreach", return_value=outcome
        ) as outreach:
            resp = self.client.post(
                "/workbench/cases",
                data={
                    "title": "wb-student",
                    "customer_email": "s@example.com",
                    "route": RouteCode.STUDENT.value,
                },
                follow_redirects=False,
            )
        outreach.assert_called_once()
        self.assertEqual(resp.status_code, 303)
        case_id = urlsplit(resp.headers["location"]).path.rsplit("/", 1)[-1]
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id)
        self.assertEqual(case.route, RouteCode.STUDENT)


if __name__ == "__main__":
    unittest.main()
