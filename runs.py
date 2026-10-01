"""Preparation runs: requested from the dashboard, executed by the worker.

A run is a row in prep_runs. The dashboard inserts it ("requested"); a worker
claims it atomically ("running"), streams the log into it, and closes it
("done" / "stopped" / "failed"). Stop is a flag on the row the pipeline polls.
One active run per user at a time.

A run the user didn't stop never just dies with the worker: when the worker
shuts down, or is killed and its heartbeat goes quiet, the run goes back to
"requested" and the next worker continues it (up to MAX_RESUMES times, so a
run that keeps crashing the worker can't loop forever).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

import database
from database import prep_runs

ACTIVE = ("requested", "running")
LOG_LIMIT = 64 * 1024
STALE_SECONDS = 120          # four missed 30-second heartbeats
MAX_RESUMES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(result):
    row = result.first()
    return dict(row._mapping) if row else None


class RunConflict(RuntimeError):
    pass


def request_run(user_id: int, limit: int | None = None, targets: list | None = None) -> int:
    """targets: application ids — a re-scan run that only does those."""
    with database.tx() as conn:
        busy = conn.execute(select(prep_runs.c.id).where(
            prep_runs.c.user_id == int(user_id), prep_runs.c.status.in_(ACTIVE))).first()
        if busy:
            raise RunConflict("A preparation run is already in progress.")
        return conn.execute(prep_runs.insert().values(
            user_id=int(user_id), status="requested", limit_n=limit, created_at=_now(), log="",
            targets=json.dumps(sorted(int(i) for i in targets)) if targets else None,
        ).returning(prep_runs.c.id)).scalar_one()


def targets_of(run: dict) -> list | None:
    try:
        value = json.loads(run.get("targets") or "null")
    except (TypeError, ValueError):
        return None
    return [int(i) for i in value] if isinstance(value, list) and value else None


def request_stop(user_id: int) -> bool:
    with database.tx() as conn:
        return conn.execute(update(prep_runs).where(
            prep_runs.c.user_id == int(user_id), prep_runs.c.status.in_(ACTIVE)
        ).values(stop_requested=1)).rowcount > 0


def latest(user_id: int) -> dict | None:
    with database.read() as conn:
        return _one(conn.execute(select(prep_runs).where(prep_runs.c.user_id == int(user_id))
                                 .order_by(prep_runs.c.id.desc()).limit(1)))


def active(user_id: int) -> dict | None:
    with database.read() as conn:
        return _one(conn.execute(select(prep_runs).where(
            prep_runs.c.user_id == int(user_id), prep_runs.c.status.in_(ACTIVE))
            .order_by(prep_runs.c.id.desc()).limit(1)))


def stop_requested(run_id: int) -> bool:
    with database.read() as conn:
        row = conn.execute(select(prep_runs.c.stop_requested).where(prep_runs.c.id == run_id)).first()
    return bool(row and row.stop_requested)


def running_count() -> int:
    with database.read() as conn:
        return conn.execute(select(func.count()).select_from(prep_runs).where(
            prep_runs.c.status == "running")).scalar_one()


def claim_next(worker_id: str) -> dict | None:
    """The oldest requested run, claimed atomically, or None."""
    with database.read() as conn:
        candidates = [r.id for r in conn.execute(select(prep_runs.c.id).where(
            prep_runs.c.status == "requested").order_by(prep_runs.c.id).limit(10))]
    for run_id in candidates:
        with database.tx() as conn:
            claimed = conn.execute(update(prep_runs).where(
                prep_runs.c.id == run_id, prep_runs.c.status == "requested"
            ).values(status="running", worker_id=worker_id, started_at=_now(),
                     heartbeat_at=_now())).rowcount == 1
        if claimed:
            with database.read() as conn:
                return _one(conn.execute(select(prep_runs).where(prep_runs.c.id == run_id)))
    return None


def heartbeat(run_id: int) -> None:
    with database.tx() as conn:
        conn.execute(update(prep_runs).where(prep_runs.c.id == run_id).values(heartbeat_at=_now()))


def finish(run_id: int, status: str, summary: dict | None = None) -> None:
    with database.tx() as conn:
        conn.execute(update(prep_runs).where(prep_runs.c.id == run_id).values(
            status=status, finished_at=_now(),
            summary=json.dumps(summary) if summary is not None else None))


def _resumes(summary: str | None) -> int:
    try:
        return int(json.loads(summary or "{}").get("resumes", 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def requeue(run_id: int, reason: str) -> bool:
    """Put an interrupted run back in the queue so a worker continues it.
    False (and the run is closed as failed) once it has been resumed
    MAX_RESUMES times."""
    with database.tx() as conn:
        row = conn.execute(select(prep_runs.c.summary, prep_runs.c.log).where(
            prep_runs.c.id == run_id)).first()
        if row is None:
            return False
        resumes = _resumes(row.summary)
        if resumes >= MAX_RESUMES:
            conn.execute(update(prep_runs).where(prep_runs.c.id == run_id).values(
                status="failed", finished_at=_now(),
                summary=json.dumps({"error": f"Interrupted {resumes + 1} times ({reason}) — "
                                             "start it again when you're ready.", "resumes": resumes})))
            return False
        note = f"\n--- Interrupted ({reason}). Continuing where it left off. ---\n"
        conn.execute(update(prep_runs).where(prep_runs.c.id == run_id).values(
            status="requested", worker_id=None, heartbeat_at=None,
            summary=json.dumps({"resumes": resumes + 1}),
            log=((row.log or "") + note)[-LOG_LIMIT:]))
    return True


def recover_stale_runs() -> list:
    """Runs whose worker died (no heartbeat for STALE_SECONDS). A run the user
    had asked to stop is closed as stopped; any other is queued again so it
    continues by itself. Returns their user ids, so the rows they left
    mid-stage can be put back first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)).isoformat()
    with database.read() as conn:
        stale = conn.execute(select(prep_runs.c.id, prep_runs.c.user_id, prep_runs.c.stop_requested).where(
            prep_runs.c.status == "running", prep_runs.c.heartbeat_at < cutoff)).all()
    users = []
    for row in stale:
        with database.tx() as conn:
            # Re-check under the write: another worker may have handled it.
            still = conn.execute(update(prep_runs).where(
                prep_runs.c.id == row.id, prep_runs.c.status == "running",
                prep_runs.c.heartbeat_at < cutoff).values(heartbeat_at=_now())).rowcount == 1
        if not still:
            continue
        users.append(row.user_id)
        if row.stop_requested:
            finish(row.id, "stopped", {"note": "The worker stopped while this run was stopping."})
        else:
            requeue(row.id, "the worker stopped")
    return users


fail_stale_runs = recover_stale_runs   # former name


class RunLog:
    """A logsink sink that keeps the run's log in the database (last 64 KB),
    written at most every second, and echoes to the worker's console."""

    def __init__(self, run_id: int, user_id: int, echo=None):
        self.run_id, self.user_id, self.echo = run_id, user_id, echo
        self._lock = threading.Lock()
        self._text = ""
        self._dirty = False
        self._last_flush = 0.0

    def write(self, text: str) -> None:
        import time
        with self._lock:
            self._text = (self._text + text)[-LOG_LIMIT:]
            self._dirty = True
            due = time.monotonic() - self._last_flush > 1.0
        if self.echo is not None:
            try:
                for line in text.splitlines(keepends=True):
                    self.echo.write(f"[u{self.user_id}] {line}" if line.strip() else line)
            except Exception:
                pass
        if due:
            self.flush()

    def flush(self) -> None:
        import time
        with self._lock:
            if not self._dirty:
                return
            text, self._dirty, self._last_flush = self._text, False, time.monotonic()
        try:
            with database.tx() as conn:
                conn.execute(update(prep_runs).where(prep_runs.c.id == self.run_id).values(log=text))
        except Exception:
            with self._lock:
                self._dirty = True
