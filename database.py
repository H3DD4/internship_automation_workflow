"""Engine, schema and migrations — PostgreSQL in production, SQLite for tests.

Every table that holds a user's data carries `user_id`, and every query in
db.py filters on it: one account can never read or touch another's rows.

Timestamps stay ISO-8601 UTC text (as in the original SQLite schema): they
sort and compare correctly as strings on both engines, and the daily-cap and
bounce-window queries rely on exactly that.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from sqlalchemy import (
    Column, ForeignKey, Index, Integer, LargeBinary, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, event, inspect, text,
)
from sqlalchemy.engine import Engine

import config

metadata = MetaData()

# ---------------------------------------------------------------------------
# Accounts and security
# ---------------------------------------------------------------------------

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(320), nullable=False, unique=True),
    # Only the platform administrator signs in with a username; everyone else
    # uses their email (or Google).
    Column("username", String(64), unique=True),
    Column("password_hash", Text, nullable=False),
    Column("full_name", Text, nullable=False, server_default=""),
    Column("role", String(16), nullable=False, server_default="user"),        # admin | user
    Column("status", String(16), nullable=False, server_default="pending"),   # pending | active | suspended
    Column("must_change_password", Integer, nullable=False, server_default="0"),
    Column("failed_logins", Integer, nullable=False, server_default="0"),
    Column("locked_until", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("last_login_at", Text),
    Column("approved_at", Text),
    Column("approved_by", Integer),
    # Set once Google or Microsoft proved this person owns the address.
    # NULL = nobody has checked it (a password sign-up).
    Column("email_verified_at", Text),
)

auth_sessions = Table(
    "auth_sessions", metadata,
    # sha256 of the cookie token — a leaked database can't be replayed as cookies.
    Column("id", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("csrf_token", String(64), nullable=False),
    Column("created_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    Column("ip", String(64)),
    Column("user_agent", Text),
    Index("idx_auth_sessions_user", "user_id"),
)

rate_buckets = Table(
    "rate_buckets", metadata,
    Column("key", String(200), primary_key=True),
    Column("window_start", Text, nullable=False),
    Column("count", Integer, nullable=False, server_default="0"),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", Text, nullable=False),
    Column("actor_user_id", Integer),
    Column("action", String(64), nullable=False),
    Column("target_user_id", Integer),
    Column("detail", Text),
    Column("ip", String(64)),
    Index("idx_audit_at", "at"),
)

system_settings = Table(
    "system_settings", metadata,
    Column("key", String(100), primary_key=True),
    Column("value", Text),
)

system_secrets = Table(
    "system_secrets", metadata,
    Column("name", String(100), primary_key=True),
    Column("ciphertext", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

# ---------------------------------------------------------------------------
# Per-user configuration
# ---------------------------------------------------------------------------

user_settings = Table(
    "user_settings", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("key", String(100), primary_key=True),
    Column("value", Text),
)

user_secrets = Table(
    "user_secrets", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("name", String(100), primary_key=True),
    Column("ciphertext", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

user_files = Table(
    "user_files", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(32), nullable=False),          # cv
    Column("filename", Text, nullable=False),
    Column("content_type", String(128), nullable=False),
    Column("size", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("content", LargeBinary, nullable=False),
    Column("uploaded_at", Text, nullable=False),
    UniqueConstraint("user_id", "kind", name="uq_user_files_kind"),
)

profiles = Table(
    "profiles", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    # custom: spec_en/spec_fr are hand-written wording (the original
    # specializations.json). template: they are rendered from `facts` + a
    # template from email_templates.py every time the profile is saved.
    Column("mode", String(16), nullable=False, server_default="template"),
    Column("template_id", String(64), nullable=False, server_default="specialist"),
    Column("language_mode", String(8), nullable=False, server_default="auto"),  # auto | en | fr
    Column("facts", Text),
    Column("spec_en", Text),
    Column("spec_fr", Text),
    Column("cv_text", Text),
    Column("updated_at", Text, nullable=False),
    # The user's own email template, built from a pasted example or the
    # section editor (JSON; see email_templates.own_template_problems).
    Column("own_template", Text),
)

company_list = Table(
    "company_list", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("email", String(320), nullable=False),
    Column("company_name", Text, nullable=False),
    Column("website", Text, nullable=False, server_default=""),
    Column("contact_name", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    # The name the user gave this list when importing it ("" = "My list").
    Column("source", Text, nullable=False, server_default=""),
    UniqueConstraint("user_id", "email", name="uq_company_list_user_email"),
)

# The Ntern list: one shared list of companies every user can scan, managed
# by the administrator. Only the companies — never anyone's drafts or sends.
catalog_companies = Table(
    "catalog_companies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("company_name", Text, nullable=False),
    Column("website", Text, nullable=False, server_default=""),
    Column("contact_name", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
)

# ---------------------------------------------------------------------------
# The pipeline's own tables (the original schema, scoped per user)
# ---------------------------------------------------------------------------

applications = Table(
    "applications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("company_name", Text, nullable=False),
    Column("email", String(320), nullable=False),
    Column("website", Text),
    Column("contact_name", Text),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("industry", Text),
    Column("mission_or_focus", Text),
    Column("tone_of_voice", Text),
    Column("talking_points", Text),
    Column("matched_extra_mentions", Text),
    Column("match_reasons", Text),
    Column("subject", Text),
    Column("body", Text),
    Column("error_message", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("sent_at", Text),
    Column("send_attempts", Integer, server_default="0"),
    Column("last_attempt_at", Text),
    Column("message_id", Text),
    Column("error_code", Text),
    Column("company_hook", Text),
    Column("hook_evidence", Text),
    Column("hook_status", Text),
    Column("hook_original", Text),
    Column("favorite", Integer, nullable=False, server_default="0"),
    # en | fr — the language the current draft is written in. NULL on rows
    # drafted before languages existed, which were all English.
    Column("language", String(8)),
    # The email style this draft was written in, when the user switched it
    # for this one company; NULL = the profile's style.
    Column("template_id", String(32)),
    # "ntern" (the shared list) or the name of the user's own list.
    Column("source", Text),
    # "Switzerland" or "Lyon, France" — from the website address and research.
    Column("location", Text),
    UniqueConstraint("user_id", "email", name="uq_applications_user_email"),
    Index("idx_applications_user_status", "user_id", "status"),
    Index("idx_applications_user_updated", "user_id", "updated_at"),
    Index("idx_applications_user_sent_at", "user_id", "sent_at"),
    Index("idx_applications_user_favorite", "user_id", "favorite"),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
    Column("timestamp", Text, nullable=False),
    Column("stage", String(32), nullable=False),
    Column("message", Text, nullable=False),
    Column("detail", Text),
    Index("idx_events_application", "application_id", "timestamp"),
)

send_jobs = Table(
    "send_jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("total_items", Integer, nullable=False, server_default="0"),
    Column("sent_count", Integer, nullable=False, server_default="0"),
    Column("failed_count", Integer, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("completed_at", Text),
    # Which worker owns the job, and when it last proved it was alive — lets
    # a second worker take over a job whose owner died.
    Column("worker_id", String(64)),
    Column("heartbeat_at", Text),
    Index("idx_send_jobs_status", "status"),
    Index("idx_send_jobs_user", "user_id", "status"),
)

send_job_items = Table(
    "send_job_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, ForeignKey("send_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("application_id", Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("error_message", Text),
    Column("message_id", Text),
    Column("attempted_at", Text),
    Index("idx_send_job_items_job_status", "job_id", "status"),
    Index("idx_send_job_items_application", "application_id"),
)

user_meta = Table(
    "user_meta", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("key", String(100), primary_key=True),
    Column("value", Text),
)

cache_entries = Table(
    "cache_entries", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("kind", String(16), primary_key=True),      # research | draft
    Column("key", String(64), primary_key=True),       # hash of the recipient address
    Column("data", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

prep_runs = Table(
    "prep_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    # requested -> running -> done | failed | stopped
    Column("status", String(16), nullable=False),
    Column("stop_requested", Integer, nullable=False, server_default="0"),
    Column("limit_n", Integer),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("heartbeat_at", Text),
    Column("worker_id", String(64)),
    Column("summary", Text),
    Column("log", Text, nullable=False, server_default=""),
    # JSON list of application ids: a re-scan run that only redoes these.
    # NULL = the whole list, as usual.
    Column("targets", Text),
    # JSON list of the sources this run scans ("ntern", list names); NULL = all.
    Column("sources", Text),
    # JSON list of countries this run keeps to (the student's target places);
    # NULL = no filter.
    Column("countries", Text),
    Index("idx_prep_runs_status", "status"),
    Index("idx_prep_runs_user", "user_id", "id"),
)

SCHEMA_VERSION = 7

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_engine_url: str | None = None
_engine_lock = threading.Lock()


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL lets the web process read while a worker writes; busy_timeout
        # retries internally instead of raising "database is locked".
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA busy_timeout = 30000")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


def get_engine() -> Engine:
    """One engine (and connection pool) per process, for the configured URL.
    Rebuilt when the URL changes, which is how tests point it at a fresh
    database per test."""
    global _engine, _engine_url
    url = config.database_url()
    if _engine is not None and _engine_url == url:
        return _engine
    with _engine_lock:
        if _engine is not None and _engine_url == url:
            return _engine
        if _engine is not None:
            _engine.dispose()
        if url.startswith("sqlite"):
            engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False},
                                   pool_pre_ping=True)
            _configure_sqlite(engine)
        else:
            engine = create_engine(url, pool_size=config.int_setting("DB_POOL_SIZE", 10),
                                   max_overflow=config.int_setting("DB_MAX_OVERFLOW", 20),
                                   pool_pre_ping=True, pool_recycle=1800)
        _engine, _engine_url = engine, url
        _initialized.discard(url)
        return engine


def dispose_engine() -> None:
    global _engine, _engine_url
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine, _engine_url = None, None


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


@contextmanager
def tx():
    """A connection inside one transaction: committed on success, rolled back
    on any exception."""
    with get_engine().begin() as conn:
        yield conn


@contextmanager
def read():
    with get_engine().connect() as conn:
        yield conn


_initialized: set = set()


def init_schema() -> None:
    """Create every table and index. Idempotent, and cheap after the first
    call in a process."""
    engine = get_engine()
    if _engine_url in _initialized:
        return
    metadata.create_all(engine)
    with engine.begin() as conn:
        existing = inspect(conn).get_table_names()
        if "schema_version" not in existing:
            conn.execute(text("CREATE TABLE schema_version (version INTEGER NOT NULL)"))
            conn.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": 1})
        _migrate(conn)
    _initialized.add(_engine_url)


def _migrate(conn) -> None:
    """Bring an existing database up to SCHEMA_VERSION, one step at a time.
    create_all() only creates missing tables; columns added to an existing
    table need an explicit step here."""
    version = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar() or 1
    if version < 2:
        columns = {c["name"] for c in inspect(conn).get_columns("users")}
        if "username" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(64)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username ON users (username)"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (2)"))
    if version < 3:
        columns = {c["name"] for c in inspect(conn).get_columns("prep_runs")}
        if "targets" not in columns:
            conn.execute(text("ALTER TABLE prep_runs ADD COLUMN targets TEXT"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (3)"))
    if version < 4:
        columns = {c["name"] for c in inspect(conn).get_columns("applications")}
        if "template_id" not in columns:
            conn.execute(text("ALTER TABLE applications ADD COLUMN template_id VARCHAR(32)"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (4)"))
    if version < 5:
        columns = {c["name"] for c in inspect(conn).get_columns("users")}
        if "email_verified_at" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verified_at TEXT"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (5)"))
    if version < 6:
        for table, column, kind in (("profiles", "own_template", "TEXT"),
                                    ("company_list", "source", "TEXT NOT NULL DEFAULT ''"),
                                    ("applications", "source", "TEXT"),
                                    ("applications", "location", "TEXT"),
                                    ("prep_runs", "sources", "TEXT")):
            if column not in {c["name"] for c in inspect(conn).get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {kind}"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (6)"))
    if version < 7:
        if "countries" not in {c["name"] for c in inspect(conn).get_columns("prep_runs")}:
            conn.execute(text("ALTER TABLE prep_runs ADD COLUMN countries TEXT"))
        conn.execute(text("INSERT INTO schema_version (version) VALUES (7)"))


def _dialect_insert(conn, table):
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table)


def upsert(conn, table, values: dict, keys: list[str]) -> None:
    """Insert `values`, or update the non-key columns when a row with the same
    `keys` exists — one atomic statement on both engines."""
    statement = _dialect_insert(conn, table).values(**values)
    changes = {name: statement.excluded[name] for name in values if name not in keys}
    if changes:
        statement = statement.on_conflict_do_update(index_elements=keys, set_=changes)
    else:
        statement = statement.on_conflict_do_nothing(index_elements=keys)
    conn.execute(statement)


def insert_ignore(conn, table, values: dict, keys: list[str]) -> bool:
    """Insert unless a row with the same `keys` exists. True if inserted."""
    statement = _dialect_insert(conn, table).values(**values).on_conflict_do_nothing(index_elements=keys)
    return conn.execute(statement).rowcount == 1


def reset_sequences(conn) -> None:
    """After rows were inserted with explicit ids (the legacy import), move
    PostgreSQL's id sequences past them. SQLite needs nothing."""
    if conn.dialect.name != "postgresql":
        return
    for table in metadata.sorted_tables:
        pk = [c for c in table.primary_key.columns]
        if len(pk) == 1 and isinstance(pk[0].type, Integer) and pk[0].autoincrement is not False:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 0) + 1, false)"
            ))
