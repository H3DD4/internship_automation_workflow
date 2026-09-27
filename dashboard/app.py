"""
Simple local dashboard. Run with:  python dashboard/app.py
Then open http://127.0.0.1:5050 in your browser.

Read-only view into applications.db — shows overall stats, every company's
current status, and (per company) the full timeline: research findings,
AI reasoning behind matched extra mentions, the generated email, and the
final send/bounce outcome.

Live view of a running pipeline: since the sender (main.py) writes to the
database in WAL mode, this dashboard can safely read it while a run is in
progress. The page auto-refreshes (toggle in the top bar) so you can watch
a run happen without touching the terminal.

Google OAuth 2.0 flow:
  /oauth/start      -> redirects to Google consent screen
  /oauth/callback   -> exchanges code for token, saves token.json
  /oauth/disconnect -> deletes token.json
"""

import os
import sys
import json
import secrets
import smtplib
import imaplib
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv
import db
import sender_worker
from ai_client import DEFAULT_BASE_URL, DEFAULT_MODEL, KEY_PORTAL_URL, SUPPORTED_MODELS

# Allows OAuth's local HTTP redirect (127.0.0.1:5050) to satisfy oauthlib's
# strict HTTPS check. Safe only because this dashboard is hardcoded to bind
# to the loopback address (see app.run(host="127.0.0.1", ...) at the bottom
# of this file) — set once here rather than per-request.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

ROOT_DIR = Path(__file__).parent.parent
UPLOAD_DIR = ROOT_DIR / "dashboard_uploads"
ENV_PATH = ROOT_DIR / ".env"
MAIN_PATH = ROOT_DIR / "main.py"
RUN_LOG_PATH = ROOT_DIR / "dashboard_run.log"
STOP_FILE = ROOT_DIR / "dashboard_stop.flag"
run_process = None


def _format_env_line(key: str, value) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def _save_env(values):
    """Update keys in .env in place, preserving comments, blank lines, and the
    existing ordering; keys not already present are appended at the end.

    Rewriting the whole file from just its parsed key=value pairs (the
    previous behavior) silently deleted every comment — and .env.example,
    which users copy to .env, is mostly comments explaining each setting,
    so the first dashboard save wiped all of that guidance.
    """
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = {key: value for key, value in values.items() if value is not None}

    updated = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                updated.append(_format_env_line(key, remaining.pop(key)))
                continue
        updated.append(line)

    for key, value in remaining.items():
        updated.append(_format_env_line(key, value))

    ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _get_or_create_dashboard_secret() -> str:
    """A hardcoded Flask secret_key lets anyone forge a valid session cookie
    for this app (used for the OAuth CSRF-state/PKCE-verifier and flash
    messages) — generate a random one on first run and persist it in .env
    so it stays stable across restarts instead of rotating (which would
    invalidate any session mid-OAuth-flow) or staying a known constant."""
    load_dotenv(ENV_PATH, override=True)
    existing = (os.getenv("DASHBOARD_SECRET", "") or "").strip()
    if existing:
        return existing
    generated = secrets.token_hex(32)
    _save_env({"DASHBOARD_SECRET": generated})
    return generated


app = Flask(__name__)
app.secret_key = _get_or_create_dashboard_secret()

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
    "skipped": "Skipped",
}

# Statuses that mean "a background worker is actively on this one right now" —
# used for the small live-activity indicator in the top bar. "researched" is
# NOT included: it means research finished and it's waiting to be picked up
# by a writer worker, not that anything is actively running on it.
IN_PROGRESS_STATUSES = {"researching", "writing", "sending", "queued"}

# Statuses whose draft can still be edited (nothing has been sent yet, and no
# sender worker owns the row).
EDITABLE_STATUSES = frozenset({"ready", "failed", "retry_wait"})

# The tabs above the table. Each value is either a group name from
# db.STATUS_GROUPS or a single status; "" means no filter.
STATUS_TABS = [
    ("", "All"),
    ("to_prepare", "To prepare"),
    ("ready", "Ready"),
    ("sent", "Sent"),
    ("problems", "Problems"),
    ("skipped", "Skipped"),
]


def _preparation_config() -> dict:
    """AI + applicant config for a one-off writer run from the dashboard
    (the Regenerate button). Mirrors main.load_config() but raises instead of
    calling sys.exit(), which would kill the dashboard process."""
    spec_path = ROOT_DIR / "specializations.json"
    with open(spec_path) as f:
        specializations = json.load(f)
    return {
        "ai_api_key": (os.getenv("AI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip(),
        "ai_base_url": os.getenv("AI_BASE_URL", DEFAULT_BASE_URL),
        "ai_model": os.getenv("AI_MODEL", DEFAULT_MODEL),
        "applicant_name": os.getenv("YOUR_NAME", ""),
        "target_role": os.getenv("YOUR_TARGET_ROLE", ""),
        "core_identity": specializations["core_identity"],
        "extra_mentions": specializations["extra_mentions"],
        "company_paragraph": specializations.get("company_paragraph", {}),
    }


def _run_state():
    global run_process
    if run_process is not None and run_process.poll() is not None:
        run_process = None
    tail = ""
    if RUN_LOG_PATH.exists():
        try:
            lines = RUN_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-60:])
        except OSError:
            tail = ""
    return {"running": run_process is not None,
            "stop_requested": STOP_FILE.exists(),
            "log_path": str(RUN_LOG_PATH) if RUN_LOG_PATH.exists() else None,
            "log_tail": tail}


