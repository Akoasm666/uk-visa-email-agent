from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from visa_agent.config import Settings, get_settings
from visa_agent.http_app import build_health
from visa_agent.storage.files import LocalFileStore


class ConfigTests(unittest.TestCase):
    def test_allowlist_empty_by_default(self) -> None:
        with patch.dict(
            "os.environ",
            {"EMAIL_OUTBOUND_ALLOWLIST": "", "SILICONFLOW_API_KEY": "x"},
            clear=False,
        ):
            # Force reload path without requiring real .env keys for allowlist.
            settings = Settings(
                app_host="127.0.0.1",
                app_port=8000,
                database_url="postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent",
                file_storage_dir=Path("data/files"),
                tenant_id="demo",
                siliconflow_api_key="x",
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
                rule_publish_token="",
            )
            self.assertEqual(settings.outbound_allowlist, frozenset())
            self.assertFalse(settings.email_configured)
            self.assertTrue(settings.model_configured)
            self.assertFalse(settings.rule_publish_enabled)

    def test_loads_dotenv_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "APP_PORT=8123\nEMAIL_OUTBOUND_ALLOWLIST=\nTENANT_ID=t-demo\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = get_settings(env_path)
            self.assertEqual(settings.app_port, 8123)
            self.assertEqual(settings.tenant_id, "t-demo")
            self.assertEqual(settings.outbound_allowlist, frozenset())


class FileStoreTests(unittest.TestCase):
    def test_put_bytes_writes_file_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            file_id, digest, path = store.put_bytes(b"hello", suffix=".txt")
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), b"hello")
            self.assertEqual(len(file_id), 32)
            self.assertEqual(len(digest), 64)


class HealthPayloadTests(unittest.TestCase):
    def test_health_includes_email_smtp_and_model_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                app_host="127.0.0.1",
                app_port=8000,
                database_url="postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent",
                file_storage_dir=Path(tmp),
                tenant_id="demo",
                siliconflow_api_key="k",
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
                rule_publish_token="",
            )
            payload = build_health(settings)
            self.assertTrue(payload["success"])
            data = payload["data"]
            self.assertEqual(data["email"]["protocol"], "smtp")
            self.assertEqual(data["email"]["allowlist_count"], 0)
            self.assertTrue(data["model"]["configured"])
            self.assertTrue(data["file_storage"]["ok"])
            # DB may be up or down depending on Docker; field must exist.
            self.assertIn("ok", data["database"])


if __name__ == "__main__":
    unittest.main()
