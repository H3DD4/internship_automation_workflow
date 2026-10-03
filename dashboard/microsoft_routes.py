"""Microsoft accounts in the dashboard: "Continue with Microsoft" (sign in or
sign up), "Connect with Microsoft" (send from Outlook / Microsoft 365), and
the address check that tells a student which way to connect works for them.

The round trip mirrors the Google one: a random state and a PKCE verifier
kept in the signed session, bound to whoever started it, checked on return.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlparse

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

import accounts
import config
import mail_providers
import microsoft_auth
from dashboard import security

bp = Blueprint("microsoft", __name__)

# Microsoft refuses consent with these codes when a university only lets its
# administrators approve apps.
_ADMIN_CONSENT_CODES = ("AADSTS65001", "AADSTS90094", "AADSTS900941", "AADSTS50105", "AADSTS65004")


def redirect_uri() -> str:
    base = config.public_base_url()
    if base:
        return f"{base}{url_for('microsoft.callback')}"
    # Microsoft allows plain http only for localhost.
    port = request.host.rsplit(":", 1)[1] if ":" in request.host else "80"
    return f"http://localhost:{port}{url_for('microsoft.callback')}"


def _canonical_host():
    """Start the round trip on the host Microsoft sends the browser back to,
    so the session cookie that holds the state is there on return."""
    expected = urlparse(redirect_uri())
    if request.host != expected.netloc:
        return redirect(f"{expected.scheme}://{expected.netloc}{request.full_path.rstrip('?')}")
    return None


def _to_mail_settings():
    if session.get("in_setup"):           # connecting a mailbox from the first-time setup
        return redirect(url_for("onboarding.wizard", step="mail"))
    return redirect(url_for("settings_page") + "#s-gmail")


def _start(purpose: str, hint: str = ""):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    scopes = microsoft_auth.requested_scopes()
    session["ms_state"], session["ms_verifier"], session["ms_purpose"] = state, verifier, purpose
    session["ms_uid"] = g.user["id"] if g.get("user") else None
    session["ms_scopes"] = scopes
    return redirect(microsoft_auth.authorization_url(
        state=state, code_challenge=challenge, redirect_uri=redirect_uri(), scopes=scopes,
        login_hint=hint, prompt="select_account"))


@bp.get("/oauth/microsoft/start")
def connect():
    if not microsoft_auth.is_configured():
        flash("Microsoft sign-in isn't set up on this platform yet — ask the administrator, or use "
              "your mail server's settings (SMTP).", "error")
        return _to_mail_settings()
    moved = _canonical_host()
    return moved or _start("connect", g.cfg.get("GMAIL_ADDRESS") or g.user["email"])


@bp.get("/auth/microsoft")
def login():
    """"Continue with Microsoft" — school, work or personal account."""
    if g.get("user"):
        return redirect(url_for("index"))
    if not microsoft_auth.is_configured():
        flash("Microsoft sign-in isn't available on this platform yet.", "error")
        return redirect(url_for("auth.login"))
    moved = _canonical_host()
    if moved:
        return moved
    if security.rate_limited(f"ms-login-ip:{security.client_ip()}", 30, 600):
        flash("Too many attempts. Wait a few minutes and try again.", "error")
        return redirect(url_for("auth.login"))
    session["ms_next"] = security.safe_next(request.args.get("next"))
    return _start("login")


def _consent_blocked_message(description: str) -> str:
    client = microsoft_auth.client_config() or {}
    link = (f"https://login.microsoftonline.com/organizations/adminconsent?client_id={client.get('client_id', '')}"
            if client else "")
    return ("Your university only lets its IT administrators approve apps like Ntern. Ask them to "
            "approve it" + (f" with this link: {link}" if link else "") + " — or, meanwhile, send "
            "through your mail server's settings (SMTP) if your university allows it.")


@bp.get("/oauth/microsoft/callback")
def callback():
    purpose = session.pop("ms_purpose", "connect")
    back = (lambda: redirect(url_for("auth.login"))) if purpose == "login" else _to_mail_settings
    expected = session.pop("ms_state", None)
    verifier = session.pop("ms_verifier", None)
    owner = session.pop("ms_uid", None)
    scopes = session.pop("ms_scopes", None) or microsoft_auth.requested_scopes()
    error = request.args.get("error")
    if error:
        description = request.args.get("error_description", "")
        if any(code in description for code in _ADMIN_CONSENT_CODES) or "admin" in description.lower():
            flash(_consent_blocked_message(description), "error")
        else:
            flash(f"Microsoft sign-in was cancelled or refused: {error[:60]}.", "error")
        return back()
    current = g.user["id"] if g.get("user") else None
    state = request.args.get("state", "")
    if not state or not expected or not secrets.compare_digest(state, expected) or owner != current \
            or not verifier or (purpose == "connect" and current is None):
        flash("Microsoft sign-in couldn't be verified — please try again.", "error")
        return back()
    try:
        token = microsoft_auth.exchange_code(request.args.get("code", ""), verifier, redirect_uri(), scopes)
        who = microsoft_auth.identity(token)
    except Exception as exc:
        text = str(exc)
        if "AADSTS50011" in text:
            text = f"the redirect URI isn't registered — the platform's Microsoft app must list {redirect_uri()}"
        flash(f"Microsoft sign-in failed: {text[:220]}", "error")
        return back()
    granted = {s.split("/")[-1].lower() for s in (token.get("scope") or "").split()}
    if purpose == "login":
        return _finish_login(token, who, granted)
    if microsoft_auth.SEND_SCOPE.lower() not in granted:
        flash("Microsoft connected, but sending wasn't allowed — connect again and accept “Send mail "
              "as you”.", "error")
        return _to_mail_settings()
    microsoft_auth.save_token(g.cfg, token, who)
    g.cfg.set_many({"MAIL_METHOD": "microsoft", "GMAIL_ADDRESS": who["sender"]})
    accounts.audit("microsoft_connected", actor=g.user["id"])
    note = "" if microsoft_auth.READ_SCOPE.lower() in granted else " Bounce detection is off (reading the inbox wasn't allowed)."
    flash(f"✓ Connected as {who['sender']} — your emails will be sent from it.{note}", "success")
    return _to_mail_settings()


def _finish_login(token: dict, who: dict, granted: set):
    from user_config import UserConfig
    fail = lambda message: (flash(message, "error"), redirect(url_for("auth.login")))[1]  # noqa: E731
    email = accounts.normalize_email(who["email"])
    user = accounts.get_user_by_email(email)
    if user and user["role"] == "admin":
        return fail("The administrator account signs in with its username and password.")
    if user is None:
        mode = accounts.signup_mode()
        if mode == "closed":
            return fail("Sign-up is closed on this platform — ask the administrator for an account.")
        # Microsoft has verified the sign-in name (an organisation can only
        # use domains it has proven it owns), so the account is ready at once.
        uid = accounts.create_user(email, None, full_name=who["name"], status="active",
                                   email_verified=True)
        accounts.audit("register_microsoft", target=uid, ip=security.client_ip(),
                       detail={"status": "active", "signup_mode": mode})
        user = accounts.get_user(uid)
        session["welcome"] = True
    if accounts.claim_by_oauth(user["id"]):
        # Someone registered this address with a password before its owner
        # proved it — that password no longer works.
        accounts.audit("unverified_password_removed", target=user["id"], ip=security.client_ip())
        flash("For your safety, the password this account was created with has been removed: "
              "nobody had confirmed the address before. Set a new one any time in Settings → Account.",
              "warning")
    if user["status"] == "pending":
        return render_template("auth/register_done.html", pending=True)
    if user["status"] != "active":
        return fail("This account is suspended. Contact the administrator.")
    cfg = UserConfig(user["id"], user["role"])
    sending = False
    if microsoft_auth.SEND_SCOPE.lower() in granted:
        microsoft_auth.save_token(cfg, token, who)
        if cfg.get("MAIL_METHOD") in ("", "microsoft"):
            cfg.set_many({"MAIL_METHOD": "microsoft", "GMAIL_ADDRESS": who["sender"]})
        sending = True
    accounts.update_user(user["id"], failed_logins=0, locked_until=None, must_change_password=0,
                         last_login_at=accounts._now())
    session_token, _ = accounts.create_session(user["id"], security.client_ip(),
                                               request.headers.get("User-Agent", ""))
    accounts.audit("login_microsoft", actor=user["id"], ip=security.client_ip())
    if session.pop("welcome", False):
        flash("Welcome to Ntern — your account is ready and your Outlook mailbox is connected. "
              "Let's set up the rest together; it takes about five minutes.", "success")
    elif not sending:
        flash("Signed in. To send from this mailbox, connect it in Settings → Email account.", "success")
    response = redirect(session.pop("ms_next", None) or url_for("index"))
    security.set_session_cookie(response, session_token)
    return response


@bp.post("/oauth/microsoft/disconnect")
def disconnect():
    microsoft_auth.disconnect(g.cfg)
    if g.cfg.get("MAIL_METHOD") == "microsoft":
        g.cfg.set_many({"MAIL_METHOD": ""})
    flash("Microsoft account disconnected.", "success")
    return _to_mail_settings()


@bp.get("/api/mail/detect")
def detect():
    """Which mail service hosts this address, and so how to connect it."""
    if security.rate_limited(f"mail-detect:{g.user['id']}", 30, 600):
        return jsonify({"ok": False, "message": "Too many checks — wait a few minutes."}), 429
    result = mail_providers.detect(request.args.get("email", "")[:320])
    if not result["kind"]:
        return jsonify({"ok": False, "message": "Type a full email address."})
    import google_auth_helper
    result["available"] = {"google": google_auth_helper.oauth_is_configured(),
                           "microsoft": microsoft_auth.is_configured()}
    return jsonify({"ok": True, **result})
