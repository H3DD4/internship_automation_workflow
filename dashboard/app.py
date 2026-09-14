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

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from dotenv import dotenv_values, load_dotenv
import db
import sender_worker

app = Flask(__name__)
app.secret_key = os.getenv("DASHBOARD_SECRET", "local-dashboard-secret")

ROOT_DIR = Path(__file__).parent.parent
UPLOAD_DIR = ROOT_DIR / "dashboard_uploads"
ENV_PATH = ROOT_DIR / ".env"
MAIN_PATH = ROOT_DIR / "main.py"
RUN_LOG_PATH = ROOT_DIR / "dashboard_run.log"
STOP_FILE = ROOT_DIR / "dashboard_stop.flag"
run_process = None

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
    "retry_later": "Retry later",
    "bounced": "Bounced",
}

# Statuses that mean "a background worker is actively on this one right now" —
# used for the small live-activity indicator in the top bar.
IN_PROGRESS_STATUSES = {"researching", "researched", "writing", "sending", "queued"}


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
        "your_bai_api_key_here", "your_anthropic_api_key_here",
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

    return {
        "ready": has_api_key and has_gmail and has_profile and has_cv and has_companies,
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
        "ai_model": os.getenv("AI_MODEL", "hy3"),
        "ai_base_url": os.getenv("AI_BASE_URL", "https://api.b.ai/v1"),
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


def _save_env(values):
    existing = dict(dotenv_values(ENV_PATH)) if ENV_PATH.exists() else {}
    existing.update(values)
    with ENV_PATH.open("w", encoding="utf-8") as env_file:
        for key, value in existing.items():
            if value is not None:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                env_file.write(f'{key}="{escaped}"\n')


@app.route("/")
def index():
    db.init_db()
    sender_worker.ensure_running()
    load_dotenv(ENV_PATH, override=True)
    stats = db.get_stats()

    page = max(1, request.args.get("page", 1, type=int))
    table_limit = 50
    status_param = request.args.get("status", "").strip() or None
    search_param = request.args.get("q", "").strip() or None
    applications, table_total = db.get_applications_paginated(
        status=status_param, search=search_param, page=page, limit=table_limit
    )
    table_pages = max(1, (table_total + table_limit - 1) // table_limit)
    pending_prep = db.count_needing_preparation()

    for a in applications:
        a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])
        try:
            a["matched_extra_mentions_list"] = json.loads(a["matched_extra_mentions"] or "[]")
        except (TypeError, json.JSONDecodeError):
            a["matched_extra_mentions_list"] = []
        # Short inline error/retry reason for the list view, so a failed or
        # retry_later row is understandable at a glance without a click-through.
        msg = a.get("error_message") or ""
        a["error_short"] = (msg[:80] + "…") if len(msg) > 80 else msg

    ordered_stats = [
        ("total", "Total companies"),
        ("sent", "Sent"),
        ("ready", "Ready to send"),
        ("queued", "Queued"),
        ("failed", "Failed"),
        ("bounced", "Bounced"),
        ("retry_wait", "Retry later"),
        ("pending", "Pending"),
        ("researching", "Researching"),
        ("researched", "Researched"),
        ("writing", "Writing"),
    ]
    stat_cards = [(label, stats.get(key, 0)) for key, label in ordered_stats if key in stats or key == "total"]

    in_progress_count = sum(stats.get(s, 0) for s in IN_PROGRESS_STATUSES)
    completed_count = sum(stats.get(s, 0) for s in
                          ("sent", "failed", "bounced", "retry_wait", "ready"))
    sent_today = db.count_sent_today()
    max_per_day = int(os.getenv("MAX_EMAILS_PER_DAY", 20))

    active_jobs = db.get_pending_send_jobs()
    active_job = active_jobs[0] if active_jobs else None
    active_job_detail = db.get_send_job(active_job["id"]) if active_job else None

    return render_template(
        "index.html",
        applications=applications,
        stat_cards=stat_cards,
        sent_today=sent_today,
        max_per_day=max_per_day,
        in_progress_count=in_progress_count,
        completed_count=completed_count,
        total_count=stats.get("total", 0),
        setup=_setup_state(),
        run_state=_run_state(),
        active_job=active_job_detail,
        table_page=page,
        table_pages=table_pages,
        table_total=table_total,
        table_limit=table_limit,
        filter_status=status_param or "",
        filter_q=search_param or "",
        pending_prep=pending_prep,
    )


