"""User accounts: passwords, sessions, brute-force protection, audit trail.

Passwords are hashed with Argon2id (argon2-cffi's defaults follow the
OWASP-recommended parameters) and transparently re-hashed on login when those
parameters are raised.

Sessions are server-side: the browser holds a random 256-bit token, the
database only its SHA-256. That makes "log out everywhere", "an admin
suspends an account" and "a password change ends every other session" take
effect on the very next request, which a signed-cookie session can't do.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import config
import database
from database import audit_log, auth_sessions, rate_buckets, system_settings, users

ROLES = ("admin", "user")
STATUSES = ("pending", "active", "suspended")
SIGNUP_MODES = ("approval", "open", "closed")

_hasher = PasswordHasher()
# Verified when the account doesn't exist, so a login for an unknown address
# takes as long as one for a real account (no user enumeration by timing).
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "123456789", "1234567890", "qwertyuiop",
    "azertyuiop", "iloveyou", "admin123", "welcome1", "letmein123", "motdepasse",
    "internship", "0123456789", "abcdefghij", "qwerty1234", "azerty1234",
}

# Brute force: an account locks after this many consecutive failures, for a
# duration that doubles each time (capped).
LOCK_AFTER_FAILURES = 5
LOCK_BASE_MINUTES = 5
LOCK_MAX_MINUTES = 24 * 60


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or "")) and len(email) <= 320


def password_problem(password: str, email: str = "") -> str | None:
    """A plain-language reason the password is too weak, or None."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS or len(set(password)) < 5:
        return "That password is too easy to guess — pick something less common."
    local = normalize_email(email).split("@")[0]
    if local and len(local) >= 4 and local in lowered:
        return "Don't build the password from your email address."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _dicts(result):
    return [dict(r._mapping) for r in result]


def _one(result):
    row = result.first()
    return dict(row._mapping) if row else None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class AccountError(ValueError):
    pass


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.$@!#%&*+-]{3,64}$")


def create_user(email: str, password: str, *, full_name: str = "", role: str = "user",
                status: str = "pending", must_change_password: bool = False,
                approved_by: int | None = None, username: str | None = None,
                check_password: bool = True) -> int:
    email = normalize_email(email)
    if not valid_email(email):
        raise AccountError("Enter a valid email address.")
    if username is not None and not USERNAME_RE.match(username):
        raise AccountError("A username is 3–64 letters, digits or symbols, without spaces.")
    problem = password_problem(password, email) if check_password else None
    if problem:
        raise AccountError(problem)
    if role not in ROLES or status not in STATUSES:
        raise AccountError("Invalid role or status.")
    now = _now()
    try:
        with database.tx() as conn:
            return conn.execute(users.insert().values(
                email=email, username=username, password_hash=hash_password(password),
                full_name=(full_name or "").strip()[:200], role=role, status=status,
                must_change_password=1 if must_change_password else 0, created_at=now,
                updated_at=now, approved_at=now if status == "active" else None,
                approved_by=approved_by,
            ).returning(users.c.id)).scalar_one()
    except IntegrityError as exc:
        raise AccountError("An account with this email already exists.") from exc


def get_user(user_id: int) -> dict | None:
    with database.read() as conn:
        return _one(conn.execute(select(users).where(users.c.id == int(user_id))))


def get_user_by_email(email: str) -> dict | None:
    with database.read() as conn:
        return _one(conn.execute(select(users).where(users.c.email == normalize_email(email))))


def get_user_by_username(username: str) -> dict | None:
    if not username:
        return None
    with database.read() as conn:
        return _one(conn.execute(select(users).where(users.c.username == username)))


def find_login(identifier: str) -> dict | None:
    """An account by email (case-insensitive) or, without an @, by username
    (exact)."""
    identifier = (identifier or "").strip()
    if "@" in identifier:
        return get_user_by_email(identifier) if valid_email(normalize_email(identifier)) else None
    return get_user_by_username(identifier)


