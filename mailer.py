"""
Mailer: sends one email through the user's chosen method —

  * Gmail API with the user's Google sign-in (OAuth),
  * Gmail SMTP with an app password,
  * any other provider's SMTP server (university, Outlook, custom domain).

Deliverability basics handled here:
- Proper MIME headers (From with display name, To, Subject, Date, Message-ID,
  Reply-To) so the message looks like a normal, well-formed email.
- Plain-text body (no HTML, tracking pixels or link shorteners).
- The CV as a real, correctly-typed attachment.
- Errors classified so the sender knows the difference between:
    - "recipient rejected" (dead mailbox) -> failed, don't retry
    - "temporary" (server busy, network)  -> retry later
    - "auth failed" (bad password, revoked token) -> account-wide, halt the job

Pacing (delays, daily caps) is the sender worker's job, not this module's.
"""

import base64
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid


class PermanentSendError(Exception):
    """Recipient/address-level failure — retrying won't help."""


class TransientSendError(Exception):
    """Network/server-level failure — could succeed on retry later."""


class AuthenticationError(PermanentSendError):
    """The login itself failed (bad credentials or revoked OAuth token). It
    will fail for EVERY company, so the caller halts the whole job instead of
    burning through the queue. Subclasses PermanentSendError so code that
    only knows that class still catches it."""


GMAIL_SMTP = ("smtp.gmail.com", 465, "ssl")


def build_message(*, from_address: str, display_name: str, to_email: str, subject: str,
                  body: str, attachment: dict | None, reply_to: str | None = None) -> EmailMessage:
    """`attachment` is {"filename", "content_type", "content"} (the CV)."""
    msg = EmailMessage()
    msg["From"] = formataddr((display_name, from_address)) if display_name and "@" in from_address else from_address
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1] if "@" in from_address else "localhost")
    msg["Reply-To"] = reply_to or from_address
    msg.set_content(body)
    if attachment:
        maintype, subtype = (attachment.get("content_type") or "application/pdf").split("/", 1)
        msg.add_attachment(attachment["content"], maintype=maintype, subtype=subtype,
                           filename=attachment["filename"])
    return msg


def _classify_smtp_error(e: Exception) -> Exception:
    # smtplib.SMTPAuthenticationError is a SUBCLASS of SMTPResponseException,
    # so it must be checked first — otherwise Gmail's 535 falls into the
    # generic branch below and every company goes "retry later" instead of
    # surfacing "check your password".
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return AuthenticationError(f"The mail server rejected the login — check the address "
                                   f"and (app) password in Settings: {e}")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return PermanentSendError(f"Recipient refused (likely invalid/dead mailbox): {e}")
    if isinstance(e, smtplib.SMTPResponseException):
        code = getattr(e, "smtp_code", None)
        if code in (550, 551, 553, 554):
            return PermanentSendError(f"SMTP {code}: recipient rejected — {e}")
        return TransientSendError(f"SMTP {code}: temporary error — {e}")
    if isinstance(e, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                      ConnectionError, TimeoutError, OSError)):
        return TransientSendError(f"Connection/server issue: {e}")
    return TransientSendError(f"Unclassified send error: {e}")


def send_via_smtp(msg: EmailMessage, *, host: str, port: int, security: str,
                  username: str, password: str) -> str:
    """Returns the Message-ID header (SMTP assigns no server id)."""
    context = ssl.create_default_context()
    try:
        if security == "starttls":
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=context)
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(username, password)
                server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise _classify_smtp_error(e) from e
    return msg["Message-ID"]


def check_smtp_login(*, host: str, port: int, security: str, username: str, password: str) -> None:
    """Log in and out — nothing is sent. Raises the classified error."""
    context = ssl.create_default_context()
    try:
        if security == "starttls":
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls(context=context)
                server.login(username, password)
        else:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
                server.login(username, password)
    except (smtplib.SMTPException, OSError) as e:
        raise _classify_smtp_error(e) from e


def _classify_oauth_error(e: Exception) -> Exception:
    """Map a Gmail API failure by its actual HTTP status (not by words in the
    message, which misclassified ordinary failures as auth failures)."""
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:
        RefreshError = ()
    if RefreshError and isinstance(e, RefreshError):
        return AuthenticationError(f"Google sign-in expired or was revoked — sign in with Google "
                                   f"again in Settings: {e}")
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = None
    if HttpError is not None and isinstance(e, HttpError):
        status = getattr(getattr(e, "resp", None), "status", None)
        if status in (401, 403):
            return AuthenticationError(f"Gmail API rejected the request ({status}) — sign in with "
                                       f"Google again in Settings: {e}")
        if status == 400:
            return PermanentSendError(f"Gmail API rejected the request (400, likely a bad recipient): {e}")
        if status == 429 or (isinstance(status, int) and status >= 500):
            return TransientSendError(f"Gmail API temporary error ({status}): {e}")
        return TransientSendError(f"Gmail API error ({status}): {e}")
    return TransientSendError(f"Gmail API send error: {e}")


def send_via_gmail_api(msg: EmailMessage, credentials) -> str:
    """Returns the Gmail-assigned message id."""
    try:
        from googleapiclient.discovery import build
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return result.get("id", "")
    except Exception as e:
        raise _classify_oauth_error(e) from e
