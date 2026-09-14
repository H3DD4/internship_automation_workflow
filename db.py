"""
Database layer (SQLite, zero setup required).
Stores one row per company application + a full event log per company so the
dashboard can show exactly what happened at each stage (research → write →
send → bounce), including the AI's reasoning (matched extras + why).
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "applications.db"


def get_connection():
    # timeout + WAL + busy_timeout: multiple worker threads (research/writer
    # pools) each open their own short-lived connection concurrently. WAL
    # mode lets readers and a writer proceed without blocking each other,
    # and busy_timeout makes SQLite automatically retry internally for up
    # to 30s instead of immediately raising "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        website TEXT,
        contact_name TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        industry TEXT,
        mission_or_focus TEXT,
        tone_of_voice TEXT,
        talking_points TEXT,
        matched_extra_mentions TEXT,
        match_reasons TEXT,
        subject TEXT,
        body TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sent_at TEXT
    );

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        stage TEXT NOT NULL,
        message TEXT NOT NULL,
        detail TEXT,
        FOREIGN KEY (application_id) REFERENCES applications (id)
    );

    CREATE TABLE IF NOT EXISTS send_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL DEFAULT 'pending',
        total_items INTEGER NOT NULL DEFAULT 0,
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS send_job_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        application_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        error_message TEXT,
        message_id TEXT,
        attempted_at TEXT,
        FOREIGN KEY (job_id) REFERENCES send_jobs (id),
        FOREIGN KEY (application_id) REFERENCES applications (id)
    );
    """)
    conn.commit()

    # Lightweight migration: add any columns that didn't exist in older DBs
    # created before this field was introduced, so upgrading never breaks.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
    for col_name, col_def in [
        ("contact_name", "TEXT"),
        ("send_attempts", "INTEGER DEFAULT 0"),
        ("last_attempt_at", "TEXT"),
        ("message_id", "TEXT"),
        ("error_code", "TEXT"),
    ]:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {col_name} {col_def}")
            conn.commit()

    # One-time status renames for the review-and-send architecture
    conn.execute("UPDATE applications SET status = 'ready' WHERE status = 'drafted'")
    conn.execute("UPDATE applications SET status = 'retry_wait' WHERE status = 'retry_later'")
    # Failed writer runs with research done → researched (retry write, no re-scrape)
    conn.execute("""
        UPDATE applications SET status = 'researched', error_message = NULL
        WHERE status = 'failed'
          AND (subject IS NULL OR subject = '')
          AND industry IS NOT NULL
    """)
    conn.commit()

    conn.close()


def _now():
    # Timezone-aware UTC (datetime.utcnow() is deprecated as of Python 3.12).
    # Every stored timestamp goes through this function, so the whole app
    # (including count_sent_today's daily-cap check below) shares one single,
    # unambiguous clock.
    return datetime.now(timezone.utc).isoformat()


def now():
    """Public timestamp helper for callers outside this module."""
    return _now()