ADMIN_EMAIL_DOMAIN = "admin.invalid"


def ensure_admin(username: str, password: str) -> int:
    """The platform administrator: one account, signed in with a username,
    kept in step with ADMIN_USERNAME / ADMIN_PASSWORD from the environment.
    Its email is on a reserved ".invalid" domain, so no Google account can
    ever match it."""
    user = get_user_by_username(username)
    if user is None:
        return create_user(f"platform-admin@{ADMIN_EMAIL_DOMAIN}", password, full_name="Administrator",
                           role="admin", status="active", username=username, check_password=False)
    fields = {}
    if user["role"] != "admin":
        fields["role"] = "admin"
    if user["status"] != "active":
        fields["status"] = "active"
    if fields:
        update_user(user["id"], **fields)
    if not _verify(user["password_hash"], password):
        with database.tx() as conn:
            conn.execute(update(users).where(users.c.id == user["id"]).values(
                password_hash=hash_password(password), failed_logins=0, locked_until=None,
                must_change_password=0, updated_at=_now()))
            conn.execute(delete(auth_sessions).where(auth_sessions.c.user_id == user["id"]))
    return user["id"]


def list_users() -> list:
    with database.read() as conn:
        return _dicts(conn.execute(select(users).order_by(users.c.id)))


def count_admins(active_only: bool = True) -> int:
    query = select(func.count()).select_from(users).where(users.c.role == "admin")
    if active_only:
        query = query.where(users.c.status == "active")
    with database.read() as conn:
        return conn.execute(query).scalar_one()


