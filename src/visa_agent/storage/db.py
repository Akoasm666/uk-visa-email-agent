"""PostgreSQL connectivity helpers.

Prefer psycopg when installed; fall back to ``psql`` CLI so S1.1 can verify
Docker Postgres without PyPI packages.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class DbHealth:
    ok: bool
    detail: str
    via: str


def _parse_url(database_url: str) -> dict[str, str | int]:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError(f"unsupported database url scheme: {parsed.scheme}")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "user": parsed.username or "",
        "password": parsed.password or "",
        "dbname": (parsed.path or "/").lstrip("/") or "postgres",
    }


def check_database(database_url: str) -> DbHealth:
    try:
        parts = _parse_url(database_url)
    except ValueError as exc:
        return DbHealth(ok=False, detail=str(exc), via="parse")

    try:
        import psycopg  # type: ignore

        with psycopg.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return DbHealth(ok=True, detail="SELECT 1 ok", via="psycopg")
    except Exception as psycopg_exc:
        psql = shutil.which("psql")
        if not psql:
            return DbHealth(
                ok=False,
                detail=f"psycopg unavailable ({psycopg_exc!s}); psql not found",
                via="none",
            )
        env = os.environ.copy()
        env["PGPASSWORD"] = str(parts["password"])
        cmd = [
            psql,
            "-h",
            str(parts["host"]),
            "-p",
            str(parts["port"]),
            "-U",
            str(parts["user"]),
            "-d",
            str(parts["dbname"]),
            "-c",
            "SELECT 1;",
            "-v",
            "ON_ERROR_STOP=1",
            "-tA",
        ]
        try:
            completed = subprocess.run(
                cmd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as cli_exc:
            return DbHealth(
                ok=False,
                detail=f"psycopg={psycopg_exc!s}; psql={cli_exc!s}",
                via="psql",
            )
        if completed.returncode == 0 and completed.stdout.strip() == "1":
            return DbHealth(ok=True, detail="SELECT 1 ok", via="psql")
        return DbHealth(
            ok=False,
            detail=(completed.stderr or completed.stdout or "psql failed").strip(),
            via="psql",
        )
