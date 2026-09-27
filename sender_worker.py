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
from pathlib import Path

from dotenv import load_dotenv

import db
import mail_service

ENV_PATH = Path(__file__).parent / ".env"


def _env_int(name: str, default: int) -> int:
    """int() on a stray non-numeric .env value would crash the worker thread
    and silently stop all sending, so fall back to the default instead."""
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


class SenderWorker:
    """Daemon thread that processes send jobs from the database."""

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._poll_interval = 2  # seconds between job polls

    def _load_settings(self):
        """Re-read pacing/cap from .env on every job so a value changed in the
        dashboard's setup form takes effect without restarting the process."""
        load_dotenv(ENV_PATH, override=True)
        self._min_delay = _env_int("MIN_DELAY_SECONDS", 45)
        self._max_delay = _env_int("MAX_DELAY_SECONDS", 120)
        self._max_per_day = _env_int("MAX_EMAILS_PER_DAY", 20)
        # random.randint(min, max) raises when min > max, which would abort the
        # whole job mid-send over nothing more than two settings being typed
        # the wrong way round.
        if self._max_delay < self._min_delay:
            self._min_delay, self._max_delay = self._max_delay, self._min_delay

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return  # already running
        self._stop_event.clear()
        db.recover_interrupted_sends()
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
        self._load_settings()
        db.update_send_job(job_id, status="running", started_at=db.now())

        while not self._stop_event.is_set():
            # Check daily cap
            sent_today = db.count_sent_today()
            if sent_today >= self._max_per_day:
                print(f"  [sender-worker] daily cap ({self._max_per_day}) reached, "
                      f"pausing job {job_id}")
                self._revert_remaining(
                    job_id, "Daily send cap reached — will resume automatically tomorrow."
                )
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
                print(f"  [sender-worker] ✗ auth failed — halting job {job_id}")
                self._revert_remaining(job_id, f"Send halted: {result.message}")
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

    def _revert_remaining(self, job_id: int, reason: str):
        """Revert all still-queued items in this job back to 'ready' so they
        aren't stuck (used on daily-cap pause and on account-wide auth failure —
        neither is specific to those particular companies)."""
        conn = db.get_connection()
        items = conn.execute(
            "SELECT application_id FROM send_job_items "
            "WHERE job_id = ? AND status = 'queued'", (job_id,)
        ).fetchall()
        now = db.now()
        for item in items:
            conn.execute(
                "UPDATE applications SET status = 'ready', error_message = ?, "
                "updated_at = ? WHERE id = ?",
                (reason, now, item["application_id"])
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
