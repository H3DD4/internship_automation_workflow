"""
google_auth_helper.py
---------------------
Handles Google OAuth 2.0 token storage/refresh and Gmail API sending.

Token life-cycle
----------------
- token.json  : stored next to this file after the user completes the OAuth flow.
- credentials.json (or the long-name client_secret_*.json): the OAuth client
  credentials downloaded from Google Cloud Console.

The dashboard (app.py) drives the OAuth redirect flow; this module only:
  1. Tells callers whether a valid (or refreshable) token exists.
  2. Returns a ready-to-use authorized `googleapiclient` service object.
  3. Sends email via the Gmail API (base64-encoded MIME).
  4. Provides the authorised Gmail address so it can be shown in the UI.
"""

from __future__ import annotations

import base64
import glob
import mimetypes
import os
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent

TOKEN_PATH = ROOT_DIR / "token.json"

# Support both "credentials.json" and the long Google-downloaded name
def _find_client_secret() -> Optional[Path]:
    for candidate in [
        ROOT_DIR / "credentials.json",
        *sorted(ROOT_DIR.glob("client_secret_*.json")),
    ]:
        if candidate.exists():
            return candidate
    return None

CLIENT_SECRET_PATH = _find_client_secret()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",   # for bounce checking
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def token_exists() -> bool:
    """True if a token.json is present (may still be expired but refreshable)."""
    return TOKEN_PATH.exists()


def get_credentials():
    """
    Return valid google.oauth2.credentials.Credentials or None.
    Refreshes the token automatically if it is expired but has a refresh token.
    """
    if not TOKEN_PATH.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_credentials(creds)
        return creds if creds.valid else None
    except Exception:
        return None


def _save_credentials(creds) -> None:
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


def get_authorized_email() -> Optional[str]:
    """Return the Gmail address stored in token.json, or None."""
    if not TOKEN_PATH.exists():
        return None
    try:
        import json
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        # The token file may store the email we retrieved at consent time.
        return data.get("email") or None
    except Exception:
        return None


def save_authorized_email(email: str) -> None:
    """Patch token.json to also store the gmail address for quick display."""
    if not TOKEN_PATH.exists():
        return
    try:
        import json
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        data["email"] = email
        TOKEN_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def revoke_token() -> None:
    """Delete the stored token (disconnect)."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


# ---------------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------------

def _build_gmail_service():
    creds = get_credentials()
    if not creds:
        raise RuntimeError("No valid OAuth token — user must re-authorise.")
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_oauth_email_address() -> Optional[str]:
    """
    Fetch the actual Gmail address from the API (used right after OAuth callback
    to confirm which account was authorised).
    """
    try:
        service = _build_gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("emailAddress")
    except Exception:
        return None


def send_email_via_gmail_api(
    to_email: str,
    subject: str,
    body: str,
    cv_file_path: str,
    reply_to: Optional[str] = None,
) -> None:
    """
    Send one email via Gmail API (OAuth).  Raises RuntimeError on failure.
    The From address is the authorised account — no credentials needed beyond token.json.
    """
    from_email = get_authorized_email() or "me"

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(from_email.split("@")[-1] if "@" in from_email else "gmail.com"))
    msg["Reply-To"] = reply_to or from_email
    msg.set_content(body)

    cv_path = Path(cv_file_path)
    if not cv_path.exists():
        raise RuntimeError(f"CV file not found: {cv_file_path}")

    ctype, _ = mimetypes.guess_type(str(cv_path))
    maintype, subtype = (ctype or "application/pdf").split("/", 1)
    msg.add_attachment(
        cv_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=cv_path.name,
    )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service = _build_gmail_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def oauth_is_configured() -> bool:
    """True if client_secret / credentials.json exists."""
    return _find_client_secret() is not None