def _resolve_saved_path(raw_value: str):
    """Resolve a path saved in .env (absolute or relative to project root)."""
    if not raw_value:
        return None
    path = Path(raw_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _is_placeholder(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered:
        return True
    # App passwords are often pasted with spaces ("xxxx xxxx xxxx xxxx") and
    # _check_gmail_credentials strips spaces before comparing — so compare
    # in both spaced and unspaced form.
    compact = lowered.replace(" ", "")
    exact_placeholders = {
        "your_bai_api_key_here", "your_anthropic_api_key_here", "your_groq_api_key_here",
        "you@gmail.com", "your full name", "xxxx xxxx xxxx xxxx",
        "xxxxxxxxxxxxxxxx", "./my_cv.pdf", "my_cv.pdf",
        "companies.xlsx", "./companies.xlsx",
    }
    return lowered.strip("\"'") in exact_placeholders or compact.strip("\"'") in exact_placeholders


def _setup_state():
    load_dotenv(ENV_PATH, override=True)
    companies_raw = (os.getenv("COMPANIES_FILE_PATH", "") or "").strip()
    cv_raw = (os.getenv("CV_FILE_PATH", "") or "").strip()
    companies_path = _resolve_saved_path("" if _is_placeholder(companies_raw) else companies_raw)
    cv_path = _resolve_saved_path("" if _is_placeholder(cv_raw) else cv_raw)
    has_companies = bool(companies_path and companies_path.is_file())
    has_cv = bool(cv_path and cv_path.is_file())
    ai_key = (os.getenv("AI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")).strip()
    gmail_address = (os.getenv("GMAIL_ADDRESS", "") or "").strip()
    gmail_password = (os.getenv("GMAIL_APP_PASSWORD", "") or "").strip()
    your_name = (os.getenv("YOUR_NAME", "") or "").strip()
    target_role = (os.getenv("YOUR_TARGET_ROLE", "") or "").strip()
    has_api_key = bool(ai_key) and not _is_placeholder(ai_key)
    has_profile = bool(your_name) and bool(target_role) and not _is_placeholder(your_name)

    # OAuth status
    oauth_connected = False
    oauth_email = ""
    oauth_configured = False
    try:
        from google_auth_helper import (
            token_exists, get_credentials, get_authorized_email, oauth_is_configured
        )
        oauth_configured = oauth_is_configured()
        if token_exists() and get_credentials() is not None:
            oauth_connected = True
            oauth_email = get_authorized_email() or gmail_address
    except ImportError:
        pass

    # Gmail is "ready" if we have OAuth OR app-password credentials
    has_gmail_oauth = oauth_connected
    has_gmail_smtp = (bool(gmail_address) and bool(gmail_password)
                      and not _is_placeholder(gmail_address)
                      and not _is_placeholder(gmail_password))
    has_gmail = has_gmail_oauth or has_gmail_smtp

    # Preparation (research + write) only needs an AI key, a profile, and a
    # companies file — Gmail and the CV are send-time-only concerns. Gating
    # "Start preparation" on Gmail/CV (as "ready" alone used to) blocked
    # drafting emails for anyone who hadn't connected Gmail yet.
    prep_ready = has_api_key and has_profile and has_companies
    send_ready = prep_ready and has_gmail and has_cv

    return {
        "ready": send_ready,
        "prep_ready": prep_ready,
        "send_ready": send_ready,
        "has_profile_and_files": has_profile and has_companies,
        "min_delay": os.getenv("MIN_DELAY_SECONDS", "45"),
        "max_delay": os.getenv("MAX_DELAY_SECONDS", "120"),
        "max_per_day": os.getenv("MAX_EMAILS_PER_DAY", "20"),
        "has_api_key": has_api_key,
        "has_gmail": has_gmail,
        "has_gmail_oauth": has_gmail_oauth,
        "has_gmail_smtp": has_gmail_smtp,
        "oauth_configured": oauth_configured,
        "oauth_email": oauth_email,
        "gmail_address": "" if _is_placeholder(gmail_address) else gmail_address,
        "name": "" if _is_placeholder(your_name) else your_name,
        "target_role": target_role,
        "companies_name": companies_path.name if has_companies else "",
        "companies_rows": _count_companies_rows(companies_path) if has_companies else None,
        "cv_name": cv_path.name if has_cv else "",
        "cv_size_kb": round(cv_path.stat().st_size / 1024, 1) if has_cv else None,
        "ai_model": os.getenv("AI_MODEL", DEFAULT_MODEL),
        "ai_base_url": os.getenv("AI_BASE_URL", DEFAULT_BASE_URL),
    }


def _count_companies_rows(companies_path: Path) -> int:
    """Best-effort row count for the setup banner. Never raises."""
    try:
        suffix = companies_path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            try:
                from openpyxl import load_workbook
                workbook = load_workbook(companies_path, read_only=True, data_only=True)
                sheet = workbook.active
                rows = sum(1 for _ in sheet.iter_rows(values_only=True)) - 1
                workbook.close()
                return max(rows, 0)
            except Exception:
                import pandas as pd
                return max(len(pd.read_excel(companies_path)), 0)
        import pandas as pd
        return max(len(pd.read_csv(companies_path)), 0)
    except Exception:
        return 0


def _validate_companies_file(path: Path) -> tuple[bool, str, int]:
    """Check the uploaded companies file has an email column + usable rows."""
    try:
        import pandas as pd
        if path.suffix.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        else:
            df = pd.read_csv(path)
    except Exception as exc:
        return False, f"Could not read that file ({exc}).", 0
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "email" not in df.columns:
        return False, f"Companies file must have an 'email' column. Found: {', '.join(df.columns) or 'none'}.", 0
    from utils import is_valid_email
    usable = int(df["email"].apply(is_valid_email).sum())
    if usable == 0:
        return False, "No valid email addresses found in that file.", 0
    return True, "", usable


def _request_origin_ok() -> bool:
    """Best-effort CSRF guard for state-changing requests.

    A browser always attaches an Origin header to a cross-site POST/fetch
    (this can't be suppressed by an attacker page), so comparing it against
    this dashboard's own origin blocks a foreign page's auto-submitted form
    or fetch() from reaching /setup, /run, /api/send-job, etc. Without this,
    any website you merely visit could silently rewrite AI_BASE_URL to
    exfiltrate your AI key and every prompt, or queue real sends.
    """
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin.rstrip("/") == request.host_url.rstrip("/")
    referer = request.headers.get("Referer")
    if referer:
        return urlparse(referer).netloc == request.host
    # A real browser submission always sends at least one of these; a
    # state-changing request with neither is treated as untrusted.
    return False


@app.before_request
def _csrf_guard():
    if request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint == "static":
        return None
    if not _request_origin_ok():
        message = "Request blocked: it didn't come from this dashboard (origin check failed)."
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": message}), 403
        abort(403, description=message)
    return None


def _decorate_rows(applications: list) -> list:
    """Add the display-only fields the table needs to each application row."""
    for a in applications:
        a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])
        try:
            a["matched_extra_mentions_list"] = json.loads(a["matched_extra_mentions"] or "[]")
        except (TypeError, json.JSONDecodeError):
            a["matched_extra_mentions_list"] = []
        # Short inline error/retry reason for the list view, so a failed or
        # retry_wait row is understandable at a glance without a click-through.
        msg = a.get("error_message") or ""
        a["error_short"] = (msg[:80] + "…") if len(msg) > 80 else msg
        a["sendable"] = bool(a["status"] in db.SENDABLE_STATUSES and a.get("subject"))
        # "09-27 20:22" — the year is always the current one and just crowded
        # the column until the whole timestamp truncated to "202…".
        a["updated_short"] = (a["updated_at"][5:16].replace("T", " ")
                               if a.get("updated_at") else "—")
    return applications


