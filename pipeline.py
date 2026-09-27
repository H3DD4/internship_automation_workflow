"""
Pipeline orchestration — preparation only (research + writing).

The pipeline prepares emails and marks them as `ready` for dashboard review.
Sending is handled separately by the dashboard's send job system.

RESUME RULES (by email, stable application id in DB):
  - sent / bounced / queued / sending  → skip entirely
  - subject+body already in DB or cache → mark ready, skip AI rewrite
  - researched in DB + research cache   → writer only, no re-scrape
  - failed / pending / writing          → continue from where it left off
"""

import json
import threading
import os
import time
from concurrent.futures import ThreadPoolExecutor

import db
import cache_store
from utils import build_greeting
from agents.research_agent import get_company_context
from agents.composer import compose_email


def _load_draft(email: str, app: dict | None) -> dict | None:
    """Draft from disk cache first, then from the DB row."""
    draft = cache_store.load_draft(email)
    if draft:
        return draft
    return db.draft_from_application(app)


def _load_research(email: str, app: dict | None) -> dict | None:
    """Research from disk cache first, then rebuild from DB columns."""
    context = cache_store.load_research(email)
    # Research saved before the verified-areas/hook format (no "areas" key)
    # carries nothing the composer can use, so treat it as a miss and redo it.
    if context and "areas" in context:
        return context
    if app:
        context = db.research_context_from_application(app)
        if context:
            cache_store.save_research(email, context)
        return context
    return None


def needs_preparation(app: dict | None) -> bool:
    """True if this email still needs research and/or writing."""
    if not app:
        return True
    if app["status"] in db.PREPARATION_DONE_STATUSES:
        return False
    if app["status"] in db.SEND_IN_FLIGHT_STATUSES:
        return False
    if _load_draft(app["email"], app):
        return False
    return True


