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

Reads the inbox via whichever credentials are available:
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

from dotenv import load_dotenv

import db

load_dotenv()

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


def _record_bounce_if_sent(from_header: str, subject: str, body_text: str) -> bool:
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

    application = db.get_application_by_email(failed_email)
    if not application:
        return False  # bounce for an address we didn't send to (or never sent)

    # Only a row we actually sent can be marked bounced by this. A bounce
    # notification matching a row that's still 'ready'/'failed'/'pending'
    # would otherwise silently overwrite a status we haven't earned yet.
    if application["status"] != "sent":
        return False

    db.update_application(application["id"], status="bounced",
                           error_message="Bounce notification detected in inbox.")
    db.log_event(application["id"], "bounce_check",
                 f"Detected bounce notification for {failed_email}",
                 detail={"bounce_subject": subject})
    print(f"  -> marked '{application['company_name']}' ({failed_email}) as bounced")
    return True


def _check_bounces_via_imap(gmail_address: str, gmail_app_password: str, days_back: int) -> int:
    print(f"Connecting to {IMAP_HOST} as {gmail_address} (IMAP) ...")
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    conn.login(gmail_address, gmail_app_password)
    conn.select("INBOX")

    since = (date.today() - timedelta(days=days_back)).strftime("%d-%b-%Y")
    # Narrow the search server-side to likely bounce senders instead of
    # downloading and inspecting every message in the inbox.
    criteria = f'(SINCE "{since}") (OR (FROM "mailer-daemon") (FROM "postmaster"))'
    status, data = conn.search(None, criteria)
    if status != "OK":
        print("IMAP search failed.")
        conn.close()
        conn.logout()
        return 0

    message_ids = data[0].split()
    print(f"Scanning {len(message_ids)} candidate bounce message(s)...")

    updated_count = 0
    for msg_id in message_ids:
        status, msg_data = conn.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        if _record_bounce_if_sent(msg.get("From") or "", msg.get("Subject") or "", _get_body_text(msg)):
            updated_count += 1

    conn.close()
    conn.logout()
    return updated_count


def _check_bounces_via_gmail_api(days_back: int) -> int:
    from google_auth_helper import get_credentials
    from googleapiclient.discovery import build

    creds = get_credentials()
    if not creds:
        print("No valid OAuth token — cannot check bounces via Gmail API.")
        return 0

    print("Connecting to Gmail API (OAuth) ...")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    query = f"(from:mailer-daemon OR from:postmaster) newer_than:{max(days_back, 1)}d"
    message_ids = []
    request = service.users().messages().list(userId="me", q=query)
    while request is not None:
        response = request.execute()
        message_ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)

    print(f"Scanning {len(message_ids)} candidate bounce message(s)...")

    updated_count = 0
    for msg_id in message_ids:
        raw = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
        raw_bytes = base64.urlsafe_b64decode(raw["raw"])
        msg = email.message_from_bytes(raw_bytes)
        if _record_bounce_if_sent(msg.get("From") or "", msg.get("Subject") or "", _get_body_text(msg)):
            updated_count += 1

    return updated_count


def check_bounces(days_back: int = 3) -> int:
    """Checks for bounce notifications using whichever credentials are
    available, preferring the connected Google OAuth account (no app
    password required) and falling back to IMAP + app password."""
    try:
        from google_auth_helper import get_credentials, token_exists
        if token_exists() and get_credentials() is not None:
            updated = _check_bounces_via_gmail_api(days_back)
            print(f"Done. {updated} application(s) updated to 'bounced'.")
            return updated
    except ImportError:
        pass

    import os
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not gmail_app_password:
        print("No Google OAuth connection and no GMAIL_ADDRESS/GMAIL_APP_PASSWORD in .env "
              "— cannot check bounces.")
        return 0

    updated = _check_bounces_via_imap(gmail_address, gmail_app_password, days_back)
    print(f"Done. {updated} application(s) updated to 'bounced'.")
    return updated


if __name__ == "__main__":
    db.init_db()
    check_bounces()
