# Dashboard "Review & Send" — Queue-Based Architecture

## Problem Statement

The current pipeline runs research → writing → sending as one uninterruptible job. Emails are sent automatically without review. The AI provider's rate limits cause `429` → empty-body JSON crashes → companies marked `failed`. There is no way to review, edit, or selectively send from the dashboard.

## New Architecture

```
                    ┌─────────────────────┐
                    │      CSV / DB       │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ PREPARATION ENGINE  │
                    │                     │
                    │  Deduplication      │
                    │  Research (AI)      │
                    │  Writer (AI)        │
                    │  Rate limiting      │
                    │  Retry / backoff    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │      DATABASE       │
                    │                     │
                    │  status = "ready"   │
                    └──────────┬──────────┘
                               │
                               ▼
              ┌────────────────────────────────┐
              │          DASHBOARD             │
              │                                │
              │  Search / filter / pagination  │
              │  Review email content          │
              │  Edit subject + body           │
              │  Select emails via checkboxes  │
              │  "Send Selected" button        │
              │  Per-row "Send" button         │
              │  Live job progress tracking    │
              └───────────────┬────────────────┘
                              │
                    "Send Selected"
                              │
                              ▼
                    ┌─────────────────────┐
                    │     SEND JOB        │
                    │                     │
                    │  Created in DB      │
                    │  Returns instantly  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  BACKGROUND SENDER  │
                    │  (worker thread)    │
                    │                     │
                    │  Polls send_jobs    │
                    │  Anti-spam pacing   │
                    │  Retry / backoff    │
                    │  Error classifying  │
                    └──────────┬──────────┘
                               │
                               ▼
                         ┌───────────┐
                         │ Gmail API │
                         │ / SMTP    │
                         └───────────┘
```

> [!IMPORTANT]
> **Golden rule**: The preparation pipeline can run for hours without sending a single email. Sending only happens when the user explicitly selects emails in the dashboard and clicks "Send".

---

## Application Status Lifecycle

```
pending → researching → researched → writing → ready
                                                  │
                                                  ├── (user edits) → ready
                                                  │
                                                  ├── (user selects + sends) → queued → sending → sent
                                                  │                                        │
                                                  │                                        ├── failed (permanent)
                                                  │                                        └── retry_wait → queued (auto-retry)
                                                  │
                                                  └── bounced (detected later by bounce_checker.py)
```

**Edit protection rules:**
| Status | Editable? | Why |
|--------|-----------|-----|
| `ready` | ✅ Yes | Waiting for review |
| `failed` | ✅ Yes | User can fix and retry |
| `retry_wait` | ✅ Yes | User can fix before retry |
| `queued` | ❌ No | In a send job, about to send |
| `sending` | ❌ No | Currently being sent |
| `sent` | ❌ No | Historical record — what was actually sent |
| `bounced` | ❌ No | Already delivered and bounced |

---

## Database Schema Changes

### Current `applications` table — columns to ADD via migration

```sql
-- New columns added to existing applications table:
ALTER TABLE applications ADD COLUMN send_attempts INTEGER DEFAULT 0;
ALTER TABLE applications ADD COLUMN last_attempt_at TEXT;
ALTER TABLE applications ADD COLUMN message_id TEXT;
ALTER TABLE applications ADD COLUMN error_code TEXT;
```

### New table: `send_jobs`

```sql
CREATE TABLE IF NOT EXISTS send_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending',
    -- status values: pending, running, completed, cancelled
    total_items INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
```

### New table: `send_job_items`

```sql
CREATE TABLE IF NOT EXISTS send_job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    application_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    -- status values: queued, sending, sent, failed, retry_wait, skipped
    error_message TEXT,
    message_id TEXT,
    attempted_at TEXT,
    FOREIGN KEY (job_id) REFERENCES send_jobs (id),
    FOREIGN KEY (application_id) REFERENCES applications (id)
);
```

### Why send_jobs + send_job_items?

When the user clicks "Send 50 emails", the dashboard creates ONE `send_job` row with 50 `send_job_items`. The HTTP request returns **immediately** with the `job_id`. A background worker thread processes the items one-by-one. The frontend polls `GET /api/send-job/<job_id>` to show live progress. The browser can disconnect/refresh without affecting sending.

---

## Phased Implementation

### Phase 1: Foundation (DB + AI Client + Mail Service)
### Phase 2: Preparation Engine (Pipeline without sending)
### Phase 3: Dashboard UI (Review + Edit + Send controls)
### Phase 4: Send Queue System (Background worker + job tracking)

---

## Phase 1: Foundation

---

### [MODIFY] [db.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/db.py)

**Changes**: Add new tables + migration columns + new query functions for send jobs.

#### 1a. Extend `init_db()` (currently lines 30-73)

After the existing `CREATE TABLE IF NOT EXISTS events (...)` block inside the `executescript("""...""")` (line 53-62), **append** these two new CREATE TABLE statements inside the same `executescript` call:

```sql
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
```

#### 1b. Extend the migration block (currently lines 66-72)

After the existing `contact_name` migration, add migrations for the 4 new application columns:

```python
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
```

This replaces the existing single `if "contact_name" not in existing_cols:` block at lines 69-71.

#### 1c. Add new query functions at the end of the file (after `count_sent_today`, after line 175)

