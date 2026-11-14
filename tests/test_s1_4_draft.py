from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from dataclasses import asdict
from pathlib import Path

from fastapi.testclient import TestClient

from visa_agent.ai_capabilities.draft import (
    _normalize_output,
    build_synthetic_context,
    generate_case_draft,
)
from visa_agent.case_service import create_case, get_case
from visa_agent.case_service.service import bump_case_version
from visa_agent.config import get_settings
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError
from visa_agent.storage.migrate import apply_migrations

DB = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TENANT = "demo"


def _settings(tmp: Path) -> object:
    env = tmp / ".env"
    env.write_text(
        "\n".join(
            [
                f"DATABASE_URL={DB}",
                f"FILE_STORAGE_DIR={tmp / 'files'}",
                "TENANT_ID=demo",
                "SILICONFLOW_API_KEY=test-key",
                "SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1",
                "SILICONFLOW_MODEL=zai-org/GLM-5.3-Flash",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return get_settings(env)


class FakeChatClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def complete(self, *, messages: list[dict[str, str]], model: str) -> dict:
        self.calls.append({"messages": messages, "model": model})
        return {
            "id": "fake-completion",
            "choices": [{"message": {"content": self.content}}],
        }


def _ok_payload(**overrides: object) -> str:
    base = {
        "draft_text": "您好，这是合成演示草稿。请补充护照有效期说明。",
        "candidate_facts": [
            {
                "key": "visit_purpose",
                "value": "tourism_family_visit_demo",
                "status": "customer_stated",
            }
        ],
        "suggested_actions": ["等待客户补充护照页扫描件"],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class DraftUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)

    def test_synthetic_payload_excludes_real_title_and_case_id(self) -> None:
        marker = "leak-test@example.com"
        case = create_case(
            database_url=DB,
            tenant_id=TENANT,
            title=f"标题含邮箱 {marker}",
            customer_email=marker,
        )
        ctx = build_synthetic_context(case)
        blob = json.dumps(asdict(ctx), ensure_ascii=False)
        self.assertNotIn(marker, blob)
        self.assertNotIn(case.id, blob)
        self.assertNotIn(case.title, blob)
        self.assertIn("SYN-DEMO-CASE", blob)

        client = FakeChatClient(_ok_payload())
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            generate_case_draft(
                settings=settings, case_id=case.id, client=client
            )
        sent = client.calls[0]["messages"][1]["content"]
        self.assertNotIn(marker, sent)
        self.assertNotIn(case.id, sent)
        self.assertNotIn(case.title, sent)

        import psycopg

        with psycopg.connect(DB) as conn:
            row = conn.execute(
                """
                SELECT request_json FROM model_calls
                WHERE case_id = %s ORDER BY created_at DESC LIMIT 1
                """,
                (case.id,),
            ).fetchone()
        audit = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        self.assertEqual(audit["local_audit"]["case_id"], case.id)
        self.assertEqual(audit["local_audit"]["title"], case.title)
        self.assertNotIn(case.id, audit["messages"][1]["content"])
        self.assertNotIn(case.title, audit["messages"][1]["content"])

    def test_generate_draft_persists_with_mock_client(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="s1.4-draft"
        )
        client = FakeChatClient(_ok_payload())
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            before = get_case(
                database_url=DB, tenant_id=TENANT, case_id=case.id
            )
            result = generate_case_draft(
                settings=settings, case_id=case.id, client=client
            )
            after = get_case(
                database_url=DB, tenant_id=TENANT, case_id=case.id
            )
        self.assertIn("合成演示草稿", result.draft_text)
        self.assertEqual(result.case_version, before.case_version)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.case_version, before.case_version)

    def test_rejects_object_draft_text_and_numeric_actions(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            _normalize_output(
                json.dumps(
                    {
                        "draft_text": {"nested": True},
                        "candidate_facts": [],
                        "suggested_actions": [],
                    }
                )
            )
        self.assertEqual(ctx.exception.code, ErrorCode.MODEL_OUTPUT_INVALID)

        with self.assertRaises(DomainError) as ctx2:
            _normalize_output(
                json.dumps(
                    {
                        "draft_text": "ok",
                        "candidate_facts": [],
                        "suggested_actions": [1, 2],
                    }
                )
            )
        self.assertEqual(ctx2.exception.code, ErrorCode.MODEL_OUTPUT_INVALID)

    def test_rejects_human_verified_fact_status(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            _normalize_output(
                json.dumps(
                    {
                        "draft_text": "ok",
                        "candidate_facts": [
                            {
                                "key": "passport",
                                "value": "invented",
                                "status": "human_verified",
                            }
                        ],
                        "suggested_actions": ["x"],
                    }
                )
            )
        self.assertEqual(ctx.exception.code, ErrorCode.MODEL_OUTPUT_INVALID)
        self.assertIn("human_verified", str(ctx.exception))

    def test_invalid_model_json_raises(self) -> None:
        case = create_case(
            database_url=DB, tenant_id=TENANT, title="s1.4-bad"
        )
        client = FakeChatClient("not-json-at-all")
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with self.assertRaises(DomainError) as ctx:
                generate_case_draft(
                    settings=settings, case_id=case.id, client=client
                )
        self.assertEqual(ctx.exception.code, ErrorCode.MODEL_OUTPUT_INVALID)


class DraftHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        apply_migrations(DB)
        from visa_agent.fastapi_app import app, bootstrap

        bootstrap()
        self.client = TestClient(app)

    def test_api_draft_uses_injectable_path_via_patch(self) -> None:
        created = self.client.post(
            "/cases", json={"title": "wb-draft", "customer_email": "x@y.com"}
        )
        case_id = created.json()["data"]["case_id"]
        fake = FakeChatClient(_ok_payload(draft_text="Workbench draft body"))
        with unittest.mock.patch(
            "visa_agent.fastapi_app.generate_case_draft",
            side_effect=lambda **kwargs: generate_case_draft(
                settings=kwargs["settings"],
                case_id=kwargs["case_id"],
                client=fake,
            ),
        ):
            resp = self.client.post(f"/cases/{case_id}/draft")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["draft_text"], "Workbench draft body")
        self.assertTrue(data["model_call_id"])

    def test_workbench_marks_stale_draft_as_historical_after_bump(self) -> None:
        created = self.client.post(
            "/cases",
            json={"title": "wb-draft-ver", "customer_email": "x@y.com"},
        )
        case_id = created.json()["data"]["case_id"]
        fake = FakeChatClient(_ok_payload(draft_text="bound to v1"))
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            generate_case_draft(
                settings=settings, case_id=case_id, client=fake
            )
        bump_case_version(
            database_url=DB, tenant_id=TENANT, case_id=case_id
        )
        page = self.client.get(f"/workbench/cases/{case_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("历史版本", page.text)
        self.assertIn("draft_case_version=1", page.text)
        self.assertIn("尚无模型草稿", page.text)

    def test_poll_without_imap_returns_validation_error(self) -> None:
        resp = self.client.post("/internal/email/poll", json={"limit": 1})
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        self.assertEqual(detail["error"], "VALIDATION_ERROR")
