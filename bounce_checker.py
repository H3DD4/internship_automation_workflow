"""
Bounce Checker (run separately, e.g. `python bounce_checker.py`, an hour or
two after sending, or as a daily cron/scheduled task — or via the dashboard's
"Check bounces" button, POST /api/check-bounces).

Why this exists: Gmail accepts most messages at send-time even if the
recipient mailbox doesn't exist — the "550 no such user" rejection often
comes back LATER as an automated bounce email from "Mail Delivery Subsystem"
in your own inbox, not as an immediate error. This script reads those bounce
notifications and updates the matching application's status to 'bounced' so
the dashboard reflects reality.

Runs per user, on that user's own mailbox. Reads the inbox via whichever
credentials the user has:
  - Google OAuth token (gmail.readonly scope) → Gmail API, no app password
    needed. Preferred when connected, since IMAP requires an app password
    that an OAuth-only user may not have set at all.
  - GMAIL_ADDRESS / GMAIL_APP_PASSWORD → IMAP (legacy / fallback path).

This is best-effort: bounce message formats vary by receiving mail server,
so not every bounce will be parsed perfectly. It looks for the failed
address in the message body using common patterns, and only ever updates a
row that we actually sent ('status' == 'sent') — a bounce notification is
never used to newly mark a row as bounced if we never recorded sending it.
"""

import base64
import email
import imaplib
import re
from datetime import date, timedelta

import db
from user_config import UserConfig

IMAP_HOST = "imap.gmail.com"

FAILED_ADDRESS_PATTERNS = [
    re.compile(r"Final-Recipient:\s*rfc822;\s*([^\s,]+@[^\s,]+)", re.IGNORECASE),
    re.compile(r"The email account that you tried to reach[^\n]*?\b([\w.+-]+@[\w.-]+)", re.IGNORECASE),
    re.compile(r"failed permanently[^\n]*?\b([\w.+-]+@[\w.-]+)", re.IGNORECASE),
    re.compile(r"Original-Recipient:\s*rfc822;\s*([^\s,]+@[^\s,]+)", re.IGNORECASE),
]

BOUNCE_SENDER_HINTS = ["mailer-daemon", "postmaster", "mail delivery subsystem"]


def _extract_failed_email(raw_message: str) -> str | None:
    for pattern in FAILED_ADDRESS_PATTERNS:
        match = pattern.search(raw_message)
        if match:
            return match.group(1).strip().rstrip(".,;")
    return None


def _get_body_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(errors="ignore"))
                except Exception:
                    continue
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(errors="ignore"))
    return "\n".join(parts)


DIAGNOSTIC_PATTERNS = [
    re.compile(r"Diagnostic-Code:\s*smtp;\s*(.+)", re.IGNORECASE),
    re.compile(r"(The email account that you tried to reach does not exist[^\n.]*)", re.IGNORECASE),
    re.compile(r"(Address not found[^\n]*)", re.IGNORECASE),
]


def _bounce_reason(body_text: str) -> str | None:
    """The receiving server's own explanation (e.g. "550 5.1.1 ... does not
    exist"), shortened, so the dashboard can say why a send didn't arrive."""
    for pattern in DIAGNOSTIC_PATTERNS:
        match = pattern.search(body_text or "")
        if match:
            reason = " ".join(match.group(1).split())
            return reason[:160] + ("…" if len(reason) > 160 else "")
    return None



def _record_bounce_if_sent(data, from_header: str, subject: str, body_text: str) -> bool:
    """Shared classify+match+update step for one candidate message, used by
    both the IMAP and Gmail-API paths. Returns True if a row was updated."""
    looks_like_bounce = (
        any(hint in from_header.lower() for hint in BOUNCE_SENDER_HINTS)
        or ("delivery" in subject.lower() and ("fail" in subject.lower() or "undeliver" in subject.lower()))
    )
    if not looks_like_bounce:
        return False

    failed_email = _extract_failed_email(body_text)
    if not failed_email:
        return False
    failed_email = failed_email.strip().lower()

    # Scoped to this user: a bounce in their inbox can only ever mark one of
    # THEIR applications.
    application = data.get_application_by_email(failed_email)
    if not application:
        return False

    # Only a row we actually sent can be marked bounced.
    if application["status"] != "sent":
        return False

    reason = _bounce_reason(body_text)
    data.update_application(application["id"], status="bounced",
                            error_message="Not delivered — " + (reason or "the recipient's mail server "
                                                               "sent back a delivery failure notice."))
    data.log_event(application["id"], "bounce_check",
                   f"Detected bounce notification for {failed_email}",
                   detail={"bounce_subject": subject})
    print(f"  -> marked '{application['company_name']}' ({failed_email}) as bounced")
    return True


