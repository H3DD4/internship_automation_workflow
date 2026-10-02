"""
Microsoft accounts: Outlook.com / Hotmail and school or work Microsoft 365
(most university mailboxes) — sign in, and send from the student's own
mailbox through Microsoft Graph.

Why Graph and OAuth, not an SMTP password: Microsoft 365 is switching off
password ("basic") authentication for SMTP — disabled by default from the end
of December 2026 — so a university mailbox can only be used by an app the
student signs in to. Everything here is free: one app registration in
Microsoft Entra for the whole platform, and Graph's mail API.

Flow: the authorization-code flow with PKCE against the "common" endpoint
(any school, work or personal Microsoft account), plain HTTPS calls — no SDK.

Identity: an account is identified by its userPrincipalName, which Microsoft
only allows on domains the organisation has verified. The free-form "mail"
attribute is never trusted for identity — an organisation's administrator
can set it to any address, which is how "nOAuth"-style account takeovers
happen. Guest identities (#EXT#) are refused.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional
from urllib.parse import urlencode

import requests

import accounts
import config
from mailer import AuthenticationError, PermanentSendError, TransientSendError
from user_config import UserConfig, get_system_secret, set_system_secret

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
CLIENT_SECRET_NAME = "MICROSOFT_OAUTH_CLIENT"
TOKEN_NAME = "MICROSOFT_TOKEN"
SEND_SCOPE = "Mail.Send"
READ_SCOPE = "Mail.Read"
BASE_SCOPES = ["openid", "email", "profile", "offline_access", "User.Read", SEND_SCOPE]
# Graph accepts a direct attachment up to ~3 MB; bigger files go through an
# upload session (up to 150 MB) in chunks of at most 4 MB.
DIRECT_ATTACHMENT_LIMIT = 3 * 1024 * 1024
UPLOAD_CHUNK = 3 * 1024 * 1024
TIMEOUT = 30


class MicrosoftError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# The platform's app registration
# ---------------------------------------------------------------------------

def client_config() -> Optional[dict]:
    """{"client_id", "client_secret"} or None when Microsoft isn't set up."""
    client_id, secret = config.get("MICROSOFT_CLIENT_ID"), config.get("MICROSOFT_CLIENT_SECRET")
    if client_id and secret:
        return {"client_id": client_id, "client_secret": secret}
    raw = get_system_secret(CLIENT_SECRET_NAME)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if data.get("client_id") and data.get("client_secret") else None


def is_configured() -> bool:
    try:
        return client_config() is not None
    except Exception:
        return False


def save_client(client_id: str, client_secret: str) -> None:
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not (8 <= len(client_id) <= 100) or not (8 <= len(client_secret) <= 200):
        raise MicrosoftError("Paste the Application (client) ID and a client secret value.")
    set_system_secret(CLIENT_SECRET_NAME, json.dumps({"client_id": client_id, "client_secret": client_secret}))


def remove_client() -> None:
    set_system_secret(CLIENT_SECRET_NAME, "")


def inbox_read_enabled() -> bool:
    # Mail.Read is an ordinary delegated permission on Microsoft (no paid
    # assessment, unlike Gmail's restricted scope), so it's on by default.
    return accounts.get_system_setting("microsoft_inbox_read", "on") == "on"


def requested_scopes() -> list:
    return BASE_SCOPES + ([READ_SCOPE] if inbox_read_enabled() else [])


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def authorization_url(*, state: str, code_challenge: str, redirect_uri: str, scopes: list,
                      login_hint: str = "", prompt: str = "select_account") -> str:
    client = client_config()
    if not client:
        raise MicrosoftError("Microsoft sign-in isn't set up on this platform.")
    params = {"client_id": client["client_id"], "response_type": "code", "redirect_uri": redirect_uri,
              "response_mode": "query", "scope": " ".join(scopes), "state": state,
              "code_challenge": code_challenge, "code_challenge_method": "S256", "prompt": prompt}
    if "@" in (login_hint or ""):
        params["login_hint"] = login_hint
    return f"{AUTHORITY}/authorize?{urlencode(params)}"


def _token_request(data: dict) -> dict:
    client = client_config()
    if not client:
        raise MicrosoftError("Microsoft sign-in isn't set up on this platform.")
    response = requests.post(f"{AUTHORITY}/token", timeout=TIMEOUT,
                             data={**data, "client_id": client["client_id"],
                                   "client_secret": client["client_secret"]})
    payload = response.json() if response.content else {}
    if response.status_code != 200 or "access_token" not in payload:
        raise MicrosoftError(payload.get("error_description") or payload.get("error")
                             or f"Microsoft answered {response.status_code}.")
    payload["expires_at"] = time.time() + int(payload.get("expires_in") or 3600) - 60
    return payload