# The funnel cards shown above the table: (group key, label, the status filter
# clicking the card applies). Each maps to a db.STATUS_GROUPS bucket.
FUNNEL_CARDS = [
    ("to_prepare", "To prepare", "to_prepare"),
    ("ready", "Ready to review", "ready"),
    ("sending", "Sending", "sending"),
    ("sent", "Sent", "sent"),
    ("problems", "Problems", "problems"),
]

# Group names usable as a ?status= filter value, expanded to their statuses so
# a funnel card and a status tab can both filter by a whole stage.
FILTER_GROUPS = db.STATUS_GROUPS


def _table_context(page: int, status_param: str, search_param: str, limit: int = 50) -> dict:
    """Shared table/stat payload for both the full page render and the JSON
    refresh endpoint, so the live-updating view can never drift from the
    server-rendered one."""
    statuses = FILTER_GROUPS.get(status_param) if status_param else None
    applications, table_total = db.get_applications_paginated(
        status=status_param if not statuses else None,
        statuses=list(statuses) if statuses else None,
        search=search_param, page=page, limit=limit,
    )
    grouped = db.get_grouped_stats()
    sent_today = db.count_sent_today()
    max_per_day = int(os.getenv("MAX_EMAILS_PER_DAY", 20))
    return {
        "applications": _decorate_rows(applications),
        "table_total": table_total,
        "table_page": page,
        "table_pages": max(1, (table_total + limit - 1) // limit),
        "table_limit": limit,
        "funnel": [
            {"key": key, "label": label, "filter": filter_value, "value": grouped.get(key, 0)}
            for key, label, filter_value in FUNNEL_CARDS
        ],
        "grouped": grouped,
        "total_count": grouped.get("total", 0),
        "skipped_count": grouped.get("skipped", 0),
        "sent_today": sent_today,
        "max_per_day": max_per_day,
        "cap_remaining": max(0, max_per_day - sent_today),
        "in_progress_count": sum(grouped["by_status"].get(s, 0) for s in IN_PROGRESS_STATUSES),
        "pending_prep": db.count_needing_preparation(),
        "filter_status": status_param or "",
        "filter_q": search_param or "",
    }


@app.route("/")
def index():
    db.init_db()
    sender_worker.ensure_running()
    load_dotenv(ENV_PATH, override=True)

    page = max(1, request.args.get("page", 1, type=int))
    status_param = request.args.get("status", "").strip()
    search_param = request.args.get("q", "").strip()

    context = _table_context(page, status_param, search_param)

    active_jobs = db.get_pending_send_jobs()
    active_job = active_jobs[0] if active_jobs else None

    return render_template(
        "index.html",
        setup=_setup_state(),
        run_state=_run_state(),
        active_job=db.get_send_job(active_job["id"]) if active_job else None,
        status_tabs=STATUS_TABS,
        supported_models=SUPPORTED_MODELS,
        key_portal_url=KEY_PORTAL_URL,
        **context,
    )


@app.get("/api/overview")
def api_overview():
    """Stats + the current table page, so the dashboard can refresh live in
    place. A full page reload (the old behavior) cleared any checked rows and
    interrupted an in-flight send's progress toast.

    The rows come back as HTML rendered from the same _rows.html partial the
    full page uses, so the live view can't drift from the server-rendered one.
    """
    page = max(1, request.args.get("page", 1, type=int))
    status_param = request.args.get("status", "").strip()
    search_param = request.args.get("q", "").strip()
    context = _table_context(page, status_param, search_param)
    run_state = _run_state()
    return jsonify({
        "ok": True,
        "rows_html": render_template("_rows.html", applications=context["applications"]),
        "funnel": context["funnel"],
        "grouped": {k: v for k, v in context["grouped"].items() if k != "by_status"},
        "table_total": context["table_total"],
        "table_page": context["table_page"],
        "table_pages": context["table_pages"],
        "total_count": context["total_count"],
        "pending_prep": context["pending_prep"],
        "in_progress_count": context["in_progress_count"],
        "sent_today": context["sent_today"],
        "max_per_day": context["max_per_day"],
        "cap_remaining": context["cap_remaining"],
        "running": run_state["running"],
        "log_tail": run_state["log_tail"],
    })


@app.post("/api/skip/<int:app_id>")
def api_skip(app_id):
    """Mark a company as deliberately skipped (or un-skip it). Skipped rows
    are left alone by preparation and can't be sent, but nothing is deleted,
    so it's always reversible."""
    payload = request.get_json(silent=True) or {}
    unskip = bool(payload.get("unskip"))
    application = db.get_application_by_id(app_id)
    if not application:
        return jsonify({"ok": False, "message": "Application not found."}), 404

    if unskip:
        if application["status"] != "skipped":
            return jsonify({"ok": False, "message": "That company isn't skipped."})
        # Back to 'ready' when a draft exists, otherwise let preparation redo it.
        new_status = "ready" if (application.get("subject") and application.get("body")) else "pending"
        db.update_application(app_id, status=new_status, error_message=None)
        db.log_event(app_id, "skip", "Un-skipped from dashboard.")
        return jsonify({"ok": True, "status": new_status, "message": "Company un-skipped."})

    if application["status"] in db.SEND_IN_FLIGHT_STATUSES or application["status"] == "sent":
        return jsonify({"ok": False,
                        "message": f"Can't skip — already {application['status']}."})
    db.update_application(app_id, status="skipped", error_message=None)
    db.log_event(app_id, "skip", "Skipped from dashboard.")
    return jsonify({"ok": True, "status": "skipped", "message": "Company skipped."})


@app.post("/api/regenerate/<int:app_id>")
def api_regenerate(app_id):
    """Re-run the writer agent for one company, reusing its saved research so
    nothing is scraped or researched again."""
    import cache_store
    from ai_client import CompatibleAIClient
    from agents.writer_agent import generate_email
    from utils import build_greeting
    import pipeline as pipeline_module

    application = db.get_application_by_id(app_id)
    if not application:
        return jsonify({"ok": False, "message": "Application not found."}), 404
    if application["status"] in db.SEND_IN_FLIGHT_STATUSES or application["status"] == "sent":
        return jsonify({"ok": False,
                        "message": f"Can't regenerate — already {application['status']}."})

    load_dotenv(ENV_PATH, override=True)
    try:
        cfg = _preparation_config()
    except (OSError, KeyError, ValueError) as exc:
        return jsonify({"ok": False, "message": f"Configuration problem: {exc}"}), 400
    if not cfg["ai_api_key"]:
        return jsonify({"ok": False, "message": "Add your AI API key in the setup form first."}), 400

    context = pipeline_module._load_research(application["email"], application)
    if context is None:
        return jsonify({"ok": False,
                        "message": "No saved research for this company yet — run preparation first."})

    client = CompatibleAIClient(cfg["ai_api_key"], cfg["ai_base_url"])
    greeting = build_greeting(application.get("contact_name"), application["company_name"])
    try:
        draft = generate_email(
            client, cfg["ai_model"], cfg["core_identity"], cfg["applicant_name"],
            context, application["company_name"], cfg["extra_mentions"],
            cfg["target_role"], greeting,
            company_paragraph_rules=cfg.get("company_paragraph"),
        )
    except Exception as exc:
        db.log_event(app_id, "write", "Regenerate failed", detail={"error": str(exc)})
        return jsonify({"ok": False, "message": f"Writer agent failed: {exc}"}), 502

    cache_store.save_draft(application["email"], draft)
    db.update_application(app_id, status="ready", subject=draft["subject"],
                          body=draft["body"], error_message=None)
    db.log_event(app_id, "write", f"Draft regenerated from dashboard: \"{draft['subject']}\"",
                 detail=draft)
    return jsonify({"ok": True, "subject": draft["subject"], "body": draft["body"],
                    "message": "Draft regenerated."})


@app.post("/setup")
def setup():
    uploads = UPLOAD_DIR
    uploads.mkdir(exist_ok=True)
    load_dotenv(ENV_PATH, override=True)
    from werkzeug.utils import secure_filename

    # Only what PREPARATION needs is required here. Gmail (address+app
    # password OR OAuth, checked separately) and the CV are send-time-only
    # concerns — requiring them up front used to block saving a workspace,
    # and therefore starting preparation, before Gmail was connected at all.
    field_names = {
        "AI_API_KEY": "ai_api_key",
        "AI_BASE_URL": "ai_base_url",
        "AI_MODEL": "ai_model",
        "YOUR_NAME": "your_name",
        "YOUR_TARGET_ROLE": "target_role",
    }
    optional_field_names = {
        "GMAIL_ADDRESS": "gmail_address",
        "GMAIL_APP_PASSWORD": "gmail_app_password",
    }
    # Numeric pacing settings: keep the saved value when left blank, and
    # ignore anything non-numeric rather than writing a value that would
    # crash the sender worker's int() parse later.
    pacing_fields = {}
    for key, form_name, minimum in (
        ("MIN_DELAY_SECONDS", "min_delay", 0),
        ("MAX_DELAY_SECONDS", "max_delay", 0),
        ("MAX_EMAILS_PER_DAY", "max_per_day", 1),
    ):
        raw = (request.form.get(form_name, "") or "").strip()
        if raw.isdigit() and int(raw) >= minimum:
            pacing_fields[key] = raw
    # Keep previously-saved values when a field is left blank (e.g. passwords
    # shown as "Saved locally" placeholders must not be wiped on re-save).
    required_fields = {
        key: request.form.get(form_name, "").strip() or os.getenv(key, "")
        for key, form_name in field_names.items()
    }
    optional_fields = {
        key: request.form.get(form_name, "").strip() or os.getenv(key, "")
        for key, form_name in optional_field_names.items()
    }
    # Back-compat: an older .env may only have ANTHROPIC_API_KEY.
    if not required_fields["AI_API_KEY"]:
        required_fields["AI_API_KEY"] = os.getenv("ANTHROPIC_API_KEY", "")
    required_fields["AI_KEY_PORTAL"] = KEY_PORTAL_URL
    if not all(v for k, v in required_fields.items() if k != "AI_KEY_PORTAL"):
        missing = [k for k, v in required_fields.items() if not v and k != "AI_KEY_PORTAL"]
        flash(f"Add every field before saving. Missing: {', '.join(missing)}.", "error")
        return redirect(url_for("index"))

    companies = request.files.get("companies_file")
    cv = request.files.get("cv_file")
    existing_companies = _resolve_saved_path(os.getenv("COMPANIES_FILE_PATH", ""))
    existing_cv = _resolve_saved_path(os.getenv("CV_FILE_PATH", ""))
    if companies and not companies.filename:
        companies = None
    if cv and not cv.filename:
        cv = None
    if companies and not companies.filename.lower().endswith((".csv", ".xlsx", ".xls")):
        flash("Upload a CSV or Excel companies file.", "error")
        return redirect(url_for("index"))
    if cv and not cv.filename.lower().endswith((".pdf", ".doc", ".docx")):
        flash("Upload a PDF or Word CV file.", "error")
        return redirect(url_for("index"))
    if not companies and (existing_companies is None or not existing_companies.is_file()):
        flash("Upload a CSV or Excel companies file.", "error")
        return redirect(url_for("index"))

    companies_path = existing_companies
    cv_path = existing_cv
    companies_rows = 0
    if companies:
        companies_path = uploads / f"companies{Path(companies.filename).suffix.lower()}"
        companies.save(companies_path)
    if cv:
        safe_name = secure_filename(cv.filename) or "cv.pdf"
        cv_path = uploads / f"cv_{safe_name}"
        cv.save(cv_path)
    # Validate file CONTENTS right away so a bad upload is caught here with a
    # clear message, not later as a cryptic pipeline failure.
    ok, message, companies_rows = _validate_companies_file(companies_path)
    if not ok:
        flash(f"Companies file problem: {message}", "error")
        return redirect(url_for("index"))
    if cv and (not cv_path.is_file() or cv_path.stat().st_size == 0):
        flash("CV file is empty or unreadable — please re-upload it.", "error")
        return redirect(url_for("index"))

    env_updates = {**required_fields, **optional_fields, **pacing_fields,
                    "COMPANIES_FILE_PATH": str(companies_path)}
    if cv_path:
        env_updates["CV_FILE_PATH"] = str(cv_path)
    _save_env(env_updates)
    load_dotenv(ENV_PATH, override=True)

    cv_note = f"CV attached as {cv_path.name}" if cv_path else "no CV yet — add one before sending"
    flash(f"Workspace saved — {companies_rows} usable email(s) in {companies_path.name}, "
          f"{cv_note}. You can start preparation below.", "success")
    return redirect(url_for("index"))


def _check_gmail_credentials(gmail_address: str, gmail_app_password: str) -> tuple[bool, str]:
    """Log in to Gmail (SMTP + IMAP) to prove the creds work.

    Uses the same logins as the real run: SMTP_SSL (what mailer.py sends
    through) and IMAP (what bounce_checker.py scans through). Logs out
    immediately — nothing is sent or read.
    """
    address = (gmail_address or "").strip()
    password = (gmail_app_password or "").strip()
    if not address or not password:
        return False, "Enter both the Gmail address and the app password first."
    if "@" not in address or "." not in address.split("@")[-1]:
        return False, f"'{address}' doesn't look like an email address — check for typos."
    if _is_placeholder(address) or _is_placeholder(password):
        return False, "That looks like the example placeholder — paste your real Gmail address and app password."
    password = password.replace(" ", "")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(address, password)
    except smtplib.SMTPAuthenticationError:
        return False, ("Gmail rejected the login (SMTP: username/password not accepted). "
                        "Almost always: (1) you used your normal Gmail password instead of an "
                        "App Password — create one at myaccount.google.com/apppasswords (needs 2-Step Verification); "
                        "(2) a typo or extra space; (3) 2-Step Verification is off.")
    except (smtplib.SMTPException, OSError, socket.timeout, socket.gaierror) as exc:
        return False, (f"Could not reach Gmail's send server ({exc}). "
                        "Credentials weren't tested — check your connection and retry.")
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"Unexpected error testing the send login: {exc}"

    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            conn.login(address, password)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        return False, ("Send login works, but inbox (IMAP) login failed. "
                       "Enable IMAP in Gmail: Settings > Forwarding and POP/IMAP > Enable IMAP. "
                       f"Detail: {exc}")
    except (OSError, socket.timeout, socket.gaierror) as exc:
        return False, (f"Send login works, but the inbox server was unreachable ({exc}). "
                        "Sending will work; bounce detection may not until IMAP is reachable.")
    except Exception as exc:  # pragma: no cover
        return False, f"Send login works, but inbox check hit an unexpected error: {exc}"

    return True, "Gmail credentials are valid — send login and inbox login both succeeded."


@app.post("/validate-gmail")
def validate_gmail():
    """Fallback when JS is off: test typed (or previously saved) Gmail creds."""
    load_dotenv(ENV_PATH, override=True)
    address = request.form.get("gmail_address", "").strip() or os.getenv("GMAIL_ADDRESS", "")
    password = request.form.get("gmail_app_password", "").strip() or os.getenv("GMAIL_APP_PASSWORD", "")
    ok, message = _check_gmail_credentials(address, password)
    flash(("Gmail OK — " if ok else "Gmail check failed — ") + message, "success" if ok else "error")
    return redirect(url_for("index"))


@app.post("/api/validate-gmail")
def api_validate_gmail():
    """JSON check for the inline Validate button (no page reload)."""
    load_dotenv(ENV_PATH, override=True)
    payload = request.get_json(silent=True) or {}
    address = (payload.get("gmail_address", "") or "").strip() or os.getenv("GMAIL_ADDRESS", "")
    password = (payload.get("gmail_app_password", "") or "").strip() or os.getenv("GMAIL_APP_PASSWORD", "")
    ok, message = _check_gmail_credentials(address, password)
    return jsonify({"ok": ok, "message": message})


# ---------------------------------------------------------------------------
# Google OAuth 2.0 routes
# ---------------------------------------------------------------------------

def _get_oauth_flow(redirect_uri: str = None, code_verifier: str = None):
    """Build a google_auth_oauthlib Flow from the client secret file.

    Re-resolves the client-secret path on every call (rather than trusting a
    module-import-time constant) so dropping the file in after the dashboard
    process has already started is picked up without a restart.
    """
    from google_auth_helper import _find_client_secret, SCOPES
    from google_auth_oauthlib.flow import Flow
    client_secret_path = _find_client_secret()
    if not client_secret_path:
        return None
    flow = Flow.from_client_secrets_file(
        str(client_secret_path),
        scopes=SCOPES,
        redirect_uri=redirect_uri or url_for("oauth_callback", _external=True),
        code_verifier=code_verifier,
    )
    return flow


@app.route("/oauth/start")
def oauth_start():
    """Redirect the user to Google's OAuth consent screen."""
    from google_auth_helper import oauth_is_configured
    if not oauth_is_configured():
        flash("Google OAuth credentials file not found — make sure client_secret_*.json is in the project folder.", "error")
        return redirect(url_for("index"))
    flow = _get_oauth_flow()
    if not flow:
        flash("Could not build OAuth flow — credentials file may be invalid.", "error")
        return redirect(url_for("index"))
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    # The Flow auto-generated a PKCE code_verifier when it was constructed
    # above — it must be reused (not re-generated) by the callback's Flow
    # when exchanging the code, or Google rejects the exchange because the
    # verifier no longer matches the code_challenge sent here.
    session["oauth_code_verifier"] = flow.code_verifier
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    """Google redirects here after the user consents."""
    from google_auth_helper import _save_credentials, get_oauth_email_address, save_authorized_email
    error = request.args.get("error")
    if error:
        flash(f"Google authorisation was denied: {error}", "error")
        return redirect(url_for("index"))

    # State check (CSRF protection)
    state = request.args.get("state", "")
    code_verifier = session.pop("oauth_code_verifier", None)
    if state != session.pop("oauth_state", None):
        flash("OAuth state mismatch — please try connecting again.", "error")
        return redirect(url_for("index"))

    try:
        flow = _get_oauth_flow(code_verifier=code_verifier)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        _save_credentials(creds)
        # Fetch the actual email address and persist it in token.json
        email = get_oauth_email_address()
        if email:
            save_authorized_email(email)
            # Also persist the email to .env so pipeline.py can read it
            _save_env({"GMAIL_ADDRESS": email})
            load_dotenv(ENV_PATH, override=True)
            flash(f"✓ Connected as {email} — Google OAuth is active. Your emails will be sent from this account.", "success")
        else:
            flash("✓ Google account connected! (Could not read email address — try reconnecting.)", "success")
    except Exception as exc:
        flash(f"OAuth callback failed: {exc}", "error")
    return redirect(url_for("index"))


@app.post("/oauth/disconnect")
def oauth_disconnect():
    """Delete token.json — user must re-authorise to use OAuth again."""
    from google_auth_helper import revoke_token
    revoke_token()
    flash("Google account disconnected. You can reconnect any time or use an App Password instead.", "success")
    return redirect(url_for("index"))


@app.get("/api/gmail-status")
def api_gmail_status():
    """JSON endpoint: returns current OAuth connection status."""
    try:
        from google_auth_helper import (
            token_exists, get_credentials, get_authorized_email, oauth_is_configured
        )
        configured = oauth_is_configured()
        if not configured:
            return jsonify({"mode": "none", "configured": False,
                            "message": "OAuth credentials file not found."})
        if not token_exists():
            return jsonify({"mode": "disconnected", "configured": True,
                            "message": "Not connected — click Connect with Google."})
        creds = get_credentials()
        if not creds:
            return jsonify({"mode": "expired", "configured": True,
                            "message": "Token expired and could not be refreshed. Please reconnect."})
        email = get_authorized_email() or "unknown"
        return jsonify({"mode": "connected", "configured": True, "email": email,
                        "message": f"Connected as {email}"})
    except ImportError:
        return jsonify({"mode": "none", "configured": False,
                        "message": "Google libraries not installed."})


@app.post("/api/validate-ai")
def api_validate_ai():
    """JSON check that the BAI key authenticates against the API."""
    import requests
    load_dotenv(ENV_PATH, override=True)
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("ai_api_key", "") or "").strip() or os.getenv("AI_API_KEY", "")
    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
    base_url = (payload.get("ai_base_url", "") or "").strip() or os.getenv("AI_BASE_URL", "")
    base_url = base_url or DEFAULT_BASE_URL
    model = (payload.get("ai_model", "") or "").strip() or os.getenv("AI_MODEL", DEFAULT_MODEL)
    if not api_key or _is_placeholder(api_key):
        return jsonify({"ok": False, "message": f"Paste your API key first (get one at {KEY_PORTAL_URL})."})
    try:
        response = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 5,
                  "messages": [{"role": "user", "content": "Reply with the single word: ok"}]},
            timeout=25,
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Could not reach the AI server at {base_url} ({exc})."})
    if response.status_code == 200:
        return jsonify({"ok": True, "message": f"AI key works — model '{model}' answered."})
    if response.status_code in (401, 403):
        return jsonify({"ok": False, "message": f"AI server rejected the key (401/403). Paste a fresh key from {KEY_PORTAL_URL}."})
    if response.status_code == 404:
        return jsonify({"ok": False, "message": f"Model '{model}' not found (404). Try another model from the dropdown."})
    return jsonify({"ok": False, "message": f"AI server returned {response.status_code}: {response.text[:200]}"})