def _check_bounces_via_imap(data, host: str, username: str, password: str, days_back: int) -> int:
    import safe_http
    safe_http.check_host(host, 993)
    conn = imaplib.IMAP4_SSL(host, timeout=30)
    try:
        conn.login(username, password)
        conn.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        # Narrow the search server-side to likely bounce senders.
        criteria = f'(SINCE "{since}") (OR (FROM "mailer-daemon") (FROM "postmaster"))'
        status, found = conn.search(None, criteria)
        if status != "OK":
            return 0
        updated = 0
        for msg_id in found[0].split()[:500]:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if _record_bounce_if_sent(data, msg.get("From") or "", msg.get("Subject") or "",
                                      _get_body_text(msg)):
                updated += 1
        return updated
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _check_bounces_via_gmail_api(data, credentials, days_back: int) -> int:
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    query = f"(from:mailer-daemon OR from:postmaster) newer_than:{max(days_back, 1)}d"
    message_ids = []
    request = service.users().messages().list(userId="me", q=query)
    while request is not None and len(message_ids) < 500:
        response = request.execute()
        message_ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)

    updated = 0
    for msg_id in message_ids[:500]:
        raw = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw["raw"]))
        if _record_bounce_if_sent(data, msg.get("From") or "", msg.get("Subject") or "",
                                  _get_body_text(msg)):
            updated += 1
    return updated


LAST_CHECK_KEY = "bounce_check"


def _imap_login(cfg: UserConfig) -> tuple | None:
    """(host, username, password) when the user saved a mailbox password."""
    password = cfg.secret("GMAIL_APP_PASSWORD")
    if not password:
        return None
    import mail_service
    method = mail_service.sending_method(cfg)
    if method == "smtp":
        host = cfg.get("IMAP_HOST")
        username = cfg.get("SMTP_USERNAME") or cfg.get("GMAIL_ADDRESS")
        return (host, username, password) if host and username else None
    address = cfg.get("GMAIL_ADDRESS")
    return (IMAP_HOST, address, password.replace(" ", "")) if address else None


def credentials_available(cfg: UserConfig) -> bool:
    """True when this user's inbox can be read in some way."""
    import google_auth_helper
    if google_auth_helper.token_exists(cfg) and google_auth_helper.can_read_inbox(cfg):
        return True
    return _imap_login(cfg) is not None


def check_bounces(cfg: UserConfig, days_back: int = 3) -> int:
    """Scan this user's inbox, preferring Google sign-in when it has inbox
    access, falling back to IMAP with the saved password."""
    import google_auth_helper
    data = db.for_user(cfg.user_id)
    if google_auth_helper.token_exists(cfg) and google_auth_helper.can_read_inbox(cfg):
        credentials = google_auth_helper.get_credentials(cfg)
        if credentials is not None:
            return _check_bounces_via_gmail_api(data, credentials, days_back)
    login = _imap_login(cfg)
    if login is None:
        raise RuntimeError("No way to read your inbox: sign in with Google with inbox access, "
                           "or save an app password.")
    return _check_bounces_via_imap(data, *login, days_back)


def run_check(cfg: UserConfig, days_back: int = 3, trigger: str = "manual") -> dict:
    """check_bounces() that never raises, and records when it ran and what it
    found so the dashboard can show "last checked 5 min ago"."""
    data = db.for_user(cfg.user_id)
    result = {"at": db.now(), "trigger": trigger, "updated": 0, "error": None}
    try:
        result["updated"] = check_bounces(cfg, days_back)
    except Exception as exc:
        result["error"] = str(exc)[:300]
    data.set_meta(LAST_CHECK_KEY, result)
    return result


def last_check(user_id: int) -> dict | None:
    return db.for_user(user_id).get_meta(LAST_CHECK_KEY)