def exchange_code(code: str, code_verifier: str, redirect_uri: str, scopes: list) -> dict:
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                           "code_verifier": code_verifier, "scope": " ".join(scopes)})


def id_token_claims(token: dict) -> dict:
    """The ID token's claims. It came straight from Microsoft's token endpoint
    over TLS in exchange for our client secret, which OpenID Connect accepts
    in place of checking its signature (OIDC Core §3.1.3.7)."""
    raw = token.get("id_token") or ""
    try:
        payload = raw.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def graph_get(token: dict, path: str, **params) -> dict:
    response = requests.get(f"{GRAPH}{path}", params=params or None, timeout=TIMEOUT,
                            headers={"Authorization": f"Bearer {token['access_token']}"})
    if response.status_code != 200:
        raise _classify(response, "reading your account")
    return response.json()


def identity(token: dict) -> dict:
    """{"email", "name", "sender", "oid", "tid"} — `email` is the verified
    sign-in name, `sender` the mailbox address mail goes out from."""
    me = graph_get(token, "/me", **{"$select": "id,displayName,mail,userPrincipalName"})
    upn = (me.get("userPrincipalName") or "").strip().lower()
    if not upn or "@" not in upn or "#ext#" in upn:
        raise MicrosoftError("This Microsoft account is a guest account — sign in with your own "
                             "school, work or personal account.")
    claims = id_token_claims(token)
    return {"email": upn, "name": (me.get("displayName") or "").strip(),
            "sender": (me.get("mail") or upn).strip().lower(),
            "oid": claims.get("oid") or me.get("id") or "", "tid": claims.get("tid") or ""}


# ---------------------------------------------------------------------------
# The user's token
# ---------------------------------------------------------------------------

def token_exists(cfg: UserConfig) -> bool:
    return cfg.has_secret(TOKEN_NAME)


def _stored(cfg: UserConfig) -> dict | None:
    raw = cfg.secret(TOKEN_NAME)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def save_token(cfg: UserConfig, token: dict, who: dict | None = None) -> None:
    previous = _stored(cfg) or {}
    data = {"access_token": token["access_token"],
            "refresh_token": token.get("refresh_token") or previous.get("refresh_token") or "",
            "expires_at": token.get("expires_at") or time.time() + 3000,
            "scope": token.get("scope") or previous.get("scope") or "",
            "email": (who or {}).get("email") or previous.get("email") or "",
            "sender": (who or {}).get("sender") or previous.get("sender") or "",
            "oid": (who or {}).get("oid") or previous.get("oid") or "",
            "tid": (who or {}).get("tid") or previous.get("tid") or ""}
    cfg.set_secret(TOKEN_NAME, json.dumps(data))


def granted_scopes(cfg: UserConfig) -> set:
    return {s.split("/")[-1].lower() for s in ((_stored(cfg) or {}).get("scope") or "").split()}


def can_send(cfg: UserConfig) -> bool:
    return SEND_SCOPE.lower() in granted_scopes(cfg)


def can_read_inbox(cfg: UserConfig) -> bool:
    return READ_SCOPE.lower() in granted_scopes(cfg)


def sender_address(cfg: UserConfig) -> str:
    data = _stored(cfg) or {}
    return data.get("sender") or data.get("email") or ""