@app.post("/run")
def run_pipeline():
    global run_process
    if run_process is not None and run_process.poll() is None:
        flash("A pipeline run is already in progress.", "error")
        return redirect(url_for("index"))
    setup_state = _setup_state()
    companies_path = os.getenv("COMPANIES_FILE_PATH")
    if not setup_state["prep_ready"] or not companies_path:
        flash("Add your AI key, profile, and companies file before starting preparation "
              "(Gmail and CV are only needed later, when you send).", "error")
        return redirect(url_for("index"))

    STOP_FILE.unlink(missing_ok=True)
    command = [sys.executable, str(MAIN_PATH), "--companies", companies_path]
    batch_limit = (request.form.get("batch_limit") or "").strip()
    if batch_limit.isdigit() and int(batch_limit) > 0:
        command.extend(["--limit", batch_limit])
    with RUN_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write("\n--- Dashboard preparation run ---\n")
        run_process = subprocess.Popen(command, cwd=ROOT_DIR, stdout=log_file,
                           stderr=subprocess.STDOUT,
                           env={**os.environ, "PIPELINE_STOP_FILE": str(STOP_FILE)})
    flash("Preparation engine started. Emails appear here as ready — select and send when you're happy.", "success")
    return redirect(url_for("index"))


@app.post("/stop")
def stop_pipeline():
    if run_process is None or run_process.poll() is not None:
        flash("No pipeline run is active.", "error")
    else:
        STOP_FILE.touch()
        flash("Stop requested. Completed drafts and sent addresses are preserved.", "success")
    return redirect(url_for("index"))


