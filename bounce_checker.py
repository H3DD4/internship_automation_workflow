"""
Bounce Checker (run separately, e.g. `python bounce_checker.py`, an hour or
two after sending, or as a daily cron/scheduled task).

Why this exists: Gmail SMTP accepts most messages at send-time even if the
recipient mailbox doesn't exist — the "550 no such user" rejection often
comes back LATER as an automated bounce email from "Mail Delivery Subsystem"
in your own inbox, not as an immediate error. This script reads those bounce
notifications and updates the matching application's status to 'bounced' so
the dashboard reflects reality.

This is best-effort: bounce message formats vary by receiving mail server,
so not every bounce will be parsed perfectly. It looks for the failed
address in the message body using common patterns.
"""

import imaplib
import email
import re
import os
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


def check_bounces(days_back: int = 3) -> int:
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_address or not gmail_app_password:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env — cannot check bounces.")
        return 0

    print(f"Connecting to {IMAP_HOST} as {gmail_address} ...")
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    conn.login(gmail_address, gmail_app_password)
    conn.select("INBOX")

    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days_back)).strftime("%d-%b-%Y")

    status, data = conn.search(None, f'(SINCE "{since}")')
    if status != "OK":
        print("IMAP search failed.")
        conn.close()
        conn.logout()
        return 0

    message_ids = data[0].split()
    print(f"Scanning {len(message_ids)} recent messages for bounce notifications...")

    updated_count = 0

    for msg_id in message_ids:
        status, msg_data = conn.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        from_header = (msg.get("From") or "").lower()
        subject = (msg.get("Subject") or "").lower()

        looks_like_bounce = any(hint in from_header for hint in BOUNCE_SENDER_HINTS) or \
            "delivery" in subject and ("fail" in subject or "undeliver" in subject)

        if not looks_like_bounce:
            continue

        body_text = _get_body_text(msg)
        failed_email = _extract_failed_email(body_text)

        if not failed_email:
            continue

        application = db.get_application_by_email(failed_email)
        if not application:
            continue  # bounce for an address we didn't send to (or already handled)

        if application["status"] == "bounced":
            continue  # already recorded

        db.update_application(application["id"], status="bounced",
                               error_message="Bounce notification detected in inbox.")
        db.log_event(application["id"], "bounce_check",
                     f"Detected bounce notification for {failed_email}",
                     detail={"bounce_subject": msg.get("Subject")})
        updated_count += 1
        print(f"  -> marked '{application['company_name']}' ({failed_email}) as bounced")

    conn.close()
    conn.logout()
    print(f"Done. {updated_count} application(s) updated to 'bounced'.")
    return updated_count


if __name__ == "__main__":
    db.init_db()
    check_bounces()
