"""Request-level security for the dashboard.

  * Authentication: a server-side session looked up from an HttpOnly cookie
    on every request (accounts.load_session) — revocable instantly.
  * Authorisation: login_required / admin_required, and every data access
    goes through the signed-in user's scope (db.for_user) — so another
    account's row is simply "not found".
  * CSRF: every state-changing request must carry the session's token (a
    hidden form field or the X-CSRF-Token header), AND come from this origin.
  * Rate limits on the endpoints worth brute-forcing (login, sign-up).
  * Security headers: a nonce-based Content-Security-Policy, no framing,
    no MIME sniffing, a strict referrer policy, HSTS in production.
"""

from __future__ import annotations

import secrets
from functools import wraps
from urllib.parse import urlparse

from flask import abort, flash, g, jsonify, redirect, request, url_for

import accounts
import config
from user_config import UserConfig

COOKIE_NAME = "__Host-sid" if config.served_over_https() else "sid"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"

# Endpoints reachable without signing in.
PUBLIC_ENDPOINTS = {"static", "auth.login", "auth.register", "health", "auth.logout",
                    "auth_google", "oauth_callback"}
# Endpoints that render a public page when nobody is signed in (and the normal
# page, with every check, when someone is).
LANDING_ENDPOINTS = {"index"}


def client_ip() -> str:
    # Behind a reverse proxy, ProxyFix (enabled with TRUST_PROXY) has already
    # put the real client address in remote_addr.
    return (request.remote_addr or "")[:64]


def is_api_request() -> bool:
    return request.path.startswith("/api/") or request.is_json


def load_current_user() -> None:
    g.user, g.auth_session, g.cfg = None, None, None
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return
    loaded = accounts.load_session(token)
    if loaded is None:
        g.clear_cookie = True
        return
    g.auth_session, g.user = loaded
    g.cfg = UserConfig(g.user["id"], g.user["role"])


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=config.SESSION_ABSOLUTE_DAYS * 86400,
                        secure=config.served_over_https(), httponly=True, samesite="Lax", path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=config.served_over_https(),
                           httponly=True, samesite="Lax")


def _same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin.rstrip("/") == request.host_url.rstrip("/")
    referer = request.headers.get("Referer")
    if referer:
        return urlparse(referer).netloc == request.host
    return False


def csrf_ok() -> bool:
    if not _same_origin():
        return False
    if g.get("auth_session") is None:
        # Pre-login forms (login, register) use the anonymous token kept in
        # the signed Flask session.
        from flask import session
        expected = session.get("anon_csrf", "")
        submitted = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER) or ""
        return bool(expected) and secrets.compare_digest(expected, submitted)
    submitted = request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FIELD) or ""
    return accounts.csrf_matches(g.auth_session, submitted)


def csrf_token() -> str:
    if g.get("auth_session") is not None:
        return g.auth_session["csrf_token"]
    from flask import session
    if "anon_csrf" not in session:
        session["anon_csrf"] = secrets.token_urlsafe(32)
    return session["anon_csrf"]


def deny(message: str, status: int = 403):
    if is_api_request():
        return jsonify({"ok": False, "message": message}), status
    abort(status, description=message)


def before_request():
    g.csp_nonce = secrets.token_urlsafe(16)
    load_current_user()
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.endpoint != "static":
        if not csrf_ok():
            return deny("Request blocked: missing or invalid security token. Reload the page and try again.")
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if g.user is None:
        # Signed-out visitors see the public landing page at "/".
        if request.endpoint in LANDING_ENDPOINTS and request.method in ("GET", "HEAD"):
            return None
        if is_api_request():
            return jsonify({"ok": False, "message": "Your session ended — sign in again.",
                            "login": url_for("auth.login")}), 401
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
    # A temporary password must be replaced before anything else.
    if g.user.get("must_change_password") and request.endpoint not in (
            "auth.change_password", "auth.logout"):
        if is_api_request():
            return jsonify({"ok": False, "message": "Change your password first."}), 403
        return redirect(url_for("auth.change_password"))
    return None


def after_request(response):
    nonce = g.get("csp_nonce", "")
    csp = ("default-src 'self'; "
           f"script-src 'self' 'nonce-{nonce}'; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src 'self' https://fonts.gstatic.com; "
           "img-src 'self' data:; connect-src 'self'; "
           "form-action 'self' https://accounts.google.com; "
           "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if config.served_over_https():
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    # Pages holding personal data are never cached by the browser or a proxy.
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    if g.get("clear_cookie"):
        clear_session_cookie(response)
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return deny("Sign in first.", 401)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None or g.user["role"] != "admin":
            # 404, not 403: the admin area doesn't advertise itself.
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def rate_limited(key: str, limit: int, window: int) -> bool:
    try:
        return accounts.hit_rate_limit(key, limit, window)
    except Exception:
        return False


def safe_next(target: str | None) -> str:
    """Only same-site relative paths, so ?next= can't become an open redirect."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


def flash_and_back(message: str, category: str, endpoint: str, **values):
    flash(message, category)
    return redirect(url_for(endpoint, **values))