@app.post("/api/check-bounces")
def api_check_bounces():
    """Scan the inbox for bounce notifications now, instead of waiting for
    the next scheduled/manual `python bounce_checker.py` run."""
    load_dotenv(ENV_PATH, override=True)
    import bounce_checker
    try:
        updated = bounce_checker.check_bounces()
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Bounce check failed: {exc}"}), 500
    return jsonify({
        "ok": True,
        "updated": updated,
        "message": (f"{updated} email(s) marked as bounced." if updated
                     else "No new bounce notifications found."),
    })


@app.post("/company/<int:app_id>/edit")
def edit_email(app_id):
    application = db.get_application_by_id(app_id)
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    if not application or not subject or not body:
        flash("Subject and body are required.", "error")
        return redirect(url_for("company_detail", app_id=app_id))

    if application["status"] not in EDITABLE_STATUSES:
        flash(f"Cannot edit — email is currently '{application['status']}'.", "error")
        return redirect(url_for("company_detail", app_id=app_id))

    db.update_application(app_id, subject=subject, body=body, status="ready",
                          error_message=None)
    db.log_event(app_id, "write", "Email edited and saved from dashboard.")
    import cache_store
    cache_store.save_draft(application["email"], {"subject": subject, "body": body})
    flash("Email draft saved. The saved version will be used on the next run.", "success")
    return redirect(url_for("company_detail", app_id=app_id))


