"""S3: Visitor scenario pack (W06-V) + model gateway groundedness (W07)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["VISA_AGENT_SKIP_APP_BOOT"] = "1"

from fastapi.testclient import TestClient

from tests.db_isolation import TEST_DB, TEST_PUBLISH_TOKEN, reset_test_database
from visa_agent.ai_capabilities.draft import (
    build_synthetic_context,
    generate_case_draft,
)
from visa_agent.ai_capabilities.gateway import (
    context_from_stored_payload,
    gate_model_output,
    groundedness_report,
    render_draft_from_grounded_facts,
)
from visa_agent.case_service import create_case, get_case
from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode, RequirementStatus
from visa_agent.domain.errors import DomainError
from visa_agent.document_pipeline import attach_evidence, record_annotation
from visa_agent.rules import (
    AnnotationPair,
    ensure_seed_visitor_pack,
    evaluate_case_requirements,
    publish_rule_pack,
    run_visitor_scenario,
    set_case_active_pack,
)
from visa_agent.rules.seed import (
    VISITOR_PACK_ID,
    VISITOR_PACK_VERSION,
    VISITOR_PACK_VERSION_S3,
)
from visa_agent.rules.visitor_pack import assess_visitor_signals, normalize_annotations
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


def _publish(version: int = VISITOR_PACK_VERSION_S3) -> None:
    publish_rule_pack(
        database_url=TEST_DB,
        pack_id=VISITOR_PACK_ID,
        pack_version=version,
        provided_token=TEST_PUBLISH_TOKEN,
        configured_token=TEST_PUBLISH_TOKEN,
    )


class FakeChatClient:
    def __init__(self, content: str) -> None:
        self.content = content

    def complete(self, *, messages: list[dict[str, str]], model: str) -> dict:
        return {
            "id": "fake-s3",
            "choices": [{"message": {"content": self.content}}],
        }


def _model_payload(**overrides: object) -> str:
    base = {
        "draft_text": "将住在曼彻斯特，与 14 名家人同行，雇主是 AcmeCorp。",
        "candidate_facts": [
            {
                "key": "visit_purpose",
                "value": "tourism_family_visit_demo",
                "status": "customer_stated",
            },
            {
                "key": "intended_stay_days",
                "value": "14",
                "status": "customer_stated",
            },
        ],
        "suggested_actions": ["请提供公司出具的在职证明"],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class VisitorScenarioUnitTests(unittest.TestCase):
    def test_empty_key_preserves_value_alignment(self) -> None:
        pairs = normalize_annotations(
            annotation_keys=["", "transfer_minor"],
            annotation_values=["yes", "no"],
        )
        self.assertEqual(
            pairs,
            (
                AnnotationPair(key="funds_note", value="yes"),
                AnnotationPair(key="transfer_minor", value="no"),
            ),
        )
        a = assess_visitor_signals(
            annotation_keys=["", "transfer_minor"],
            annotation_values=["yes", "no"],
        )
        self.assertNotIn("transfer_minor", a.transfer_triggers_affirmative)
        self.assertIn("transfer_minor", a.transfer_triggers_negative)
        self.assertEqual(a.transfer_polarity, "unknown")

    def test_formula_field_polarity_and_clause_split(self) -> None:
        neg = assess_visitor_signals(
            annotations=[AnnotationPair(key="student_fixed_funds", value="no")]
        )
        self.assertFalse(neg.student_funds_rejected)

        mixed = assess_visitor_signals(
            annotations=[
                AnnotationPair(
                    key="funds_note",
                    value="未使用其他资金估算方法。采用了 student_fixed_funds 公式。",
                )
            ]
        )
        self.assertTrue(mixed.student_funds_rejected)

        english = assess_visitor_signals(
            annotations=[
                AnnotationPair(
                    key="funds_note",
                    value=(
                        "Did not use another funds estimation method. "
                        "Applied the student_fixed_funds formula."
                    ),
                )
            ]
        )
        self.assertTrue(english.student_funds_rejected)

        same_sentence = assess_visitor_signals(
            annotations=[
                AnnotationPair(
                    key="funds_note",
                    value=(
                        "Did not use another funds estimation method but "
                        "applied the student_fixed_funds formula"
                    ),
                )
            ]
        )
        self.assertTrue(same_sentence.student_funds_rejected)

        cas_yes = assess_visitor_signals(
            annotations=[AnnotationPair(key="cas", value="yes")]
        )
        self.assertTrue(cas_yes.student_funds_rejected)

        only_neg = assess_visitor_signals(
            annotations=[
                AnnotationPair(
                    key="funds_note",
                    value="未使用 student_fixed_funds 公式。",
                )
            ]
        )
        self.assertFalse(only_neg.student_funds_rejected)


class VisitorEvaluateIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        ensure_seed_visitor_pack(database_url=TEST_DB)
        _publish(VISITOR_PACK_VERSION)
        _publish(VISITOR_PACK_VERSION_S3)

    def _bind_v2(self, case_id: str) -> None:
        set_case_active_pack(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case_id,
            pack_id=VISITOR_PACK_ID,
            pack_version=VISITOR_PACK_VERSION_S3,
        )

    def _annotate(
        self, case_id: str, *, field_key: str, field_value: str, data: bytes
    ) -> None:
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            ev = attach_evidence(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case_id,
                file_store=store,
                data=data,
                original_filename="note.pdf",
                classification="bank",
                expected_case_version=case.case_version,
            )
            case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id)
            record_annotation(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case_id,
                evidence_id=ev.id,
                field_key=field_key,
                field_value=field_value,
                expected_case_version=case.case_version,
            )

    def test_v2_partial_transfer_no_keeps_unknown(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="s3-v2-funds"
        )
        self._bind_v2(case.id)
        self._annotate(
            case.id,
            field_key="funds_note",
            field_value="Used the student_fixed_funds formula",
            data=b"%PDF-s3",
        )
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            # reuse existing evidence via new attach for transfer note
            ev = attach_evidence(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                file_store=store,
                data=b"%PDF-tr",
                original_filename="tr.pdf",
                classification="other",
                expected_case_version=case.case_version,
            )
            case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
            record_annotation(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                evidence_id=ev.id,
                field_key="transfer_minor",
                field_value="no",
                expected_case_version=case.case_version,
            )
        result = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        by_key = {s.requirement_key: s for s in result.states}
        self.assertEqual(
            by_key["funds_for_stay"].status,
            RequirementStatus.NEEDS_CORRECTION.value,
        )
        self.assertEqual(
            by_key["transfer_human_triggers"].status,
            RequirementStatus.APPLICABILITY_UNKNOWN.value,
        )

    def test_v2_english_formula_after_other_negation(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="s3-en-formula"
        )
        self._bind_v2(case.id)
        self._annotate(
            case.id,
            field_key="funds_note",
            field_value=(
                "Did not use another funds estimation method. "
                "Applied the student_fixed_funds formula."
            ),
            data=b"%PDF-en",
        )
        result = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        by_key = {s.requirement_key: s for s in result.states}
        self.assertEqual(
            by_key["funds_for_stay"].status,
            RequirementStatus.NEEDS_CORRECTION.value,
        )
        self.assertNotEqual(
            by_key["funds_for_stay"].status,
            RequirementStatus.RECEIVED_PENDING_REVIEW.value,
        )

    def test_v2_cas_yes_needs_correction(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="s3-cas-yes"
        )
        self._bind_v2(case.id)
        self._annotate(
            case.id, field_key="cas", field_value="yes", data=b"%PDF-cas"
        )
        result = evaluate_case_requirements(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id
        )
        by_key = {s.requirement_key: s for s in result.states}
        self.assertEqual(
            by_key["funds_for_stay"].status,
            RequirementStatus.NEEDS_CORRECTION.value,
        )


class ModelGatewayUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()

    def test_template_replaces_invented_prose(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="gw-template"
        )
        ctx = build_synthetic_context(case)
        facts = [
            {
                "key": "intended_stay_days",
                "value": "14",
                "status": "customer_stated",
            }
        ]
        body, actions, safe = gate_model_output(
            context=ctx,
            draft_text="将住在曼彻斯特，与 14 名家人同行。",
            facts=facts,
            actions=["虚构雇主 Acme 开证明"],
        )
        self.assertNotIn("曼彻斯特", body)
        self.assertNotIn("家人", body)
        self.assertIn("计划停留天数：14 天", body)
        self.assertIn("请提供公司出具的在职证明", actions)
        expected = render_draft_from_grounded_facts(context=ctx, facts=facts)
        self.assertEqual((body, actions), expected)
        self.assertNotIn("曼彻斯特", safe)

        persisted = generate_case_draft(
            settings=_settings(file_storage_dir=Path(tempfile.mkdtemp())),
            case_id=case.id,
            client=FakeChatClient(_model_payload()),
        )
        self.assertNotIn("曼彻斯特", persisted.draft_text)
        self.assertNotIn("AcmeCorp", persisted.draft_text)
        self.assertIn("计划停留天数：14 天", persisted.draft_text)
        self.assertIn("请提供公司出具的在职证明", persisted.suggested_actions)

        import psycopg

        with psycopg.connect(TEST_DB) as conn:
            row = conn.execute(
                "SELECT response_text FROM model_calls WHERE id = %s",
                (persisted.model_call_id,),
            ).fetchone()
        self.assertNotIn("曼彻斯特", row[0])
        self.assertNotIn("AcmeCorp", row[0])

    def test_side_effect_blocked_before_persist(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="gw-side"
        )
        ctx = build_synthetic_context(case)
        with self.assertRaises(DomainError):
            gate_model_output(
                context=ctx,
                draft_text="您的申请已获签，材料也已寄出。",
                facts=[],
                actions=[],
            )
        for claim in (
            "Your application is approved. Documents sent.",
            "Application approved.",
            "您的申请已获签，材料也已寄出。",
        ):
            with self.assertRaises(DomainError) as err:
                generate_case_draft(
                    settings=_settings(file_storage_dir=Path(tempfile.mkdtemp())),
                    case_id=case.id,
                    client=FakeChatClient(
                        json.dumps(
                            {
                                "draft_text": claim,
                                "candidate_facts": [],
                                "suggested_actions": [],
                            }
                        )
                    ),
                )
            self.assertEqual(err.exception.code, ErrorCode.MODEL_OUTPUT_INVALID)
            import psycopg

            with psycopg.connect(TEST_DB) as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE case_id = %s",
                    (case.id,),
                ).fetchone()[0]
            self.assertEqual(n, 0)

    def test_groundedness_unverifiable_without_corpus(self) -> None:
        case = create_case(
            database_url=TEST_DB, tenant_id=TENANT, title="gw-gen"
        )
        settings = _settings(file_storage_dir=Path(tempfile.mkdtemp()))
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            from visa_agent import fastapi_app as fa

            client = TestClient(fa.app)
            empty = client.get(f"/cases/{case.id}/draft/groundedness").json()
            self.assertEqual(empty["data"]["report"]["status"], "no_draft")

            generate_case_draft(
                settings=settings,
                case_id=case.id,
                client=FakeChatClient(_model_payload()),
            )
            mid = client.get(f"/cases/{case.id}/draft/groundedness").json()
            self.assertTrue(mid["data"]["report"]["verifiable"])
            self.assertTrue(mid["data"]["report"]["all_grounded"])
            self.assertTrue(mid["data"]["report"]["template_rendered"])

            missing = groundedness_report(
                context=None,
                facts=[{"key": "intended_stay_days", "value": "14"}],
                has_draft=True,
                generation_context_missing=True,
            )
            self.assertEqual(missing["status"], "unverifiable")
            self.assertIsNone(missing["all_grounded"])
            self.assertIsNone(context_from_stored_payload(synthetic_payload={}))


class S3ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        ensure_seed_visitor_pack(database_url=TEST_DB)
        _publish(VISITOR_PACK_VERSION_S3)

    def test_legacy_keys_with_empty_slot(self) -> None:
        settings = _settings()
        with patch("visa_agent.fastapi_app.get_settings", return_value=settings):
            from visa_agent import fastapi_app as fa

            client = TestClient(fa.app)
            ran = client.post(
                "/visitor-scenarios",
                json={
                    "scenario_id": "standard_adult_tourist",
                    "annotation_keys": ["", "transfer_minor"],
                    "annotation_values": ["yes", "no"],
                },
            )
            self.assertEqual(ran.status_code, 200)
            self.assertEqual(ran.json()["data"]["transfer_polarity"], "unknown")
            self.assertFalse(ran.json()["data"]["transfer_human_required"])


if __name__ == "__main__":
    unittest.main()
