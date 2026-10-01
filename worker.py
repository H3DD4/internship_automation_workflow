"""
The background worker — everything that happens without a browser open:

  * preparation runs (research + drafting), several users at once, capped
    by MAX_CONCURRENT_RUNS so one big list can't starve the others;
  * sending: one thread per user with queued emails (each user keeps their
    own pacing and daily cap; users never wait behind each other);
  * bounce checks for recent sends, per user, on each user's schedule;
  * recovery: runs and send jobs orphaned by a crash are detected through
    their heartbeats and resumed or closed cleanly;
  * housekeeping: expired sessions and rate-limit counters.

Production: its own process, `python worker.py` (see docker-compose.yml).
Local development: started inside the dashboard process (EMBEDDED_WORKER).
Every claim is atomic in the database, so running two workers is safe.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import accounts
import config
import database
import db
import logsink
import runs
import sender_worker
import vault

POLL_SECONDS = 2
BOUNCE_PROBE_SECONDS = 60
HOUSEKEEPING_SECONDS = 600
RECOVERY_SECONDS = 30


def execute_prep_run(user_id: int, role: str, *, limit: int | None = None,
                     stop_check=None, include_all: bool = False) -> dict:
    """Research and draft a user's companies. Prints progress (captured by
    the caller's log sink). Returns the pipeline's result counts."""
    import drafting
    import pipeline
    from ai_client import RateLimiter, resolve_ai_settings
    from model_router import build_router
    from user_config import UserConfig

    cfg = UserConfig(user_id, role)
    dcfg = drafting.load_config(user_id, cfg)
    env = cfg.ai_env()
    ai = resolve_ai_settings(env)
    router = build_router(env, rate_limiter=RateLimiter(max(1, cfg.int_setting("AI_MAX_RPM", 800))))
    print("AI model pool (tier 1 = best; lower tiers only when every tier-1 model is unavailable):")
    for row in router.snapshot():
        tiers = ", ".join(f"{task} t{tier}" for task, tier in sorted(row["tiers"].items()))
        print(f"  {row['name']:<40} {tiers}")
    if not router.deployments:
        raise drafting.NotReady("No AI provider has a key yet — add one in Settings.")
    print()

    rows, skipped = pipeline.select_rows(user_id, limit=limit, include_all=include_all)
    data = db.for_user(user_id)
    if skipped:
        print(f"Skipping {skipped} already-prepared or sent row(s) from your list.")
    print(f"Preparing {len(rows)} this run ({data.count_needing_preparation()} still need work in the database).")
    print(f"Languages available: {', '.join(l.upper() for l in dcfg.available_languages)} "
          f"(mode: {dcfg.language_mode}).")
    print("Preparation only — review and send from the dashboard.\n")

    research_workers = max(1, cfg.int_setting("RESEARCH_WORKERS", 3))
    writer_workers = max(1, cfg.int_setting("WRITER_WORKERS", 2))
    print(f"Preparation engine: {research_workers} research worker(s), {writer_workers} writer worker(s).\n")

    results = pipeline.Pipeline(
        user_id, router, dcfg, ai["model"], research_workers=research_workers,
        writer_workers=writer_workers, translation_model=ai["translation_model"],
        stop_check=stop_check,
    ).run(rows)

    print("\n--- Run summary ---")
    for status, count in results.items():
        print(f"  {status}: {count}")
    if sum(results.values()) != len(rows):
        print(f"\n  WARNING: {len(rows)} companies loaded but only {sum(results.values())} "
              f"accounted for in the summary above.")
    return results


class Worker:
    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._run_threads: dict = {}      # run_id -> thread
        self._sender_threads: dict = {}   # user_id -> thread
        self._bounce_threads: dict = {}   # user_id -> thread
        self._next_bounce_probe = 0.0
        self._next_housekeeping = 0.0
        self._next_recovery = 0.0
        self._thread = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        logsink.install()
        self._thread = threading.Thread(target=self.loop, name="worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def loop(self) -> None:
        print(f"[worker] {self.worker_id} started.")
        while not self.stop_event.is_set():
            for step in (self._recover_runs, self._housekeeping, self._start_runs, self._start_senders,
                         self._probe_bounces):
                try:
                    step()
                except Exception as exc:  # the loop must survive anything
                    print(f"[worker] {step.__name__} error: {exc!r}")
            self.stop_event.wait(POLL_SECONDS)
        print("[worker] stopping — waiting for in-flight work to reach a safe point.")

    @staticmethod
    def _alive(threads: dict) -> dict:
        return {k: t for k, t in threads.items() if t.is_alive()}

    # -- preparation -----------------------------------------------------------

    def _start_runs(self) -> None:
        with self._lock:
            self._run_threads = self._alive(self._run_threads)
            capacity = config.MAX_CONCURRENT_RUNS - len(self._run_threads)
        while capacity > 0 and not self.stop_event.is_set():
            run = runs.claim_next(self.worker_id)
            if run is None:
                return
            thread = threading.Thread(target=self._execute_run, args=(run,),
                                      name=f"run-{run['id']}", daemon=True)
            with self._lock:
                self._run_threads[run["id"]] = thread
            thread.start()
            capacity -= 1

    def _execute_run(self, run: dict) -> None:
        import drafting
        run_id, user_id = run["id"], run["user_id"]
        user = accounts.get_user(user_id)
        sink = runs.RunLog(run_id, user_id, echo=sys.__stdout__)
        beating = threading.Event()

        def heartbeat():
            while not beating.wait(30):
                runs.heartbeat(run_id)
        threading.Thread(target=heartbeat, daemon=True, name=f"run-{run_id}-heartbeat").start()

        status, summary = "done", None
        user_stopped = False
        with logsink.bound(sink):
            print(f"--- Preparation run #{run_id} ---")
            try:
                if not user or user["status"] != "active":
                    raise drafting.NotReady("This account is not active.")
                summary = execute_prep_run(
                    user_id, user["role"], limit=run.get("limit_n"),
                    stop_check=lambda: self.stop_event.is_set() or runs.stop_requested(run_id))
                user_stopped = runs.stop_requested(run_id)
                if user_stopped or self.stop_event.is_set():
                    status = "stopped"
            except drafting.NotReady as exc:
                print(f"Can't start: {exc}")
                status, summary = "failed", {"error": str(exc)}
            except Exception as exc:
                print(f"Run failed: {exc!r}")
                status, summary = "failed", {"error": str(exc)[:300]}
            finally:
                beating.set()
                # Whatever happened, no company is left half-way: anything
                # still "researching" goes back to the queue, anything still
                # "writing" keeps its research and is drafted next time.
                try:
                    db.for_user(user_id).recover_stale_preparation_rows()
                except Exception as exc:
                    print(f"Couldn't tidy unfinished companies: {exc!r}")
                sink.flush()
        if status == "stopped" and not user_stopped:
            # The worker is shutting down (restart, redeploy) — not the user's
            # choice, so the run continues when a worker is back.
            runs.requeue(run_id, "the app restarted")
            return
        runs.finish(run_id, status, summary)

    # -- sending ---------------------------------------------------------------

    def _start_senders(self) -> None:
        with self._lock:
            self._sender_threads = self._alive(self._sender_threads)
            busy = set(self._sender_threads)
        cutoff = sender_worker.stale_cutoff()
        for job in db.pending_send_jobs():
            uid = job["user_id"]
            if uid in busy:
                continue
            owned_elsewhere = (job["worker_id"] and job["worker_id"] != self.worker_id
                               and job["heartbeat_at"] and job["heartbeat_at"] >= cutoff)
            if owned_elsewhere:
                continue
            if len(busy) >= config.MAX_CONCURRENT_SENDERS:
                return
            user = accounts.get_user(uid)
            if not user or user["status"] != "active":
                continue
            thread = threading.Thread(target=self._send_for_user, args=(uid, user["role"]),
                                      name=f"sender-u{uid}", daemon=True)
            with self._lock:
                self._sender_threads[uid] = thread
            busy.add(uid)
            thread.start()

    def _send_for_user(self, user_id: int, role: str) -> None:
        try:
            sender_worker.UserSender(user_id, role, self.worker_id, self.stop_event).run_pending()
        except Exception as exc:
            print(f"[worker] sender for user {user_id} failed: {exc!r}")

    # -- bounces ---------------------------------------------------------------

    def _probe_bounces(self) -> None:
        if time.monotonic() < self._next_bounce_probe:
            return
        self._next_bounce_probe = time.monotonic() + BOUNCE_PROBE_SECONDS
        import bounce_checker
        from user_config import UserConfig
        with self._lock:
            self._bounce_threads = self._alive(self._bounce_threads)
        for uid in db.users_with_recent_sends(sender_worker.BOUNCE_WINDOW_DAYS):
            if uid in self._bounce_threads or len(self._bounce_threads) >= 4:
                continue
            user = accounts.get_user(uid)
            if not user or user["status"] != "active":
                continue
            cfg = UserConfig(uid, user["role"])
            interval = cfg.int_setting("BOUNCE_CHECK_MINUTES", 30)
            if interval <= 0:
                continue
            last = bounce_checker.last_check(uid)
            if last and last.get("at"):
                try:
                    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])
                    if elapsed < timedelta(minutes=interval):
                        continue
                except ValueError:
                    pass
            if not bounce_checker.credentials_available(cfg):
                continue

            def check(cfg=cfg):
                result = bounce_checker.run_check(cfg, sender_worker.BOUNCE_WINDOW_DAYS, trigger="auto")
                if result["updated"] or result["error"]:
                    print(f"[worker] bounce check u{cfg.user_id}: {result['updated']} newly bounced"
                          + (f" (error: {result['error']})" if result["error"] else ""))
            thread = threading.Thread(target=check, daemon=True, name=f"bounce-u{uid}")
            with self._lock:
                self._bounce_threads[uid] = thread
            thread.start()

    # -- housekeeping ----------------------------------------------------------

    def _recover_runs(self) -> None:
        """Runs whose worker died without a word (killed, crashed): their
        unfinished companies are put back and the run continues."""
        if time.monotonic() < self._next_recovery:
            return
        self._next_recovery = time.monotonic() + RECOVERY_SECONDS
        for uid in runs.recover_stale_runs():
            db.for_user(uid).recover_stale_preparation_rows()

    def _housekeeping(self) -> None:
        if time.monotonic() < self._next_housekeeping:
            return
        self._next_housekeeping = time.monotonic() + HOUSEKEEPING_SECONDS
        accounts.purge_expired_sessions()
        accounts.purge_rate_buckets()


_embedded: Worker | None = None
_embedded_lock = threading.Lock()


def ensure_embedded() -> None:
    """Start the in-process worker once (local development)."""
    global _embedded
    with _embedded_lock:
        if _embedded is None:
            _embedded = Worker()
            _embedded.start()


def main() -> None:
    vault.ensure_dev_keys()
    database.init_schema()
    worker = Worker()
    logsink.install()

    def shutdown(*_):
        worker.stop()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    worker.loop()


if __name__ == "__main__":
    main()