@app.route("/company/<int:app_id>")
def company_detail(app_id):
    db.init_db()
    application = db.get_application_by_id(app_id)
    if not application:
        return "Not found", 404

    events = db.get_events(app_id)
    for e in events:
        if e["detail"]:
            try:
                e["detail_parsed"] = json.loads(e["detail"])
            except json.JSONDecodeError:
                e["detail_parsed"] = None
        else:
            e["detail_parsed"] = None

    try:
        talking_points = json.loads(application["talking_points"] or "[]")
    except (TypeError, json.JSONDecodeError):
        talking_points = []
    try:
        matched_extras = json.loads(application["matched_extra_mentions"] or "[]")
    except (TypeError, json.JSONDecodeError):
        matched_extras = []
    try:
        match_reasons = json.loads(application["match_reasons"] or "{}")
    except (TypeError, json.JSONDecodeError):
        match_reasons = {}

    application["status_label"] = STATUS_LABELS.get(application["status"], application["status"])
    application["sendable"] = bool(application["status"] in db.SENDABLE_STATUSES
                                    and application.get("subject"))
    application["editable"] = application["status"] in EDITABLE_STATUSES

    load_dotenv(ENV_PATH, override=True)
    cv_path = _resolve_saved_path((os.getenv("CV_FILE_PATH", "") or "").strip())
    cv_info = None
    if cv_path and cv_path.is_file():
        cv_info = {"name": cv_path.name, "size_kb": round(cv_path.stat().st_size / 1024, 1)}

    word_count = len((application.get("body") or "").split())

    return render_template(
        "detail.html",
        application=application,
        events=events,
        talking_points=talking_points,
        matched_extras=matched_extras,
        match_reasons=match_reasons,
        prev_id=db.get_adjacent_application_id(app_id, direction="prev"),
        next_id=db.get_adjacent_application_id(app_id, direction="next"),
        from_email=(os.getenv("GMAIL_ADDRESS", "") or "").strip() or "your Gmail account",
        cv_info=cv_info,
        word_count=word_count,
    )