def update_user(user_id: int, **fields) -> None:
    allowed = {"full_name", "role", "status", "must_change_password", "approved_at",
               "approved_by", "failed_logins", "locked_until", "last_login_at", "email"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown user field(s): {unknown}")
    fields["updated_at"] = _now()
    try:
        with database.tx() as conn:
            conn.execute(update(users).where(users.c.id == int(user_id)).values(**fields))
    except IntegrityError as exc:
        raise AccountError("An account with this email already exists.") from exc


def set_password(user_id: int, password: str, *, must_change: bool = False,
                 keep_session: str | None = None) -> None:
    """Change a password and end every session except `keep_session` (the one
    making the change, when it's the user themself)."""
    user = get_user(user_id)
    if not user:
        raise AccountError("No such account.")
    problem = password_problem(password, user["email"])
    if problem:
        raise AccountError(problem)
    with database.tx() as conn:
        conn.execute(update(users).where(users.c.id == int(user_id)).values(
            password_hash=hash_password(password), must_change_password=1 if must_change else 0,
            failed_logins=0, locked_until=None, updated_at=_now()))
        query = delete(auth_sessions).where(auth_sessions.c.user_id == int(user_id))
        if keep_session:
            query = query.where(auth_sessions.c.id != keep_session)
        conn.execute(query)


def delete_user(user_id: int) -> None:
    """Remove an account and everything it owns. Children are deleted
    explicitly (not only through ON DELETE CASCADE) so the result is the same
    whatever the engine's foreign-key settings."""
    from database import (applications, cache_entries, company_list, events, prep_runs,
                          profiles, send_job_items, send_jobs, user_files, user_meta,
                          user_secrets, user_settings)
    uid = int(user_id)
    with database.tx() as conn:
        app_ids = select(applications.c.id).where(applications.c.user_id == uid)
        job_ids = select(send_jobs.c.id).where(send_jobs.c.user_id == uid)
        conn.execute(delete(send_job_items).where(send_job_items.c.job_id.in_(job_ids)))
        conn.execute(delete(send_job_items).where(send_job_items.c.application_id.in_(app_ids)))
        conn.execute(delete(events).where(events.c.application_id.in_(app_ids)))
        for table in (send_jobs, applications, company_list, cache_entries, prep_runs,
                      user_meta, user_files, user_secrets, user_settings, profiles,
                      auth_sessions):
            conn.execute(delete(table).where(table.c.user_id == uid))
        conn.execute(delete(users).where(users.c.id == uid))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class LoginResult:
    def __init__(self, ok: bool, user: dict | None = None, reason: str = ""):
        self.ok, self.user, self.reason = ok, user, reason


GENERIC_LOGIN_ERROR = "Incorrect email or password."


def authenticate(identifier: str, password: str) -> LoginResult:
    """Check credentials (email, or username for the administrator). The
    error never says which part was wrong, nor whether an account exists."""
    user = find_login(identifier)
    if not user:
        _verify(_DUMMY_HASH, password or "")
        return LoginResult(False, reason=GENERIC_LOGIN_ERROR)

    if user["locked_until"] and user["locked_until"] > _now():
        _verify(_DUMMY_HASH, password or "")
        return LoginResult(False, reason="Too many failed attempts. Try again in a few minutes.")

    if not _verify(user["password_hash"], password or ""):
        failures = (user["failed_logins"] or 0) + 1
        fields = {"failed_logins": failures}
        if failures >= LOCK_AFTER_FAILURES:
            steps = failures - LOCK_AFTER_FAILURES
            minutes = min(LOCK_BASE_MINUTES * (2 ** steps), LOCK_MAX_MINUTES)
            fields["locked_until"] = (_now_dt() + timedelta(minutes=minutes)).isoformat()
        update_user(user["id"], **fields)
        return LoginResult(False, reason=GENERIC_LOGIN_ERROR)

    if user["status"] == "pending":
        return LoginResult(False, reason="Your account is waiting for an administrator's approval.")
    if user["status"] != "active":
        return LoginResult(False, reason="This account is suspended. Contact the administrator.")

    fields = {"failed_logins": 0, "locked_until": None, "last_login_at": _now()}
    update_user(user["id"], **fields)
    if _hasher.check_needs_rehash(user["password_hash"]):
        with database.tx() as conn:
            conn.execute(update(users).where(users.c.id == user["id"])
                         .values(password_hash=hash_password(password)))
    return LoginResult(True, user=get_user(user["id"]))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, ip: str = "", user_agent: str = "") -> tuple[str, str]:
    """Returns (cookie_token, session_id). A fresh token on every login, so a
    token planted before login (session fixation) is worthless after it."""
    token = secrets.token_urlsafe(32)
    session_id = _token_id(token)
    now = _now_dt()
    with database.tx() as conn:
        conn.execute(auth_sessions.insert().values(
            id=session_id, user_id=int(user_id), csrf_token=secrets.token_urlsafe(32),
            created_at=now.isoformat(), last_seen_at=now.isoformat(),
            expires_at=(now + timedelta(days=config.SESSION_ABSOLUTE_DAYS)).isoformat(),
            ip=(ip or "")[:64], user_agent=(user_agent or "")[:300]))
    return token, session_id


def load_session(token: str) -> tuple[dict, dict] | None:
    """(session, user) for a valid cookie token, or None. Enforces the
    absolute lifetime, the idle timeout, and the account's current status."""
    if not token or len(token) > 200:
        return None
    session_id = _token_id(token)
    now = _now_dt()
    with database.read() as conn:
        row = conn.execute(select(auth_sessions, users.c.status, users.c.role)
                           .join(users, users.c.id == auth_sessions.c.user_id)
                           .where(auth_sessions.c.id == session_id)).first()
    if not row:
        return None
    session = dict(row._mapping)
    idle_limit = datetime.fromisoformat(session["last_seen_at"]) + timedelta(days=config.SESSION_IDLE_DAYS)
    if session["expires_at"] <= now.isoformat() or idle_limit <= now or session["status"] != "active":
        end_session(session_id)
        return None
    # Touch at most once a minute: a write on every request would be waste.
    if (now - datetime.fromisoformat(session["last_seen_at"])).total_seconds() > 60:
        with database.tx() as conn:
            conn.execute(update(auth_sessions).where(auth_sessions.c.id == session_id)
                         .values(last_seen_at=now.isoformat()))
    user = get_user(session["user_id"])
    if not user:
        return None
    return session, user


