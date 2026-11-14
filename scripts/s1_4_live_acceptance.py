#!/usr/bin/env python3
"""S1.4 live acceptance: real model draft + allowlisted SMTP.

Receive half is confirmed manually in the allowlisted inbox (no IMAP required).

Usage:
  cd LSH
  LSH_LIVE_S1_4=1 PYTHONPATH=src python3 scripts/s1_4_live_acceptance.py

Requires .env with SiliconFlow + SMTP + EMAIL_OUTBOUND_ALLOWLIST.
Writes evidence under data/s1_4_evidence/.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visa_agent.ai_capabilities import generate_case_draft  # noqa: E402
from visa_agent.case_service import create_case  # noqa: E402
from visa_agent.config import get_settings  # noqa: E402
from visa_agent.email_adapter import OutboxRequest, enqueue_and_maybe_send  # noqa: E402
from visa_agent.storage.migrate import apply_migrations  # noqa: E402


def main() -> int:
    if os.environ.get("LSH_LIVE_S1_4") != "1":
        print("Set LSH_LIVE_S1_4=1 to run live acceptance (refuses by default).")
        return 2

    settings = get_settings()
    apply_migrations(settings.database_url)
    evidence_dir = ROOT / "data" / "s1_4_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if not settings.model_configured:
        print("FAIL: model not configured")
        return 1
    if not settings.email_configured:
        print("FAIL: SMTP not configured")
        return 1
    if not settings.outbound_allowlist:
        print("FAIL: outbound allowlist empty")
        return 1

    recipient = sorted(settings.outbound_allowlist)[0]
    case = create_case(
        database_url=settings.database_url,
        tenant_id=settings.tenant_id,
        title=f"S1.4 live {token}",
        customer_email=recipient,
    )
    print(f"case_id={case.id}")

    draft = generate_case_draft(settings=settings, case_id=case.id)
    print(f"model_call_id={draft.model_call_id}")
    print(f"draft_chars={len(draft.draft_text)}")

    subject = f"[LSH-S1.4] live round-trip {token}"
    body = (
        f"S1.4 allowlisted live send.\n"
        f"token={token}\n"
        f"case_id={case.id}\n"
        f"model_call_id={draft.model_call_id}\n\n"
        f"--- draft excerpt ---\n"
        f"{draft.draft_text[:800]}\n"
    )
    out = enqueue_and_maybe_send(
        settings=settings,
        req=OutboxRequest(
            kind="ordinary_reply",
            recipients=(recipient,),
            subject=subject,
            body_text=body,
            send_operation_id=f"s1-4-live-{token}",
            send_now=True,
        ),
    )
    print(f"outbox status={out.status} reasons={out.block_reasons}")
    if out.status != "provider_accepted" or out.block_reasons:
        print("FAIL: outbound not accepted")
        return 1

    print(
        f"MANUAL RECEIVE CHECK: open {recipient} and confirm subject "
        f"contains token {token}"
    )

    evidence = {
        "stamp": stamp,
        "token": token,
        "case_id": case.id,
        "recipient": recipient,
        "model_call_id": draft.model_call_id,
        "prompt_digest": draft.prompt_digest,
        "draft_excerpt": draft.draft_text[:500],
        "outbox_id": out.outbox_id,
        "outbox_status": out.status,
        "provider_response": out.provider_response,
        "email_event_id": out.email_event_id,
        "receive_check": "manual_allowlist_inbox",
        "subject": subject,
    }
    path = evidence_dir / f"{stamp}_{token}.json"
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"evidence={path}")
    print("PASS: model draft + allowlisted SMTP accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
