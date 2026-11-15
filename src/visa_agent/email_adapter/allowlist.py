"""Outbound allowlist: static .env union dynamic DB entries (case emails)."""

from __future__ import annotations

import re

import psycopg

from visa_agent.config import Settings

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str | None) -> str | None:
    email = (value or "").strip().lower()
    if not email or "@" not in email:
        return None
    if "<" in email and ">" in email:
        email = email[email.rfind("<") + 1 : email.rfind(">")].strip()
    if not _EMAIL_RE.match(email):
        return None
    return email


def list_db_allowlist(
    *,
    database_url: str,
    tenant_id: str,
) -> frozenset[str]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT email FROM outbound_allowlist_entries
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        ).fetchall()
    return frozenset(str(r[0]).strip().lower() for r in rows if r[0])


def effective_outbound_allowlist(settings: Settings) -> frozenset[str]:
    """``.env`` allowlist ∪ DB entries for this tenant."""
    static = settings.outbound_allowlist or frozenset()
    try:
        dynamic = list_db_allowlist(
            database_url=settings.database_url,
            tenant_id=settings.tenant_id,
        )
    except Exception:  # noqa: BLE001
        # Table missing mid-migrate / tests without migrate — fall back to static.
        return frozenset(static)
    return frozenset(static) | dynamic


def add_outbound_allowlist_email(
    *,
    database_url: str,
    tenant_id: str,
    email: str,
    source: str = "case_create",
    case_id: str | None = None,
) -> str | None:
    """Insert customer email into DB allowlist. Returns normalized email or None."""
    norm = normalize_email(email)
    if not norm:
        return None
    src = (source or "case_create").strip() or "case_create"
    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO outbound_allowlist_entries (
                tenant_id, email, source, case_id
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, email) DO UPDATE
            SET case_id = COALESCE(EXCLUDED.case_id, outbound_allowlist_entries.case_id),
                source = outbound_allowlist_entries.source
            """,
            (tenant_id, norm, src, case_id),
        )
        conn.commit()
    return norm
