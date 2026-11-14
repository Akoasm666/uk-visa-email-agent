from __future__ import annotations

# Isolate before any fastapi_app import can boot the demo database.
import os

os.environ["VISA_AGENT_SKIP_APP_BOOT"] = "1"

import tempfile
import threading
import time
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

from tests.db_isolation import TEST_DB, TEST_PUBLISH_TOKEN, reset_test_database
from visa_agent.case_service import (
    build_transition_context,
    create_case,
    get_case,
    record_final_review,
)
from visa_agent.config import Settings
from visa_agent.document_pipeline import (
    attach_evidence,
    record_annotation,
    set_evidence_quarantine,
)
from visa_agent.domain.enums import ErrorCode, RequirementStatus, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.rules import (
    ensure_seed_visitor_pack,
    evaluate_case_requirements,
    get_rule_pack,
    impact_scan,
    list_case_requirements,
    publish_rule_pack,
    set_case_active_pack,
)
from visa_agent.rules.seed import VISITOR_PACK_ID, VISITOR_PACK_VERSION
from visa_agent.storage.files import LocalFileStore

TENANT = "demo"


def _settings(**overrides: object) -> Settings:
    base = dict(
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
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _publish(pack_id: str = VISITOR_PACK_ID, version: int = VISITOR_PACK_VERSION):
    return publish_rule_pack(
        database_url=TEST_DB,
        pack_id=pack_id,
        pack_version=version,
        provided_token=TEST_PUBLISH_TOKEN,
        configured_token=TEST_PUBLISH_TOKEN,
    )


def _insert_pack(
    *,
    pack_id: str,
    version: int,
    route: str,
    source_url: str = "https://example.test/rules",
    retrieved: str | None = "2026-11-14",
    requirements: list[tuple[str, str]] | None = None,
) -> None:
    with psycopg.connect(TEST_DB) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO rule_packs (
                    pack_id, pack_version, route, title,
                    source_url, source_retrieved_on, published
                ) VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (pack_id, pack_version) DO NOTHING
                """,
                (
                    pack_id,
                    version,
                    route,
                    f"{pack_id} v{version}",
                    source_url,
                    date.fromisoformat(retrieved) if retrieved else None,
                ),
            )
            for key, sev in requirements or [("passport_valid", "required")]:
                conn.execute(
                    """
                    INSERT INTO rule_requirements (
                        id, pack_id, pack_version, requirement_key,
                        title, description, severity
                    ) VALUES (%s, %s, %s, %s, %s, '', %s)
                    ON CONFLICT (pack_id, pack_version, requirement_key) DO NOTHING
                    """,
                    (uuid.uuid4().hex, pack_id, version, key, key, sev),
                )


class RulesContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        ensure_seed_visitor_pack(database_url=TEST_DB)

    def test_seed_idempotent_concurrent(self) -> None:
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                ensure_seed_visitor_pack(database_url=TEST_DB)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        pack = get_rule_pack(
            database_url=TEST_DB,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION,
        )
        self.assertFalse(pack.published)
        self.assertGreaterEqual(len(pack.requirements), 7)

    def test_seed_does_not_mutate_existing_pack(self) -> None:
        reset_test_database()
        _insert_pack(
            pack_id=VISITOR_PACK_ID,
            version=VISITOR_PACK_VERSION,
            route=RouteCode.STANDARD_VISITOR.value,
            requirements=[("passport_valid", "required")],
        )
        published = _publish()
        before_count = len(published.requirements)
        self.assertEqual(before_count, 1)
        published_at = published.published_at

        # Seed would want to insert the full visitor requirement set — must no-op.
        ensure_seed_visitor_pack(database_url=TEST_DB)
        after = get_rule_pack(
            database_url=TEST_DB,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION,
        )
        self.assertEqual(len(after.requirements), before_count)
        self.assertTrue(after.published)
        self.assertEqual(after.published_at, published_at)
        self.assertEqual(
            {r.key for r in after.requirements}, {"passport_valid"}
        )

    def test_publish_requires_configured_admin_token(self) -> None:
        with self.assertRaises(DomainError) as disabled:
            publish_rule_pack(
                database_url=TEST_DB,
                pack_id=VISITOR_PACK_ID,
                pack_version=VISITOR_PACK_VERSION,
                provided_token="anything",
                configured_token="",
            )
        self.assertEqual(disabled.exception.code, ErrorCode.UNAUTHORIZED)

        with self.assertRaises(DomainError) as missing:
            publish_rule_pack(
                database_url=TEST_DB,
                pack_id=VISITOR_PACK_ID,
                pack_version=VISITOR_PACK_VERSION,
                provided_token="",
                configured_token=TEST_PUBLISH_TOKEN,
            )
        self.assertEqual(missing.exception.code, ErrorCode.UNAUTHORIZED)

        with self.assertRaises(DomainError) as wrong:
            publish_rule_pack(
                database_url=TEST_DB,
                pack_id=VISITOR_PACK_ID,
                pack_version=VISITOR_PACK_VERSION,
                provided_token="ops",
                configured_token=TEST_PUBLISH_TOKEN,
            )
        self.assertEqual(wrong.exception.code, ErrorCode.UNAUTHORIZED)

        first = _publish()
        self.assertTrue(first.published)
        published_at = first.published_at
        self.assertIsNotNone(published_at)
        time.sleep(0.05)
        second = _publish()
        self.assertEqual(second.published_at, published_at)

    def test_empty_or_sourceless_pack_cannot_publish(self) -> None:
        _insert_pack(
            pack_id="empty_pack",
            version=1,
            route=RouteCode.STANDARD_VISITOR.value,
            source_url="",
            retrieved=None,
            requirements=[],
        )
        with self.assertRaises(DomainError) as ctx:
            publish_rule_pack(
                database_url=TEST_DB,
                pack_id="empty_pack",
                pack_version=1,
                provided_token=TEST_PUBLISH_TOKEN,
                configured_token=TEST_PUBLISH_TOKEN,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)

        _insert_pack(
            pack_id="nosource",
            version=1,
            route=RouteCode.STANDARD_VISITOR.value,
            source_url="",
            retrieved="2026-11-14",
            requirements=[("x", "required")],
        )
        with self.assertRaises(DomainError):
            publish_rule_pack(
                database_url=TEST_DB,
                pack_id="nosource",
                pack_version=1,
                provided_token=TEST_PUBLISH_TOKEN,
                configured_token=TEST_PUBLISH_TOKEN,
            )

    def test_route_mismatch_and_switch_invalidates_approval(self) -> None:
        _publish()
        student = create_case(
            database_url=TEST_DB,
            tenant_id=TENANT,
            title="student",
            route=RouteCode.STUDENT,
        )
        with self.assertRaises(DomainError) as mismatch:
            set_case_active_pack(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=student.id,
                pack_id=VISITOR_PACK_ID,
                pack_version=VISITOR_PACK_VERSION,
            )
        self.assertEqual(mismatch.exception.code, ErrorCode.VALIDATION_ERROR)

        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="visitor-switch"
        )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION,
        )
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=case.case_version,
        )
        ctx_before = build_transition_context(database_url=TEST_DB, case=case)
        self.assertEqual(
            ctx_before.final_review_approval_case_version, case.case_version
        )

        _insert_pack(
            pack_id=VISITOR_PACK_ID,
            version=2,
            route=RouteCode.STANDARD_VISITOR.value,
            requirements=[
                ("passport_valid", "required"),
                ("transfer_human_triggers", "transfer_human"),
            ],
        )
        _publish(VISITOR_PACK_ID, 2)
        after = set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=2,
        )
        self.assertEqual(after.case_version, case.case_version + 1)
        ctx_after = build_transition_context(database_url=TEST_DB, case=after)
        self.assertIsNone(ctx_after.final_review_approval_case_version)

    def test_evaluate_only_bound_pack_unknown_blocks_clear(self) -> None:
        _publish()
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="eval-bound"
        )
        with self.assertRaises(DomainError):
            evaluate_case_requirements(
                database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
            )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION,
        )
        result = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertFalse(result.all_required_clear)
        self.assertIn("transfer_human_triggers", result.blocking_keys)
        self.assertIn("sponsor_if_any", result.blocking_keys)
        for st in result.states:
            self.assertNotIn(
                st.status,
                {
                    RequirementStatus.EVIDENCE_ACCEPTED.value,
                    RequirementStatus.NOT_APPLICABLE.value,
                    RequirementStatus.EXEMPTED_WITH_BASIS.value,
                },
            )

    def test_rejected_evidence_annotations_ignored(self) -> None:
        _publish()
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="rej-ann"
        )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION,
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            ev = attach_evidence(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=b"%PDF-bad",
                original_filename="p.pdf",
                classification="passport",
                expected_case_version=2,
            )
            record_annotation(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                evidence_id=ev.id,
                field_key="funds_for_stay",
                field_value="x",
                expected_case_version=3,
            )
            set_evidence_quarantine(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                evidence_id=ev.id,
                quarantine_status="rejected",
                rejection_reason="fake",
                expected_case_version=4,
            )
        result = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        by_key = {s.requirement_key: s.status for s in result.states}
        self.assertEqual(
            by_key["passport_valid"], RequirementStatus.APPLICABLE_MISSING.value
        )
        self.assertEqual(
            by_key["funds_for_stay"], RequirementStatus.APPLICABLE_MISSING.value
        )

    def test_stale_eval_cannot_overwrite_newer_case_version(self) -> None:
        _publish()
        _insert_pack(
            pack_id=VISITOR_PACK_ID,
            version=2,
            route=RouteCode.STANDARD_VISITOR.value,
            requirements=[
                ("passport_valid", "required"),
                ("transfer_human_triggers", "transfer_human"),
            ],
        )
        _publish(VISITOR_PACK_ID, 2)
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="race-eval"
        )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=1,
        )
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                INSERT INTO case_requirement_states (
                    id, tenant_id, case_id, case_version, pack_id, pack_version,
                    requirement_key, status, reason
                ) VALUES (%s, %s, %s, 1, %s, 1, 'passport_valid',
                          'evidence_accepted', 'stale')
                """,
                (uuid.uuid4().hex, TENANT, case.id, VISITOR_PACK_ID),
            )
            conn.commit()

        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=2,
        )
        v2 = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertEqual(v2.pack_version, 2)

        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                INSERT INTO case_requirement_states (
                    id, tenant_id, case_id, case_version, pack_id, pack_version,
                    requirement_key, status, reason
                ) VALUES (%s, %s, %s, 1, %s, 2, 'passport_valid',
                          'evidence_accepted', 'stale-overwrite')
                ON CONFLICT (tenant_id, case_id, pack_id, pack_version, requirement_key)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    case_version = EXCLUDED.case_version,
                    updated_at = NOW()
                WHERE case_requirement_states.case_version
                      <= EXCLUDED.case_version
                """,
                (uuid.uuid4().hex, TENANT, case.id, VISITOR_PACK_ID),
            )
            conn.commit()

        listed = list_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        self.assertTrue(listed)
        self.assertTrue(all(s.pack_version == 2 for s in listed))
        self.assertTrue(all(s.case_version == v2.case_version for s in listed))
        passport = next(s for s in listed if s.requirement_key == "passport_valid")
        self.assertNotEqual(passport.status, RequirementStatus.EVIDENCE_ACCEPTED.value)
        self.assertNotEqual(passport.reason, "stale-overwrite")

    def test_impact_scan_tenant_isolated(self) -> None:
        _publish()
        ours = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="ours"
        )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=ours.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=1,
        )
        other = create_case(
            database_url=TEST_DB, tenant_id="other_tenant", title="theirs"
        )
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id="other_tenant",
            case_id=other.id,
            pack_id=VISITOR_PACK_ID,
            pack_version=1,
        )
        rows = impact_scan(
            database_url=TEST_DB,
            tenant_id=TENANT,
            pack_id=VISITOR_PACK_ID,
            from_version=1,
            to_version=None,
        )
        ids = {r["case_id"] for r in rows}
        self.assertIn(ours.id, ids)
        self.assertNotIn(other.id, ids)


class RulesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        self.settings = _settings()
        # Import only after SKIP_APP_BOOT; bootstrap test DB before serving.
        from visa_agent import fastapi_app as fa

        fa.bootstrap(self.settings)
        self.fa = fa
        self._patcher = patch.object(fa, "get_settings", return_value=self.settings)
        self._patcher.start()
        self.client = TestClient(fa.app)

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_api_publish_requires_admin_token(self) -> None:
        denied = self.client.post(
            f"/rule-packs/{VISITOR_PACK_ID}/{VISITOR_PACK_VERSION}/publish"
        )
        self.assertEqual(denied.status_code, 403)

        name_only = self.client.post(
            f"/rule-packs/{VISITOR_PACK_ID}/{VISITOR_PACK_VERSION}/publish",
            headers={"X-Human-Operator": "ops"},
        )
        self.assertEqual(name_only.status_code, 403)

        wrong = self.client.post(
            f"/rule-packs/{VISITOR_PACK_ID}/{VISITOR_PACK_VERSION}/publish",
            headers={"X-Rule-Publish-Token": "wrong"},
        )
        self.assertEqual(wrong.status_code, 403)

        ok = self.client.post(
            f"/rule-packs/{VISITOR_PACK_ID}/{VISITOR_PACK_VERSION}/publish",
            headers={"X-Rule-Publish-Token": TEST_PUBLISH_TOKEN},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["data"]["published"])

        # Bearer form also accepted.
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                UPDATE rule_packs SET published = FALSE, published_at = NULL
                WHERE pack_id = %s AND pack_version = %s
                """,
                (VISITOR_PACK_ID, VISITOR_PACK_VERSION),
            )
            conn.commit()
        bearer = self.client.post(
            f"/rule-packs/{VISITOR_PACK_ID}/{VISITOR_PACK_VERSION}/publish",
            headers={"Authorization": f"Bearer {TEST_PUBLISH_TOKEN}"},
        )
        self.assertEqual(bearer.status_code, 200)

    def test_api_impact_route_and_evaluate_flow(self) -> None:
        self.client.post(
            f"/rule-packs/{VISITOR_PACK_ID}/{VISITOR_PACK_VERSION}/publish",
            headers={"X-Rule-Publish-Token": TEST_PUBLISH_TOKEN},
        )
        impact = self.client.get(
            f"/rule-packs/{VISITOR_PACK_ID}/impact",
            params={"from_version": 1},
        )
        self.assertEqual(impact.status_code, 200, impact.text)
        self.assertIsInstance(impact.json()["data"], list)

        created = self.client.post("/cases", json={"title": "api-rules"})
        case_id = created.json()["data"]["case_id"]
        bind = self.client.post(
            f"/cases/{case_id}/active-pack",
            json={"pack_id": VISITOR_PACK_ID, "pack_version": 1},
        )
        self.assertEqual(bind.status_code, 200)
        self.assertGreaterEqual(bind.json()["data"]["case_version"], 2)

        ev = self.client.post(f"/cases/{case_id}/requirements/evaluate")
        self.assertEqual(ev.status_code, 200)
        self.assertFalse(ev.json()["data"]["all_required_clear"])

        root = self.client.get("/")
        self.assertTrue(
            root.json()["data"]["stage"].startswith("S2."),
            root.json()["data"]["stage"],
        )


if __name__ == "__main__":
    unittest.main()