@app.post("/setup")
def setup():
    uploads = UPLOAD_DIR
    uploads.mkdir(exist_ok=True)
    load_dotenv(ENV_PATH, override=True)
    from werkzeug.utils import secure_filename
    field_names = {
        "AI_API_KEY": "ai_api_key",
        "AI_BASE_URL": "ai_base_url",
        "AI_MODEL": "ai_model",
        "GMAIL_ADDRESS": "gmail_address",
        "GMAIL_APP_PASSWORD": "gmail_app_password",
        "YOUR_NAME": "your_name",
        "YOUR_TARGET_ROLE": "target_role",
    }
    # Keep previously-saved values when a field is left blank (e.g. passwords
    # shown as "Saved locally" placeholders must not be wiped on re-save).
    required_fields = {
        key: request.form.get(form_name, "").strip() or os.getenv(key, "")
        for key, form_name in field_names.items()
    }
    # Back-compat: an older .env may only have ANTHROPIC_API_KEY.
    if not required_fields["AI_API_KEY"]:
        required_fields["AI_API_KEY"] = os.getenv("ANTHROPIC_API_KEY", "")
    required_fields["AI_KEY_PORTAL"] = "https://chat.b.ai/key"
    if not all(required_fields.values()):
        missing = [k for k, v in required_fields.items() if not v and k != "AI_KEY_PORTAL"]
        flash(f"Add every credential and profile field before saving. Missing: {', '.join(missing)}.", "error")
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
    if not cv and (existing_cv is None or not existing_cv.is_file()):
        flash("Upload your CV so it can be attached to applications.", "error")
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
    if not cv_path or not cv_path.is_file() or cv_path.stat().st_size == 0:
        flash("CV file is empty or unreadable — please re-upload it.", "error")
        return redirect(url_for("index"))
    _save_env({**required_fields,
               "CV_FILE_PATH": str(cv_path),
               "COMPANIES_FILE_PATH": str(companies_path)})
    load_dotenv(ENV_PATH, override=True)
    flash(f"Workspace saved — {companies_rows} usable email(s) in {companies_path.name}, "
          f"CV attached as {cv_path.name}. You can start with a dry run below.", "success")
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

def _get_oauth_flow(redirect_uri: str = None):
    """Build a google_auth_oauthlib Flow from the client secret file."""
    from google_auth_helper import CLIENT_SECRET_PATH, SCOPES
    from google_auth_oauthlib.flow import Flow
    if not CLIENT_SECRET_PATH:
        return None
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri or url_for("oauth_callback", _external=True),
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
    if state != session.pop("oauth_state", None):
        flash("OAuth state mismatch — please try connecting again.", "error")
        return redirect(url_for("index"))

    try:
        flow = _get_oauth_flow()
        # oauthlib strict HTTPS check — allow HTTP for localhost only
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        flow.fetch_token(authorization_response=request.url.replace("http://", "http://"))
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


@app.route("/oauth/disconnect")
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
    base_url = base_url or "https://api.b.ai/v1"
    model = (payload.get("ai_model", "") or "").strip() or os.getenv("AI_MODEL", "hy3")
    if not api_key or _is_placeholder(api_key):
        return jsonify({"ok": False, "message": "Paste your BAI API key first (get one at https://chat.b.ai/key)."})
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
        return jsonify({"ok": False, "message": "AI server rejected the key (401/403). Paste a fresh key from https://chat.b.ai/key."})
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
    if not setup_state["ready"] or not companies_path:
        flash("Finish the setup form before starting the pipeline.", "error")
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


@app.post("/company/<int:app_id>/edit")
def edit_email(app_id):
    application = db.get_application_by_id(app_id)
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    if not application or not subject or not body:
        flash("Subject and body are required.", "error")
        return redirect(url_for("company_detail", app_id=app_id))

    EDITABLE_STATUSES = {"ready", "failed", "retry_wait", "retry_later"}
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
    applications = db.get_all_applications()
    application = next((a for a in applications if a["id"] == app_id), None)
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

    return render_template(
        "detail.html",
        application=application,
        events=events,
        talking_points=talking_points,
        matched_extras=matched_extras,
        match_reasons=match_reasons,
    )


@app.post("/api/send-job")
def api_create_send_job():
    """Create a send job from selected application IDs. Returns immediately."""
    payload = request.get_json(silent=True) or {}
    app_ids = payload.get("app_ids", [])

    if not app_ids:
        return jsonify({"ok": False, "message": "No emails selected."})

    # Validate all IDs exist and are in a sendable status
    sendable_statuses = {"ready", "failed", "retry_wait", "retry_later"}
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

    EDITABLE_STATUSES = {"ready", "failed", "retry_wait", "retry_later"}
    if app["status"] not in EDITABLE_STATUSES:
        return jsonify({"ok": False,
                        "message": f"Cannot edit — status is '{app['status']}'."})

    db.update_application(int(app_id), subject=subject, body=body,
                          status="ready", error_message=None)
    db.log_event(int(app_id), "write", "Email edited from dashboard.")

    import cache_store
    cache_store.save_draft(app["email"], {"subject": subject, "body": body})

    return jsonify({"ok": True, "message": "Draft saved."})


if __name__ == "__main__":
    db.init_db()
    sender_worker.ensure_running()
    print("Dashboard running at http://127.0.0.1:5050")
    print("Background sender worker started.")
    app.run(host="127.0.0.1", port=5050, debug=False)