def get_or_create_application(company_name: str, email: str, website: str, contact_name: str = "") -> int:
    conn = get_connection()
    row = conn.execute("SELECT id FROM applications WHERE email = ?", (email,)).fetchone()
    if row:
        conn.close()
        return row["id"]

    now = _now()
    cur = conn.execute(
        """INSERT INTO applications (company_name, email, website, contact_name, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
        (company_name, email, website, contact_name, now, now),
    )
    conn.commit()
    app_id = cur.lastrowid
    conn.close()
    return app_id


def get_application_by_email(email: str):
    conn = get_connection()
    row = conn.execute("SELECT * FROM applications WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_application(app_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [app_id]
    conn = get_connection()
    conn.execute(f"UPDATE applications SET {columns} WHERE id = ?", values)
    conn.commit()
    conn.close()


def log_event(app_id: int, stage: str, message: str, detail: dict = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO events (application_id, timestamp, stage, message, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (app_id, _now(), stage, message, json.dumps(detail) if detail is not None else None),
    )
    conn.commit()
    conn.close()


def get_events(app_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM events WHERE application_id = ? ORDER BY timestamp ASC", (app_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_applications():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_connection()
    rows = conn.execute("SELECT status, COUNT(*) as c FROM applications GROUP BY status").fetchall()
    conn.close()
    stats = {r["status"]: r["c"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


def count_sent_today() -> int:
    # sent_at is stored via _now() which is UTC — must compare against the
    # UTC calendar date, not the local one, or this under/overcounts by
    # however many hours the local timezone is offset near midnight.
    today_prefix = datetime.now(timezone.utc).date().isoformat()
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM applications WHERE status = 'sent' AND sent_at LIKE ?",
        (f"{today_prefix}%",),
    ).fetchone()
    conn.close()
    return row["c"]


def get_application_by_id(app_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_applications_paginated(status: str = None, search: str = None,
                                page: int = 1, limit: int = 50) -> tuple[list, int]:
    """Returns (rows, total_count) with pagination, optional status filter, optional search."""
    conn = get_connection()
    conditions = []
    params = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if search:
        search_pattern = f"%{search}%"
        conditions.append("(company_name LIKE ? OR email LIKE ?)")
        params.extend([search_pattern, search_pattern])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_row = conn.execute(
        f"SELECT COUNT(*) as c FROM applications {where}", params
    ).fetchone()
    total = count_row["c"]

    offset = (page - 1) * limit
    rows = conn.execute(
        f"SELECT * FROM applications {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def create_send_job(app_ids: list[int]) -> int:
    """Create a send job and its items. Returns the job_id."""
    conn = get_connection()
    now = _now()
    cur = conn.execute(
        "INSERT INTO send_jobs (status, total_items, created_at) VALUES ('pending', ?, ?)",
        (len(app_ids), now)
    )
    job_id = cur.lastrowid
    for app_id in app_ids:
        conn.execute(
            "INSERT INTO send_job_items (job_id, application_id, status) VALUES (?, ?, 'queued')",
            (job_id, app_id)
        )
        conn.execute(
            "UPDATE applications SET status = 'queued', updated_at = ? WHERE id = ?",
            (now, app_id)
        )
    conn.commit()
    conn.close()
    return job_id


def get_send_job(job_id: int) -> dict | None:
    conn = get_connection()
    job = conn.execute("SELECT * FROM send_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        return None

    items = conn.execute(
        "SELECT sji.*, a.company_name, a.email "
        "FROM send_job_items sji JOIN applications a ON sji.application_id = a.id "
        "WHERE sji.job_id = ? ORDER BY sji.id", (job_id,)
    ).fetchall()
    conn.close()

    result = dict(job)
    result["items"] = [dict(i) for i in items]

    # Compute live counts from items
    statuses = [i["status"] for i in result["items"]]
    result["queued"] = statuses.count("queued")
    result["sending"] = statuses.count("sending")
    result["sent_count"] = statuses.count("sent")
    result["failed_count"] = statuses.count("failed") + statuses.count("retry_wait")
    return result


def get_next_queued_item(job_id: int) -> dict | None:
    """Get the next queued item from a send job. Returns None when all are processed."""
    conn = get_connection()
    row = conn.execute(
        "SELECT sji.*, a.company_name, a.email, a.subject, a.body "
        "FROM send_job_items sji JOIN applications a ON sji.application_id = a.id "
        "WHERE sji.job_id = ? AND sji.status = 'queued' ORDER BY sji.id LIMIT 1",
        (job_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_send_job_item(item_id: int, **fields):
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [item_id]
    conn = get_connection()
    conn.execute(f"UPDATE send_job_items SET {columns} WHERE id = ?", values)
    conn.commit()
    conn.close()


def update_send_job(job_id: int, **fields):
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn = get_connection()
    conn.execute(f"UPDATE send_jobs SET {columns} WHERE id = ?", values)
    conn.commit()
    conn.close()


# Statuses where preparation is finished — never regenerate the email.
PREPARED_STATUSES = frozenset({"ready", "queued", "sending", "sent", "bounced"})

# Statuses where the sender owns the row — pipeline must not touch these.
SEND_IN_FLIGHT_STATUSES = frozenset({"queued", "sending"})


def draft_from_application(app: dict) -> dict | None:
    """Return {subject, body} from a DB row, or None if no draft stored."""
    if not app:
        return None
    subject = (app.get("subject") or "").strip()
    body = (app.get("body") or "").strip()
    if subject and body:
        return {"subject": subject, "body": body}
    return None


def research_context_from_application(app: dict) -> dict | None:
    """Rebuild research JSON from DB columns when the disk cache is missing."""
    if not app:
        return None
    try:
        talking_points = json.loads(app.get("talking_points") or "[]")
        matched = json.loads(app.get("matched_extra_mentions") or "[]")
        reasons = json.loads(app.get("match_reasons") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not app.get("industry") and not talking_points and not matched:
        return None
    return {
        "industry": app.get("industry") or "unknown",
        "mission_or_focus": app.get("mission_or_focus") or "",
        "tone_of_voice": app.get("tone_of_voice") or "unknown",
        "talking_points": talking_points,
        "matched_extra_mentions": matched,
        "match_reasons": reasons,
    }


def count_needing_preparation() -> int:
    """Rows in DB that still need research and/or writing."""
    conn = get_connection()
    row = conn.execute("""
        SELECT COUNT(*) AS c FROM applications
        WHERE status NOT IN ('sent', 'bounced', 'ready', 'queued', 'sending')
          AND NOT (
            subject IS NOT NULL AND subject != ''
            AND body IS NOT NULL AND body != ''
          )
    """).fetchone()
    conn.close()
    return row["c"]


def get_pending_send_jobs() -> list:
    """Return all send jobs that need processing (pending or running)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM send_jobs WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
