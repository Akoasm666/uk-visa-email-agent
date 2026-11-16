"""Send through the configured SMTP server over STARTTLS."""

from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from smtplib import (
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
)

from visa_agent.config import Settings
from visa_agent.email_adapter.errors import (
    SendPartialAcceptedError,
    SendStatusUnknownError,
)
from visa_agent.email_adapter.proxy import SocksSMTP
from visa_agent.email_adapter.tls import mail_tls_context


class SendRejectedError(RuntimeError):
    """Provider clearly refused the message; safe to mark failed and allow retry."""


def _format_accepted(message_id: str, *, quit_note: str | None = None) -> str:
    if quit_note:
        return f"{message_id};{quit_note}"
    return message_id


def _annotate_partial(
    err: SendPartialAcceptedError, *, quit_note: str
) -> SendPartialAcceptedError:
    return SendPartialAcceptedError(
        accepted=list(err.accepted),
        refused=dict(err.refused),
        provider_response=f"{err.provider_response};{quit_note}",
    )


def send_smtp(
    settings: Settings,
    *,
    recipients: list[str],
    subject: str,
    body_text: str,
    attachments: list[tuple[str, bytes]] | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    body_html: str | None = None,
) -> str:
    if not settings.email_configured:
        raise RuntimeError("SMTP is not configured")
    cleaned = [r.strip() for r in recipients if r and r.strip()]
    if not cleaned:
        raise RuntimeError("recipients required")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(cleaned)
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    for filename, data in attachments or []:
        content_type, _ = mimetypes.guess_type(filename)
        if content_type not in {"application/pdf", "image/jpeg", "image/png"}:
            content_type = "application/octet-stream"
        maintype, subtype = content_type.split("/")
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )

    context = mail_tls_context()

    # Track outcomes before context __exit__ runs QUIT. A post-accept / post-partial
    # QUIT failure (e.g. 421) must not rewrite the outcome to rejected/failed.
    accepted_id: str | None = None
    partial_error: SendPartialAcceptedError | None = None

    try:
        endpoint = (settings.email_smtp_host, settings.email_smtp_port)
        connection = (
            SocksSMTP(*endpoint, proxy_url=settings.email_proxy_url, timeout=30)
            if settings.email_proxy_url
            else smtplib.SMTP(*endpoint, timeout=30)
        )
        with connection as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings.email_smtp_user, settings.email_smtp_password)
            try:
                failed = smtp.send_message(msg)
            except SMTPRecipientsRefused as exc:
                # RCPT-stage refusal: DATA/body was never sent. Always retryable
                # failed — never partial_accepted (even if some RCPT codes looked OK).
                raise SendRejectedError(
                    f"SMTP recipients refused before DATA: {exc.recipients}"
                ) from exc
            except SMTPResponseException as exc:
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
            else:
                # send_message returned: DATA was accepted for at least one recipient.
                # A non-empty failed dict is post-DATA partial refusal only.
                if failed:
                    refused_keys = {str(k).strip().lower() for k in failed}
                    accepted = [
                        r for r in cleaned if r.strip().lower() not in refused_keys
                    ]
                    if accepted:
                        mid = msg.get("Message-ID") or "smtp-partial"
                        # Stash partial — raise only after leaving the `with` block
                        # so QUIT 421 cannot replace it with SendRejectedError.
                        partial_error = SendPartialAcceptedError(
                            accepted=accepted,
                            refused={str(k): v for k, v in failed.items()},
                            provider_response=(
                                f"{mid};partial_accepted={accepted};"
                                f"refused={list(failed.keys())}"
                            ),
                        )
                    else:
                        raise SendRejectedError(
                            f"SMTP recipients refused: {failed}"
                        )
                else:
                    accepted_id = msg.get("Message-ID") or "smtp-accepted"
            # Leaving the `with` block issues QUIT via SMTP.__exit__.
    except (SendStatusUnknownError, SendRejectedError):
        raise
    except SMTPRecipientsRefused as exc:
        if partial_error is not None:
            raise _annotate_partial(
                partial_error,
                quit_note=f"quit_recipients_refused:{exc.recipients}",
            ) from None
        if accepted_id is not None:
            return _format_accepted(
                accepted_id, quit_note=f"quit_recipients_refused:{exc.recipients}"
            )
        raise SendRejectedError(
            f"SMTP recipients refused: {exc.recipients}"
        ) from exc
    except SMTPResponseException as exc:
        if partial_error is not None:
            # Partial already determined; QUIT/close noise stays on the note only.
            raise _annotate_partial(
                partial_error,
                quit_note=f"quit:{exc.smtp_code}:{exc.smtp_error!r}",
            ) from None
        if accepted_id is not None:
            return _format_accepted(
                accepted_id,
                quit_note=f"quit:{exc.smtp_code}:{exc.smtp_error!r}",
            )
        raise SendRejectedError(
            f"SMTP rejected with {exc.smtp_code}: {exc.smtp_error!r}"
        ) from exc
    except SMTPServerDisconnected as exc:
        if partial_error is not None:
            raise _annotate_partial(
                partial_error, quit_note=f"quit_disconnect:{exc}"
            ) from None
        if accepted_id is not None:
            return _format_accepted(
                accepted_id, quit_note=f"quit_disconnect:{exc}"
            )
        raise SendRejectedError(
            f"SMTP connection failed before sending: {exc}"
        ) from exc
    except (TimeoutError, ConnectionError) as exc:
        if partial_error is not None:
            raise _annotate_partial(
                partial_error, quit_note=f"quit_error:{exc}"
            ) from None
        if accepted_id is not None:
            return _format_accepted(accepted_id, quit_note=f"quit_error:{exc}")
        raise SendRejectedError(
            f"SMTP connection failed before sending: {exc}"
        ) from exc
    except OSError as exc:
        if partial_error is not None:
            raise _annotate_partial(
                partial_error, quit_note=f"quit_error:{exc}"
            ) from None
        if accepted_id is not None:
            return _format_accepted(accepted_id, quit_note=f"quit_error:{exc}")
        raise SendRejectedError(
            f"SMTP connection failed before sending: {exc}"
        ) from exc
    except SMTPException:
        raise

    if partial_error is not None:
        raise partial_error
    assert accepted_id is not None
    return accepted_id
