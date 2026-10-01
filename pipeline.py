"""
Pipeline orchestration — preparation only (research + writing).

The pipeline prepares emails and marks them as `ready` for dashboard review.
Sending is handled separately by the send job system.

Runs for ONE user: every read and write goes through that user's scope
(db.for_user), their own AI pool and their own profile.

RESUME RULES (by email, stable application id in DB):
  - sent / bounced / queued / sending  → skip entirely
  - subject+body already in DB or cache → mark ready, skip AI rewrite
  - researched in DB + research cache   → writer only, no re-scrape
  - failed / pending / writing          → continue from where it left off
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cache_store
import db
import drafting
import logsink
from agents.research_agent import get_company_context
from model_router import RoutingCancelled


def _load_draft(user_id: int, email: str, app: dict | None) -> dict | None:
    """Draft from the cache first, then from the DB row."""
    draft = cache_store.load_draft(user_id, email)
    if draft:
        return draft
    return db.draft_from_application(app)


def _load_research(user_id: int, email: str, app: dict | None) -> dict | None:
    """Research from the cache first, then rebuilt from DB columns."""
    context = cache_store.load_research(user_id, email)
    # Research saved before the verified-areas/hook format (no "areas" key)
    # carries nothing the composer can use, so treat it as a miss and redo it.
    if context and "areas" in context:
        return context
    if app:
        context = db.research_context_from_application(app)
        if context:
            cache_store.save_research(user_id, email, context)
        return context
    return None


def needs_preparation(user_id: int, app: dict | None) -> bool:
    """True if this email still needs research and/or writing."""
    if not app:
        return True
    if app["status"] in db.PREPARATION_DONE_STATUSES:
        return False
    if app["status"] in db.SEND_IN_FLIGHT_STATUSES:
        return False
    if _load_draft(user_id, app["email"], app):
        return False
    return True


class Pipeline:
    def __init__(self, user_id: int, client, dcfg: "drafting.DraftingConfig", model: str,
                 research_workers: int = 3, writer_workers: int = 2,
                 translation_model: str | None = None, stop_check=None):
        self.user_id = user_id
        self.data = db.for_user(user_id)
        self.client = client
        self.dcfg = dcfg
        self.model = model
        self.translation_model = translation_model

        # Worker threads inherit this run's log sink, so every line they print
        # lands in THIS user's activity log, never in another user's.
        sink = logsink.current()
        self.research_pool = ThreadPoolExecutor(max_workers=research_workers,
                                                 thread_name_prefix=f"research-u{user_id}",
                                                 initializer=logsink.bind, initargs=(sink,))
        self.writer_pool = ThreadPoolExecutor(max_workers=writer_workers,
                                               thread_name_prefix=f"writer-u{user_id}",
                                               initializer=logsink.bind, initargs=(sink,))

        self._shutdown = threading.Event()
        # Distinct from _shutdown, which is also set on normal completion to
        # retire the watcher thread: this one means the user asked to stop.
        self._stop_requested = threading.Event()
        # Answers "did the user press Stop?" (the worker reads it from the DB).
        self._stop_check = stop_check
        # A router waiting for a model must hear "Stop" too, or a worker can
        # sit in a long wait while the stop request goes unread.
        if getattr(client, "is_router", False):
            client.should_stop = self._should_stop

        self._lock = threading.Lock()
        self._local = threading.local()
        self.total_companies = 0
        self.terminal_count = 0
        self.results = {"ready": 0, "failed": 0, "skipped": 0}

    def _record(self, outcome: str):
        with self._lock:
            self.results[outcome] = self.results.get(outcome, 0) + 1
            self.terminal_count += 1
        # Lets _guard tell "this stage already accounted for its company"
        # apart from "this stage died before it could". Thread-local because
        # the two stages run in different pools and never nest.
        self._local.recorded = True

    def _guard(self, task, row_label: str, *args):
        """Outermost net: a stage that dies must not take its company with it.

        Nothing calls .result() on these futures, so an exception escaping a
        task would be swallowed by the executor — the company would silently
        disappear from the run. Only a raising stage is accounted for here;
        research *succeeds* by handing off to the writer pool without
        recording anything. Assumes nothing about being able to print.
        """
        self._local.recorded = False
        try:
            task(*args)
        except BaseException as exc:  # noqa: BLE001 — last line of defence
            try:
                print(f"  [error] {row_label} crashed: {exc!r}".encode(
                    "ascii", "replace").decode("ascii"))
            except Exception:
                pass
            if not getattr(self._local, "recorded", False):
                self._record("failed")

    def _should_stop(self) -> bool:
        return self._shutdown.is_set()

    def _watch_stop(self):
        """Polls the stop request and sets _shutdown as soon as it's made —
        the submit loop in run() finishes almost instantly (it only enqueues
        futures), so checking there alone would never catch a Stop click
        made after all rows are queued."""
        while not self._shutdown.is_set():
            try:
                stop = self._stop_check()
            except Exception:
                stop = False
            if stop:
                self._stop_requested.set()
                self._shutdown.set()
                break
            time.sleep(1)

    def _mark_ready_from_draft(self, app_id: int, email: str, company_name: str,
                                draft: dict, *, reused: bool):
        fields = {"status": "ready", "subject": draft["subject"], "body": draft["body"],
                  "error_message": None}
        if draft.get("language"):
            fields["language"] = draft["language"]
        self.data.update_application(app_id, **fields)
        cache_store.save_draft(self.user_id, email, draft)
        tag = "reused existing draft" if reused else "draft ready"
        print(f"  [ready] {company_name} (id={app_id}) — {tag}.")
        self._record("ready")

    def _research_task(self, row):
        company_name, email, website, contact_name = row
        if self._should_stop():
            self._record("skipped")
            return
        existing = None
        try:
            existing = self.data.get_application_by_email(email)
            app_id = self.data.get_or_create_application(company_name, email, website, contact_name)

            if existing and existing["status"] in db.PREPARATION_DONE_STATUSES:
                print(f"  [skip] {company_name} <{email}> (id={app_id}) — already {existing['status']}.")
                self._record("skipped")
                return

            if existing and existing["status"] in db.SEND_IN_FLIGHT_STATUSES:
                print(f"  [skip] {company_name} <{email}> (id={app_id}) — send in progress ({existing['status']}).")
                self._record("skipped")
                return

            draft = _load_draft(self.user_id, email, existing)
            if draft:
                self._mark_ready_from_draft(app_id, email, company_name, draft, reused=True)
                return

            context = _load_research(self.user_id, email, existing)
            if context is not None:
                print(f"  [research] {company_name} (id={app_id}) ... (cached, skipping re-scrape)")
            else:
                print(f"  [research] {company_name} (id={app_id}) ...")
                self.data.update_application(app_id, status="researching")
                self.data.log_event(app_id, "research",
                                    f"Scraping and analyzing {website or '(no website given)'}")

                context = get_company_context(
                    self.client, self.model, company_name, website, self.dcfg.research_areas,
                    translation_model=self.translation_model,
                )
                cache_store.save_research(self.user_id, email, context)

                self.data.update_application(
                    app_id,
                    status="researched",
                    error_message=None,          # a fresh result replaces any older failure
                    industry=context.get("industry"),
                    mission_or_focus=context.get("mission_or_focus"),
                    tone_of_voice=context.get("tone_of_voice"),
                    talking_points=json.dumps(context.get("talking_points", [])),
                    matched_extra_mentions=json.dumps(context.get("areas", [])),
                    match_reasons=json.dumps(context.get("match_reasons", {})),
                    company_hook=context.get("company_hook") or None,
                    hook_original=context.get("hook_original") or None,
                    hook_evidence=context.get("hook_evidence") or None,
                    hook_status=context.get("hook_status"),
                )
                self.data.log_event(
                    app_id, "research",
                    f"CV areas: {', '.join(context.get('areas') or []) or 'none'} · "
                    f"hook: {context.get('company_hook') or context.get('hook_status')}"
                    + (f" · site language: {context['site_language']}" if context.get("site_language") else "")
                    # Which model did the research, so a draft is always
                    # traceable to what wrote its company-specific line.
                    + (f" · via {context['research_model']}" if context.get("research_model") else ""),
                    detail=context,
                )

            self.writer_pool.submit(self._guard, self._writer_task,
                                     f"writer stage for id={app_id}",
                                     app_id, company_name, email, website,
                                     contact_name, context)

        except RoutingCancelled:
            # Stopped while waiting for a model. Put the company back exactly as
            # it was — never a draft built from half a research — so the next
            # run simply picks it up again.
            try:
                app_id = self.data.get_or_create_application(company_name, email, website, contact_name)
                previous = (existing or {}).get("status") or "pending"
                self.data.update_application(
                    app_id, status="pending" if previous == "researching" else previous)
            except Exception:
                pass
            print(f"  [stop] {company_name} — left for the next run.")
            self._record("skipped")

        except Exception as e:
            print(f"  [error] research stage crashed for {company_name}: {e}")
            try:
                app_id = self.data.get_or_create_application(company_name, email, website, contact_name)
                self.data.update_application(app_id, status="failed", error_message=f"Research error: {e}")
                self.data.log_event(app_id, "research", "Research stage crashed", detail={"error": str(e)})
            except Exception:
                pass
            self._record("failed")

    def _writer_task(self, app_id, company_name, email, website, contact_name, context):
        if self._should_stop():
            self._record("skipped")
            return
        try:
            existing = self.data.get_application_by_id(app_id)
            draft = _load_draft(self.user_id, email, existing)
            if draft:
                print(f"  [write] {company_name} (id={app_id}) ... (existing draft, skipping re-write)")
                self._mark_ready_from_draft(app_id, email, company_name, draft, reused=True)
                return

            print(f"  [write] generating email for {company_name} (id={app_id}) ...")
            self.data.update_application(app_id, status="writing")
            row = existing or {"email": email, "company_name": company_name,
                               "website": website, "contact_name": contact_name}
            email_content = drafting.compose_for(self.dcfg, row, context)
            cache_store.save_draft(self.user_id, email, email_content)

            self.data.update_application(app_id, status="ready",
                                         subject=email_content["subject"],
                                         body=email_content["body"],
                                         language=email_content["language"],
                                         error_message=None)
            self.data.log_event(app_id, "write",
                                f"Draft ready ({email_content['language'].upper()}): "
                                f"\"{email_content['subject']}\"",
                                detail=email_content)

            print(f"  [ready] {company_name} (id={app_id}) — email ready for review.")
            self._record("ready")

        except Exception as e:
            print(f"  [error] writer stage failed for {company_name} (id={app_id}): {e}")
            try:
                self.data.update_application(app_id, status="failed",
                                             error_message=f"Writer agent error: {e}")
                self.data.log_event(app_id, "write", "Writer agent failed", detail={"error": str(e)})
            except Exception:
                pass
            self._record("failed")

    def run(self, companies_rows):
        """
        companies_rows: list of (company_name, email, website, contact_name) tuples.
        Returns the results dict. Safe to Ctrl+C.
        """
        recovered = self.data.recover_stale_preparation_rows()
        if recovered:
            print(f"Recovered {recovered} row(s) left mid-stage by a previous crashed/interrupted run.")

        self.total_companies = len(companies_rows)
        if self.total_companies == 0:
            print("Nothing to prepare — all selected companies are already done or in-flight.")
            return self.results

        stop_watcher = None
        if self._stop_check:
            stop_watcher = threading.Thread(target=logsink.wrap(self._watch_stop), daemon=True,
                                             name=f"stop-watcher-u{self.user_id}")
            stop_watcher.start()

        for row in companies_rows:
            if self._should_stop():
                break
            self.research_pool.submit(self._guard, self._research_task,
                                       f"research stage for <{row[1]}>", row)

        try:
            self.research_pool.shutdown(wait=True,
                                         cancel_futures=self._shutdown.is_set())
            self.writer_pool.shutdown(wait=True,
                                       cancel_futures=self._shutdown.is_set())
        except KeyboardInterrupt:
            print("\nInterrupted — shutting down (completed work is saved)...")
            self._stop_requested.set()
            self._shutdown.set()
            self.research_pool.shutdown(wait=False, cancel_futures=True)
            self.writer_pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self._shutdown.set()  # let the stop-watcher thread exit promptly

        if self._stop_requested.is_set():
            # Work cancelled by the stop request never ran, so it never
            # recorded an outcome. Count it as skipped, otherwise the caller's
            # reconciliation check reports companies as "lost".
            with self._lock:
                unaccounted = self.total_companies - self.terminal_count
                if unaccounted > 0:
                    self.results["skipped"] = self.results.get("skipped", 0) + unaccounted
                    self.terminal_count += unaccounted
            print("Stopped on request — prepared work is saved; re-run to continue.")

        return self.results


def select_rows(user_id: int, *, limit: int | None = None, include_all: bool = False) -> tuple[list, int]:
    """The user's company-list rows that still need work, in upload order.
    Returns (rows, skipped_as_done)."""
    data = db.for_user(user_id)
    all_rows = data.company_list_rows()
    if include_all:
        rows, skipped = all_rows, 0
    else:
        # Two queries in total, not one per row: on a 22k-row list a
        # per-row lookup held the run for minutes before the first company.
        known = data.get_applications_by_email()
        cached_drafts = data.cache_keys("draft")
        rows, skipped = [], 0
        for row in all_rows:
            app = known.get(row[1])
            done = bool(app) and (
                app["status"] in db.PREPARATION_DONE_STATUSES
                or app["status"] in db.SEND_IN_FLIGHT_STATUSES
                or db.draft_from_application(app) is not None
                or cache_store._key(app["email"]) in cached_drafts)
            if done:
                skipped += 1
            else:
                rows.append(row)
    return (rows[:limit] if limit else rows), skipped
