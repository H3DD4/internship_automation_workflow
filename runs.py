"""Preparation runs: requested from the dashboard, executed by the worker.

A run is a row in prep_runs. The dashboard inserts it ("requested"); a worker
claims it atomically ("running"), streams the log into it, and closes it
("done" / "stopped" / "failed"). Stop is a flag on the row the pipeline polls.
One active run per user at a time.
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
STALE_SECONDS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(result):
    row = result.first()
    return dict(row._mapping) if row else None


class RunConflict(RuntimeError):
    pass


def request_run(user_id: int, limit: int | None = None) -> int:
    with database.tx() as conn:
        busy = conn.execute(select(prep_runs.c.id).where(
            prep_runs.c.user_id == int(user_id), prep_runs.c.status.in_(ACTIVE))).first()
        if busy:
            raise RunConflict("A preparation run is already in progress.")
        return conn.execute(prep_runs.insert().values(
            user_id=int(user_id), status="requested", limit_n=limit, created_at=_now(), log=""
        ).returning(prep_runs.c.id)).scalar_one()


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


def fail_stale_runs() -> list:
    """Runs whose worker died (no heartbeat for STALE_SECONDS): marked failed,
    returning their user ids so their half-done rows can be recovered."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)).isoformat()
    with database.tx() as conn:
        stale = conn.execute(select(prep_runs.c.id, prep_runs.c.user_id).where(
            prep_runs.c.status == "running", prep_runs.c.heartbeat_at < cutoff)).all()
        for row in stale:
            conn.execute(update(prep_runs).where(prep_runs.c.id == row.id).values(
                status="failed", finished_at=_now(),
                summary=json.dumps({"error": "The worker stopped during this run."})))
    return [row.user_id for row in stale]


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
