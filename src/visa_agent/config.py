"""Environment configuration for S1.

File-backed keys are read from the .env file only (including empty values).
They are never written back into ``os.environ``, so removing the file cannot
leave stale allowlist values active via the process environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = ROOT / ".env"

_FILE_BACKED_KEYS = frozenset(
    {
        "APP_HOST",
        "APP_PORT",
        "DATABASE_URL",
        "FILE_STORAGE_DIR",
        "TENANT_ID",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MODEL",
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_PORT",
        "EMAIL_SMTP_USER",
        "EMAIL_SMTP_PASSWORD",
        "EMAIL_FROM",
        "EMAIL_IMAP_HOST",
        "EMAIL_IMAP_PORT",
        "EMAIL_IMAP_USER",
        "EMAIL_IMAP_PASSWORD",
        "EMAIL_OUTBOUND_ALLOWLIST",
    }
)


def parse_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _split_allowlist(raw: str) -> frozenset[str]:
    if not raw.strip():
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _value(file_vals: dict[str, str], key: str, default: str = "") -> str:
    if key in file_vals:
        return file_vals[key]
    # File-backed keys never fall back to process environment.
    if key in _FILE_BACKED_KEYS:
        return default
    return default


@dataclass(frozen=True, slots=True)
class Settings:
    app_host: str
    app_port: int
    database_url: str
    file_storage_dir: Path
    tenant_id: str
    siliconflow_api_key: str
    siliconflow_base_url: str
    siliconflow_model: str
    email_smtp_host: str
    email_smtp_port: int
    email_smtp_user: str
    email_smtp_password: str
    email_from: str
    email_imap_host: str
    email_imap_port: int
    email_imap_user: str
    email_imap_password: str
    outbound_allowlist: frozenset[str]

    @property
    def model_configured(self) -> bool:
        return bool(self.siliconflow_api_key)

    @property
    def email_configured(self) -> bool:
        return bool(
            self.email_smtp_host
            and self.email_smtp_user
            and self.email_smtp_password
            and self.email_from
        )

    @property
    def imap_configured(self) -> bool:
        return bool(
            self.email_imap_host
            and self.email_imap_user
            and self.email_imap_password
        )


def get_settings(env_path: Path | None = None) -> Settings:
    path = env_path or DEFAULT_ENV_PATH
    file_vals = parse_dotenv_file(path)

    storage = Path(
        _value(file_vals, "FILE_STORAGE_DIR", str(ROOT / "data" / "files"))
    ).expanduser()
    return Settings(
        app_host=_value(file_vals, "APP_HOST", "127.0.0.1"),
        app_port=int(_value(file_vals, "APP_PORT", "8000")),
        database_url=_value(
            file_vals,
            "DATABASE_URL",
            "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent",
        ),
        file_storage_dir=storage,
        tenant_id=_value(file_vals, "TENANT_ID", "demo"),
        siliconflow_api_key=_value(file_vals, "SILICONFLOW_API_KEY", ""),
        siliconflow_base_url=_value(
            file_vals, "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
        ).rstrip("/"),
        siliconflow_model=_value(
            file_vals, "SILICONFLOW_MODEL", "zai-org/GLM-5.3-Flash"
        ),
        email_smtp_host=_value(file_vals, "EMAIL_SMTP_HOST", ""),
        email_smtp_port=int(_value(file_vals, "EMAIL_SMTP_PORT", "587")),
        email_smtp_user=_value(file_vals, "EMAIL_SMTP_USER", ""),
        email_smtp_password=_value(file_vals, "EMAIL_SMTP_PASSWORD", ""),
        email_from=_value(file_vals, "EMAIL_FROM", ""),
        email_imap_host=_value(file_vals, "EMAIL_IMAP_HOST", ""),
        email_imap_port=int(_value(file_vals, "EMAIL_IMAP_PORT", "993")),
        email_imap_user=_value(file_vals, "EMAIL_IMAP_USER", ""),
        email_imap_password=_value(file_vals, "EMAIL_IMAP_PASSWORD", ""),
        outbound_allowlist=_split_allowlist(
            _value(file_vals, "EMAIL_OUTBOUND_ALLOWLIST", "")
        ),
    )
