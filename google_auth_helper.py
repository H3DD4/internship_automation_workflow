"""
Google sign-in for sending through Gmail — one OAuth client for the whole
platform, one token per user.

- The OAuth client (client id + secret) is the platform's: the admin uploads
  the JSON from Google Cloud once (Admin → Platform), or the operator sets
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET. Users never deal with Google Cloud;
  they just click "Sign in with Google".
- Each user's token is stored encrypted (vault) as their GOOGLE_TOKEN secret,
  refreshed automatically, and written back after each refresh.

Scopes: sending needs gmail.send (a "sensitive" scope: Google verifies the
app once). Reading the inbox for bounce notices needs gmail.readonly, a
"restricted" scope that requires a paid yearly security assessment for a
public app — so it is requested only when the admin enables it
(inbox_read_scope). Without it, bounce checking works through IMAP with an
app password, or is simply off.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

import accounts
import config
from user_config import UserConfig, get_system_secret, set_system_secret

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
BASE_SCOPES = [SEND_SCOPE, "https://www.googleapis.com/auth/userinfo.email", "openid"]
CLIENT_SECRET_NAME = "GOOGLE_OAUTH_CLIENT"


def inbox_read_scope_enabled() -> bool:
    return accounts.get_system_setting("google_inbox_read", "on" if not config.is_production() else "off") == "on"


def requested_scopes() -> list:
    return BASE_SCOPES + ([READ_SCOPE] if inbox_read_scope_enabled() else [])


# ---------------------------------------------------------------------------
# The platform's OAuth client
# ---------------------------------------------------------------------------

def client_config() -> Optional[dict]:
    """{"web": {...}} or None when Google sign-in isn't set up."""
    client_id, client_secret = config.get("GOOGLE_CLIENT_ID"), config.get("GOOGLE_CLIENT_SECRET")
    if client_id and client_secret:
        return {"web": {"client_id": client_id, "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token"}}
    raw = get_system_secret(CLIENT_SECRET_NAME)
    if not raw:
        return None
    data = json.loads(raw)
    kind = "web" if "web" in data else "installed"
    return {"web": data[kind]}


def oauth_is_configured() -> bool:
    try:
        return client_config() is not None
    except Exception:
        return False


def validate_client_secret(raw: bytes) -> tuple:
    """(ok, message) for an uploaded OAuth client JSON from Google Cloud."""
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


def save_client_secret(raw: bytes) -> None:
    set_system_secret(CLIENT_SECRET_NAME, raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Per-user tokens
# ---------------------------------------------------------------------------

def token_exists(cfg: UserConfig) -> bool:
    return cfg.has_secret("GOOGLE_TOKEN")


def _token_data(cfg: UserConfig) -> dict | None:
    raw = cfg.secret("GOOGLE_TOKEN")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def save_credentials(cfg: UserConfig, creds, email: str | None = None) -> None:
    data = json.loads(creds.to_json())
    previous = _token_data(cfg) or {}
    data["email"] = email or previous.get("email") or ""
    cfg.set_secret("GOOGLE_TOKEN", json.dumps(data))


def get_credentials(cfg: UserConfig):
    """Valid google Credentials for this user, refreshed if needed, or None."""
    data = _token_data(cfg)
    if not data:
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_info(data)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(cfg, creds)
        return creds if creds.valid else None
    except Exception:
        return None


def get_authorized_email(cfg: UserConfig) -> Optional[str]:
    return ((_token_data(cfg) or {}).get("email") or None)


def granted_scopes(cfg: UserConfig) -> set:
    data = _token_data(cfg) or {}
    scopes = data.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return set(scopes)


def can_read_inbox(cfg: UserConfig) -> bool:
    return READ_SCOPE in granted_scopes(cfg)


def revoke_token(cfg: UserConfig) -> None:
    cfg.delete_secret("GOOGLE_TOKEN")


def email_from_id_token(credentials) -> Optional[str]:
    """The account email from the OpenID id_token Google returned over TLS
    from its own token endpoint."""
    token = getattr(credentials, "id_token", None)
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except (ValueError, TypeError):
        return None


def missing_required_scopes(credentials) -> list:
    granted = set(getattr(credentials, "granted_scopes", None) or getattr(credentials, "scopes", None) or [])
    if not granted:
        return []
    wanted = [SEND_SCOPE] + ([READ_SCOPE] if inbox_read_scope_enabled() else [])
    return [scope for scope in wanted if scope not in granted]
