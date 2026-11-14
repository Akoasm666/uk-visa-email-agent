"""SMTP send via configured relay (Brevo)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from smtplib import (
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
)

from visa_agent.config import Settings
from visa_agent.email_adapter.errors import SendStatusUnknownError


class SendRejectedError(RuntimeError):
    """Provider clearly refused the message; safe to mark failed and allow retry."""


def _format_accepted(message_id: str, *, quit_note: str | None = None) -> str:
    if quit_note:
        return f"{message_id};{quit_note}"
    return message_id


def send_smtp(
    settings: Settings,
    *,
    recipients: list[str],
    subject: str,
    body_text: str,
) -> str:
    if not settings.email_configured:
        raise RuntimeError("SMTP is not configured")
    if len(recipients) != 1:
        raise RuntimeError(
            "S1 demo allows exactly one recipient per send operation "
            f"(got {len(recipients)})"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = recipients[0]
    msg.set_content(body_text)

    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        context = ssl.create_default_context()

    # Track accept before the context __exit__ runs QUIT. A post-accept QUIT
    # failure (e.g. 421) must not rewrite the outcome to rejected/unknown.
    accepted_id: str | None = None

    try:
        with smtplib.SMTP(
            settings.email_smtp_host, settings.email_smtp_port, timeout=30
        ) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings.email_smtp_user, settings.email_smtp_password)
            try:
                failed = smtp.send_message(msg)
            except SMTPRecipientsRefused as exc:
                # Must run before OSError: SMTPException subclasses OSError on 3.x.
                raise SendRejectedError(
                    f"SMTP recipients refused: {exc.recipients}"
                ) from exc
            except SMTPResponseException as exc:
                # Explicit provider codes during DATA/RCPT (e.g. 451) — retryable.
                raise SendRejectedError(
                    f"SMTP rejected with {exc.smtp_code}: {exc.smtp_error!r}"
                ) from exc
            except SMTPServerDisconnected as exc:
                raise SendStatusUnknownError(
                    f"smtp result uncertain during/after send: {exc}"
                ) from exc
            except (TimeoutError, ConnectionError) as exc:
                raise SendStatusUnknownError(
                    f"smtp result uncertain during/after send: {exc}"
                ) from exc
            except OSError as exc:
                raise SendStatusUnknownError(
                    f"smtp result uncertain during/after send: {exc}"
                ) from exc

            if failed:
                raise SendRejectedError(f"SMTP recipients refused: {failed}")
            accepted_id = msg.get("Message-ID") or "smtp-accepted"
            # Leaving the `with` block issues QUIT via SMTP.__exit__.
    except (SendStatusUnknownError, SendRejectedError):
        raise
    except SMTPRecipientsRefused as exc:
        if accepted_id is not None:
            return _format_accepted(
                accepted_id, quit_note=f"quit_recipients_refused:{exc.recipients}"
            )
        raise SendRejectedError(
            f"SMTP recipients refused: {exc.recipients}"
        ) from exc
    except SMTPResponseException as exc:
        if accepted_id is not None:
            # Mail already accepted; record QUIT/close code without failing the op.
            return _format_accepted(
                accepted_id,
                quit_note=f"quit:{exc.smtp_code}:{exc.smtp_error!r}",
            )
        raise SendRejectedError(
            f"SMTP rejected with {exc.smtp_code}: {exc.smtp_error!r}"
        ) from exc
    except SMTPServerDisconnected as exc:
        if accepted_id is not None:
            return _format_accepted(
                accepted_id, quit_note=f"quit_disconnect:{exc}"
            )
        raise SendStatusUnknownError(
            f"smtp connection lost while waiting for confirmation: {exc}"
        ) from exc
    except (TimeoutError, ConnectionError) as exc:
        if accepted_id is not None:
            return _format_accepted(accepted_id, quit_note=f"quit_error:{exc}")
        raise SendStatusUnknownError(
            f"smtp connection lost while waiting for confirmation: {exc}"
        ) from exc
    except OSError as exc:
        if accepted_id is not None:
            return _format_accepted(accepted_id, quit_note=f"quit_error:{exc}")
        raise SendStatusUnknownError(
            f"smtp connection lost while waiting for confirmation: {exc}"
        ) from exc
    except SMTPException:
        raise

    assert accepted_id is not None
    return accepted_id
