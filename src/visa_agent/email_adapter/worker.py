"""Standalone IMAP receiver using the same pipeline as the internal poll API."""

from __future__ import annotations

import argparse
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import psycopg

from visa_agent.case_service.closure import resume_closure_notices
from visa_agent.case_service.itinerary import attach_existing_itinerary_pdfs
from visa_agent.case_service.material_confirmation import resume_material_confirmations
from visa_agent.case_service.translations import prepare_pending_translations
from visa_agent.case_service.visitor_purpose import backfill_visitor_purposes
from visa_agent.config import Settings, get_settings
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.imap_poll import poll_imap_inbox
from visa_agent.rules import ensure_seed_route_packs
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations

_LOG = logging.getLogger(__name__)


def _prepare_translations(settings: Settings) -> None:
    try:
        prepare_pending_translations(settings)
    except (OSError, psycopg.Error) as exc:
        _LOG.warning("Cannot prepare translation draft (%s)", type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Use a specified configuration file")
    parser.add_argument("--once", action="store_true", help="Run one poll and exit")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between polls")
    parser.add_argument("--mailbox", default="INBOX", help="IMAP folder name")
    parser.add_argument("--limit", type=int, default=10, help="Messages per poll (1–50)")
    args = parser.parse_args(argv)
    if args.interval < 1 or not 1 <= args.limit <= 50:
        parser.error("--interval must be positive and --limit must be between 1 and 50")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = get_settings(args.env_file)
    except (OSError, ValueError):
        _LOG.error("Cannot load the receiver configuration. Check the configured file and ports.")
        return 2
    required_imap_fields = (
        ("EMAIL_IMAP_HOST", settings.email_imap_host),
        ("EMAIL_IMAP_USER", settings.email_imap_user),
        ("EMAIL_IMAP_PASSWORD", settings.email_imap_password),
    )
    missing = [name for name, value in required_imap_fields if not value]
    if missing:
        _LOG.error("Set %s in the receiver configuration", ", ".join(missing))
        return 2

    try:
        settings.file_storage_dir.mkdir(parents=True, exist_ok=True)
        apply_migrations(settings.database_url)
        ensure_seed_route_packs(database_url=settings.database_url)
        backfill_visitor_purposes(settings)
        converted = attach_existing_itinerary_pdfs(settings)
        if converted:
            _LOG.info("Existing itinerary PDFs attached, count=%d", converted)
    except (OSError, psycopg.Error) as exc:
        _LOG.error("Cannot initialize receiver storage (%s)", type(exc).__name__)
        return 1
    store = LocalFileStore(settings.file_storage_dir)
    if not settings.email_configured:
        _LOG.warning("SMTP configuration is incomplete. Automatic replies cannot be sent.")
    _LOG.info("IMAP receiver started")

    stopping = Event()

    def stop(_signal: int, _frame: object) -> None:
        stopping.set()

    if not args.once:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    translation_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translation")
    translation_run = None
    while not stopping.is_set():
        failed = False
        try:
            result = poll_imap_inbox(
                settings=settings,
                file_store=store,
                limit=args.limit,
                mailbox=args.mailbox,
            )
            failed = not result.success
            if result.count or result.failed_count:
                log = _LOG.warning if failed else _LOG.info
                log("IMAP poll completed, received=%d failed=%d", result.count, result.failed_count)
                for failure in result.failures:
                    _LOG.warning("IMAP message processing raised an error for UID %s", failure.uid)
        except DomainError as exc:
            failed = True
            cause = exc.__cause__
            _LOG.error("IMAP poll failed (%s)", type(cause).__name__ if cause else exc.code.value)
        try:
            resume_material_confirmations(settings)
        except psycopg.Error as exc:
            _LOG.warning("Cannot resume material confirmations (%s)", type(exc).__name__)
        try:
            resume_closure_notices(settings)
        except (DomainError, psycopg.Error) as exc:
            _LOG.warning("Cannot resume case completion notices (%s)", type(exc).__name__)
        if args.once:
            translation_worker.shutdown()
            _prepare_translations(settings)
            return int(failed)
        if translation_run is None or translation_run.done():
            if translation_run and (failure := translation_run.exception()):
                _LOG.warning("Translation task stopped (%s)", type(failure).__name__)
            translation_run = translation_worker.submit(_prepare_translations, settings)
        stopping.wait(args.interval)
    translation_worker.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