```python
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


def get_pending_send_jobs() -> list:
    """Return all send jobs that need processing (pending or running)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM send_jobs WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

---

### [MODIFY] [ai_client.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/ai_client.py)

**Why**: Fix the JSON crash loop. Add configurable rate limiter + retry with backoff + status-code check before JSON parse.

**Replace the entire file** (currently 39 lines) with:

```python
"""Small OpenAI-compatible chat client used by both pipeline agents.

Includes:
- Thread-safe global rate limiter (configurable RPM)
- Retry with exponential backoff + jitter on 429, 5xx, empty body, bad JSON
- Status-code check BEFORE json parsing (fixes the crash loop)
"""

import json
import time
import random
import threading
from types import SimpleNamespace

import requests


class RateLimiter:
    """Thread-safe token-bucket-style rate limiter. One global instance shared
    across all worker threads — no matter how many research/writer workers,
    total API calls never exceed max_per_minute."""

    def __init__(self, max_per_minute: int = 800):
        self.interval = 60.0 / max_per_minute
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self._last_call = time.time()


# Single global instance — configurable via AI_MAX_RPM env var at startup
_global_rate_limiter = None


def get_global_rate_limiter(max_rpm: int = None) -> RateLimiter:
    global _global_rate_limiter
    if _global_rate_limiter is None:
        import os
        rpm = max_rpm or int(os.getenv("AI_MAX_RPM", "800"))
        _global_rate_limiter = RateLimiter(max_per_minute=rpm)
    return _global_rate_limiter


class CompatibleAIClient:
    def __init__(self, api_key: str, base_url: str, rate_limiter: RateLimiter = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or get_global_rate_limiter()
        self.messages = _Messages(self)


class _Messages:
    def __init__(self, client: CompatibleAIClient):
        self.client = client

    def create(self, model: str, max_tokens: int, system: str, messages: list,
               max_attempts: int = 5):
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }

        for attempt in range(1, max_attempts + 1):
            # Rate-limit before every call
            self.client.rate_limiter.wait()

            # --- Network-level errors ---
            try:
                response = requests.post(
                    f"{self.client.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.client.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=120,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI provider unreachable after {max_attempts} attempts: {e}"
                    ) from e
                self._backoff(attempt, f"connection error: {e}")
                continue

            # --- Retryable HTTP status codes (429 rate-limit, 5xx server error) ---
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI provider error {response.status_code} after "
                        f"{max_attempts} attempts: {response.text[:300]}"
                    )
                self._backoff(attempt, f"HTTP {response.status_code}")
                continue

            # --- Non-retryable HTTP errors (401, 403, 404, etc.) ---
            if response.status_code != 200:
                raise RuntimeError(
                    f"AI provider error {response.status_code}: {response.text[:500]}"
                )

            # --- Safe JSON parsing (guard against empty 200 body) ---
            body_text = response.text.strip()
            if not body_text:
                if attempt == max_attempts:
                    raise RuntimeError(
                        "AI provider returned empty response body after retries"
                    )
                self._backoff(attempt, "empty response body")
                continue

            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI provider returned invalid JSON: {e}\n"
                        f"Body: {body_text[:300]}"
                    ) from e
                self._backoff(attempt, f"bad JSON: {e}")
                continue

            # --- Extract content ---
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError) as e:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI response missing expected fields: {e}\n"
                        f"Data: {json.dumps(data)[:300]}"
                    ) from e
                self._backoff(attempt, f"malformed response: {e}")
                continue

            return SimpleNamespace(content=[SimpleNamespace(text=content)])

        raise RuntimeError("AI call failed: exhausted all retry attempts")

    @staticmethod
    def _backoff(attempt: int, reason: str):
        delay = (2 ** attempt) + random.uniform(0, 1)
        print(f"    [ai-retry] attempt {attempt} failed ({reason}) — "
              f"retrying in {delay:.1f}s")
        time.sleep(delay)
```

---

### [NEW] [mail_service.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/mail_service.py)

**Why**: Wraps `mailer.py` with a clean interface returning structured results. The dashboard and sender worker call this — they never import mailer exceptions directly.

```python
"""
Mail service — clean wrapper around mailer.py.

Returns structured results instead of raising raw exceptions.
This decouples the send logic from Gmail-specific exception handling,
so the dashboard/sender worker don't need to understand SMTP error codes.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

from mailer import send_email, PermanentSendError, TransientSendError, AuthenticationError


@dataclass
class SendResult:
    success: bool
    retryable: bool = False
    error_code: str = ""
    message: str = ""
    message_id: str = ""
    provider: str = ""


def send(app: dict) -> SendResult:
    """Send one email for an application dict (from db.get_application_by_id).
    
    Returns a SendResult — never raises an exception to the caller.
    """
    load_dotenv(override=True)
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    cv_path = os.getenv("CV_FILE_PATH", "")

    if not app.get("subject") or not app.get("body"):
        return SendResult(
            success=False, retryable=False,
            error_code="no_draft", message="No email draft (missing subject or body)."
        )

    if not cv_path:
        return SendResult(
            success=False, retryable=False,
            error_code="no_cv", message="CV_FILE_PATH not set in .env."
        )

    # Detect which provider will be used
    provider = "smtp"
    try:
        from google_auth_helper import token_exists, get_credentials
        if token_exists() and get_credentials() is not None:
            provider = "gmail_api"
    except ImportError:
        pass

    try:
        send_email(
            gmail_address, gmail_password,
            app["email"], app["subject"], app["body"], cv_path
        )
        return SendResult(
            success=True,
            message=f"Sent to {app['email']}",
            provider=provider
        )
    except AuthenticationError as e:
        return SendResult(
            success=False, retryable=False,
            error_code="auth_failed", message=str(e), provider=provider
        )
    except PermanentSendError as e:
        return SendResult(
            success=False, retryable=False,
            error_code="permanent", message=str(e), provider=provider
        )
    except TransientSendError as e:
        return SendResult(
            success=False, retryable=True,
            error_code="transient", message=str(e), provider=provider
        )
    except Exception as e:
        return SendResult(
            success=False, retryable=True,
            error_code="unknown", message=str(e), provider=provider
        )
```

---

### [NEW] [sender_worker.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/sender_worker.py)

**Why**: Background thread that polls the DB for pending send jobs, processes items one-by-one with anti-spam pacing, updates DB status in real-time. Dashboard creates jobs; this worker fulfils them.

```python
"""
Background sender worker — processes send_jobs from the database.

