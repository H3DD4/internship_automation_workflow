"""
Mailer: sends the final email — prefers Gmail API (OAuth) when a valid
token.json exists, falls back to Gmail SMTP + App Password otherwise.

Anti-spam / deliverability basics handled here:
- Proper MIME headers (From, To, Subject, Date, Message-ID, Reply-To) so the
  email looks like a normal, well-formed message rather than a bulk-mailer artifact.
- Plain-text body (no sketchy HTML/tracking pixels/link-shorteners that trigger filters).
- Real, correctly-typed PDF attachment.
- Classifies errors so we know the difference between:
    - "recipient address rejected" (typo'd/dead mailbox) -> status 'failed', don't retry
    - "temporary/transient" (server busy, connection issue) -> caller may retry later
    - "auth failed" (bad credentials / revoked token) -> surfaces a clear error

Actual send pacing (delays between emails, daily caps) is handled by the
orchestrator (main.py), not here.
"""

import smtplib
import ssl
import mimetypes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


class PermanentSendError(Exception):
    """Recipient/address-level failure — retrying won't help (e.g. mailbox doesn't exist)."""
    pass


class TransientSendError(Exception):
    """Network/server-level failure — could succeed on retry later."""
    pass


class AuthenticationError(PermanentSendError):
    """
    Gmail login itself failed (bad credentials or revoked OAuth token). This is
    account-wide, not per-recipient — unlike a bad address, it will fail for
    EVERY company, so the caller must not treat it like an ordinary
    PermanentSendError (which only kills the one company being sent to).
    Deliberately a subclass of PermanentSendError so any old code that only
    catches PermanentSendError still catches this too, but callers that care
    about the distinction (see pipeline.py) can catch it specifically first.
    """
    pass


def _classify_smtp_error(e: Exception) -> Exception:
    # NOTE: smtplib.SMTPAuthenticationError is a SUBCLASS of
    # smtplib.SMTPResponseException. This check must come first, or an auth
    # failure falls into the generic SMTPResponseException branch below and
    # (since Gmail's auth-failure code, 535, isn't in the permanent-code
    # list) gets silently misclassified as a transient error — every company
    # in the run would then be marked "retry_later" instead of surfacing a
    # clear "check your Gmail credentials" failure.
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return AuthenticationError(
            f"Gmail authentication failed — check GMAIL_ADDRESS/GMAIL_APP_PASSWORD in .env: {e}"
        )

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


def _send_via_smtp(gmail_address: str, gmail_app_password: str, to_email: str,
                   subject: str, body: str, cv_path: Path,
                   reply_to: str = None) -> None:
    """Send via SMTP + App Password (legacy / fallback path)."""
    msg = EmailMessage()
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=gmail_address.split("@")[-1])
    msg["Reply-To"] = reply_to or gmail_address
    msg.set_content(body)

    ctype, _ = mimetypes.guess_type(str(cv_path))
    maintype, subtype = (ctype or "application/pdf").split("/", 1)
    msg.add_attachment(
        cv_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=cv_path.name,
    )

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
            server.login(gmail_address, gmail_app_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise _classify_smtp_error(e) from e


def _send_via_oauth(to_email: str, subject: str, body: str, cv_path: Path,
                    reply_to: str = None) -> None:
    """Send via Gmail API using the stored OAuth token (preferred path)."""
    try:
        from google_auth_helper import send_email_via_gmail_api
        send_email_via_gmail_api(
            to_email=to_email,
            subject=subject,
            body=body,
            cv_file_path=str(cv_path),
            reply_to=reply_to,
        )
    except Exception as e:
        err_str = str(e).lower()
        # Token revoked / expired and couldn't refresh → auth error
        if any(k in err_str for k in ("token", "invalid_grant", "unauthorized",
                                       "credentials", "authoris", "oauth")):
            raise AuthenticationError(
                f"OAuth token is invalid or revoked — reconnect Google account in the dashboard: {e}"
            ) from e
        # Recipient-level errors from Gmail API
        if "invalid to" in err_str or "recipient" in err_str:
            raise PermanentSendError(f"Gmail API rejected recipient: {e}") from e
        raise TransientSendError(f"Gmail API send error: {e}") from e


def send_email(gmail_address: str, gmail_app_password: str, to_email: str,
               subject: str, body: str, cv_file_path: str,
               reply_to: str = None) -> None:
    """
    Sends one email.
    - If a valid OAuth token exists → uses Gmail API (no password needed).
    - Otherwise → falls back to SMTP + App Password.

    Raises PermanentSendError, AuthenticationError, or TransientSendError on
    failure (never a raw library exception), so the caller can decide what to do.
    """
    cv_path = Path(cv_file_path)
    if not cv_path.exists():
        raise PermanentSendError(f"CV file not found at '{cv_file_path}' — check CV_FILE_PATH in .env")

    # Prefer OAuth when a token is present
    try:
        from google_auth_helper import get_credentials, token_exists
        if token_exists() and get_credentials() is not None:
            _send_via_oauth(to_email, subject, body, cv_path, reply_to)
            return
    except ImportError:
        pass  # google libraries not installed — fall through to SMTP

    # SMTP fallback
    if not gmail_address or not gmail_app_password:
        raise AuthenticationError(
            "No OAuth token and no GMAIL_APP_PASSWORD configured — "
            "connect your Google account via the dashboard or add an app password."
        )
    _send_via_smtp(gmail_address, gmail_app_password, to_email, subject, body, cv_path, reply_to)