def access_token(cfg: UserConfig) -> dict:
    """A usable token, refreshed when it's about to expire. Raises
    AuthenticationError when the student has to sign in again."""
    data = _stored(cfg)
    if not data:
        raise AuthenticationError("No Microsoft account connected — connect it in Settings.")
    if data.get("expires_at", 0) > time.time():
        return data
    if not data.get("refresh_token"):
        raise AuthenticationError("Your Microsoft sign-in expired — connect it again in Settings.")
    try:
        fresh = _token_request({"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                "scope": " ".join(requested_scopes())})
    except (MicrosoftError, requests.RequestException) as exc:
        raise AuthenticationError(f"Your Microsoft sign-in expired or was revoked — connect it again "
                                  f"in Settings ({str(exc)[:120]}).") from exc
    save_token(cfg, fresh)
    return _stored(cfg)


def disconnect(cfg: UserConfig) -> None:
    cfg.delete_secret(TOKEN_NAME)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _classify(response, what: str) -> Exception:
    try:
        error = response.json().get("error", {})
        detail = f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    except ValueError:
        detail = response.text[:200]
    status = response.status_code
    if status in (401, 403):
        return AuthenticationError(f"Microsoft refused {what} ({status} {detail[:160]}) — connect your "
                                   "account again in Settings, or ask your university's IT whether "
                                   "third-party apps may send mail.")
    if status in (400, 404, 413):
        return PermanentSendError(f"Microsoft rejected {what} ({status} {detail[:160]}).")
    return TransientSendError(f"Microsoft had a temporary problem {what} ({status} {detail[:160]}).")


def send(cfg: UserConfig, *, to_email: str, subject: str, body: str, attachment: dict | None) -> str:
    """Send one email from the student's own mailbox. Returns the message's
    Internet Message-ID. Raises the mailer's error classes."""
    token = access_token(cfg)
    headers = {"Authorization": f"Bearer {token['access_token']}", "Content-Type": "application/json"}
    try:
        draft = requests.post(f"{GRAPH}/me/messages", headers=headers, timeout=TIMEOUT, json={
            "subject": subject, "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_email}}]})
        if draft.status_code not in (200, 201):
            raise _classify(draft, "creating the email")
        message = draft.json()
        message_id = message["id"]
        if attachment:
            _attach(headers, message_id, attachment)
        sent = requests.post(f"{GRAPH}/me/messages/{message_id}/send", headers=headers, timeout=TIMEOUT)
        if sent.status_code not in (200, 202):
            raise _classify(sent, "sending the email")
        return message.get("internetMessageId") or message_id
    except requests.RequestException as exc:
        raise TransientSendError(f"Couldn't reach Microsoft: {exc}") from exc


def _attach(headers: dict, message_id: str, attachment: dict) -> None:
    content = attachment["content"]
    name = attachment["filename"]
    kind = attachment.get("content_type") or "application/pdf"
    if len(content) <= DIRECT_ATTACHMENT_LIMIT:
        response = requests.post(f"{GRAPH}/me/messages/{message_id}/attachments", headers=headers,
                                 timeout=TIMEOUT, json={
                                     "@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                                     "contentType": kind,
                                     "contentBytes": base64.b64encode(content).decode("ascii")})
        if response.status_code not in (200, 201):
            raise _classify(response, "attaching your CV")
        return
    session = requests.post(f"{GRAPH}/me/messages/{message_id}/attachments/createUploadSession",
                            headers=headers, timeout=TIMEOUT,
                            json={"AttachmentItem": {"attachmentType": "file", "name": name,
                                                     "size": len(content), "contentType": kind}})
    if session.status_code not in (200, 201):
        raise _classify(session, "preparing your CV upload")
    upload_url = session.json()["uploadUrl"]
    for start in range(0, len(content), UPLOAD_CHUNK):
        chunk = content[start:start + UPLOAD_CHUNK]
        end = start + len(chunk) - 1
        # The upload URL carries its own authorisation; no bearer token.
        part = requests.put(upload_url, data=chunk, timeout=TIMEOUT * 2, headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{len(content)}"})
        if part.status_code not in (200, 201, 202):
            raise _classify(part, "uploading your CV")


# ---------------------------------------------------------------------------
# Bounce notices
# ---------------------------------------------------------------------------

BOUNCE_SUBJECT_HINTS = ("undeliverable", "delivery status notification", "non remis", "unzustellbar",
                        "mail delivery failed", "returned mail", "failure notice", "échec de la remise")
BOUNCE_SENDER_HINTS = ("postmaster", "mailer-daemon", "microsoftexchange")


def bounce_messages(cfg: UserConfig, days_back: int) -> list:
    """Raw MIME bytes of recent delivery-failure notices in the inbox."""
    from datetime import datetime, timedelta, timezone
    token = access_token(cfg)
    since = (datetime.now(timezone.utc) - timedelta(days=max(days_back, 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    url = f"{GRAPH}/me/mailFolders/inbox/messages"
    params = {"$filter": f"receivedDateTime ge {since}", "$select": "id,subject,from",
              "$top": "100", "$orderby": "receivedDateTime desc"}
    found, seen = [], 0
    while url and seen < 1000:
        response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        if response.status_code != 200:
            raise _classify(response, "reading your inbox")
        page = response.json()
        for item in page.get("value", []):
            seen += 1
            subject = (item.get("subject") or "").lower()
            sender = ((item.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
            if any(h in sender for h in BOUNCE_SENDER_HINTS) or any(h in subject for h in BOUNCE_SUBJECT_HINTS):
                raw = requests.get(f"{GRAPH}/me/messages/{item['id']}/$value", headers=headers, timeout=TIMEOUT)
                if raw.status_code == 200:
                    found.append(raw.content)
        url, params = page.get("@odata.nextLink"), None
    return found[:500]