Runs as a daemon thread started by the dashboard. Polls for pending/running
jobs, sends queued items one-by-one with anti-spam pacing, updates the DB
after each send so the dashboard can show live progress via polling.

NEVER call this from the pipeline. Only the dashboard starts this worker.
"""

import os
import random
import threading
import time

import db
import mail_service


class SenderWorker:
    """Daemon thread that processes send jobs from the database."""

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._poll_interval = 2  # seconds between job polls
        self._min_delay = int(os.getenv("MIN_DELAY_SECONDS", "45"))
        self._max_delay = int(os.getenv("MAX_DELAY_SECONDS", "120"))
        self._max_per_day = int(os.getenv("MAX_EMAILS_PER_DAY", "20"))

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return  # already running
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                         name="sender-worker")
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self):
        """Main loop: poll for pending jobs, process their items."""
        while not self._stop_event.is_set():
            try:
                jobs = db.get_pending_send_jobs()
                for job in jobs:
                    if self._stop_event.is_set():
                        break
                    self._process_job(job["id"])
            except Exception as e:
                print(f"  [sender-worker] error in loop: {e}")

            # Wait before next poll
            self._stop_event.wait(timeout=self._poll_interval)

    def _process_job(self, job_id: int):
        """Process all queued items in a single send job."""
        db.update_send_job(job_id, status="running", started_at=db.now())

        auth_failed = False
        items_sent_this_job = 0

        while not self._stop_event.is_set():
            # Check daily cap
            sent_today = db.count_sent_today()
            if sent_today >= self._max_per_day:
                print(f"  [sender-worker] daily cap ({self._max_per_day}) reached, "
                      f"pausing job {job_id}")
                # Mark remaining items as skipped (they stay queued for next day)
                break

            # Get next queued item
            item = db.get_next_queued_item(job_id)
            if item is None:
                break  # all items processed

            app_id = item["application_id"]
            item_id = item["id"]

            # Mark as sending
            db.update_send_job_item(item_id, status="sending",
                                     attempted_at=db.now())
            db.update_application(app_id, status="sending")

            # Get full application data
            app = db.get_application_by_id(app_id)
            if not app:
                db.update_send_job_item(item_id, status="failed",
                                         error_message="Application not found")
                continue

            # Send via mail_service
            result = mail_service.send(app)

            if result.success:
                db.update_send_job_item(
                    item_id, status="sent",
                    message_id=result.message_id
                )
                db.update_application(
                    app_id, status="sent", sent_at=db.now(),
                    error_message=None, message_id=result.message_id,
                    send_attempts=app.get("send_attempts", 0) + 1,
                    last_attempt_at=db.now()
                )
                db.log_event(app_id, "send",
                             f"Email sent successfully via {result.provider}.")
                items_sent_this_job += 1
                print(f"  [sender-worker] ✓ sent to {app['email']}")

            elif result.error_code == "auth_failed":
                # Auth failure is account-wide — stop the entire job
                db.update_send_job_item(item_id, status="failed",
                                         error_message=result.message)
                db.update_application(app_id, status="ready",
                                       error_message=result.message,
                                       error_code=result.error_code)
                db.log_event(app_id, "send",
                             "Auth failed — job halted.",
                             detail={"error": result.message})
                auth_failed = True
                print(f"  [sender-worker] ✗ auth failed — halting job {job_id}")
                # Revert remaining queued items back to ready
                self._revert_remaining(job_id)
                break

            elif result.retryable:
                db.update_send_job_item(item_id, status="retry_wait",
                                         error_message=result.message)
                db.update_application(
                    app_id, status="retry_wait",
                    error_message=result.message,
                    error_code=result.error_code,
                    send_attempts=app.get("send_attempts", 0) + 1,
                    last_attempt_at=db.now()
                )
                db.log_event(app_id, "send",
                             f"Transient send failure: {result.message}",
                             detail={"error": result.message})
                print(f"  [sender-worker] ~ retry_wait for {app['email']}: "
                      f"{result.message}")

            else:
                db.update_send_job_item(item_id, status="failed",
                                         error_message=result.message)
                db.update_application(
                    app_id, status="failed",
                    error_message=result.message,
                    error_code=result.error_code,
                    send_attempts=app.get("send_attempts", 0) + 1,
                    last_attempt_at=db.now()
                )
                db.log_event(app_id, "send",
                             f"Permanent send failure: {result.message}",
                             detail={"error": result.message})
                print(f"  [sender-worker] ✗ failed for {app['email']}: "
                      f"{result.message}")

            # Anti-spam pacing between sends (only if more items remain)
            next_item = db.get_next_queued_item(job_id)
            if next_item and not self._stop_event.is_set():
                delay = random.randint(self._min_delay, self._max_delay)
                print(f"  [sender-worker] anti-spam pacing: {delay}s")
                self._stop_event.wait(timeout=delay)

        # Mark job completed
        db.update_send_job(job_id, status="completed", completed_at=db.now())

    def _revert_remaining(self, job_id: int):
        """On auth failure: revert all still-queued items back to 'ready'."""
        conn = db.get_connection()
        items = conn.execute(
            "SELECT application_id FROM send_job_items "
            "WHERE job_id = ? AND status = 'queued'", (job_id,)
        ).fetchall()
        for item in items:
            conn.execute(
                "UPDATE applications SET status = 'ready', updated_at = ? "
                "WHERE id = ?", (db.now(), item["application_id"])
            )
        conn.execute(
            "UPDATE send_job_items SET status = 'skipped' "
            "WHERE job_id = ? AND status = 'queued'", (job_id,)
        )
        conn.commit()
        conn.close()


# Module-level singleton
_worker = SenderWorker()


def ensure_running():
    """Start the sender worker if not already running. Safe to call multiple times."""
    _worker.start()


def is_running() -> bool:
    return _worker.is_running
```

---

## Phase 2: Preparation Engine

---

### [MODIFY] [pipeline.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/pipeline.py)

**What changes**: Remove ALL of stage 3 (sender). Pipeline ends after writing. Status goes to `ready` instead of `drafted`.

#### 2a. Remove unused imports (line 59)

**Delete** the entire mailer import line:
```python
from mailer import send_email, PermanentSendError, TransientSendError, AuthenticationError
```

#### 2b. Remove `READY_QUEUE_PUT_TIMEOUT_SECONDS` constant (line 63)

**Delete** this line.

#### 2c. Simplify `Pipeline.__init__` (lines 67-88)

Replace with (remove `ready_queue`, `ready_queue_size`, `_cap_reached`, `_auth_failed`):

```python
class Pipeline:
    def __init__(self, client, cfg, model: str, dry_run: bool,
                 research_workers: int = 3, writer_workers: int = 2):
        self.client = client
        self.cfg = cfg
        self.model = model
        self.dry_run = dry_run

        self.research_pool = ThreadPoolExecutor(max_workers=research_workers,
                                                 thread_name_prefix="research")
        self.writer_pool = ThreadPoolExecutor(max_workers=writer_workers,
                                               thread_name_prefix="writer")

        self._shutdown = threading.Event()

        self._lock = threading.Lock()
        self.total_companies = 0
        self.terminal_count = 0
        self.results = {"ready": 0, "failed": 0, "skipped": 0}
```

#### 2d. Delete `auth_failed` property (lines 92-95)

Remove entirely.

#### 2e. Stage 1 `_research_task` (lines 108-164)

**Keep exactly as-is.** No changes. It correctly submits to `self.writer_pool`.

#### 2f. Stage 2 `_writer_task` (lines 168-217)

Replace lines 184-208 (from `db.update_application(app_id, status="drafted",...` through `self._record("drafted")`) with:

```python
                db.update_application(app_id, status="ready",
                                       subject=email_content["subject"],
                                       body=email_content["body"])
                db.log_event(app_id, "write",
                             f"Draft ready: \"{email_content['subject']}\"",
                             detail=email_content)

            # Pipeline does NOT send — mark as ready for dashboard review
            print(f"  [ready] {company_name} — email ready for review.")
            self._record("ready")  # terminal: draft ready for review
```

Note: `"ready"` replaces `"drafted"` as the status. The `if self.dry_run:` block, the `ready_queue.put()`, and the `queue.Full` handler are **all deleted** — the pipeline never sends, so dry_run vs live run is the same at this stage.

#### 2g. Delete entire Stage 3 (lines 219-329)

Delete `_run_sender` method and `_sleep_interruptible` method completely.

#### 2h. Simplify `run()` method (lines 333-365)

Replace with:

```python
    def run(self, companies_rows):
        """
        companies_rows: list of (company_name, email, website, contact_name) tuples.
        Returns the results dict. Safe to Ctrl+C.
        """
        self.total_companies = len(companies_rows)
        if self.total_companies == 0:
            return self.results

        for row in companies_rows:
            if self._shutdown.is_set():
                break
            self.research_pool.submit(self._research_task, row)

        try:
            # Wait for all research + writer tasks to finish
            self.research_pool.shutdown(wait=True,
                                         cancel_futures=self._shutdown.is_set())
            self.writer_pool.shutdown(wait=True,
                                       cancel_futures=self._shutdown.is_set())
        except KeyboardInterrupt:
            print("\nInterrupted — shutting down (completed work is saved)...")
            self._shutdown.set()
            self.research_pool.shutdown(wait=False, cancel_futures=True)
            self.writer_pool.shutdown(wait=False, cancel_futures=True)

        return self.results
```

#### 2i. Remove unused imports from top

Remove `queue` (line 47) and `random` (line 48). Keep `threading`, `time`, `os`, `json`.

---

### [MODIFY] [main.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/main.py)

#### Changes:

1. **Line 277** — Remove ready-queue-size from the print statement. Change to:
```python
    print(f"Pipeline: {research_workers} research worker(s), "
          f"{writer_workers} writer worker(s).\n")
```

2. **Lines 283-288** — Remove `ready_queue_size` from `Pipeline()` constructor:
```python
    pipeline = Pipeline(
        client, cfg, cfg["ai_model"], dry_run=args.dry_run,
        research_workers=research_workers,
        writer_workers=writer_workers,
    )
```

3. **Lines 295-299** — Delete the `if pipeline.auth_failed:` block entirely.

4. **Line 309** — Change the closing message to:
```python
    print("\nOpen the dashboard to review and send: python dashboard/app.py")
```

---

## Phase 3: Dashboard UI

---

### [MODIFY] [dashboard/app.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/app.py)

#### 3a. Add imports (after line 35 `import db`)

```python
import mail_service
import sender_worker
```

#### 3b. Update `STATUS_LABELS` dict (lines 48-59)

Change `"drafted"` to `"ready"` and update labels:

```python
STATUS_LABELS = {
    "pending": "Pending",
    "researching": "Researching",
    "researched": "Researched",
    "writing": "Writing email",
    "ready": "Ready to send",
    "queued": "Queued",
    "sending": "Sending",
    "sent": "Sent",
    "failed": "Failed",
    "retry_wait": "Retry later",
    "bounced": "Bounced",
}
```

#### 3c. Update `IN_PROGRESS_STATUSES` (line 63)

Add `"queued"`:
```python
IN_PROGRESS_STATUSES = {"researching", "researched", "writing", "sending", "queued"}
```

#### 3d. In the `index()` route (lines 219-267)

After `db.init_db()` on line 221, **start the sender worker**:
```python
    sender_worker.ensure_running()
```

Update `ordered_stats` (lines 237-248). Replace `"drafted"` with `"ready"` and add `"queued"`:
```python
    ordered_stats = [
        ("total", "Total companies"),
        ("sent", "Sent"),
        ("ready", "Ready to send"),
        ("queued", "Queued"),
        ("failed", "Failed"),
        ("bounced", "Bounced"),
        ("retry_later", "Retry later"),
        ("pending", "Pending"),
        ("researching", "Researching"),
        ("researched", "Researched"),
        ("writing", "Writing"),
    ]
```

Update the `completed_count` line (line 252) to include `"ready"`:
```python
    completed_count = sum(stats.get(s, 0) for s in
                          ("sent", "failed", "bounced", "retry_later", "ready"))
```

Pass `active_job` info to the template. After `max_per_day` (line 254), add:
```python
    active_jobs = db.get_pending_send_jobs()
    active_job = active_jobs[0] if active_jobs else None
    active_job_detail = db.get_send_job(active_job["id"]) if active_job else None
```

Add `active_job=active_job_detail` to the `render_template()` call.

#### 3e. Update `edit_email` route (lines 605-619)

Add edit protection. After fetching the application (line 607), add a status check:

```python
    EDITABLE_STATUSES = {"ready", "failed", "retry_wait"}
    if application["status"] not in EDITABLE_STATUSES:
        flash(f"Cannot edit — email is currently '{application['status']}'.", "error")
        return redirect(url_for("company_detail", app_id=app_id))
```

Change `status="drafted"` to `status="ready"` on line 613:
```python
    db.update_application(app_id, subject=subject, body=body, status="ready",
                          error_message=None)
```

#### 3f. Add new API routes (before `if __name__ == "__main__":` block)

**Route 1: `POST /api/send-job`** — create a send job (returns instantly):

```python
@app.post("/api/send-job")
def api_create_send_job():
    """Create a send job from selected application IDs. Returns immediately."""
    payload = request.get_json(silent=True) or {}
    app_ids = payload.get("app_ids", [])

    if not app_ids:
        return jsonify({"ok": False, "message": "No emails selected."})

    # Validate all IDs exist and are in a sendable status
    sendable_statuses = {"ready", "failed", "retry_wait"}
    valid_ids = []
    for aid in app_ids:
        aid = int(aid)
        app = db.get_application_by_id(aid)
        if not app:
            continue
        if app["status"] not in sendable_statuses:
            continue
        if not app.get("subject") or not app.get("body"):
            continue
        valid_ids.append(aid)

    if not valid_ids:
        return jsonify({"ok": False,
                        "message": "None of the selected emails are ready to send."})

    job_id = db.create_send_job(valid_ids)

    # Ensure sender worker is running
    sender_worker.ensure_running()

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "total": len(valid_ids),
        "message": f"Send job created for {len(valid_ids)} email(s)."
    })
```

**Route 2: `GET /api/send-job/<job_id>`** — poll job progress:

```python
@app.get("/api/send-job/<int:job_id>")
def api_send_job_status(job_id):
    """Poll send job progress."""
    job = db.get_send_job(job_id)
    if not job:
        return jsonify({"ok": False, "message": "Job not found."}), 404

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "status": job["status"],
        "total": job["total_items"],
        "queued": job.get("queued", 0),
        "sending": job.get("sending", 0),
        "sent": job.get("sent_count", 0),
        "failed": job.get("failed_count", 0),
        "items": [
            {
                "app_id": item["application_id"],
                "company": item.get("company_name", ""),
                "email": item.get("email", ""),
                "status": item["status"],
                "error": item.get("error_message", ""),
            }
            for item in job.get("items", [])
        ],
    })
```

**Route 3: `GET /api/applications`** — paginated list:

```python
@app.get("/api/applications")
def api_applications():
    """Paginated applications list for frontend refresh."""
    status = request.args.get("status", "").strip() or None
    search = request.args.get("search", "").strip() or None
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    limit = min(limit, 200)  # hard cap

    rows, total = db.get_applications_paginated(
        status=status, search=search, page=page, limit=limit
    )
    for a in rows:
        a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])

    return jsonify({
        "applications": rows,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
    })
```

**Route 4: `POST /api/update-draft`** — inline edit with protection:

```python
@app.post("/api/update-draft")
def api_update_draft():
    """Update subject/body for an application (with edit protection)."""
    payload = request.get_json(silent=True) or {}
    app_id = payload.get("app_id")
    subject = (payload.get("subject") or "").strip()
    body = (payload.get("body") or "").strip()

    if not app_id or not subject or not body:
        return jsonify({"ok": False, "message": "app_id, subject, and body required."})

    app = db.get_application_by_id(int(app_id))
    if not app:
        return jsonify({"ok": False, "message": "Application not found."})

    EDITABLE_STATUSES = {"ready", "failed", "retry_wait"}
    if app["status"] not in EDITABLE_STATUSES:
        return jsonify({"ok": False,
                        "message": f"Cannot edit — status is '{app['status']}'."})

    db.update_application(int(app_id), subject=subject, body=body,
                          status="ready", error_message=None)
    db.log_event(int(app_id), "write", "Email edited from dashboard.")

    import cache_store
    cache_store.save_draft(app["email"], {"subject": subject, "body": body})

    return jsonify({"ok": True, "message": "Draft saved."})
```

#### 3g. Start sender worker on app startup

At the bottom, change the `if __name__` block (lines 665-668):

```python
if __name__ == "__main__":
    db.init_db()
    sender_worker.ensure_running()
    print("Dashboard running at http://127.0.0.1:5050")
    print("Background sender worker started.")
    app.run(host="127.0.0.1", port=5050, debug=False)
```

---

### [MODIFY] [index.html](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/templates/index.html)

#### Table changes — add checkboxes + send buttons

Replace the table header `<tr>` (lines 211-219) with:
```html
      <tr>
        <th><input type="checkbox" id="select-all-cb" title="Select all sendable"></th>
        <th>Company</th>
        <th>Email</th>
        <th>Status</th>
        <th>Extra mentions</th>
        <th>Subject line</th>
        <th>Updated</th>
        <th></th>
      </tr>
```

Replace each table body row (lines 222-243) with:
```html
      {% for a in applications %}
      <tr data-search="{{ (a.company_name ~ ' ' ~ a.email) | lower }}"
          data-status="{{ a.status }}" data-app-id="{{ a.id }}">
        <td>
          {% if a.status in ('ready', 'failed', 'retry_wait') and a.subject %}
          <input type="checkbox" class="row-select-cb" data-app-id="{{ a.id }}">
          {% endif %}
        </td>
        <td class="cell-company">{{ a.company_name }}</td>
        <td class="cell-mono">{{ a.email }}</td>
        <td>
          <span class="status-pill status-{{ a.status }}">{{ a.status_label }}</span>
          {% if a.error_short %}
          <div class="row-error" title="{{ a.error_message }}">{{ a.error_short }}</div>
          {% endif %}
        </td>
        <td>
          {% if a.matched_extra_mentions_list %}
            {% for m in a.matched_extra_mentions_list %}<span class="chip">{{ m }}</span>{% endfor %}
          {% else %}
            <span class="cell-empty">—</span>
          {% endif %}
        </td>
        <td class="cell-subject">{{ a.subject or "—" }}</td>
        <td class="cell-mono cell-time">{{ a.updated_at[:16].replace("T", " ") if a.updated_at else "—" }}</td>
        <td class="row-actions">
          {% if a.status in ('ready', 'failed', 'retry_wait') and a.subject %}
          <button class="button button--primary button--sm send-one-btn"
                  data-app-id="{{ a.id }}" type="button">Send</button>
          {% endif %}
          <a class="detail-link" href="{{ url_for('company_detail', app_id=a.id) }}">View →</a>
        </td>
      </tr>
      {% endfor %}
```

#### Add batch action bar + send toast HTML

Insert **after** the `</section>` closing `cap-bar-section` (after line 178) and **before** `<section class="stats-row">` (line 180):

```html
<div class="batch-actions" id="batch-actions" style="display: none;">
  <div class="batch-actions-inner">
    <span id="selected-count">0 selected</span>
    <button class="button button--primary" id="send-selected-btn" type="button">Send Selected</button>
    <button class="button button--ghost" id="deselect-all-btn" type="button">Deselect all</button>
  </div>
</div>

<div class="send-toast" id="send-toast" style="display: none;">
  <div class="send-toast-inner">
    <span id="send-toast-text">Sending...</span>
    <div class="send-toast-bar"><div class="send-toast-fill" id="send-toast-fill"></div></div>
  </div>
</div>
```

#### Add the send JavaScript

Append this **inside** the existing `<script>` block, after the auto-refresh IIFE (after line 417), before the closing `</script>`:

```javascript
// --- Checkbox selection + send via job queue ---
(function () {
  const selectAllCb = document.getElementById("select-all-cb");
  const batchBar = document.getElementById("batch-actions");
  const countEl = document.getElementById("selected-count");
  const sendBtn = document.getElementById("send-selected-btn");
  const deselectBtn = document.getElementById("deselect-all-btn");
  const toast = document.getElementById("send-toast");
  const toastText = document.getElementById("send-toast-text");
  const toastFill = document.getElementById("send-toast-fill");
  if (!selectAllCb) return;

  function cbs() { return Array.from(document.querySelectorAll(".row-select-cb")); }
  function checked() { return cbs().filter(c => c.checked).map(c => +c.dataset.appId); }

  function updateBar() {
    const ids = checked();
    batchBar.style.display = ids.length ? "flex" : "none";
    countEl.textContent = ids.length + " selected";
  }

  selectAllCb.addEventListener("change", () => {
    cbs().filter(c => c.closest("tr").style.display !== "none")
         .forEach(c => { c.checked = selectAllCb.checked; });
    updateBar();
  });
  document.addEventListener("change", e => {
    if (e.target.classList.contains("row-select-cb")) updateBar();
  });
  deselectBtn.addEventListener("click", () => {
    cbs().forEach(c => { c.checked = false; });
    selectAllCb.checked = false;
    updateBar();
  });

  function showToast(text, pct) {
    toast.style.display = "flex";
    toastText.textContent = text;
    toastFill.style.width = (pct * 100) + "%";
  }
  function hideToast() { toast.style.display = "none"; }

  async function createSendJob(appIds) {
    showToast("Creating send job...", 0);
    sendBtn.disabled = true;

    try {
      const resp = await fetch("/api/send-job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app_ids: appIds }),
      });
      const data = await resp.json();
      if (!data.ok) {
        showToast("✗ " + data.message, 0);
        setTimeout(hideToast, 3000);
        sendBtn.disabled = false;
        return;
      }
      // Poll for progress
      pollJobProgress(data.job_id, data.total);
    } catch (err) {
      showToast("✗ Network error: " + err.message, 0);
      setTimeout(hideToast, 4000);
      sendBtn.disabled = false;
    }
  }

  async function pollJobProgress(jobId, total) {
    const poll = async () => {
      try {
        const resp = await fetch(`/api/send-job/${jobId}`);
        const data = await resp.json();
        if (!data.ok) return;

        const done = data.sent + data.failed;
        const pct = total > 0 ? done / total : 0;
        showToast(
          `Sending: ${data.sent} sent, ${data.failed} failed, ` +
          `${data.queued + data.sending} remaining`,
          pct
        );

        // Update table rows in real-time
        (data.items || []).forEach(item => {
          const row = document.querySelector(`tr[data-app-id="${item.app_id}"]`);
          if (!row) return;
          const pill = row.querySelector(".status-pill");
          if (pill) {
            pill.className = "status-pill status-" + item.status;
            const labels = {sent:"Sent", failed:"Failed", sending:"Sending",
                           queued:"Queued", retry_wait:"Retry later"};
            pill.textContent = labels[item.status] || item.status;
          }
          if (item.status === "sent") {
            const cb = row.querySelector(".row-select-cb");
            if (cb) { cb.checked = false; cb.disabled = true; }
            const btn = row.querySelector(".send-one-btn");
            if (btn) btn.remove();
          }
        });

        if (data.status === "completed") {
          showToast(`✓ Done: ${data.sent} sent, ${data.failed} failed.`, 1);
          setTimeout(() => { hideToast(); window.location.reload(); }, 2500);
          sendBtn.disabled = false;
          updateBar();
          return;
        }
        setTimeout(poll, 2000);  // poll every 2s
      } catch (err) {
        setTimeout(poll, 3000);
      }
    };
    poll();
  }

  sendBtn.addEventListener("click", () => {
    const ids = checked();
    if (!ids.length) return;
    if (!confirm(`Send ${ids.length} email(s) now?`)) return;
    createSendJob(ids);
  });

  // Single-row send buttons
  document.addEventListener("click", e => {
    const btn = e.target.closest(".send-one-btn");
    if (!btn) return;
    if (!confirm("Send this email now?")) return;
    btn.disabled = true;
    btn.textContent = "Sending…";
    createSendJob([+btn.dataset.appId]);
  });
})();
```

---

### [MODIFY] [detail.html](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/templates/detail.html)

After the `</form>` tag (line 63), but still inside the `<section class="panel">`, add a send button:

```html
      </form>
      {% if application.status in ('ready', 'failed', 'retry_wait') and application.subject %}
      <button class="button button--primary send-detail-btn" id="send-detail-btn"
              data-app-id="{{ application.id }}" type="button"
              style="margin-top: 12px;">
        Send this email now
      </button>
      <span class="field-hint" id="send-detail-result" aria-live="polite"></span>
      {% endif %}
```

Add the JS before `{% endblock %}` (after line 86):

```html
<script>
(function () {
  const btn = document.getElementById("send-detail-btn");
  const result = document.getElementById("send-detail-result");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    if (!confirm("Send this email now?")) return;
    btn.disabled = true;
    btn.textContent = "Sending…";
    result.textContent = "";
    try {
      const resp = await fetch("/api/send-job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app_ids: [parseInt(btn.dataset.appId)] }),
      });
      const data = await resp.json();
      if (!data.ok) {
        result.textContent = "✗ " + data.message;
        result.style.color = "#F87171";
        btn.disabled = false;
        btn.textContent = "Send this email now";
        return;
      }
      // Poll for completion
      const poll = async () => {
        const r = await fetch(`/api/send-job/${data.job_id}`);
        const j = await r.json();
        if (j.status === "completed") {
          const item = j.items[0] || {};
          if (item.status === "sent") {
            result.textContent = "✓ Sent!";
            result.style.color = "#4ADE80";
            btn.textContent = "Sent ✓";
            setTimeout(() => window.location.reload(), 1500);
          } else {
            result.textContent = "✗ " + (item.error || "Send failed");
            result.style.color = "#F87171";
            btn.disabled = false;
            btn.textContent = "Send this email now";
          }
        } else {
          result.textContent = "Sending…";
          setTimeout(poll, 1500);
        }
      };
      poll();
    } catch (err) {
      result.textContent = "✗ " + err.message;
      result.style.color = "#F87171";
      btn.disabled = false;
      btn.textContent = "Send this email now";
    }
  });
})();
</script>
```

---

### [MODIFY] [style.css](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/static/style.css)

**Append** at end of file (after line 484):

```css
/* ── Batch action bar ── */
.batch-actions {
  position: sticky; bottom: 0; z-index: 100;
  display: flex; justify-content: center; padding: 12px 0;
  background: linear-gradient(to top, #12151A 60%, transparent);
  pointer-events: none;
}
.batch-actions-inner {
  display: flex; align-items: center; gap: 14px;
  background: #1A1E25; border: 1px solid #4FD1C5; border-radius: 12px;
  padding: 12px 22px;
  box-shadow: 0 8px 32px rgba(0,0,0,.45), 0 0 0 1px rgba(79,209,197,.15);
  pointer-events: auto;
  font: 13px "JetBrains Mono", monospace; color: #C4CAD3;
}

/* ── Row checkboxes ── */
.row-select-cb, #select-all-cb {
  accent-color: #4FD1C5; width: 16px; height: 16px; cursor: pointer;
}
.app-table th:first-child, .app-table td:first-child {
  width: 36px; text-align: center; padding-left: 12px; padding-right: 4px;
}

/* ── Row actions ── */
.row-actions {
  display: flex; align-items: center; gap: 8px; white-space: nowrap;
}

/* ── Send toast ── */
.send-toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  z-index: 200;
}
.send-toast-inner {
  background: #1A1E25; border: 1px solid #262B34; border-radius: 10px;
  padding: 14px 22px; min-width: 380px;
  box-shadow: 0 12px 40px rgba(0,0,0,.55);
  font-size: 13px; color: #E7EAEF;
}
.send-toast-bar {
  height: 4px; background: #1F232B; border-radius: 999px;
  margin-top: 10px; overflow: hidden;
}
.send-toast-fill {
  height: 100%; background: #4FD1C5; border-radius: 999px;
  transition: width 0.3s ease; width: 0%;
}

/* ── Status pills for new statuses ── */
.status-ready {
  background: rgba(79, 209, 197, 0.15); color: #4FD1C5;
}
.status-queued {
  background: rgba(96, 165, 250, 0.12); color: #60A5FA;
}
```

---

## Phase 4: Verification

### Automated Import Tests

```bash
cd c:\Users\MSI\OneDrive\Bureau\internship_automation_optimized_final\internship_automation_optimized

python -c "from ai_client import CompatibleAIClient, RateLimiter; print('AI client OK')"
python -c "from mail_service import send, SendResult; print('Mail service OK')"
python -c "from sender_worker import ensure_running, is_running; print('Sender worker OK')"
python -c "from pipeline import Pipeline; print('Pipeline OK')"
python -c "from dashboard.app import app; print('Dashboard OK')"
python -c "import db; db.init_db(); j = db.create_send_job([]);  print(f'DB OK, job_id={j}')"
```

### Manual Verification Steps

1. **Run `python main.py --dry-run --limit 3`** → verify 3 companies end up with `status = 'ready'` in the DB (not `drafted`, not `sent`)
2. **Run `python dashboard/app.py`** → open `http://127.0.0.1:5050`
3. **Verify the table** shows checkboxes on `ready` rows, and "Send" buttons
4. **Verify no checkbox** appears on `sent` rows
5. **Click "View →"** on a ready company → verify "Send this email now" button appears
6. **Edit** a subject+body → save → verify status stays `ready`
7. **Select 2 emails** via checkboxes → click "Send Selected"
8. **Verify**: toast appears saying "Creating send job...", then polls for progress, then shows "✓ Done: 2 sent"
9. **Verify**: status pills update to "Sent" in real-time
10. **Verify**: browser refresh shows the emails as "Sent" with `sent_at` timestamps
11. **Check the DB**: `send_jobs` table has 1 row with `status='completed'`, `send_job_items` has 2 rows with `status='sent'`
12. **Try editing** a sent email → verify it's rejected ("Cannot edit — status is 'sent'")

---

## File Summary

| File | Action | Lines (approx) |
|------|--------|-----------------|
| [db.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/db.py) | MODIFY | 175 → ~320 |
| [ai_client.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/ai_client.py) | MODIFY (full rewrite) | 39 → ~140 |
| [mail_service.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/mail_service.py) | **NEW** | ~75 |
| [sender_worker.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/sender_worker.py) | **NEW** | ~175 |
| [pipeline.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/pipeline.py) | MODIFY | 366 → ~140 |
| [main.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/main.py) | MODIFY (minor) | 314 → ~305 |
| [dashboard/app.py](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/app.py) | MODIFY | 669 → ~800 |
| [index.html](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/templates/index.html) | MODIFY | 421 → ~560 |
| [detail.html](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/templates/detail.html) | MODIFY | 88 → ~145 |
| [style.css](file:///c:/Users/MSI/OneDrive/Bureau/internship_automation_optimized_final/internship_automation_optimized/dashboard/static/style.css) | MODIFY (append) | 484 → ~540 |

**Files unchanged**: `mailer.py`, `google_auth_helper.py`, `retry.py`, `cache_store.py`, `utils.py`, `bounce_checker.py`, `agents/research_agent.py`, `agents/writer_agent.py`, `specializations.json`, `requirements.txt`, `base.html`

> [!WARNING]
> **Breaking change**: After this update, the pipeline **never sends emails**. All sending goes through the dashboard's send job system. The `status = "drafted"` is renamed to `"ready"` everywhere. Existing `drafted` rows in the DB will need a one-time migration (`UPDATE applications SET status = 'ready' WHERE status = 'drafted'`) — add this to the `init_db()` migration block.

> [!IMPORTANT]
> The existing `mailer.py` is NOT modified, but it is now wrapped by `mail_service.py` which provides a clean `SendResult` interface. The dashboard and sender worker never import mailer exceptions directly.
