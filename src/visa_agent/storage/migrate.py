from __future__ import annotations

from pathlib import Path

import psycopg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def apply_migrations(database_url: str) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(database_url) as conn:
        conn.execute(sql)
        conn.commit()
