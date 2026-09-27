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


_initialized_path = None


def init_db():
    """Create tables/columns and run one-time migrations.

    Guarded to run its body only once per DB_PATH per process:
    dashboard/app.py calls this on every request (index(), company_detail()),
    and the one-time status-rename migrations below must not re-fire on
    every page load — among other things, a row that legitimately fails
    again later in the same process would otherwise get silently flipped
    back to 'researched' (and its error message lost) the next time someone
    just loads the page. Keying on DB_PATH (rather than a plain bool) means
    tests that repoint DB_PATH at a fresh tmp file per test still get a real
    init instead of a stale skip. CREATE TABLE IF NOT EXISTS / guarded ALTER
    TABLE are idempotent either way, but skipping them too avoids a
    redundant DB round-trip on every request.
    """
    global _initialized_path
    if _initialized_path == DB_PATH:
        return
    _initialized_path = DB_PATH

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

    -- Every dashboard page load filters/counts by status and orders by
    -- updated_at (get_applications_paginated, get_stats, count_* helpers),
    -- and the sender worker's get_next_queued_item filters send_job_items by
    -- (job_id, status) on every item it processes.
    CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);
    CREATE INDEX IF NOT EXISTS idx_applications_updated_at ON applications (updated_at);
    CREATE INDEX IF NOT EXISTS idx_send_job_items_job_status ON send_job_items (job_id, status);
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
    # Failed writer runs with research done → researched (retry write, no re-scrape).
    # error_message is deliberately kept (not cleared) — losing the error is
    # what made a genuine write failure look like it "disappeared" once this
    # ran again on the next page load, before init_db() was guarded to run
    # its migrations only once per process.
    conn.execute("""
        UPDATE applications SET status = 'researched'
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
    #
    # Filters on sent_at alone, NOT status = 'sent': the daily cap exists to
    # limit how many messages actually left this Gmail account today, which
    # doesn't change if a message we sent this morning is later found to
    # have bounced (status becomes 'bounced', sent_at is untouched) — it
    # still counts against today's quota either way.
    today_prefix = datetime.now(timezone.utc).date().isoformat()
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM applications WHERE sent_at LIKE ?",
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


# Statuses from which an application can be queued for sending. ("retry_later"
# was the pre-migration name for "retry_wait" — init_db() converts it once at
# startup, so no row can be in that status by the time this is checked.)
SENDABLE_STATUSES = frozenset({"ready", "failed", "retry_wait"})


def create_send_job(app_ids: list[int]) -> tuple[int | None, list[int]]:
    """Create a send job for the given application ids.

    Each id is claimed with a single atomic UPDATE ... WHERE status IN
    (sendable) — this is the only place that flips a row to 'queued', so two
    concurrent requests for the same row (a double-click, or the row's own
    "Send" button plus "Send selected" in the same instant) can never both
    win: the second UPDATE simply matches zero rows once the first has
    already moved the status away from a sendable state.

    Returns (job_id, queued_app_ids). queued_app_ids may be shorter than
    app_ids (or empty, with job_id None) if some/all were already claimed,
    not in a sendable status, or have no draft.
    """
    conn = get_connection()
    now = _now()
    placeholders = ",".join("?" * len(SENDABLE_STATUSES))
    queued_ids = []
    for app_id in app_ids:
        cur = conn.execute(
            f"""UPDATE applications SET status = 'queued', updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                  AND subject IS NOT NULL AND subject != ''
                  AND body IS NOT NULL AND body != ''""",
            (now, app_id, *SENDABLE_STATUSES)
        )
        if cur.rowcount == 1:
            queued_ids.append(app_id)

    if not queued_ids:
        conn.commit()
        conn.close()
        return None, []

    cur = conn.execute(
        "INSERT INTO send_jobs (status, total_items, created_at) VALUES ('pending', ?, ?)",
        (len(queued_ids), now)
    )
    job_id = cur.lastrowid
    for app_id in queued_ids:
        conn.execute(
            "INSERT INTO send_job_items (job_id, application_id, status) VALUES (?, ?, 'queued')",
            (job_id, app_id)
        )
    conn.commit()
    conn.close()
    return job_id, queued_ids


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


def recover_stale_preparation_rows() -> int:
    """Call once when a preparation run starts.

    A killed/crashed run (Ctrl+C mid-call, container restart) can leave rows
    in 'researching' or 'writing' forever — nothing re-visits them once the
    process that set that status is gone, so the dashboard would show them
    as permanently "in progress". Reset 'researching' back to 'pending' (redo
    the scrape+research) and 'writing' back to 'researched' (research is
    already saved — only the write step needs to redo). Returns the count.
    """
    conn = get_connection()
    now = _now()
    cur1 = conn.execute(
        "UPDATE applications SET status = 'pending', updated_at = ? WHERE status = 'researching'",
        (now,),
    )
    cur2 = conn.execute(
        "UPDATE applications SET status = 'researched', updated_at = ? WHERE status = 'writing'",
        (now,),
    )
    conn.commit()
    count = cur1.rowcount + cur2.rowcount
    conn.close()
    return count


def recover_interrupted_sends() -> int:
    """Call once when the sender worker starts (dashboard process launch).

    If the previous process was killed mid-send, a row's application and its
    send_job_item can be left in 'sending' forever — 'sending' isn't in
    SEND_IN_FLIGHT's queued-only pickup (get_next_queued_item only selects
    'queued'), so nothing would ever move it again. We don't know whether the
    email actually went out before the crash, so we don't guess 'sent' —
    we surface it as 'retry_wait' (sendable, editable) with a message telling
    the user to check their Sent folder first. Also sweeps 'queued' items
    left behind by a run that never got a job started, and closes out any
    'running' job that has nothing left to do.
    Returns the number of rows recovered.
    """
    conn = get_connection()
    now = _now()
    message = "Interrupted by a restart — check your Gmail Sent folder before resending."

    stuck_items = conn.execute(
        "SELECT id, application_id FROM send_job_items WHERE status = 'sending'"
    ).fetchall()
    for item in stuck_items:
        conn.execute(
            "UPDATE send_job_items SET status = 'retry_wait', error_message = ? WHERE id = ?",
            (message, item["id"]),
        )
        conn.execute(
            "UPDATE applications SET status = 'retry_wait', error_message = ?, updated_at = ? "
            "WHERE id = ?",
            (message, now, item["application_id"]),
        )

    # Orphaned applications stuck as 'queued'/'sending' with no live send_job_item
    # at all (e.g. the job row itself failed to insert) — return them to 'ready'.
    orphaned = conn.execute("""
        SELECT id FROM applications
        WHERE status IN ('queued', 'sending')
          AND id NOT IN (SELECT application_id FROM send_job_items WHERE status IN ('queued', 'sending'))
    """).fetchall()
    for row in orphaned:
        conn.execute(
            "UPDATE applications SET status = 'ready', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )

    # Close out any 'running' job with nothing left queued/sending.
    conn.execute("""
        UPDATE send_jobs SET status = 'completed', completed_at = ?
        WHERE status = 'running'
          AND id NOT IN (SELECT job_id FROM send_job_items WHERE status IN ('queued', 'sending'))
    """, (now,))

    conn.commit()
    recovered = len(stuck_items) + len(orphaned)
    conn.close()
    return recovered


def get_pending_send_jobs() -> list:
    """Return all send jobs that need processing (pending or running)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM send_jobs WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
