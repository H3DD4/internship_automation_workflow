"""Platform configuration — the SERVER's settings, read from the environment
(or a .env file next to this one).

Everything that belongs to a user — AI keys, Gmail, CV, pacing, profile —
lives in the database, per account, never here. This file only holds what
the operator of the deployment decides: where the database is, the keys that
sign sessions and encrypt secrets, and a few platform-wide limits.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
ENV_PATH = ROOT_DIR / ".env"

# override=False: a real environment variable (Docker, systemd, the test
# suite) always wins over the file.
load_dotenv(ENV_PATH, override=False)


def get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def int_setting(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def bool_setting(name: str, default: bool) -> bool:
    value = get(name, "").lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def environment() -> str:
    """"production" turns on every hardening switch (secure cookies, HSTS,
    no private-network fetches, a mandatory secret key)."""
    return get("APP_ENV", "development").lower()


def is_production() -> bool:
    return environment() == "production"


def database_url() -> str:
    url = get("DATABASE_URL")
    if url:
        # Hosting platforms hand out "postgres://" URLs; SQLAlchemy wants the
        # driver named explicitly.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    if is_production():
        raise RuntimeError("DATABASE_URL must be set in production (PostgreSQL).")
    return f"sqlite:///{(ROOT_DIR / 'data' / 'app.db').as_posix()}"


def public_base_url() -> str:
    """The address users reach the app at, e.g. https://apply.example.com —
    used for the Google sign-in redirect. Empty = derive from the request."""
    return get("PUBLIC_BASE_URL").rstrip("/")


def served_over_https() -> bool:
    """Whether browsers reach the app over HTTPS. Decides Secure cookies and
    HSTS: on by default in production, off when PUBLIC_BASE_URL is plain
    http (the one-command local stack at http://127.0.0.1:5050)."""
    base = public_base_url()
    if base:
        return base.startswith("https://")
    return is_production()


def local_http_base() -> bool:
    """PUBLIC_BASE_URL is http on this machine — the only case where Google's
    OAuth library may be told to accept plain http."""
    base = public_base_url()
    return base.startswith(("http://127.0.0.1", "http://localhost"))


def admin_credentials() -> tuple[str, str] | None:
    """(username, password) of the platform administrator, from the
    environment (ADMIN_USERNAME / ADMIN_PASSWORD), or None."""
    username, password = get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD", "")
    return (username, password) if username and password else None


def allow_private_urls() -> bool:
    """Whether the server may fetch private/loopback addresses (company
    websites, custom AI endpoints). Always off in production: a company list
    is user input, and "http://169.254.169.254/" would otherwise read the
    host's cloud credentials."""
    if is_production():
        return False
    return bool_setting("ALLOW_PRIVATE_URLS", True)


def embedded_worker() -> bool:
    """Run the background worker inside the web process. Convenient locally;
    in production the worker is its own process (worker.py) so several web
    processes never each start one."""
    return bool_setting("EMBEDDED_WORKER", not is_production())


# Platform-wide limits, overridable by the operator.
MAX_CV_BYTES = int_setting("MAX_CV_BYTES", 5 * 1024 * 1024)
MAX_COMPANIES_UPLOAD_BYTES = int_setting("MAX_COMPANIES_UPLOAD_BYTES", 10 * 1024 * 1024)
MAX_COMPANIES_PER_USER = int_setting("MAX_COMPANIES_PER_USER", 50000)
MAX_CONCURRENT_RUNS = int_setting("MAX_CONCURRENT_RUNS", 4)
MAX_CONCURRENT_SENDERS = int_setting("MAX_CONCURRENT_SENDERS", 16)
# Ceilings a user can't raise above in their own settings.
USER_MAX_RESEARCH_WORKERS = int_setting("USER_MAX_RESEARCH_WORKERS", 4)
USER_MAX_EMAILS_PER_DAY = int_setting("USER_MAX_EMAILS_PER_DAY", 100)
SESSION_IDLE_DAYS = int_setting("SESSION_IDLE_DAYS", 7)
SESSION_ABSOLUTE_DAYS = int_setting("SESSION_ABSOLUTE_DAYS", 30)
