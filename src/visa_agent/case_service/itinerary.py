"""Prepare, revise and confirm the current visitor itinerary by email."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import psycopg

from visa_agent.ai_capabilities.visitor_itinerary import (
    compose_itinerary,
    draft_itinerary,
    english_itinerary,
    request_travel_details,
    revise_itinerary,
)
from visa_agent.ai_capabilities.visitor_trip import VisitorTrip
from visa_agent.case_service.service import CaseRecord, bump_case_version_in_conn, get_case
from visa_agent.config import Settings
from visa_agent.delivery.itinerary_document import PDF_MIME, itinerary_pdf
from visa_agent.domain.enums import RouteCode
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.storage.files import LocalFileStore

_LOG = logging.getLogger(__name__)
_LEGACY_PDF_LOCKED_STAGES = ("final_review", "ready_to_deliver", "sent_awaiting_ack", "delivered", "closed")


@dataclass(frozen=True, slots=True)
class ItineraryReply:
    text: str
    remaining_body: str
    attachment_id: str | None = None


@dataclass(frozen=True, slots=True)
class CaseItinerary:
    start: date | None
    days: int | None
    awaiting_details: bool
    source_event_id: str
    draft_text: str
    version: int
    draft_event_id: str | None
    evidence_id: str | None
    message_id: str | None
    confirmed: bool
    last_reply: str
    english_pdf: bool = False
    destination_cities: tuple[str, ...] = ()
    advisor_reviewed: bool = False
    departure_window: str = ""
    duration_approximate: bool = False

    @property
    def status(self) -> str:
        if not self.draft_text:
            return "awaiting_details" if self.awaiting_details else "details_recorded"
        if self.confirmed:
            return "confirmed"
        if self.advisor_reviewed:
            return "advisor_reviewed"
        return "awaiting_confirmation" if self.message_id else "draft_ready"


def _current(conn: psycopg.Connection, tenant_id: str, case_id: str) -> CaseItinerary | None:
    row = conn.execute(
        """
        SELECT start_date, duration_days, awaiting_details, source_email_event_id,
               draft_text, draft_version, draft_source_email_event_id, draft_evidence_id,
               draft_message_id, confirmed_at IS NOT NULL,
               last_reply_text, english_pdf, destination_cities,
               EXISTS (
                   SELECT 1 FROM case_evidence e
                   WHERE e.id = i.draft_evidence_id AND e.tenant_id = i.tenant_id AND e.case_id = i.case_id
                     AND e.classification = 'itinerary' AND e.deleted_at IS NULL
                     AND e.quarantine_status <> 'rejected' AND e.advisor_reviewed_version = e.case_version
               ), departure_window, duration_approximate
        FROM case_itinerary_requests i WHERE tenant_id = %s AND case_id = %s
        """,
        (tenant_id, case_id),
    ).fetchone()
    if row is None:
        return None
    *details, cities, reviewed, window, approximate = row
    return CaseItinerary(*details, tuple(cities), reviewed, window, approximate)


def get_case_itinerary(
    *, settings: Settings, case_id: str, conn: psycopg.Connection | None = None,
) -> CaseItinerary | None:
    if conn is not None:
        return _current(conn, settings.tenant_id, case_id)
    with psycopg.connect(settings.database_url) as conn:
        return _current(conn, settings.tenant_id, case_id)


def _itinerary_email_note(version: int, language: str) -> str:
    if language == "zh":
        return f"这是第 {version} 版行程单。如需调整，请直接回复这封邮件。"
    return f"This is itinerary version {version}. If you’d like any changes, reply to this email."


def _save_draft_file(
    conn: psycopg.Connection, *, settings: Settings, case: CaseRecord,
    event_id: str, previous_id: str | None, draft: str, version: int,
    confirmed: bool = False,
) -> str:
    data = itinerary_pdf(draft, version=version)
    store = LocalFileStore(settings.file_storage_dir)
    file_id, digest, _, _ = store.put_bytes_content_addressed(data, suffix=".pdf")
    if previous_id:
        conn.execute(
            """
            UPDATE case_evidence SET deleted_at = NOW()
            WHERE tenant_id = %s AND case_id = %s AND id = %s AND deleted_at IS NULL
            """,
            (settings.tenant_id, case.id, previous_id),
        )
    evidence_id = uuid.uuid4().hex
    filename = f"Itinerary-v{version:02d}.pdf"
    conn.execute(
        """
        INSERT INTO case_evidence (
            id, tenant_id, case_id, case_version, file_id, content_sha256, original_filename,
            content_type, byte_size, classification, quarantine_status, source_email_event_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'itinerary', %s, %s)
        """,
        (evidence_id, settings.tenant_id, case.id, case.case_version, file_id, digest,
         filename, PDF_MIME, len(data), "accepted_for_review" if confirmed else "isolated", event_id),
    )
    return evidence_id


def _remember_reply(
    conn: psycopg.Connection, *, tenant_id: str, case_id: str, event_id: str, reply: str,
    confirmed: bool = False,
) -> None:
    conn.execute(
        """
        UPDATE case_itinerary_requests SET source_email_event_id = %s, last_reply_text = %s,
            confirmed_at = CASE WHEN %s THEN COALESCE(confirmed_at, NOW()) ELSE confirmed_at END
        WHERE tenant_id = %s AND case_id = %s
        """,
        (event_id, reply, confirmed, tenant_id, case_id),
    )


def prepare_itinerary_reply(
    *, settings: Settings, case: CaseRecord, email_event_id: str, sender: str,
    body: str, language: str, references: tuple[str, ...] = (), reply_to: str | None = None,
    tourism: bool = False, trip: VisitorTrip | None = None,
) -> ItineraryReply | None:
    customer = normalize_email(case.customer_email)
    if case.route != RouteCode.STANDARD_VISITOR or not customer or normalize_email(sender) != customer:
        return None
    if trip is None or trip.itinerary_action == "decline":
        return None
    remaining = body.replace(trip.itinerary_quote, "", 1).strip() if trip.itinerary_quote else body
    action = trip.itinerary_action
    zh = language == "zh"
    with psycopg.connect(settings.database_url) as conn:
        previous = _current(conn, settings.tenant_id, case.id)
        replying_to_current = bool(previous and previous.message_id and (
            previous.message_id == reply_to or previous.message_id in references
        ))
        if previous and previous.source_event_id == email_event_id and previous.last_reply:
            attachment = previous.evidence_id if previous.draft_event_id == email_event_id else None
            return ItineraryReply(previous.last_reply, remaining, attachment)

        if previous and previous.draft_text:
            if action not in {"request", "revise", "confirm"}:
                return None
            if not replying_to_current:
                reply = (
                    "请直接回复当前行程单邮件，以便我核对您要确认或修改的是哪份行程。"
                    if zh else "Please reply directly to the current itinerary email to confirm or change it."
                )
                _remember_reply(conn, tenant_id=settings.tenant_id, case_id=case.id,
                                event_id=email_event_id, reply=reply)
                return ItineraryReply(reply, remaining)
            if action == "confirm":
                reply = (f"已记录您确认第 {previous.version} 版行程单。" if zh
                         else f"Your confirmation of itinerary version {previous.version} is recorded.")
                _remember_reply(conn, tenant_id=settings.tenant_id, case_id=case.id,
                                event_id=email_event_id, reply=reply, confirmed=True)
                if previous.evidence_id and not (previous.confirmed or previous.advisor_reviewed):
                    updated = bump_case_version_in_conn(
                        conn=conn, database_url=settings.database_url, tenant_id=settings.tenant_id,
                        case_id=case.id, expected_case_version=case.case_version, required=True,
                    )
                    conn.execute(
                        """
                        UPDATE case_evidence SET quarantine_status = 'accepted_for_review', case_version = %s
                        WHERE tenant_id = %s AND case_id = %s AND id = %s
                          AND deleted_at IS NULL AND quarantine_status = 'isolated'
                        """,
                        (updated.case_version, settings.tenant_id, case.id, previous.evidence_id),
                    )
                return ItineraryReply(reply, remaining)
        elif action == "confirm":
            return None

        request_body = trip.itinerary_quote or body
        old_start, old_days = (previous.start, previous.days) if previous else (None, None)
        departure_stated = trip.departure is not None or bool(trip.departure_quote)
        duration_stated = trip.days is not None or bool(trip.duration_quote)
        start = trip.departure if departure_stated else old_start
        days = trip.days if duration_stated else old_days
        window = trip.departure_quote if departure_stated else (previous.departure_window if previous else "")
        if start:
            window = ""
        approximate = trip.approximate if duration_stated else bool(previous and previous.duration_approximate)
        requested = action in {"request", "revise"}
        active_itinerary = None
        has_draft = bool(previous and previous.draft_text)
        if not has_draft and not requested:
            active_itinerary = conn.execute(
                """SELECT 1 FROM case_evidence WHERE tenant_id = %s AND case_id = %s
                   AND classification = 'itinerary' AND deleted_at IS NULL
                   AND quarantine_status <> 'rejected' LIMIT 1""",
                (settings.tenant_id, case.id),
            ).fetchone()
        has_details = departure_stated or duration_stated
        can_prepare = not (has_draft or active_itinerary or case.human_takeover or case.paused or case.cancelled)
        auto_plan = tourism and can_prepare and (has_details or trip.purpose == "tourism")
        waiting_for_details = bool(previous and previous.awaiting_details and has_details)
        should_prepare = requested or auto_plan or waiting_for_details
        if not (should_prepare or (can_prepare and has_details)):
            return None

        cities = trip.cities or (previous.destination_cities if previous else ())
        if start and days and date.max - start < timedelta(days=days - 1):
            start = None

        draft = ""
        if previous and previous.draft_text:
            if start is None or days is None:
                reply = request_travel_details(start, days, language)
            else:
                try:
                    previous_english = english_itinerary(previous.draft_text)
                    if not previous.evidence_id and action == "request":
                        draft = previous_english
                    else:
                        draft = revise_itinerary(
                            settings=settings, previous=previous_english, start=start, days=days,
                            request=request_body,
                            calendar_changed=(start, days) != (old_start, old_days),
                        ) or ""
                except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
                    _LOG.warning("Cannot convert legacy itinerary for revision")
                reply = ("请明确希望修改哪一日、地点或活动，我会保留当前行程供您核对。" if zh
                         else "Please specify the date, place, or activity to change. The current itinerary remains available.")
                if draft == previous.draft_text:
                    draft = ""
            if not draft:
                _remember_reply(conn, tenant_id=settings.tenant_id, case_id=case.id,
                                event_id=email_event_id, reply=reply)
                return ItineraryReply(reply, remaining)
        elif should_prepare and start is not None and days is not None:
            draft = draft_itinerary(settings=settings, start=start, days=days, cities=cities, request=request_body)
            if draft is None:
                draft = compose_itinerary(start, days, cities=cities)

        version = (previous.version if previous else 0) + bool(draft)
        attachment = None
        if draft:
            updated = bump_case_version_in_conn(
                conn=conn, database_url=settings.database_url, tenant_id=settings.tenant_id,
                case_id=case.id, expected_case_version=case.case_version, required=True,
            )
            attachment = _save_draft_file(
                conn, settings=settings, case=updated, event_id=email_event_id,
                previous_id=previous.evidence_id if previous else None, draft=draft,
                version=version,
            )
            end = start + timedelta(days=days - 1)
            length_zh = f"约 {days} 天" if approximate else f"{days} 天"
            length_en = f"about {days} days" if approximate else f"{days} days"
            summary = (f"附件是您的 PDF 行程单，{length_zh}，{start} 至 {end}。"
                       if zh else f"Your PDF itinerary for {length_en}, {start} to {end}, is attached.")
            reply = summary + "\n\n" + _itinerary_email_note(version, language)
        elif should_prepare:
            reply = request_travel_details(start, days, language)
        else:
            reply = ""
        conn.execute(
            """
            INSERT INTO case_itinerary_requests
                (tenant_id, case_id, start_date, duration_days, awaiting_details, source_email_event_id,
                 draft_text, draft_version, draft_source_email_event_id, draft_evidence_id, last_reply_text,
                 english_pdf, destination_cities, departure_window, duration_approximate)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, case_id) DO UPDATE SET
                start_date = EXCLUDED.start_date,
                duration_days = EXCLUDED.duration_days,
                awaiting_details = EXCLUDED.awaiting_details,
                source_email_event_id = EXCLUDED.source_email_event_id,
                draft_text = EXCLUDED.draft_text,
                draft_version = EXCLUDED.draft_version,
                draft_source_email_event_id = EXCLUDED.draft_source_email_event_id,
                draft_evidence_id = EXCLUDED.draft_evidence_id,
                draft_message_id = NULL,
                confirmed_at = NULL,
                last_reply_text = EXCLUDED.last_reply_text,
                english_pdf = EXCLUDED.english_pdf,
                destination_cities = EXCLUDED.destination_cities,
                departure_window = EXCLUDED.departure_window,
                duration_approximate = EXCLUDED.duration_approximate
            """,
            (settings.tenant_id, case.id, start, days, should_prepare and not draft, email_event_id,
             draft, version, email_event_id if draft else None, attachment, reply, bool(draft), list(cities),
             window, approximate),
        )
    return ItineraryReply(reply, remaining, attachment) if reply else None


def attach_existing_itinerary_pdfs(settings: Settings) -> int:
    """Archive English PDFs for existing active itineraries without sending another email."""
    with psycopg.connect(settings.database_url) as conn:
        legacy = conn.execute(
            """
            SELECT i.case_id FROM case_itinerary_requests i
            JOIN cases c ON c.tenant_id = i.tenant_id AND c.id = i.case_id
            WHERE i.tenant_id = %s AND i.draft_text <> '' AND NOT i.english_pdf
              AND NOT (c.status = ANY(%s))
            """,
            (settings.tenant_id, list(_LEGACY_PDF_LOCKED_STAGES)),
        ).fetchall()
    converted = 0
    for (case_id,) in legacy:
        prior = get_case_itinerary(settings=settings, case_id=case_id)
        if not prior or not prior.draft_text or prior.english_pdf:
            continue
        try:
            draft = english_itinerary(prior.draft_text)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
            _LOG.warning("English PDF conversion is pending for an existing itinerary")
            continue
        with psycopg.connect(settings.database_url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         (f"customer-inbound:{settings.tenant_id}:{case_id}",))
            if prior != _current(conn, settings.tenant_id, case_id):
                continue
            keys = dict(database_url=settings.database_url, tenant_id=settings.tenant_id, case_id=case_id)
            case = get_case(**keys, conn=conn, for_update=True)
            if case.status.value in _LEGACY_PDF_LOCKED_STAGES:
                continue
            if prior.evidence_id and conn.execute(
                """
                SELECT 1 FROM outbox_messages
                WHERE tenant_id = %s AND case_id = %s AND attachment_evidence_id = %s
                    AND status IN ('pending', 'processing', 'failed', 'blocked')
                LIMIT 1
                """, (settings.tenant_id, case_id, prior.evidence_id),
            ).fetchone():
                continue
            updated = bump_case_version_in_conn(
                **keys, conn=conn, expected_case_version=case.case_version, required=True,
            )
            evidence_id = _save_draft_file(
                conn, settings=settings, case=updated, event_id=prior.draft_event_id or prior.source_event_id,
                previous_id=prior.evidence_id, draft=draft, version=prior.version, confirmed=prior.confirmed,
            )
            conn.execute(
                """
                UPDATE case_itinerary_requests SET draft_evidence_id = %s, draft_text = %s, english_pdf = TRUE
                WHERE tenant_id = %s AND case_id = %s
                """,
                (evidence_id, draft, settings.tenant_id, case_id),
            )
            converted += 1
    return converted


def record_itinerary_sent(
    *, settings: Settings, case_id: str, inbound_event_id: str, send_operation_id: str,
) -> None:
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            UPDATE case_itinerary_requests AS r
            SET draft_message_id = o.rfc_message_id
            FROM outbox_messages o
            WHERE r.tenant_id = %s AND r.case_id = %s AND r.draft_source_email_event_id = %s
              AND o.tenant_id = r.tenant_id AND o.case_id = r.case_id
              AND o.send_operation_id = %s AND o.status = 'provider_accepted'
              AND o.rfc_message_id IS NOT NULL
            """,
            (settings.tenant_id, case_id, inbound_event_id, send_operation_id),
        )
