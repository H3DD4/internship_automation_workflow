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
    """Persist the token, keeping the account email stored alongside it.

    creds.to_json() knows nothing about that email, so writing it alone on
    every token refresh (roughly hourly) silently dropped the address — and
    later sends went out with no proper From address."""
    import json
    data = json.loads(creds.to_json())
    email = get_authorized_email()
    if email:
        data["email"] = email
    TOKEN_PATH.write_text(json.dumps(data), encoding="utf-8")


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

def _build_gmail_service(credentials=None):
    """`credentials` lets a caller that already resolved (and possibly
    refreshed) the token via get_credentials() pass it straight through,
    instead of this function calling get_credentials() again — each call
    can trigger a network refresh when the token is expired, so resolving
    it once per send instead of independently at every layer avoids doing
    that refresh redundantly (up to 3x per send previously)."""
    creds = credentials if credentials is not None else get_credentials()
    if not creds:
        raise RuntimeError("No valid OAuth token — user must re-authorise.")
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _email_from_id_token(credentials) -> Optional[str]:
    """The account email from the OpenID id_token Google returns alongside the
    access token. It came straight from Google's token endpoint over TLS, so
    decoding its payload without re-verifying the signature is fine here."""
    import json
    token = getattr(credentials, "id_token", None)
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except (ValueError, TypeError):
        return None


def get_oauth_email_address(credentials=None) -> Optional[str]:
    """
    The Gmail address that was just authorised: from the Gmail profile, or
    failing that from the id_token Google issued with the access token.
    """
    try:
        service = _build_gmail_service(credentials)
        profile = service.users().getProfile(userId="me").execute()
        if profile.get("emailAddress"):
            return profile["emailAddress"]
    except Exception:
        pass
    return _email_from_id_token(credentials) if credentials is not None else None


REQUIRED_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def missing_required_scopes(credentials) -> list:
    """Google's consent screen lets people untick individual permissions.
    Without gmail.send nothing can be sent, and without gmail.readonly bounces
    can't be detected — report which of the two were left unticked."""
    granted = set(getattr(credentials, "granted_scopes", None) or getattr(credentials, "scopes", None) or [])
    if not granted:
        return []
    return [scope for scope in (REQUIRED_SEND_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
            if scope not in granted]


def validate_client_secret(raw: bytes) -> tuple:
    """(ok, message) for an uploaded OAuth client JSON from Google Cloud."""
    import json
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False, "That file isn't valid JSON — download it again from Google Cloud Console."
    kind = "web" if "web" in data else "installed" if "installed" in data else None
    if not kind:
        return False, ("That JSON isn't an OAuth client file. In Google Cloud Console, open "
                       "Credentials → your OAuth 2.0 Client ID → Download JSON.")
    client = data[kind]
    missing = [k for k in ("client_id", "client_secret", "auth_uri", "token_uri") if not client.get(k)]
    if missing:
        return False, f"The client file is missing {', '.join(missing)}."
    return True, kind


def save_client_secret(raw: bytes) -> Path:
    path = ROOT_DIR / "credentials.json"
    path.write_bytes(raw)
    return path


def send_email_via_gmail_api(
    to_email: str,
    subject: str,
    body: str,
    cv_file_path: str,
    reply_to: Optional[str] = None,
    credentials=None,
) -> str:
    """
    Send one email via Gmail API (OAuth). Returns the Gmail-assigned message
    id. Raises googleapiclient.errors.HttpError / google.auth.exceptions.
    RefreshError (or another exception) on failure — mailer.py classifies
    these into Permanent/Transient/Authentication errors.
    The From address is the authorised account — no credentials needed beyond token.json.
    """
    from email.utils import formataddr
    from_email = get_authorized_email() or "me"
    display_name = (os.getenv("YOUR_NAME") or "").strip()

    msg = EmailMessage()
    # "Mohamed Hedda <address>" rather than a bare address: it's what the
    # recipient's inbox shows as the sender.
    msg["From"] = formataddr((display_name, from_email)) if display_name and "@" in from_email else from_email
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
    service = _build_gmail_service(credentials)
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return result.get("id", "")


def oauth_is_configured() -> bool:
    """True if client_secret / credentials.json exists."""
    return _find_client_secret() is not None