def end_session(session_id: str) -> None:
    with database.tx() as conn:
        conn.execute(delete(auth_sessions).where(auth_sessions.c.id == session_id))


def end_all_sessions(user_id: int) -> int:
    with database.tx() as conn:
        return conn.execute(delete(auth_sessions).where(
            auth_sessions.c.user_id == int(user_id))).rowcount


def count_sessions(user_id: int) -> int:
    with database.read() as conn:
        return conn.execute(select(func.count()).select_from(auth_sessions).where(
            auth_sessions.c.user_id == int(user_id))).scalar_one()


def purge_expired_sessions() -> int:
    idle_cutoff = (_now_dt() - timedelta(days=config.SESSION_IDLE_DAYS)).isoformat()
    with database.tx() as conn:
        return conn.execute(delete(auth_sessions).where(
            (auth_sessions.c.expires_at <= _now()) | (auth_sessions.c.last_seen_at <= idle_cutoff)
        )).rowcount


def csrf_matches(session: dict, submitted: str) -> bool:
    expected = session.get("csrf_token") or ""
    return bool(submitted) and hmac.compare_digest(expected, submitted)


# ---------------------------------------------------------------------------
# Rate limiting (fixed window, stored in the database so it holds across
# every web process)
# ---------------------------------------------------------------------------

def hit_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit for `key`. True when the key is over `limit` in the
    current window — the caller should refuse the request."""
    now = _now_dt()
    key = key[:200]
    with database.tx() as conn:
        if database.insert_ignore(conn, rate_buckets,
                                  {"key": key, "window_start": now.isoformat(), "count": 1}, ["key"]):
            return False
        query = select(rate_buckets).where(rate_buckets.c.key == key)
        if conn.dialect.name == "postgresql":
            query = query.with_for_update()
        row = conn.execute(query).first()
        start = datetime.fromisoformat(row.window_start)
        if (now - start).total_seconds() >= window_seconds:
            conn.execute(update(rate_buckets).where(rate_buckets.c.key == key)
                         .values(window_start=now.isoformat(), count=1))
            return False
        conn.execute(update(rate_buckets).where(rate_buckets.c.key == key)
                     .values(count=rate_buckets.c.count + 1))
        return row.count + 1 > limit


def purge_rate_buckets(older_than_seconds: int = 86400) -> None:
    cutoff = (_now_dt() - timedelta(seconds=older_than_seconds)).isoformat()
    with database.tx() as conn:
        conn.execute(delete(rate_buckets).where(rate_buckets.c.window_start < cutoff))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit(action: str, *, actor: int | None = None, target: int | None = None,
          detail: dict | None = None, ip: str = "") -> None:
    with database.tx() as conn:
        conn.execute(audit_log.insert().values(
            at=_now(), actor_user_id=actor, action=action[:64], target_user_id=target,
            detail=json.dumps(detail, ensure_ascii=False)[:2000] if detail else None,
            ip=(ip or "")[:64]))


def recent_audit(limit: int = 100) -> list:
    with database.read() as conn:
        return _dicts(conn.execute(select(audit_log).order_by(audit_log.c.id.desc()).limit(limit)))


# ---------------------------------------------------------------------------
# Platform settings
# ---------------------------------------------------------------------------

def get_system_setting(key: str, default: str = "") -> str:
    with database.read() as conn:
        row = conn.execute(select(system_settings.c.value).where(system_settings.c.key == key)).first()
    return row.value if row and row.value is not None else default


def set_system_setting(key: str, value: str) -> None:
    with database.tx() as conn:
        database.upsert(conn, system_settings, {"key": key, "value": value}, ["key"])


def signup_mode() -> str:
    mode = get_system_setting("signup_mode", config.get("SIGNUP_MODE", "approval"))
    return mode if mode in SIGNUP_MODES else "approval"