@app.post("/api/send-job")
def api_create_send_job():
    """Create a send job from selected application IDs. Returns immediately.

    The actual eligibility check (sendable status + has a draft) happens
    atomically inside db.create_send_job — that single UPDATE...WHERE is what
    prevents two overlapping requests (a double-click, or this row's own
    "Send" button plus "Send selected" in the same instant) from both
    queueing the same application into two jobs.
    """
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("app_ids", [])
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"ok": False, "message": "No emails selected."}), 400
    try:
        app_ids = [int(aid) for aid in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid application id."}), 400

    job_id, queued_ids = db.create_send_job(app_ids)

    if not queued_ids:
        return jsonify({"ok": False,
                        "message": "None of the selected emails are ready to send "
                                    "(already sending, or no draft)."})

    sender_worker.ensure_running()

    skipped = len(app_ids) - len(queued_ids)
    message = f"Send job created for {len(queued_ids)} email(s)."
    if skipped:
        message += f" ({skipped} already in progress or not ready were skipped.)"

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "total": len(queued_ids),
        "message": message,
    })


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


@app.get("/api/applications")
def api_applications():
    """Paginated applications list for frontend refresh."""
    status = request.args.get("status", "").strip() or None
    search = request.args.get("search", "").strip() or None
    # type=int on Werkzeug's MultiDict.get falls back to the default instead
    # of raising when the value doesn't parse (e.g. ?page=abc) — a plain
    # int() call here would 500 on any non-numeric query param.
    page = max(1, request.args.get("page", 1, type=int) or 1)
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))  # hard cap

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


