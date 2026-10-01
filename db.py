"""Data access for the application pipeline, scoped to one user.

    data = db.for_user(user_id)
    data.get_application_by_id(app_id)   # None unless it belongs to user_id

Every method filters on the owning user, so a request for another account's
row behaves exactly like a request for a row that doesn't exist. The method
names and semantics are the ones the single-user version had — the atomic
send claim, crash recovery, the funnel groups — only the scope is new.

Module-level functions at the bottom serve the background worker, which
works across users (finding pending jobs) and then drops into a user scope
for everything it does.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

import database
from database import (
    applications, cache_entries, company_list, events, send_job_items, send_jobs, user_meta,
)


def _now():
    # Timezone-aware UTC. Every stored timestamp goes through here, so the
    # daily-cap check and the bounce window share one unambiguous clock.
    return datetime.now(timezone.utc).isoformat()


def now():
    """Public timestamp helper for callers outside this module."""
    return _now()


def init_db():
    database.init_schema()


def _dicts(result) -> list:
    return [dict(row._mapping) for row in result]


def _one(result):
    row = result.first()
    return dict(row._mapping) if row else None


# The pipeline stages a company moves through, grouped into the funnel
# buckets the dashboard shows.
STATUS_GROUPS = {
    "to_prepare": ("pending", "researching", "researched", "writing"),
    "ready": ("ready",),
    "sending": ("queued", "sending"),
    "sent": ("sent",),
    "problems": ("failed", "retry_wait", "bounced"),
    "skipped": ("skipped",),
}

# Statuses from which an application can be queued for sending.
SENDABLE_STATUSES = frozenset({"ready", "failed", "retry_wait"})
# Statuses where the sender owns the row — pipeline must not touch these.
SEND_IN_FLIGHT_STATUSES = frozenset({"queued", "sending"})
# Finished as far as preparation is concerned.
PREPARATION_DONE_STATUSES = frozenset({"sent", "bounced", "skipped"})

_APPLICATION_COLUMNS = {c.name for c in applications.columns} - {"id", "user_id", "created_at"}
_JOB_ITEM_COLUMNS = {c.name for c in send_job_items.columns} - {"id", "job_id"}
_JOB_COLUMNS = {c.name for c in send_jobs.columns} - {"id", "user_id"}


def _checked(fields: dict, allowed: set) -> dict:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown column(s): {', '.join(sorted(unknown))}")
    return fields


def _like_pattern(search: str) -> str:
    escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class UserData:
    """Everything the pipeline, sender and dashboard do, for one user."""

    def __init__(self, user_id: int):
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("A valid user id is required.")
        self.user_id = user_id

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    def get_or_create_application(self, company_name: str, email: str, website: str,
                                  contact_name: str = "") -> int:
        uid = self.user_id
        with database.tx() as conn:
            row = conn.execute(select(applications.c.id).where(
                applications.c.user_id == uid, applications.c.email == email)).first()
            if row:
                return row.id
        now = _now()
        try:
            with database.tx() as conn:
                result = conn.execute(applications.insert().values(
                    user_id=uid, company_name=company_name, email=email, website=website,
                    contact_name=contact_name, status="pending", created_at=now, updated_at=now,
                ).returning(applications.c.id))
                return result.scalar_one()
        except IntegrityError:
            # Two workers created the same row at once; the other one won.
            with database.read() as conn:
                return conn.execute(select(applications.c.id).where(
                    applications.c.user_id == uid, applications.c.email == email)).scalar_one()

    def get_applications_by_email(self) -> dict:
        """Every application keyed by its exact email, in one query."""
        with database.read() as conn:
            rows = _dicts(conn.execute(select(applications).where(
                applications.c.user_id == self.user_id)))
        return {row["email"]: row for row in rows}

    def get_application_by_email(self, email: str):
        with database.read() as conn:
            return _one(conn.execute(select(applications).where(
                applications.c.user_id == self.user_id, applications.c.email == email)))

    def get_application_by_id(self, app_id: int):
        with database.read() as conn:
            return _one(conn.execute(select(applications).where(
                applications.c.user_id == self.user_id, applications.c.id == int(app_id))))

    def update_application(self, app_id: int, **fields):
        if not fields:
            return
        fields = _checked(dict(fields), _APPLICATION_COLUMNS)
        fields["updated_at"] = _now()
        with database.tx() as conn:
            conn.execute(update(applications).where(
                applications.c.user_id == self.user_id, applications.c.id == int(app_id)
            ).values(**fields))

    def get_all_applications(self):
        with database.read() as conn:
            return _dicts(conn.execute(select(applications).where(
                applications.c.user_id == self.user_id).order_by(applications.c.updated_at.desc())))

    def get_adjacent_application_id(self, app_id: int, direction: str = "next") -> int | None:
        base = select(applications.c.id).where(applications.c.user_id == self.user_id)
        if direction == "prev":
            query = base.where(applications.c.id < app_id).order_by(applications.c.id.desc())
        else:
            query = base.where(applications.c.id > app_id).order_by(applications.c.id.asc())
        with database.read() as conn:
            row = conn.execute(query.limit(1)).first()
        return row.id if row else None

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def log_event(self, app_id: int, stage: str, message: str, detail: dict = None):
        with database.tx() as conn:
            owned = conn.execute(select(applications.c.id).where(
                applications.c.user_id == self.user_id, applications.c.id == int(app_id))).first()
            if not owned:
                return
            conn.execute(events.insert().values(
                application_id=int(app_id), timestamp=_now(), stage=stage, message=message,
                detail=json.dumps(detail) if detail is not None else None))

    def get_events(self, app_id: int):
        query = (select(events).join(applications, applications.c.id == events.c.application_id)
                 .where(applications.c.user_id == self.user_id, events.c.application_id == int(app_id))
                 .order_by(events.c.timestamp.asc(), events.c.id.asc()))
        with database.read() as conn:
            return _dicts(conn.execute(query))

    # ------------------------------------------------------------------
    # Stats and the table
    # ------------------------------------------------------------------

    def get_stats(self):
        with database.read() as conn:
            rows = conn.execute(select(applications.c.status, func.count().label("c"))
                                .where(applications.c.user_id == self.user_id)
                                .group_by(applications.c.status)).all()
        stats = {row.status: row.c for row in rows}
        stats["total"] = sum(stats.values())
        return stats

    def get_grouped_stats(self) -> dict:
        stats = self.get_stats()
        grouped = {group: sum(stats.get(status, 0) for status in statuses)
                   for group, statuses in STATUS_GROUPS.items()}
        grouped["total"] = stats.get("total", 0)
        grouped["by_status"] = {k: v for k, v in stats.items() if k != "total"}
        return grouped

    def get_applications_paginated(self, status: str = None, statuses: list = None,
                                   search: str = None, page: int = 1, limit: int = 50,
                                   favorite_only: bool = False) -> tuple[list, int]:
        query = select(applications).where(applications.c.user_id == self.user_id)
        count = select(func.count()).select_from(applications).where(
            applications.c.user_id == self.user_id)
        conditions = []
        if favorite_only:
            conditions.append(applications.c.favorite == 1)
        if statuses:
            conditions.append(applications.c.status.in_(list(statuses)))
        elif status:
            conditions.append(applications.c.status == status)
        if search:
            pattern = _like_pattern(search)
            conditions.append(func.lower(applications.c.company_name).like(pattern, escape="\\")
                              | func.lower(applications.c.email).like(pattern, escape="\\"))
        for condition in conditions:
            query, count = query.where(condition), count.where(condition)
        offset = (max(1, page) - 1) * limit
        with database.read() as conn:
            total = conn.execute(count).scalar_one()
            rows = _dicts(conn.execute(query.order_by(applications.c.updated_at.desc(),
                                                      applications.c.id.desc())
                                       .limit(limit).offset(offset)))
        return rows, total

    def count_sent_since(self, days: int) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with database.read() as conn:
            return conn.execute(select(func.count()).select_from(applications).where(
                applications.c.user_id == self.user_id, applications.c.status == "sent",
                applications.c.sent_at >= since)).scalar_one()

    def count_sent_today(self) -> int:
        # Counts sent_at alone, not status = 'sent': the cap limits how many
        # messages left the account today, which a later bounce doesn't undo.
        today = datetime.now(timezone.utc).date()
        with database.read() as conn:
            return conn.execute(select(func.count()).select_from(applications).where(
                applications.c.user_id == self.user_id,
                applications.c.sent_at >= today.isoformat(),
                applications.c.sent_at < (today + timedelta(days=1)).isoformat())).scalar_one()

    # ------------------------------------------------------------------
    # Favorites
    # ------------------------------------------------------------------

    def set_favorite(self, app_ids: list, favorite: bool) -> int:
        """Star or unstar companies. Leaves updated_at alone so rows don't
        jump around the table as you star down the list."""
        ids = [int(i) for i in app_ids]
        if not ids:
            return 0
        with database.tx() as conn:
            result = conn.execute(update(applications).where(
                applications.c.user_id == self.user_id, applications.c.id.in_(ids)
            ).values(favorite=1 if favorite else 0))
            return result.rowcount

    def count_favorites(self) -> dict:
        base = (select(func.count()).select_from(applications)
                .where(applications.c.user_id == self.user_id, applications.c.favorite == 1))
        with database.read() as conn:
            total = conn.execute(base).scalar_one()
            sendable = conn.execute(base.where(
                applications.c.status.in_(list(SENDABLE_STATUSES)),
                applications.c.subject.is_not(None), applications.c.subject != "")).scalar_one()
        return {"total": total, "sendable": sendable}

    def get_sendable_favorites(self) -> list:
        with database.read() as conn:
            return _dicts(conn.execute(
                select(applications.c.id, applications.c.company_name, applications.c.email)
                .where(applications.c.user_id == self.user_id, applications.c.favorite == 1,
                       applications.c.status.in_(list(SENDABLE_STATUSES)),
                       applications.c.subject.is_not(None), applications.c.subject != "")
                .order_by(applications.c.id)))

    # ------------------------------------------------------------------
    # Send jobs
    # ------------------------------------------------------------------

    def create_send_job(self, app_ids: list[int]) -> tuple[int | None, list[int]]:
        """Each id is claimed with one atomic UPDATE ... WHERE status IN
        (sendable) — the only place a row becomes 'queued' — so a double
        click can never queue the same company twice."""
        now = _now()
        queued_ids = []
        with database.tx() as conn:
            for app_id in app_ids:
                result = conn.execute(update(applications).where(
                    applications.c.user_id == self.user_id, applications.c.id == int(app_id),
                    applications.c.status.in_(list(SENDABLE_STATUSES)),
                    applications.c.subject.is_not(None), applications.c.subject != "",
                    applications.c.body.is_not(None), applications.c.body != "",
                ).values(status="queued", updated_at=now))
                if result.rowcount == 1:
                    queued_ids.append(int(app_id))
            if not queued_ids:
                return None, []
            job_id = conn.execute(send_jobs.insert().values(
                user_id=self.user_id, status="pending", total_items=len(queued_ids),
                created_at=now).returning(send_jobs.c.id)).scalar_one()
            conn.execute(send_job_items.insert(), [
                {"job_id": job_id, "application_id": app_id, "status": "queued"}
                for app_id in queued_ids])
        return job_id, queued_ids

    def get_send_job(self, job_id: int) -> dict | None:
        with database.read() as conn:
            job = _one(conn.execute(select(send_jobs).where(
                send_jobs.c.user_id == self.user_id, send_jobs.c.id == int(job_id))))
            if not job:
                return None
            items = _dicts(conn.execute(
                select(send_job_items, applications.c.company_name, applications.c.email)
                .join(applications, applications.c.id == send_job_items.c.application_id)
                .where(send_job_items.c.job_id == int(job_id)).order_by(send_job_items.c.id)))
        job["items"] = items
        statuses = [i["status"] for i in items]
        job["queued"] = statuses.count("queued")
        job["sending"] = statuses.count("sending")
        job["sent_count"] = statuses.count("sent")
        job["failed_count"] = statuses.count("failed") + statuses.count("retry_wait")
        return job

    def get_pending_send_jobs(self) -> list:
        with database.read() as conn:
            return _dicts(conn.execute(select(send_jobs).where(
                send_jobs.c.user_id == self.user_id,
                send_jobs.c.status.in_(["pending", "running"])).order_by(send_jobs.c.id)))

    def get_next_queued_item(self, job_id: int) -> dict | None:
        with database.read() as conn:
            return _one(conn.execute(
                select(send_job_items, applications.c.company_name, applications.c.email,
                       applications.c.subject, applications.c.body)
                .join(applications, applications.c.id == send_job_items.c.application_id)
                .join(send_jobs, send_jobs.c.id == send_job_items.c.job_id)
                .where(send_jobs.c.user_id == self.user_id, send_job_items.c.job_id == int(job_id),
                       send_job_items.c.status == "queued")
                .order_by(send_job_items.c.id).limit(1)))

    def claim_job_item(self, item_id: int) -> bool:
        """queued -> sending, atomically. False when something else (another
        worker, a cancel) got there first — so an email is never sent twice."""
        with database.tx() as conn:
            result = conn.execute(update(send_job_items).where(
                send_job_items.c.id == int(item_id), send_job_items.c.status == "queued",
                send_job_items.c.job_id.in_(select(send_jobs.c.id).where(
                    send_jobs.c.user_id == self.user_id)),
            ).values(status="sending", attempted_at=_now()))
            return result.rowcount == 1

    def update_send_job_item(self, item_id: int, **fields):
        if not fields:
            return
        fields = _checked(dict(fields), _JOB_ITEM_COLUMNS)
        with database.tx() as conn:
            conn.execute(update(send_job_items).where(
                send_job_items.c.id == int(item_id),
                send_job_items.c.job_id.in_(select(send_jobs.c.id).where(
                    send_jobs.c.user_id == self.user_id)),
            ).values(**fields))

    def update_send_job(self, job_id: int, **fields):
        if not fields:
            return
        fields = _checked(dict(fields), _JOB_COLUMNS)
        with database.tx() as conn:
            conn.execute(update(send_jobs).where(
                send_jobs.c.user_id == self.user_id, send_jobs.c.id == int(job_id)
            ).values(**fields))

    def revert_remaining(self, job_id: int, reason: str) -> None:
        """Put every still-queued item of a job back to 'ready' (daily cap
        reached, or the account's login failed — neither is about those
        particular companies)."""
        now = _now()
        with database.tx() as conn:
            owned = conn.execute(select(send_jobs.c.id).where(
                send_jobs.c.user_id == self.user_id, send_jobs.c.id == int(job_id))).first()
            if not owned:
                return
            app_ids = [r.application_id for r in conn.execute(
                select(send_job_items.c.application_id).where(
                    send_job_items.c.job_id == int(job_id), send_job_items.c.status == "queued"))]
            if app_ids:
                conn.execute(update(applications).where(
                    applications.c.user_id == self.user_id, applications.c.id.in_(app_ids)
                ).values(status="ready", error_message=reason, updated_at=now))
            conn.execute(update(send_job_items).where(
                send_job_items.c.job_id == int(job_id), send_job_items.c.status == "queued"
            ).values(status="skipped"))

    # ------------------------------------------------------------------
    # Per-user key/value state (e.g. when the inbox was last scanned)
    # ------------------------------------------------------------------

    def get_meta(self, key: str):
        with database.read() as conn:
            row = conn.execute(select(user_meta.c.value).where(
                user_meta.c.user_id == self.user_id, user_meta.c.key == key)).first()
        if not row:
            return None
        try:
            return json.loads(row.value)
        except (TypeError, ValueError):
            return None

    def set_meta(self, key: str, value) -> None:
        with database.tx() as conn:
            database.upsert(conn, user_meta, {"user_id": self.user_id, "key": key,
                                              "value": json.dumps(value)}, ["user_id", "key"])

    # ------------------------------------------------------------------
    # Preparation bookkeeping
    # ------------------------------------------------------------------

    def count_needing_preparation(self) -> int:
        done = list(PREPARATION_DONE_STATUSES | {"ready"} | SEND_IN_FLIGHT_STATUSES)
        has_draft = ((applications.c.subject.is_not(None)) & (applications.c.subject != "")
                     & (applications.c.body.is_not(None)) & (applications.c.body != ""))
        with database.read() as conn:
            return conn.execute(select(func.count()).select_from(applications).where(
                applications.c.user_id == self.user_id, applications.c.status.not_in(done),
                ~has_draft)).scalar_one()

    def recover_stale_preparation_rows(self) -> int:
        """Rows left 'researching'/'writing' by a run that died go back to
        where they can be picked up again."""
        now = _now()
        with database.tx() as conn:
            first = conn.execute(update(applications).where(
                applications.c.user_id == self.user_id, applications.c.status == "researching"
            ).values(status="pending", error_message=None, updated_at=now)).rowcount
            second = conn.execute(update(applications).where(
                applications.c.user_id == self.user_id, applications.c.status == "writing"
            ).values(status="researched", error_message=None, updated_at=now)).rowcount
        return first + second

    def recover_interrupted_sends(self) -> int:
        """A send killed mid-flight is surfaced as 'retry_wait' (we can't know
        whether it went out), orphaned 'queued' rows go back to 'ready', and
        finished 'running' jobs are closed."""
        now = _now()
        message = "Interrupted by a restart — check your Gmail Sent folder before resending."
        uid = self.user_id
        user_jobs = select(send_jobs.c.id).where(send_jobs.c.user_id == uid)
        with database.tx() as conn:
            stuck = conn.execute(select(send_job_items.c.id, send_job_items.c.application_id).where(
                send_job_items.c.status == "sending", send_job_items.c.job_id.in_(user_jobs))).all()
            for item in stuck:
                conn.execute(update(send_job_items).where(send_job_items.c.id == item.id)
                             .values(status="retry_wait", error_message=message))
                conn.execute(update(applications).where(
                    applications.c.user_id == uid, applications.c.id == item.application_id
                ).values(status="retry_wait", error_message=message, updated_at=now))
            live = select(send_job_items.c.application_id).where(
                send_job_items.c.status.in_(["queued", "sending"]))
            orphaned = conn.execute(update(applications).where(
                applications.c.user_id == uid, applications.c.status.in_(["queued", "sending"]),
                applications.c.id.not_in(live)).values(status="ready", updated_at=now)).rowcount
            busy_jobs = select(send_job_items.c.job_id).where(
                send_job_items.c.status.in_(["queued", "sending"]))
            conn.execute(update(send_jobs).where(
                send_jobs.c.user_id == uid, send_jobs.c.status == "running",
                send_jobs.c.id.not_in(busy_jobs)).values(status="completed", completed_at=now))
        return len(stuck) + orphaned

    # ------------------------------------------------------------------
    # The companies list (what preparation works through)
    # ------------------------------------------------------------------

    def company_list_count(self) -> int:
        with database.read() as conn:
            return conn.execute(select(func.count()).select_from(company_list).where(
                company_list.c.user_id == self.user_id)).scalar_one()

    def company_list_rows(self) -> list:
        """(company_name, email, website, contact_name) in upload order."""
        with database.read() as conn:
            rows = conn.execute(
                select(company_list.c.company_name, company_list.c.email,
                       company_list.c.website, company_list.c.contact_name)
                .where(company_list.c.user_id == self.user_id).order_by(company_list.c.id)).all()
        return [tuple(row) for row in rows]

    def company_list_emails(self) -> set:
        with database.read() as conn:
            return {row.email for row in conn.execute(select(company_list.c.email).where(
                company_list.c.user_id == self.user_id))}

    def add_companies(self, rows: list) -> int:
        """Append rows (dicts with email, company_name, website, contact_name),
        skipping addresses already in the list. Returns how many were added."""
        existing = self.company_list_emails()
        now = _now()
        fresh, seen = [], set()
        for row in rows:
            email = row["email"]
            if email in existing or email in seen:
                continue
            seen.add(email)
            fresh.append({"user_id": self.user_id, "email": email,
                          "company_name": row.get("company_name") or "",
                          "website": row.get("website") or "",
                          "contact_name": row.get("contact_name") or "", "created_at": now})
        if not fresh:
            return 0
        with database.tx() as conn:
            for start in range(0, len(fresh), 1000):
                conn.execute(company_list.insert(), fresh[start:start + 1000])
        return len(fresh)

    def clear_company_list(self) -> int:
        """Forget the uploaded list. Applications already created (drafts,
        sent history) are untouched."""
        with database.tx() as conn:
            return conn.execute(company_list.delete().where(
                company_list.c.user_id == self.user_id)).rowcount

    # ------------------------------------------------------------------
    # Research / draft cache
    # ------------------------------------------------------------------

    def cache_get(self, kind: str, key: str):
        with database.read() as conn:
            row = conn.execute(select(cache_entries.c.data).where(
                cache_entries.c.user_id == self.user_id, cache_entries.c.kind == kind,
                cache_entries.c.key == key)).first()
        if not row:
            return None
        try:
            return json.loads(row.data)
        except (TypeError, ValueError):
            return None

    def cache_keys(self, kind: str) -> set:
        with database.read() as conn:
            return {row.key for row in conn.execute(select(cache_entries.c.key).where(
                cache_entries.c.user_id == self.user_id, cache_entries.c.kind == kind))}

    def cache_put(self, kind: str, key: str, data: dict) -> None:
        with database.tx() as conn:
            database.upsert(conn, cache_entries, {
                "user_id": self.user_id, "kind": kind, "key": key,
                "data": json.dumps(data, ensure_ascii=False), "updated_at": _now(),
            }, ["user_id", "kind", "key"])


def for_user(user_id: int) -> UserData:
    return UserData(int(user_id))


# ---------------------------------------------------------------------------
# Helpers on plain rows (no database access)
# ---------------------------------------------------------------------------

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
    """Rebuild the research context from DB columns when the cache is
    missing. None for rows researched before the verified-areas format, so
    they get researched again rather than composed from nothing."""
    if not app:
        return None
    try:
        areas = json.loads(app.get("matched_extra_mentions") or "[]")
        reasons = json.loads(app.get("match_reasons") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not app.get("hook_status"):
        return None
    hook = app.get("company_hook") or ""
    return {
        "industry": app.get("industry") or "unknown",
        "mission_or_focus": app.get("mission_or_focus") or "",
        "tone_of_voice": app.get("tone_of_voice") or "unknown",
        "company_hook": hook,
        "hook_original": app.get("hook_original") or "",
        "hook_evidence": app.get("hook_evidence") or "",
        "hook_status": app.get("hook_status"),
        "areas": areas,
        "area_notes": [f"{k}: {v}" for k, v in reasons.items()],
        "talking_points": [hook] if hook else [],
        "matched_extra_mentions": areas,
        "match_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Cross-user queries for the background worker
# ---------------------------------------------------------------------------

def users_with_pending_send_jobs() -> list[int]:
    with database.read() as conn:
        return [row.user_id for row in conn.execute(
            select(send_jobs.c.user_id).where(send_jobs.c.status.in_(["pending", "running"]))
            .distinct())]


def users_with_recent_sends(days: int) -> list[int]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database.read() as conn:
        return [row.user_id for row in conn.execute(
            select(applications.c.user_id).where(applications.c.status == "sent",
                                                 applications.c.sent_at >= since).distinct())]


def all_user_ids_with_applications() -> list[int]:
    with database.read() as conn:
        return [row.user_id for row in conn.execute(select(applications.c.user_id).distinct())]


def pending_send_jobs() -> list:
    """Every pending/running job, oldest first: (id, user_id, worker_id, heartbeat_at)."""
    with database.read() as conn:
        return _dicts(conn.execute(
            select(send_jobs.c.id, send_jobs.c.user_id, send_jobs.c.worker_id,
                   send_jobs.c.heartbeat_at, send_jobs.c.status)
            .where(send_jobs.c.status.in_(["pending", "running"])).order_by(send_jobs.c.id)))


def claim_send_job(job_id: int, worker_id: str, stale_before: str) -> bool:
    """Take ownership of a job nobody owns, or whose owner stopped sending
    heartbeats before `stale_before`. Atomic, so two workers can never both
    process one job."""
    now = _now()
    with database.tx() as conn:
        result = conn.execute(update(send_jobs).where(
            send_jobs.c.id == int(job_id), send_jobs.c.status.in_(["pending", "running"]),
            (send_jobs.c.worker_id.is_(None)) | (send_jobs.c.worker_id == worker_id)
            | (send_jobs.c.heartbeat_at.is_(None)) | (send_jobs.c.heartbeat_at < stale_before),
        ).values(worker_id=worker_id, heartbeat_at=now))
        return result.rowcount == 1


def heartbeat_send_job(job_id: int, worker_id: str) -> bool:
    """False when another worker has taken the job over (we were too slow)."""
    with database.tx() as conn:
        return conn.execute(update(send_jobs).where(
            send_jobs.c.id == int(job_id), send_jobs.c.worker_id == worker_id
        ).values(heartbeat_at=_now())).rowcount == 1


def recover_job_items(job_id: int) -> int:
    """A job taken over from a dead worker: an item left 'sending' may or may
    not have gone out, so it becomes 'retry_wait' with a clear message rather
    than being sent a second time."""
    message = "Interrupted by a restart — check your Sent folder before resending."
    now = _now()
    with database.tx() as conn:
        stuck = conn.execute(select(send_job_items.c.id, send_job_items.c.application_id).where(
            send_job_items.c.job_id == int(job_id), send_job_items.c.status == "sending")).all()
        for item in stuck:
            conn.execute(update(send_job_items).where(send_job_items.c.id == item.id)
                         .values(status="retry_wait", error_message=message))
            conn.execute(update(applications).where(applications.c.id == item.application_id)
                         .values(status="retry_wait", error_message=message, updated_at=now))
    return len(stuck)


def ping() -> bool:
    with database.read() as conn:
        return conn.execute(text("SELECT 1")).scalar_one() == 1


