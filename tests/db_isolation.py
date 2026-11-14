"""Isolated Postgres database for tests that must not mutate the demo DB."""

from __future__ import annotations

import psycopg

from visa_agent.storage.migrate import apply_migrations

ADMIN_URL = "postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/visa_agent"
TEST_DB_NAME = "visa_agent_test"
TEST_DB = (
    f"postgresql://visa_agent:visa_agent_dev@127.0.0.1:15432/{TEST_DB_NAME}"
)

# Test-only publish credential (never the production default — prod default is empty).
TEST_PUBLISH_TOKEN = "test-rule-publish-token"


def ensure_test_database() -> str:
    """Create visa_agent_test if missing; apply schema; return URL."""
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (TEST_DB_NAME,),
        ).fetchone()
        if row is None:
            conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    apply_migrations(TEST_DB)
    return TEST_DB


def reset_test_database() -> str:
    """Truncate app tables in the isolated test DB (keep schema)."""
    url = ensure_test_database()
    with psycopg.connect(url) as conn:
        with conn.transaction():
            conn.execute(
                """
                DO $$
                DECLARE r RECORD;
                BEGIN
                  FOR r IN (
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public'
                  ) LOOP
                    EXECUTE format(
                      'TRUNCATE TABLE %I RESTART IDENTITY CASCADE',
                      r.tablename
                    );
                  END LOOP;
                END $$;
                """
            )
    return url
