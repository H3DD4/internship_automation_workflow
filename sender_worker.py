"""
Sends one user's queued emails — pacing, daily cap, error routing.

worker.py runs one UserSender thread per user with work waiting, so users
send in parallel and never wait behind each other's anti-spam delays, while
each user's own emails still go out one at a time with a random gap.

Each item is claimed atomically (queued -> sending) before it is sent, and
the job carries a heartbeat: even with two worker processes, an email is
never sent twice.
"""

import random
import threading
from datetime import datetime, timedelta, timezone

import db
import mail_service
from user_config import UserConfig

BOUNCE_WINDOW_DAYS = 3
MANUAL_BOUNCE_WINDOW_DAYS = 30   # "Check bounces now" looks further back
# A job whose worker hasn't checked in for this long is considered orphaned.
JOB_STALE_SECONDS = 120


def stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=JOB_STALE_SECONDS)).isoformat()


class UserSender:
    def __init__(self, user_id: int, role: str, worker_id: str, stop_event: threading.Event):
        self.user_id = user_id
        self.data = db.for_user(user_id)
        self.cfg = UserConfig(user_id, role)
        self.worker_id = worker_id
        self._stop = stop_event

    def _load_settings(self):
        """Re-read pacing/cap for every job so a value changed in Settings
        takes effect without restarting anything."""
        self.cfg = UserConfig(self.user_id, self.cfg.role)
        self._min_delay = max(0, self.cfg.int_setting("MIN_DELAY_SECONDS", 45))
        self._max_delay = max(0, self.cfg.int_setting("MAX_DELAY_SECONDS", 120))
        self._max_per_day = max(1, self.cfg.int_setting("MAX_EMAILS_PER_DAY", 20))
        # random.randint(min, max) raises when min > max.
        if self._max_delay < self._min_delay:
            self._min_delay, self._max_delay = self._max_delay, self._min_delay

    def run_pending(self) -> None:
        """Process every job of this user this worker can claim, oldest first."""
        for job in self.data.get_pending_send_jobs():
            if self._stop.is_set():
                return
            if not db.claim_send_job(job["id"], self.worker_id, stale_cutoff()):
                continue  # another worker is on it
            if job["status"] == "running":
                db.recover_job_items(job["id"])
            self.process_job(job["id"])

    def process_job(self, job_id: int):
        """Process all queued items in a single send job."""
        self._load_settings()
        self.data.update_send_job(job_id, status="running", started_at=db.now())

        while not self._stop.is_set():
            if not db.heartbeat_send_job(job_id, self.worker_id):
                return  # taken over by another worker; it finishes the job

            if self.data.count_sent_today() >= self._max_per_day:
                print(f"  [sender u{self.user_id}] daily cap ({self._max_per_day}) reached, "
                      f"pausing job {job_id}")
                self.data.revert_remaining(
                    job_id, "Daily send cap reached — send again tomorrow.")
                break

            item = self.data.get_next_queued_item(job_id)
            if item is None:
                break
            if not self.data.claim_job_item(item["id"]):
                continue

            app_id = item["application_id"]
            item_id = item["id"]
            self.data.update_application(app_id, status="sending")
            app = self.data.get_application_by_id(app_id)
            if not app:
                self.data.update_send_job_item(item_id, status="failed",
                                               error_message="Application not found")
                continue

            result = mail_service.send(self.cfg, app)
            attempts = (app.get("send_attempts") or 0) + 1

            if result.success:
                self.data.update_send_job_item(item_id, status="sent", message_id=result.message_id)
                self.data.update_application(app_id, status="sent", sent_at=db.now(),
                                             error_message=None, message_id=result.message_id,
                                             send_attempts=attempts, last_attempt_at=db.now())
                self.data.log_event(app_id, "send", f"Email sent successfully via {result.provider}.")
                print(f"  [sender u{self.user_id}] sent application id={app_id}")

            elif result.error_code in ("auth_failed", "no_cv"):
                # Account-wide: every remaining email would fail the same way.
                self.data.update_send_job_item(item_id, status="failed", error_message=result.message)
                self.data.update_application(app_id, status="ready", error_message=result.message,
                                             error_code=result.error_code)
                self.data.log_event(app_id, "send", "Sending halted.",
                                    detail={"error": result.message})
                self.data.revert_remaining(job_id, f"Send halted: {result.message}")
                break

            elif result.retryable:
                self.data.update_send_job_item(item_id, status="retry_wait", error_message=result.message)
                self.data.update_application(app_id, status="retry_wait", error_message=result.message,
                                             error_code=result.error_code, send_attempts=attempts,
                                             last_attempt_at=db.now())
                self.data.log_event(app_id, "send", f"Transient send failure: {result.message}",
                                    detail={"error": result.message})

            else:
                self.data.update_send_job_item(item_id, status="failed", error_message=result.message)
                self.data.update_application(app_id, status="failed", error_message=result.message,
                                             error_code=result.error_code, send_attempts=attempts,
                                             last_attempt_at=db.now())
                self.data.log_event(app_id, "send", f"Permanent send failure: {result.message}",
                                    detail={"error": result.message})

            # Anti-spam pacing between sends (only if more items remain).
            if self.data.get_next_queued_item(job_id) and not self._stop.is_set():
                delay = random.randint(self._min_delay, self._max_delay)
                # Wake up regularly to keep the job's heartbeat fresh.
                waited = 0
                while waited < delay and not self._stop.is_set():
                    step = min(30, delay - waited)
                    self._stop.wait(timeout=step)
                    waited += step
                    db.heartbeat_send_job(job_id, self.worker_id)

        if not self._stop.is_set():
            self.data.update_send_job(job_id, status="completed", completed_at=db.now())