class Pipeline:
    def __init__(self, client, cfg, model: str,
                 research_workers: int = 3, writer_workers: int = 2):
        self.client = client
        self.cfg = cfg
        self.model = model

        self.research_pool = ThreadPoolExecutor(max_workers=research_workers,
                                                 thread_name_prefix="research")
        self.writer_pool = ThreadPoolExecutor(max_workers=writer_workers,
                                               thread_name_prefix="writer")

        self._shutdown = threading.Event()
        # Distinct from _shutdown, which is also set on normal completion to
        # retire the watcher thread: this one means the user asked to stop.
        self._stop_requested = threading.Event()
        self._stop_file = os.getenv("PIPELINE_STOP_FILE")

        self._lock = threading.Lock()
        self.total_companies = 0
        self.terminal_count = 0
        self.results = {"ready": 0, "failed": 0, "skipped": 0}

    def _record(self, outcome: str):
        with self._lock:
            self.results[outcome] = self.results.get(outcome, 0) + 1
            self.terminal_count += 1

    def _should_stop(self) -> bool:
        return self._shutdown.is_set()

    def _watch_stop_file(self):
        """Polls for the stop file and sets _shutdown as soon as it appears —
        the submit loop in run() finishes almost instantly (it only enqueues
        futures), so checking the stop file there alone would never catch a
        Stop click made after all rows are queued. Tasks themselves check
        _should_stop() at their own entry point, so setting the flag here is
        what actually makes "Stop safely" take effect on not-yet-started work."""
        while not self._shutdown.is_set():
            if self._stop_file and os.path.exists(self._stop_file):
                self._stop_requested.set()
                self._shutdown.set()
                break
            time.sleep(1)

    def _mark_ready_from_draft(self, app_id: int, email: str, company_name: str,
                                draft: dict, *, reused: bool):
        db.update_application(app_id, status="ready",
                               subject=draft["subject"], body=draft["body"],
                               error_message=None)
        cache_store.save_draft(email, draft)
        tag = "reused existing draft" if reused else "draft ready"
        print(f"  [ready] {company_name} (id={app_id}) — {tag}.")
        self._record("ready")

    def _research_task(self, row):
        company_name, email, website, contact_name = row
        if self._should_stop():
            self._record("skipped")
            return
        try:
            existing = db.get_application_by_email(email)
            app_id = db.get_or_create_application(company_name, email, website, contact_name)

            if existing and existing["status"] in db.PREPARATION_DONE_STATUSES:
                print(f"  [skip] {company_name} <{email}> (id={app_id}) — already {existing['status']}.")
                self._record("skipped")
                return

            if existing and existing["status"] in db.SEND_IN_FLIGHT_STATUSES:
                print(f"  [skip] {company_name} <{email}> (id={app_id}) — send in progress ({existing['status']}).")
                self._record("skipped")
                return

            draft = _load_draft(email, existing)
            if draft:
                self._mark_ready_from_draft(app_id, email, company_name, draft, reused=True)
                return

            context = _load_research(email, existing)
            if context is not None:
                print(f"  [research] {company_name} (id={app_id}) ... (cached, skipping re-scrape)")
            else:
                print(f"  [research] {company_name} (id={app_id}) ...")
                db.update_application(app_id, status="researching")
                db.log_event(app_id, "research",
                             f"Scraping and analyzing {website or '(no website given)'}")

                context = get_company_context(
                    self.client, self.model, company_name, website, self.cfg["spec"]["areas"]
                )
                cache_store.save_research(email, context)

                db.update_application(
                    app_id,
                    status="researched",
                    industry=context.get("industry"),
                    mission_or_focus=context.get("mission_or_focus"),
                    tone_of_voice=context.get("tone_of_voice"),
                    talking_points=json.dumps(context.get("talking_points", [])),
                    matched_extra_mentions=json.dumps(context.get("areas", [])),
                    match_reasons=json.dumps(context.get("match_reasons", {})),
                    company_hook=context.get("company_hook") or None,
                    hook_evidence=context.get("hook_evidence") or None,
                    hook_status=context.get("hook_status"),
                )
                db.log_event(
                    app_id, "research",
                    f"CV areas: {', '.join(context.get('areas') or []) or 'none'} · "
                    f"hook: {context.get('company_hook') or context.get('hook_status')}",
                    detail=context,
                )

            self.writer_pool.submit(self._writer_task, app_id, company_name, email, website,
                                     contact_name, context)

        except Exception as e:
            print(f"  [error] research stage crashed for {company_name}: {e}")
            try:
                app_id = db.get_or_create_application(company_name, email, website, contact_name)
                db.update_application(app_id, status="failed", error_message=f"Research error: {e}")
                db.log_event(app_id, "research", "Research stage crashed", detail={"error": str(e)})
            except Exception:
                pass
            self._record("failed")

    def _writer_task(self, app_id, company_name, email, website, contact_name, context):
        if self._should_stop():
            self._record("skipped")
            return
        try:
            existing = db.get_application_by_id(app_id)
            draft = _load_draft(email, existing)
            if draft:
                print(f"  [write] {company_name} (id={app_id}) ... (existing draft, skipping re-write)")
                self._mark_ready_from_draft(app_id, email, company_name, draft, reused=True)
                return

            print(f"  [write] generating email for {company_name} (id={app_id}) ...")
            db.update_application(app_id, status="writing")
            greeting = build_greeting(contact_name, company_name)

            email_content = compose_email(
                self.cfg["spec"], context, company_name, greeting,
                self.cfg["applicant_name"], self.cfg["target_role"],
            )
            cache_store.save_draft(email, email_content)

            db.update_application(app_id, status="ready",
                                   subject=email_content["subject"],
                                   body=email_content["body"],
                                   error_message=None)
            db.log_event(app_id, "write",
                         f"Draft ready: \"{email_content['subject']}\"",
                         detail=email_content)

            print(f"  [ready] {company_name} (id={app_id}) — email ready for review.")
            self._record("ready")

        except Exception as e:
            print(f"  [error] writer stage failed for {company_name} (id={app_id}): {e}")
            try:
                db.update_application(app_id, status="failed", error_message=f"Writer agent error: {e}")
                db.log_event(app_id, "write", "Writer agent failed", detail={"error": str(e)})
            except Exception:
                pass
            self._record("failed")

    def run(self, companies_rows):
        """
        companies_rows: list of (company_name, email, website, contact_name) tuples.
        Returns the results dict. Safe to Ctrl+C.
        """
        recovered = db.recover_stale_preparation_rows()
        if recovered:
            print(f"Recovered {recovered} row(s) left mid-stage by a previous crashed/interrupted run.")

        self.total_companies = len(companies_rows)
        if self.total_companies == 0:
            print("Nothing to prepare — all selected companies are already done or in-flight.")
            return self.results

        stop_watcher = None
        if self._stop_file:
            stop_watcher = threading.Thread(target=self._watch_stop_file, daemon=True,
                                             name="stop-watcher")
            stop_watcher.start()

        for row in companies_rows:
            if self._should_stop():
                break
            self.research_pool.submit(self._research_task, row)

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
            # reconciliation check reports companies as "lost" when in fact
            # the user asked us to stop.
            with self._lock:
                unaccounted = self.total_companies - self.terminal_count
                if unaccounted > 0:
                    self.results["skipped"] = self.results.get("skipped", 0) + unaccounted
                    self.terminal_count += unaccounted
            print("Stopped on request — prepared work is saved; re-run to continue.")

        return self.results