@app.post("/api/update-draft")
def api_update_draft():
    """Update subject/body for an application (with edit protection)."""
    payload = request.get_json(silent=True) or {}
    app_id = payload.get("app_id")
    subject = (payload.get("subject") or "").strip()
    body = (payload.get("body") or "").strip()

    if not app_id or not subject or not body:
        return jsonify({"ok": False, "message": "app_id, subject, and body required."})
    try:
        app_id = int(app_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid app_id."}), 400

    app = db.get_application_by_id(app_id)
    if not app:
        return jsonify({"ok": False, "message": "Application not found."})

    if app["status"] not in EDITABLE_STATUSES:
        return jsonify({"ok": False,
                        "message": f"Cannot edit — status is '{app['status']}'."})

    db.update_application(app_id, subject=subject, body=body,
                          status="ready", error_message=None)
    db.log_event(app_id, "write", "Email edited from dashboard.")

    import cache_store
    cache_store.save_draft(app["email"], {"subject": subject, "body": body})

    return jsonify({"ok": True, "message": "Draft saved."})


if __name__ == "__main__":
    db.init_db()
    sender_worker.ensure_running()
    print("Dashboard running at http://127.0.0.1:5050")
    print("Background sender worker started.")
    app.run(host="127.0.0.1", port=5050, debug=False)
