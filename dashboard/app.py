"""
The dashboard — a multi-user web app. Run locally with:

    python dashboard/app.py          # http://127.0.0.1:5050

In production it runs under gunicorn (see Dockerfile) with the background
worker as its own process (python worker.py).

Every view works on the signed-in user's data only: db.for_user(g.user) is
the only way this module reads or writes applications, so another account's
row is indistinguishable from one that doesn't exist.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

import accounts
import cache_store
import company_import
import config
import database
import db
import drafting
import email_templates
import profiles
import runs
import vault
from ai_client import (DEFAULT_PROVIDER, PROVIDERS, CompatibleAIClient, list_provider_models,
                       resolve_ai_settings)
from dashboard import security

ROOT_DIR = Path(__file__).parent.parent

vault.ensure_dev_keys()
if not config.is_production() or config.local_http_base():
    # Google's OAuth library insists on https; a local run is http on loopback.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Google may return a slightly different scope list than requested; which
# scopes actually came back is checked explicitly in the callback.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

app = Flask(__name__)
app.secret_key = config.get("SECRET_KEY")
app.config.update(
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 365,
    MAX_CONTENT_LENGTH=max(config.MAX_CV_BYTES, config.MAX_COMPANIES_UPLOAD_BYTES) + 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.served_over_https(),
    SESSION_COOKIE_NAME="__Host-flask" if config.served_over_https() else "flask",
)
if config.bool_setting("TRUST_PROXY", config.is_production()):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

from dashboard.admin_routes import bp as admin_bp  # noqa: E402
from dashboard.auth_routes import bp as auth_bp  # noqa: E402
from dashboard.profile_routes import bp as profile_bp  # noqa: E402
from dashboard.microsoft_routes import bp as microsoft_bp  # noqa: E402

app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(microsoft_bp)

STATUS_LABELS = {
    "pending": "Pending", "researching": "Researching", "researched": "Researched",
    "writing": "Writing email", "ready": "Ready to send", "queued": "Queued",
    "sending": "Sending", "sent": "Sent", "failed": "Failed", "retry_wait": "Retry later",
    "bounced": "Not delivered", "skipped": "Skipped",
}
IN_PROGRESS_STATUSES = {"researching", "writing", "sending", "queued"}
EDITABLE_STATUSES = frozenset({"ready", "failed", "retry_wait"})
STATUS_TABS = [("", "All"), ("favorites", "★ Favorites"), ("to_prepare", "To prepare"),
               ("ready", "Ready"), ("sent", "Sent"), ("problems", "Problems"), ("skipped", "Skipped"),
               ("no_match", "No CV match")]
FUNNEL_CARDS = [("to_prepare", "To prepare", "to_prepare"), ("ready", "Ready to review", "ready"),
                ("sending", "Sending", "sending"), ("sent", "Sent", "sent"),
                ("problems", "Problems", "problems")]
FILTER_GROUPS = db.STATUS_GROUPS
LANGUAGE_LABELS = {"en": "English", "fr": "Français"}

_started = False


@app.context_processor
def _microsoft_flag():
    import microsoft_auth
    return {"microsoft_enabled": microsoft_auth.is_configured}


@app.before_request
def _before():
    global _started
    if not _started:
        database.init_schema()
        admin = config.admin_credentials()
        if admin:
            accounts.ensure_admin(*admin)
        if config.embedded_worker() and not app.config.get("TESTING"):
            import worker
            worker.ensure_embedded()
        _started = True
    return security.before_request()


@app.after_request
def _after(response):
    return security.after_request(response)


def data() -> db.UserData:
    return db.for_user(g.user["id"])


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

@app.context_processor
def _inject_globals():
    def asset(filename: str) -> str:
        try:
            version = int((Path(app.static_folder) / filename).stat().st_mtime)
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    labels = {}
    if g.get("user"):
        if "_specs" not in g:
            g._specs = profiles.specs_for(g.user["id"])
        _, spec_en, spec_fr = g._specs
        for spec in (spec_fr, spec_en):
            for area in (spec or {}).get("areas", []):
                if spec is spec_en or area["id"] not in labels:
                    labels[area["id"]] = area["label"]
    return {"asset": asset, "area_labels": labels, "csrf_token": security.csrf_token,
            "csp_nonce": g.get("csp_nonce", ""), "current_user": g.get("user"),
            "language_labels": LANGUAGE_LABELS,
            "pending_approvals": _pending_approvals()}


def _pending_approvals() -> int:
    if not g.get("user") or g.user["role"] != "admin":
        return 0
    try:
        return sum(1 for u in accounts.list_users() if u["status"] == "pending")
    except Exception:
        return 0


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(429)
def _error(exc):
    code = getattr(exc, "code", 500)
    messages = {400: "That request couldn't be understood.", 403: "You can't do that.",
                404: "Not found.", 413: "That upload is too large.", 429: "Too many requests."}
    message = getattr(exc, "description", None) if code == 403 else None
    if security.is_api_request():
        return jsonify({"ok": False, "message": message or messages.get(code, "Error")}), code
    return render_template("error.html", code=code, message=message or messages.get(code, "Error")), code


@app.get("/healthz")
def health():
    try:
        db.ping()
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False}), 503


# ---------------------------------------------------------------------------
# Setup state (what's configured, what's missing)
# ---------------------------------------------------------------------------

# The Google sign-in check can refresh an expired token over the network, and
# a Testing-mode token past its 7 days fails that refresh every time. Cached
# per user on the stored token's version plus a short TTL: a sign-in, a
# refresh or a disconnect changes the version and shows on the very next load.
_OAUTH_STATUS_TTL = 120
_oauth_cache: dict = {}


def _oauth_status(cfg) -> dict:
    import time
    import google_auth_helper
    version = cfg.secret_version("GOOGLE_TOKEN")
    if not version:
        _oauth_cache.pop(cfg.user_id, None)
        return {"connected": False, "valid": False, "email": "", "can_read": False}
    hit = _oauth_cache.get(cfg.user_id)
    now = time.monotonic()
    if hit and hit[0] == version and now - hit[1] < _OAUTH_STATUS_TTL:
        return hit[2]
    valid = google_auth_helper.get_credentials(cfg) is not None
    value = {"connected": True, "valid": valid, "email": google_auth_helper.get_authorized_email(cfg) or "",
             "can_read": google_auth_helper.can_read_inbox(cfg)}
    # A refresh inside get_credentials() rewrites the token: key on the
    # version as it stands AFTER the check.
    _oauth_cache[cfg.user_id] = (cfg.secret_version("GOOGLE_TOKEN"), now, value)
    return value


def _setup_state() -> dict:
    import google_auth_helper
    import mail_service
    cfg = g.cfg
    ai = resolve_ai_settings(cfg.ai_env())
    saved = cfg.saved_secret_names()
    profile, spec_en, spec_fr = profiles.specs_for(g.user["id"])
    has_profile = bool(spec_en) and not email_templates.spec_problems(spec_en)
    companies_rows = data().company_list_count()
    ntern_rows = db.catalog_count()
    cv = cfg.cv_info()
    method = mail_service.sending_method(cfg)
    oauth = _oauth_status(cfg)
    oauth_connected, oauth_email, oauth_valid = oauth["connected"], oauth["email"], oauth["valid"]
    import microsoft_auth
    ms_connected = microsoft_auth.token_exists(cfg)
    has_gmail = bool(method) and (method != "oauth" or oauth_valid) and (method != "microsoft" or ms_connected)
    has_api_key = bool(ai["api_key"]) or any(p["key_env"] in saved for p in PROVIDERS.values())
    prep_ready = has_api_key and has_profile and (companies_rows > 0 or ntern_rows > 0)
    return {
        "prep_ready": prep_ready,
        "send_ready": prep_ready and has_gmail and bool(cv),
        "has_api_key": has_api_key,
        "has_profile": has_profile,
        "has_companies": companies_rows > 0 or ntern_rows > 0,
        "has_own_companies": companies_rows > 0,
        "ntern_rows": ntern_rows,
        "companies_rows": companies_rows,
        "has_gmail": has_gmail,
        "mail_method": method,
        "mail_label": mail_service.PROVIDER_LABELS.get(method, ""),
        "from_address": mail_service.from_address(cfg, method) if method else "",
        "has_gmail_oauth": oauth_valid,
        "oauth_connected": oauth_connected,
        "oauth_expired": oauth_connected and not oauth_valid,
        "oauth_email": oauth_email,
        "oauth_configured": google_auth_helper.oauth_is_configured(),
        "oauth_can_read_inbox": oauth["can_read"],
        "ms_configured": microsoft_auth.is_configured(),
        "ms_connected": ms_connected,
        "ms_email": microsoft_auth.sender_address(cfg) if ms_connected else "",
        "ms_can_read_inbox": ms_connected and microsoft_auth.can_read_inbox(cfg),
        "has_mail_password": "GMAIL_APP_PASSWORD" in saved,
        "gmail_address": cfg.get("GMAIL_ADDRESS"),
        "smtp_host": cfg.get("SMTP_HOST"), "smtp_port": cfg.get("SMTP_PORT"),
        "smtp_security": cfg.get("SMTP_SECURITY", "ssl"), "smtp_username": cfg.get("SMTP_USERNAME"),
        "imap_host": cfg.get("IMAP_HOST"),
        "cv_name": cv["name"] if cv else "", "cv_size_kb": cv["size_kb"] if cv else None,
        "min_delay": cfg.get("MIN_DELAY_SECONDS", "45"), "max_delay": cfg.get("MAX_DELAY_SECONDS", "120"),
        "max_per_day": str(cfg.int_setting("MAX_EMAILS_PER_DAY", 20)),
        "bounce_minutes": cfg.get("BOUNCE_CHECK_MINUTES", "30"),
        "research_workers": str(cfg.int_setting("RESEARCH_WORKERS", 3)),
        "writer_workers": str(cfg.int_setting("WRITER_WORKERS", 2)),
        "ai_max_rpm": cfg.get("AI_MAX_RPM", "800"),
        "ai_fallbacks": ", ".join(ai["fallbacks"]),
        "ai_translation_model": cfg.get("AI_TRANSLATION_MODEL"),
        "ai_provider": ai["provider"], "ai_provider_label": ai["label"],
        "ai_model": ai["model"], "ai_base_url": ai["base_url"],
        "saved_provider_keys": {pid: p["key_env"] in saved for pid, p in PROVIDERS.items()},
        "profile_mode": profile.get("mode") or "template",
        "template_id": profile.get("template_id") or email_templates.DEFAULT_TEMPLATE,
        "template_name": (email_templates.TEMPLATES.get(profile.get("template_id") or "", {})
                          .get("name", {}).get("en") or "Your own wording"),
        "language_mode": profile.get("language_mode") or "auto",
        "languages": [lang for lang, spec in (("en", spec_en), ("fr", spec_fr)) if spec],
        "name": cfg.get("YOUR_NAME"), "target_role": cfg.get("YOUR_TARGET_ROLE"),
        "user_max_per_day": None if g.user["role"] == "admin" else config.USER_MAX_EMAILS_PER_DAY,
        "user_max_workers": None if g.user["role"] == "admin" else config.USER_MAX_RESEARCH_WORKERS,
    }


def _run_state() -> dict:
    run = runs.latest(g.user["id"])
    running = bool(run and run["status"] in runs.ACTIVE)
    tail = "\n".join(((run or {}).get("log") or "").splitlines()[-60:])
    return {"running": running, "stop_requested": bool(run and run["stop_requested"] and running),
            "status": (run or {}).get("status"), "log_tail": tail,
            "queued": bool(run and run["status"] == "requested")}


def _bounce_check_state() -> dict:
    import bounce_checker
    last = bounce_checker.last_check(g.user["id"]) or {}
    interval = g.cfg.int_setting("BOUNCE_CHECK_MINUTES", 30)
    return {"last_at": last.get("at"), "last_error": last.get("error"),
            "last_trigger": last.get("trigger"), "auto_minutes": interval if interval > 0 else 0,
            "available": __import__("bounce_checker").credentials_available(g.cfg)}


def _run_sources() -> list:
    """What a run can scan: the shared Ntern list, then the user's own lists.
    Ticked by default: the user's own lists — or the Ntern list when they
    have none yet."""
    own = data().company_sources()
    choices = [{"value": s["name"], "label": s["label"], "count": s["count"], "checked": True,
                "ntern": False} for s in own]
    ntern = db.catalog_count()
    if ntern:
        choices.insert(0, {"value": db.NTERN_SOURCE, "label": "Ntern list", "count": ntern,
                           "checked": not own, "ntern": True})
    return choices


def _decorate_rows(applications: list) -> list:
    for a in applications:
        a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])
        a["source_label"] = db.source_label(a.get("source"))
        a["source_is_ntern"] = a.get("source") == db.NTERN_SOURCE
        try:
            a["matched_extra_mentions_list"] = json.loads(a["matched_extra_mentions"] or "[]")
        except (TypeError, json.JSONDecodeError):
            a["matched_extra_mentions_list"] = []
        msg = a.get("error_message") or ""
        if a["status"] == "bounced":
            msg = msg.removeprefix("Not delivered — ")
        a["error_short"] = (msg[:80] + "…") if len(msg) > 80 else msg
        a["sendable"] = bool(a["status"] in db.SENDABLE_STATUSES and a.get("subject"))
        a["rescannable"] = a["status"] in db.RESCANNABLE_STATUSES
        a["not_checked"] = a.get("hook_status") in db.NOT_CHECKED_HOOK_STATUSES
        a["no_cv_match"] = bool(a["status"] in db.REVIEW_STATUSES and a.get("hook_status")
                                and not a["not_checked"] and not a["matched_extra_mentions_list"])
        a["favorite"] = bool(a.get("favorite"))
        a["updated_short"] = (a["updated_at"][5:16].replace("T", " ") if a.get("updated_at") else "—")
        a["language"] = a.get("language") or ("en" if a.get("subject") else "")
    return applications


def _table_context(page: int, status_param: str, search_param: str, limit: int = 50) -> dict:
    d = data()
    favorite_only = status_param == "favorites"
    no_match_tab = status_param == "no_match"
    statuses = FILTER_GROUPS.get(status_param) if status_param and not favorite_only else None
    # Companies whose site matched nothing on the CV live in their own tab.
    # Favorites and searches still find them: those are deliberate choices.
    cv_match = "none" if no_match_tab else ("all" if favorite_only or search_param else "match")
    applications, table_total = d.get_applications_paginated(
        status=status_param if not statuses and not favorite_only and not no_match_tab else None,
        statuses=list(statuses) if statuses else None,
        search=search_param, page=page, limit=limit, favorite_only=favorite_only, cv_match=cv_match)
    grouped = d.get_grouped_stats(hide_no_match=True)
    favorites = d.count_favorites()
    grouped["favorites"] = favorites["total"]
    sent_today = d.count_sent_today()
    max_per_day = g.cfg.int_setting("MAX_EMAILS_PER_DAY", 20)
    return {
        "applications": _decorate_rows(applications),
        "table_total": table_total, "table_page": page,
        "table_pages": max(1, (table_total + limit - 1) // limit), "table_limit": limit,
        "funnel": [{"key": key, "label": label, "filter": fv, "value": grouped.get(key, 0)}
                   for key, label, fv in FUNNEL_CARDS],
        "grouped": grouped,
        "bounced_count": grouped["by_status"].get("bounced", 0),
        "failed_count": grouped["by_status"].get("failed", 0),
        "bounce_check": _bounce_check_state(),
        "total_count": grouped.get("total", 0),
        "skipped_count": grouped.get("skipped", 0),
        "sent_today": sent_today, "max_per_day": max_per_day,
        "cap_remaining": max(0, max_per_day - sent_today),
        "in_progress_count": sum(grouped["by_status"].get(s, 0) for s in IN_PROGRESS_STATUSES),
        "pending_prep": d.count_needing_preparation(),
        "favorites_total": favorites["total"], "favorites_sendable": favorites["sendable"],
        "no_match_count": grouped.get("no_match", 0), "hiding_no_match": cv_match == "match",
        "filter_status": status_param or "", "filter_q": search_param or "",
    }


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------

LEGAL_UPDATED = "2 October 2026"


def _legal(page: str):
    # CONTACT_EMAIL in .env: the address shown for privacy questions.
    return render_template("legal.html", page=page, updated=LEGAL_UPDATED, year=date.today().year,
                           contact=config.get("CONTACT_EMAIL"))


@app.get("/privacy")
def privacy():
    return _legal("privacy")


@app.get("/terms")
def terms():
    return _legal("terms")


@app.route("/")
def index():
    if g.user is None:
        import google_auth_helper
        import email_templates
        # The styles, their benchmarks and sources come from the same guide
        # the Profile page shows, so the landing page can't promise more.
        return render_template("landing.html", google=google_auth_helper.oauth_is_configured(),
                               year=date.today().year, styles=email_templates.template_choices("en"),
                               rules=email_templates.proven_rules("en"))
    page =max(1, request.args.get("page", 1, type=int))
    status_param = request.args.get("status", "").strip()[:40]
    search_param = request.args.get("q", "").strip()[:120]
    context = _table_context(page, status_param, search_param)
    active_jobs = data().get_pending_send_jobs()
    return render_template(
        "index.html", setup=_setup_state(), run_state=_run_state(), run_sources=_run_sources(),
        active_job=data().get_send_job(active_jobs[0]["id"]) if active_jobs else None,
        status_tabs=STATUS_TABS, **context)


@app.get("/api/overview")
def api_overview():
    page = max(1, request.args.get("page", 1, type=int))
    status_param = request.args.get("status", "").strip()[:40]
    search_param = request.args.get("q", "").strip()[:120]
    context = _table_context(page, status_param, search_param)
    run_state = _run_state()
    return jsonify({
        "ok": True,
        "rows_html": render_template("_rows.html", applications=context["applications"]),
        "funnel": context["funnel"],
        "grouped": {k: v for k, v in context["grouped"].items() if k != "by_status"},
        **{key: context[key] for key in (
            "table_total", "table_page", "table_pages", "total_count", "pending_prep",
            "in_progress_count", "sent_today", "max_per_day", "cap_remaining", "bounced_count",
            "failed_count", "bounce_check", "favorites_total", "favorites_sendable",
            "no_match_count")},
        "running": run_state["running"], "stop_requested": run_state["stop_requested"],
        "log_tail": run_state["log_tail"],
    })


@app.post("/api/skip/<int:app_id>")
def api_skip(app_id):
    payload = request.get_json(silent=True) or {}
    unskip = bool(payload.get("unskip"))
    d = data()
    application = d.get_application_by_id(app_id)
    if not application:
        return jsonify({"ok": False, "message": "Application not found."}), 404
    if unskip:
        if application["status"] != "skipped":
            return jsonify({"ok": False, "message": "That company isn't skipped."})
        new_status = "ready" if (application.get("subject") and application.get("body")) else "pending"
        d.update_application(app_id, status=new_status, error_message=None)
        d.log_event(app_id, "skip", "Un-skipped from dashboard.")
        return jsonify({"ok": True, "status": new_status, "message": "Company un-skipped."})
    if application["status"] in db.SEND_IN_FLIGHT_STATUSES or application["status"] == "sent":
        return jsonify({"ok": False, "message": f"Can't skip — already {application['status']}."})
    d.update_application(app_id, status="skipped", error_message=None)
    d.log_event(app_id, "skip", "Skipped from dashboard.")
    return jsonify({"ok": True, "status": "skipped", "message": "Company skipped."})


def _int_ids(raw) -> list | None:
    if not isinstance(raw, list) or not raw or len(raw) > 5000:
        return None
    try:
        return [int(i) for i in raw]
    except (TypeError, ValueError):
        return None


@app.post("/api/favorite")
def api_favorite():
    payload = request.get_json(silent=True) or {}
    app_ids = _int_ids(payload.get("app_ids"))
    if app_ids is None:
        return jsonify({"ok": False, "message": "No valid companies given."}), 400
    favorite = bool(payload.get("favorite", True))
    changed = data().set_favorite(app_ids, favorite)
    counts = data().count_favorites()
    return jsonify({"ok": True, "favorite": favorite, "changed": changed,
                    "favorites_total": counts["total"], "favorites_sendable": counts["sendable"]})


@app.get("/api/favorites/sendable")
def api_sendable_favorites():
    return jsonify({"ok": True, "recipients": [
        {"id": r["id"], "company": r["company_name"], "email": r["email"]}
        for r in data().get_sendable_favorites()]})


def _rebuild(app_id: int, lang: str | None, style: str | None = None):
    import pipeline as pipeline_module
    from agents.draft_guard import GuardRejection
    d = data()
    application = d.get_application_by_id(app_id)
    if not application:
        return jsonify({"ok": False, "message": "Application not found."}), 404
    if application["status"] in db.SEND_IN_FLIGHT_STATUSES or application["status"] in ("sent", "bounced"):
        return jsonify({"ok": False, "message": f"Can't change it — already {application['status']}."})
    try:
        dcfg = drafting.load_config(g.user["id"], g.cfg)
    except drafting.NotReady as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if lang and lang not in dcfg.available_languages:
        return jsonify({"ok": False, "message": f"No {LANGUAGE_LABELS.get(lang, lang)} wording yet — "
                                                "add it on your Profile page."}), 400
    context = pipeline_module._load_research(g.user["id"], application["email"], application) or {}
    # Rebuilding keeps the language and style the draft is in unless a switch was asked.
    try:
        draft = drafting.compose_for(dcfg, application, context, lang=lang or application.get("language"),
                                     style=style)
    except drafting.NotReady as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except GuardRejection as exc:
        return jsonify({"ok": False, "message": f"Your wording produced an invalid email: {exc}"}), 400
    cache_store.save_draft(g.user["id"], application["email"], draft)
    d.update_application(app_id, status="ready", subject=draft["subject"], body=draft["body"],
                         language=draft["language"], template_id=draft.get("template_id"),
                         error_message=None)
    d.log_event(app_id, "write", f"Draft rebuilt ({draft['language'].upper()}): \"{draft['subject']}\"",
                detail=draft)
    return jsonify({"ok": True, "subject": draft["subject"], "body": draft["body"],
                    "language": draft["language"], "message": "Draft rebuilt."})


@app.post("/api/regenerate/<int:app_id>")
def api_regenerate(app_id):
    """Rebuild one draft from its saved research and the current profile —
    instant, no AI."""
    return _rebuild(app_id, None)


@app.post("/api/style/<int:app_id>")
def api_style(app_id):
    """The style switch: rewrite this company's draft in another email style,
    from the same research and the same approved facts — instant, no AI."""
    style = str((request.get_json(silent=True) or {}).get("style") or "")
    try:
        dcfg = drafting.load_config(g.user["id"], g.cfg)
    except drafting.NotReady as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if style not in {c["id"] for c in drafting.style_choices(dcfg)}:
        return jsonify({"ok": False, "message": "That style isn't available for your profile."}), 400
    return _rebuild(app_id, None, style)


@app.post("/api/language/<int:app_id>")
def api_language(app_id):
    """The EN/FR switch: rewrite this company's draft in the other language."""
    lang = ((request.get_json(silent=True) or {}).get("language") or "").lower()
    if lang not in LANGUAGE_LABELS:
        return jsonify({"ok": False, "message": "Unknown language."}), 400
    return _rebuild(app_id, lang)


@app.post("/run")
def run_pipeline():
    setup_state = _setup_state()
    if not setup_state["prep_ready"]:
        flash("Add your AI key, your profile and a companies list before starting preparation "
              "(the email account and CV are only needed later, to send).", "error")
        return redirect(url_for("index"))
    raw = (request.form.get("batch_limit") or "").strip()
    limit = int(raw) if raw.isdigit() and int(raw) > 0 else None
    sources = None
    if request.form.get("sources_shown"):
        sources = [s for s in request.form.getlist("source") if s == db.NTERN_SOURCE or s == db.clean_source_name(s)]
        if not sources:
            flash("Tick at least one list to scan.", "error")
            return redirect(url_for("index"))
    try:
        runs.request_run(g.user["id"], limit, sources=sources)
    except runs.RunConflict as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    if config.embedded_worker():
        import worker
        worker.ensure_embedded()
    flash("Preparation started. Emails appear here as they're ready — review, then send.", "success")
    return redirect(url_for("index"))


@app.post("/api/rescan")
def api_rescan():
    """Research and draft these companies again from scratch — after an API
    error, a fixed key, or to check a match again. Starts a run for just
    them when none is running."""
    payload = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in (payload.get("app_ids") or [])][:1000]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid selection."}), 400
    if not ids:
        return jsonify({"ok": False, "message": "Select at least one company."}), 400
    if not _setup_state()["prep_ready"]:
        return jsonify({"ok": False, "message": "Add your AI key and profile in Settings first."}), 400
    reset = data().reset_for_rescan(ids)
    if not reset:
        return jsonify({"ok": False, "message": "Nothing to re-scan: sent, queued and skipped "
                                                "companies are left as they are."}), 400
    note = f" ({len(ids) - len(reset)} sent, queued or skipped left as they are.)" if len(reset) < len(ids) else ""
    try:
        runs.request_run(g.user["id"], targets=reset)
    except runs.RunConflict:
        return jsonify({"ok": True, "started": False, "reset": len(reset),
                        "message": f"{len(reset)} reset. A preparation run is already going — they'll be "
                                   f"done by the next one.{note}"})
    if config.embedded_worker():
        import worker
        worker.ensure_embedded()
    return jsonify({"ok": True, "started": True, "reset": len(reset),
                    "message": f"Re-scanning {len(reset)} compan{'y' if len(reset) == 1 else 'ies'}.{note}"})


@app.post("/stop")
def stop_pipeline():
    if runs.request_stop(g.user["id"]):
        flash("Stop requested. Finished drafts and sent history are kept.", "success")
    else:
        flash("No preparation run is active.", "error")
    return redirect(url_for("index"))


@app.post("/api/check-bounces")
def api_check_bounces():
    import bounce_checker
    import sender_worker
    if security.rate_limited(f"bounce:{g.user['id']}", 6, 600):
        return jsonify({"ok": False, "message": "Checked very recently — try again in a few minutes."}), 429
    result = bounce_checker.run_check(g.cfg, sender_worker.MANUAL_BOUNCE_WINDOW_DAYS, trigger="manual")
    if result["error"]:
        return jsonify({"ok": False, "message": f"Bounce check failed: {result['error']}",
                        "bounce_check": _bounce_check_state()}), 500
    updated = result["updated"]
    return jsonify({"ok": True, "updated": updated,
                    "message": (f"{updated} email(s) were not delivered — marked in red." if updated
                                else "No new delivery failures found."),
                    "bounce_check": _bounce_check_state()})


@app.post("/company/<int:app_id>/edit")
def edit_email(app_id):
    d = data()
    application = d.get_application_by_id(app_id)
    if not application:
        abort(404)
    subject = request.form.get("subject", "").strip()[:500]
    body = request.form.get("body", "").strip()[:20000]
    if not subject or not body:
        flash("Subject and body are required.", "error")
        return redirect(url_for("company_detail", app_id=app_id))
    if application["status"] not in EDITABLE_STATUSES:
        flash(f"Cannot edit — email is currently '{application['status']}'.", "error")
        return redirect(url_for("company_detail", app_id=app_id))
    d.update_application(app_id, subject=subject, body=body, status="ready", error_message=None)
    d.log_event(app_id, "write", "Email edited and saved from dashboard.")
    cache_store.save_draft(g.user["id"], application["email"],
                           {"subject": subject, "body": body, "language": application.get("language") or "en"})
    flash("Email draft saved.", "success")
    return redirect(url_for("company_detail", app_id=app_id))


_ERROR_EXPLANATIONS = [
    ("charmap", "Couldn't print a character in the company name to the console."),
    ("codec can't encode", "Couldn't print a character in the company name to the console."),
    ("invalid api_key", "The AI provider rejected the API key."),
    (" 401", "The AI provider rejected the API key."),
    (" 403", "The AI provider refused the request (403)."),
    (" 429", "The AI provider's rate limit was hit."),
    ("rate limit", "The AI provider's rate limit was hit."),
    ("invalid json", "The AI returned an answer that wasn't usable."),
    ("empty message content", "The AI returned an empty answer."),
    ("empty response body", "The AI returned an empty answer."),
    ("cannot schedule new futures", "The run was stopped before this step could start."),
    ("unreachable", "Couldn't reach the AI provider."),
]
_FAILURE_MESSAGES = {"Writer agent failed", "Research stage crashed"}


def _event_error_summary(event: dict) -> str:
    detail = event.get("detail_parsed")
    error = detail.get("error") if isinstance(detail, dict) else None
    if not error and event.get("message") not in _FAILURE_MESSAGES:
        return ""
    text = str(error or "").lower()
    for needle, explanation in _ERROR_EXPLANATIONS:
        if needle in text:
            return explanation
    return (str(error)[:140] + "…") if error and len(str(error)) > 140 else (str(error) or "Failed.")


def _starts_attempt(event: dict) -> bool:
    return (event.get("message") or "").startswith(("Scraping and analyzing", "Re-researched"))


def _split_superseded_events(events: list) -> tuple[list, list]:
    """(earlier, current): history before the research attempt that produced
    the current draft folds away."""
    produced_draft = [i for i, e in enumerate(events) if e["stage"] == "write" and not e["error_summary"]]
    if not produced_draft:
        return [], events
    starts = [i for i, e in enumerate(events) if _starts_attempt(e) and i <= produced_draft[-1]]
    if not starts or starts[-1] == 0:
        return [], events
    return events[:starts[-1]], events[starts[-1]:]


@app.route("/company/<int:app_id>")
def company_detail(app_id):
    import mail_service
    d = data()
    application = d.get_application_by_id(app_id)
    if not application:
        abort(404)
    events = d.get_events(app_id)
    for e in events:
        try:
            e["detail_parsed"] = json.loads(e["detail"]) if e["detail"] else None
        except json.JSONDecodeError:
            e["detail_parsed"] = None
        e["error_summary"] = _event_error_summary(e)
    earlier_events, current_events = _split_superseded_events(events)

    def _json(value, default):
        try:
            return json.loads(value or default)
        except (TypeError, json.JSONDecodeError):
            return json.loads(default)

    application["status_label"] = STATUS_LABELS.get(application["status"], application["status"])
    application["sendable"] = bool(application["status"] in db.SENDABLE_STATUSES and application.get("subject"))
    application["editable"] = application["status"] in EDITABLE_STATUSES
    application["rescannable"] = application["status"] in db.RESCANNABLE_STATUSES
    application["language"] = application.get("language") or ("en" if application.get("subject") else "")
    method = mail_service.sending_method(g.cfg)
    _, spec_en, spec_fr = profiles.specs_for(g.user["id"])
    import pipeline as pipeline_module
    research = pipeline_module._load_research(g.user["id"], application["email"], application) or {}
    try:
        dcfg = drafting.load_config(g.user["id"], g.cfg)
        styles = drafting.style_choices(dcfg)
        current_style = application.get("template_id") or dcfg.template_id
    except drafting.NotReady:
        styles, current_style = [], ""
    return render_template(
        "detail.html", application=application, events=events,
        styles=styles, current_style=current_style,
        email_name=drafting.email_company_name(application, research),
        source_label=db.source_label(application.get("source")),
        earlier_events=earlier_events, current_events=current_events,
        earlier_failed=sum(1 for e in earlier_events if e["error_summary"]),
        talking_points=_json(application["talking_points"], "[]"),
        matched_extras=_json(application["matched_extra_mentions"], "[]"),
        match_reasons=_json(application["match_reasons"], "{}"),
        prev_id=d.get_adjacent_application_id(app_id, direction="prev"),
        next_id=d.get_adjacent_application_id(app_id, direction="next"),
        from_email=(mail_service.from_address(g.cfg, method) if method else "") or "your email account",
        cv_info=g.cfg.cv_info(),
        word_count=len((application.get("body") or "").split()),
        languages_available=[lang for lang, spec in (("en", spec_en), ("fr", spec_fr)) if spec],
    )


@app.post("/api/send-job")
def api_create_send_job():
    payload = request.get_json(silent=True) or {}
    app_ids = _int_ids(payload.get("app_ids"))
    if app_ids is None:
        return jsonify({"ok": False, "message": "No valid emails selected."}), 400
    if not g.cfg.cv_info():
        return jsonify({"ok": False, "message": "Upload your CV in Settings before sending."})
    cap = max(1, g.cfg.int_setting("MAX_EMAILS_PER_DAY", 20))
    if data().count_sent_today() >= cap:
        return jsonify({"ok": False, "message": f"You've reached today's limit of {cap} emails — "
                        "nothing was sent. Send again tomorrow, or raise the limit in Settings → Sending pace."})
    job_id, queued_ids = data().create_send_job(app_ids)
    if not queued_ids:
        return jsonify({"ok": False, "message": "None of the selected emails are ready to send "
                                                "(already sending, or no draft)."})
    if config.embedded_worker() and not app.config.get("TESTING"):
        import worker
        worker.ensure_embedded()
    skipped = len(app_ids) - len(queued_ids)
    message = f"Send job created for {len(queued_ids)} email(s)."
    if skipped:
        message += f" ({skipped} already in progress or not ready were skipped.)"
    return jsonify({"ok": True, "job_id": job_id, "total": len(queued_ids), "message": message})


@app.get("/api/send-job/<int:job_id>")
def api_send_job_status(job_id):
    job = data().get_send_job(job_id)
    if not job:
        return jsonify({"ok": False, "message": "Job not found."}), 404
    return jsonify({
        "ok": True, "job_id": job_id, "status": job["status"], "total": job["total_items"],
        "queued": job.get("queued", 0), "sending": job.get("sending", 0),
        "sent": job.get("sent_count", 0), "failed": job.get("failed_count", 0),
        "items": [{"app_id": i["application_id"], "company": i.get("company_name", ""),
                   "email": i.get("email", ""), "status": i["status"],
                   "error": i.get("error_message", "")} for i in job.get("items", [])],
    })


@app.get("/api/applications")
def api_applications():
    status = request.args.get("status", "").strip()[:40] or None
    search = request.args.get("search", "").strip()[:120] or None
    page = max(1, request.args.get("page", 1, type=int) or 1)
    limit = max(1, min(request.args.get("limit", 50, type=int) or 50, 200))
    rows, total = data().get_applications_paginated(status=status, search=search, page=page, limit=limit)
    fields = ("id", "company_name", "email", "website", "status", "subject", "updated_at",
              "sent_at", "favorite", "language")
    return jsonify({"applications": [{**{k: r.get(k) for k in fields},
                                      "status_label": STATUS_LABELS.get(r["status"], r["status"])}
                                     for r in rows],
                    "total": total, "page": page, "limit": limit,
                    "pages": (total + limit - 1) // limit})


@app.post("/api/update-draft")
def api_update_draft():
    payload = request.get_json(silent=True) or {}
    subject = (payload.get("subject") or "").strip()[:500]
    body = (payload.get("body") or "").strip()[:20000]
    try:
        app_id = int(payload.get("app_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid app_id."}), 400
    if not subject or not body:
        return jsonify({"ok": False, "message": "Subject and body are required."})
    d = data()
    application = d.get_application_by_id(app_id)
    if not application:
        return jsonify({"ok": False, "message": "Application not found."}), 404
    if application["status"] not in EDITABLE_STATUSES:
        return jsonify({"ok": False, "message": f"Cannot edit — status is '{application['status']}'."})
    d.update_application(app_id, subject=subject, body=body, status="ready", error_message=None)
    d.log_event(app_id, "write", "Email edited from dashboard.")
    cache_store.save_draft(g.user["id"], application["email"],
                           {"subject": subject, "body": body, "language": application.get("language") or "en"})
    return jsonify({"ok": True, "message": "Draft saved.", "status": "ready",
                    "previous_status": application["status"]})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings")
def settings_page():
    return render_template("settings.html", setup=_setup_state(), pool=_pool_rows(),
                           run_sources=_run_sources(),
                           provider_cards=_provider_cards(),
                           oauth_redirect_uri=oauth_redirect_uri(),
                           bounce_check=_bounce_check_state(), providers=PROVIDERS,
                           sessions=accounts.count_sessions(g.user["id"]))


def _digits(name: str, minimum: int, maximum: int) -> str | None:
    raw = (request.form.get(name, "") or "").strip()
    if raw.isdigit() and minimum <= int(raw) <= maximum:
        return raw
    return None


@app.post("/settings")
def save_settings():
    import safe_http
    cfg = g.cfg
    section = request.form.get("section", "")
    updates = {}

    if section == "ai":
        provider = (request.form.get("ai_provider") or DEFAULT_PROVIDER).strip().lower()
        if provider not in PROVIDERS:
            provider = DEFAULT_PROVIDER
        preset = PROVIDERS[provider]
        updates["AI_PROVIDER"] = provider
        typed_base = (request.form.get("ai_base_url") or "").strip().rstrip("/")
        if typed_base and typed_base != preset["base_url"]:
            try:
                safe_http.check_url(typed_base)
                if config.is_production() and not typed_base.startswith("https://"):
                    raise safe_http.BlockedURL("Use an https:// address.")
            except (safe_http.BlockedURL, Exception) as exc:
                return security.flash_and_back(f"That API address can't be used: {exc}", "error", "settings_page")
            updates[f"{provider.upper()}_BASE_URL"] = typed_base
        elif typed_base == preset["base_url"]:
            updates[f"{provider.upper()}_BASE_URL"] = ""
        updates["AI_MODEL"] = (request.form.get("ai_model") or "").strip()[:120] or preset["default_model"]
        updates["AI_FALLBACK_MODELS"] = ",".join(
            m.strip()[:120] for m in (request.form.get("ai_fallbacks") or "").split(",") if m.strip())[:1000]
        updates["AI_TRANSLATION_MODEL"] = (request.form.get("ai_translation_model") or "").strip()[:120]
        typed_key = (request.form.get("ai_api_key") or "").strip()
        if typed_key:
            if len(typed_key) > 500:
                return security.flash_and_back("That API key is too long.", "error", "settings_page")
            cfg.set_secret(preset["key_env"], typed_key)
        if request.form.get("remove_key") == "on":
            cfg.delete_secret(preset["key_env"])
        anchor = "#s-ai"

    elif section == "sending":
        limits = {"MIN_DELAY_SECONDS": ("min_delay", 0, 3600), "MAX_DELAY_SECONDS": ("max_delay", 0, 7200),
                  "MAX_EMAILS_PER_DAY": ("max_per_day", 1, 500), "BOUNCE_CHECK_MINUTES": ("bounce_minutes", 0, 1440)}
        for key, (form_name, lo, hi) in limits.items():
            value = _digits(form_name, lo, hi)
            if value is not None:
                updates[key] = value
        anchor = "#s-sending"

    elif section == "advanced":
        limits = {"RESEARCH_WORKERS": ("research_workers", 1, 16), "WRITER_WORKERS": ("writer_workers", 1, 16),
                  "AI_MAX_RPM": ("ai_max_rpm", 1, 5000)}
        for key, (form_name, lo, hi) in limits.items():
            value = _digits(form_name, lo, hi)
            if value is not None:
                updates[key] = value
        anchor = "#s-advanced"

    elif section == "details":
        updates["YOUR_NAME"] = (request.form.get("your_name") or "").strip()[:120]
        updates["YOUR_TARGET_ROLE"] = (request.form.get("target_role") or "").strip()[:120]
        anchor = "#s-details"

    elif section == "mail":
        method = request.form.get("mail_method", "")
        if method not in ("oauth", "microsoft", "app_password", "smtp"):
            return security.flash_and_back("Choose how to send.", "error", "settings_page")
        updates["MAIL_METHOD"] = method
        if method in ("app_password", "smtp"):
            address = accounts.normalize_email(request.form.get("gmail_address", ""))
            if address and not accounts.valid_email(address):
                return security.flash_and_back("Enter a valid email address.", "error", "settings_page")
            updates["GMAIL_ADDRESS"] = address
        if method == "smtp":
            host = (request.form.get("smtp_host") or "").strip().lower()[:253]
            port = _digits("smtp_port", 1, 65535) or "465"
            imap = (request.form.get("imap_host") or "").strip().lower()[:253]
            try:
                safe_http.check_host(host, int(port))
                if imap:
                    safe_http.check_host(imap, 993)
            except safe_http.BlockedURL as exc:
                return security.flash_and_back(str(exc), "error", "settings_page")
            updates.update({"SMTP_HOST": host, "SMTP_PORT": port, "IMAP_HOST": imap,
                            "SMTP_SECURITY": "starttls" if request.form.get("smtp_security") == "starttls" else "ssl",
                            "SMTP_USERNAME": (request.form.get("smtp_username") or "").strip()[:320]})
        password = (request.form.get("gmail_app_password") or "").strip()
        if password:
            cfg.set_secret("GMAIL_APP_PASSWORD", password[:500])
        anchor = "#s-gmail"
    else:
        abort(400)

    cfg.set_many(updates)
    flash("Settings saved.", "success")
    return redirect(url_for("settings_page") + anchor)


@app.post("/settings/cv")
def upload_cv():
    upload = request.files.get("cv_file")
    if not upload or not upload.filename:
        return security.flash_and_back("Choose your CV file first.", "error", "settings_page")
    try:
        info = g.cfg.save_cv(upload.filename, upload.read(config.MAX_CV_BYTES + 1))
    except ValueError as exc:
        return security.flash_and_back(str(exc), "error", "settings_page")
    # Keep the profile's CV text in step, so claims are checked against the
    # CV that is actually attached.
    try:
        stored = g.cfg.cv()
        text = profiles.extract_cv_text(stored["filename"], stored["content"])
        if profiles.load(g.user["id"]):
            profiles.save(g.user["id"], cv_text=text)
    except profiles.ProfileError:
        pass
    flash(f"CV saved as {info['filename']} — it's attached to every application.", "success")
    return redirect(url_for("settings_page") + "#s-files")


@app.get("/settings/cv")
def download_cv():
    cv = g.cfg.cv()
    if not cv:
        abort(404)
    return Response(cv["content"], mimetype=cv["content_type"], headers={
        "Content-Disposition": f"attachment; filename=\"{cv['filename']}\"",
        "X-Content-Type-Options": "nosniff"})


@app.post("/api/companies/preview")
def companies_preview():
    upload = request.files.get("companies_file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "Choose a file first."}), 400
    try:
        report = company_import.parse(upload.filename, upload.read(config.MAX_COMPANIES_UPLOAD_BYTES + 1),
                                      data().company_list_emails())
    except company_import.ImportFailure as exc:
        return jsonify({"ok": False, "message": str(exc)})
    return jsonify({"ok": True, **{k: report[k] for k in ("total", "invalid", "duplicates",
                                                          "already_listed", "errors", "columns")},
                    "usable": len(report["rows"]), "new": len(report["new_rows"]),
                    "sample": report["new_rows"][:5]})


@app.post("/api/companies/import")
def companies_import():
    upload = request.files.get("companies_file")
    mode = request.form.get("mode", "append")
    source = db.clean_source_name(request.form.get("source"))
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "Choose a file first."}), 400
    d = data()
    try:
        report = company_import.parse(upload.filename, upload.read(config.MAX_COMPANIES_UPLOAD_BYTES + 1),
                                      set() if mode == "replace" else d.company_list_emails())
    except company_import.ImportFailure as exc:
        return jsonify({"ok": False, "message": str(exc)})
    if not report["rows"]:
        return jsonify({"ok": False, "message": "No usable rows in that file."})
    if mode == "replace":
        d.clear_company_list(source)
    room = config.MAX_COMPANIES_PER_USER - d.company_list_count()
    if room <= 0:
        return jsonify({"ok": False, "message": f"Your list is full ({config.MAX_COMPANIES_PER_USER} companies)."})
    added = d.add_companies(report["new_rows"][:room], source)
    accounts.audit("companies_imported", actor=g.user["id"], detail={"added": added, "mode": mode,
                                                                      "source": source})
    return jsonify({"ok": True, "added": added, "total": d.company_list_count(),
                    "message": f"{added} compan{'y' if added == 1 else 'ies'} added to "
                               f"“{db.source_label(source)}”."})


@app.post("/api/companies/clear")
def companies_clear():
    """Remove one of the user's lists (or all of them). Drafts and sent
    history stay."""
    payload = request.get_json(silent=True) or {}
    source = payload.get("source")
    removed = data().clear_company_list(None if source is None else db.clean_source_name(source))
    return jsonify({"ok": True, "message": f"Removed {removed} compan{'y' if removed == 1 else 'ies'}. "
                                          "Drafts and sent history are kept."})


@app.get("/companies-template.csv")
def companies_template():
    return Response(company_import.TEMPLATE_CSV, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=companies-template.csv"})


def _check_mail_login() -> tuple[bool, str]:
    import mail_service
    import mailer
    import safe_http
    payload = request.get_json(silent=True) or {}
    cfg = g.cfg
    method = payload.get("mail_method") or mail_service.sending_method(cfg)
    if method == "oauth":
        return False, "Google sign-in is checked by signing in — use the Google button."
    address = (payload.get("gmail_address") or "").strip() or cfg.get("GMAIL_ADDRESS")
    password = (payload.get("gmail_app_password") or "").strip() or cfg.secret("GMAIL_APP_PASSWORD")
    if not address or not password:
        return False, "Enter the address and the password first."
    if method == "smtp":
        host = (payload.get("smtp_host") or cfg.get("SMTP_HOST")).strip()
        port = int(payload.get("smtp_port") or cfg.get("SMTP_PORT") or 465)
        security_mode = payload.get("smtp_security") or cfg.get("SMTP_SECURITY", "ssl")
        username = (payload.get("smtp_username") or cfg.get("SMTP_USERNAME") or address).strip()
    else:
        host, port, security_mode = mailer.GMAIL_SMTP
        username, password = address, password.replace(" ", "")
    try:
        safe_http.check_host(host, port)
        mailer.check_smtp_login(host=host, port=port, security=security_mode,
                                username=username, password=password)
    except (safe_http.BlockedURL, mailer.PermanentSendError, mailer.TransientSendError) as exc:
        hint = (" For Gmail you need an App Password (myaccount.google.com/apppasswords, with "
                "2-Step Verification on), not your normal password." if method == "app_password" else "")
        return False, f"{exc}{hint}"
    return True, "Login works — nothing was sent."


@app.post("/api/validate-gmail")
def api_validate_gmail():
    if security.rate_limited(f"mailcheck:{g.user['id']}", 10, 600):
        return jsonify({"ok": False, "message": "Too many checks — wait a few minutes."}), 429
    ok, message = _check_mail_login()
    return jsonify({"ok": ok, "message": message})


# ---------------------------------------------------------------------------
# Google: sign in / sign up, and connecting Gmail for sending
# ---------------------------------------------------------------------------
# One OAuth client for the platform, one callback (/oauth/callback, the URI
# registered on Google's side) and two purposes kept in the session:
#   login    "Continue with Google" on the sign-in / sign-up pages — proves who
#            the visitor is (verified, signed ID token) and, in the same
#            consent, lets the app send from their Gmail;
#   connect  Settings → Email account, for a user who's already signed in.

LOCAL_OAUTH_HOST = "127.0.0.1"
LOGIN_EXTRA_SCOPES = ["https://www.googleapis.com/auth/userinfo.profile"]


def oauth_redirect_uri() -> str:
    base = config.public_base_url()
    if base:
        return f"{base}{url_for('oauth_callback')}"
    # Local: always 127.0.0.1 — Google compares redirect URIs character for
    # character, so opening the app as "localhost" would otherwise fail.
    port = request.host.rsplit(":", 1)[1] if ":" in request.host else "80"
    return f"http://{LOCAL_OAUTH_HOST}:{port}{url_for('oauth_callback')}"


def _canonical_host_redirect():
    """The session cookie belongs to the host it was set on; start the Google
    round trip on the host Google will send the browser back to."""
    from urllib.parse import urlparse
    expected = urlparse(oauth_redirect_uri()).netloc
    if request.host != expected:
        return redirect(f"{urlparse(oauth_redirect_uri()).scheme}://{expected}{request.full_path.rstrip('?')}")
    return None


def _to_mail_settings():
    return redirect(url_for("settings_page") + "#s-gmail")


def _oauth_flow(scopes: list, code_verifier: str | None = None):
    import google_auth_helper
    from google_auth_oauthlib.flow import Flow
    client = google_auth_helper.client_config()
    if not client:
        return None
    return Flow.from_client_config(client, scopes=scopes, redirect_uri=oauth_redirect_uri(),
                                   code_verifier=code_verifier)


def _start_google(purpose: str, hint: str = ""):
    import secrets as _secrets
    import google_auth_helper
    scopes = google_auth_helper.requested_scopes() + (LOGIN_EXTRA_SCOPES if purpose == "login" else [])
    flow = _oauth_flow(scopes)
    if not flow:
        return None
    state = _secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["oauth_purpose"] = purpose
    session["oauth_scopes"] = scopes
    session["oauth_uid"] = g.user["id"] if g.get("user") else None
    params = {"access_type": "offline", "state": state, "include_granted_scopes": "false",
              # A connect needs a fresh refresh token; a returning sign-in
              # only needs to pick the account.
              "prompt": "consent" if purpose == "connect" else "select_account"}
    if "@" in hint:
        params["login_hint"] = hint
    auth_url, _ = flow.authorization_url(**params)
    session["oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth/start")
def oauth_start():
    moved = _canonical_host_redirect()
    if moved:
        return moved
    started = _start_google("connect", g.cfg.get("GMAIL_ADDRESS") or g.user["email"])
    if started is None:
        flash("Google sign-in isn't set up on this platform yet — ask the administrator, or use "
              "an app password.", "error")
        return _to_mail_settings()
    return started


@app.route("/auth/google")
def auth_google():
    """"Continue with Google" — signs in, or signs up, in one step."""
    if g.get("user"):
        return redirect(url_for("index"))
    moved = _canonical_host_redirect()
    if moved:
        return moved
    if security.rate_limited(f"google-login-ip:{security.client_ip()}", 30, 600):
        flash("Too many attempts. Wait a few minutes and try again.", "error")
        return redirect(url_for("auth.login"))
    session["oauth_next"] = security.safe_next(request.args.get("next"))
    started = _start_google("login")
    if started is None:
        flash("Google sign-in isn't available on this platform yet.", "error")
        return redirect(url_for("auth.login"))
    return started


def _store_google_token(cfg, creds, email: str, granted: set) -> bool:
    """Keep the Gmail token when sending was allowed. A returning sign-in may
    come back without a refresh token; the one already stored is kept."""
    import google_auth_helper
    if google_auth_helper.SEND_SCOPE not in granted:
        return False
    if not creds.refresh_token:
        previous = json.loads(cfg.secret("GOOGLE_TOKEN") or "{}")
        if not previous.get("refresh_token"):
            return False
        data = json.loads(creds.to_json())
        data["refresh_token"] = previous["refresh_token"]
        data["email"] = email
        cfg.set_secret("GOOGLE_TOKEN", json.dumps(data))
    else:
        google_auth_helper.save_credentials(cfg, creds, email)
    # Only when Gmail is (or becomes) the way this person sends: someone who
    # sends through SMTP or Outlook keeps the From address that matches the
    # server they log in to.
    if cfg.get("MAIL_METHOD") in ("", "oauth"):
        cfg.set_many({"GMAIL_ADDRESS": email, "MAIL_METHOD": "oauth"})
    return True


def verify_google_identity(creds) -> dict:
    """The signed ID token, verified against Google's keys and our client id.
    Raises ValueError when it isn't valid."""
    import google_auth_helper
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token as google_id_token
    client_id = google_auth_helper.client_config()["web"]["client_id"]
    claims = google_id_token.verify_oauth2_token(creds.id_token, GoogleRequest(), client_id)
    if not claims.get("email") or not claims.get("email_verified"):
        raise ValueError("Google didn't confirm this email address.")
    return claims


def _finish_google_login(creds, granted: set):
    from user_config import UserConfig
    fail = lambda message: (flash(message, "error"), redirect(url_for("auth.login")))[1]  # noqa: E731
    try:
        claims = verify_google_identity(creds)
    except Exception as exc:
        accounts.audit("login_google_failed", detail={"error": str(exc)[:120]}, ip=security.client_ip())
        return fail("Google sign-in couldn't be verified — please try again.")
    email = accounts.normalize_email(claims["email"])
    user = accounts.get_user_by_email(email)
    if user and user["role"] == "admin":
        return fail("The administrator account signs in with its username and password.")
    if user is None:
        mode = accounts.signup_mode()
        if mode == "closed":
            return fail("Sign-up is closed on this platform — ask the administrator for an account.")
        # Google has verified who owns this address, so the account is ready
        # at once and the visitor lands inside the app. The approval queue
        # is for password sign-ups, whose address nobody has checked.
        uid = accounts.create_user(email, None, full_name=claims.get("name") or "",
                                   status="active", email_verified=True)
        accounts.audit("register_google", target=uid, ip=security.client_ip(),
                       detail={"status": "active", "signup_mode": mode})
        user = accounts.get_user(uid)
        session["welcome"] = True
    if accounts.claim_by_oauth(user["id"]):
        # Someone registered this address with a password before its owner
        # proved it — that password no longer works.
        accounts.audit("unverified_password_removed", target=user["id"], ip=security.client_ip())
        flash("For your safety, the password this account was created with has been removed: "
              "nobody had confirmed the address before. Set a new one any time in Settings → Account.",
              "warning")
    if user["status"] == "pending":
        return render_template("auth/register_done.html", pending=True)
    if user["status"] != "active":
        return fail("This account is suspended. Contact the administrator.")
    # The mailbox is connected only to an account that may be used.
    cfg = UserConfig(user["id"], user["role"])
    sending = _store_google_token(cfg, creds, email, granted)
    # Signing in with Google proves the identity: a pending temporary
    # password is no longer needed.
    accounts.update_user(user["id"], failed_logins=0, locked_until=None, must_change_password=0,
                         last_login_at=accounts._now())
    token, _ = accounts.create_session(user["id"], security.client_ip(), request.headers.get("User-Agent", ""))
    accounts.audit("login_google", actor=user["id"], ip=security.client_ip())
    if session.pop("welcome", False):
        flash("Welcome to Ntern — your account is ready. Follow the four steps below to prepare your first applications.", "success")
    elif not sending:
        flash("Signed in. To send from this Gmail, connect it in Settings → Email account.", "success")
    response = redirect(session.pop("oauth_next", None) or url_for("index"))
    security.set_session_cookie(response, token)
    return response


@app.route("/oauth/callback")
def oauth_callback():
    import google_auth_helper
    purpose = session.pop("oauth_purpose", "connect")
    back = (lambda: redirect(url_for("auth.login"))) if purpose == "login" else _to_mail_settings
    error = request.args.get("error")
    if error:
        hint = (" — while the platform's Google app is in Testing mode, only its listed test users "
                "can sign in." if error == "access_denied" else "")
        flash(f"Google sign-in was cancelled or denied: {error[:60]}{hint}", "error")
        return back()
    state = request.args.get("state", "")
    expected = session.pop("oauth_state", None)
    owner = session.pop("oauth_uid", None)
    code_verifier = session.pop("oauth_code_verifier", None)
    scopes = session.pop("oauth_scopes", None) or google_auth_helper.requested_scopes()
    current = g.user["id"] if g.get("user") else None
    if not state or not expected or state != expected or owner != current or \
            (purpose == "connect" and current is None):
        flash("Google sign-in couldn't be verified — please try again.", "error")
        return back()
    try:
        flow = _oauth_flow(scopes, code_verifier=code_verifier)
        flow.fetch_token(code=request.args.get("code", ""))
        creds = flow.credentials
        granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if purpose == "login":
            return _finish_google_login(creds, granted)
        missing = google_auth_helper.missing_required_scopes(creds)
        if google_auth_helper.SEND_SCOPE in missing:
            flash("Google connected, but the “Send email on your behalf” permission was left "
                  "unticked — connect again and tick it.", "error")
            return _to_mail_settings()
        email = google_auth_helper.email_from_id_token(creds)
        google_auth_helper.save_credentials(g.cfg, creds, email)
        g.cfg.set_many({"MAIL_METHOD": "oauth", "GMAIL_ADDRESS": email or g.cfg.get("GMAIL_ADDRESS")})
        accounts.audit("google_connected", actor=g.user["id"])
        note = (" Bounce detection needs the “Read email” permission — reconnect and tick it."
                if missing else "")
        flash(f"✓ Connected as {email or 'your Google account'} — your emails will be sent from it.{note}",
              "success")
    except Exception as exc:
        text = str(exc)
        if "redirect_uri_mismatch" in text:
            text = f"redirect URI mismatch — the platform's Google client must list {oauth_redirect_uri()}"
        flash(f"Google sign-in failed: {text[:200]}", "error")
        return back()
    return _to_mail_settings()


@app.post("/oauth/disconnect")
def oauth_disconnect():
    import google_auth_helper
    google_auth_helper.revoke_token(g.cfg)
    if g.cfg.get("MAIL_METHOD") == "oauth":
        g.cfg.set_many({"MAIL_METHOD": ""})
    flash("Google account disconnected.", "success")
    return _to_mail_settings()


# ---------------------------------------------------------------------------
# AI provider tools
# ---------------------------------------------------------------------------

def _ai_settings_from_request(payload: dict) -> dict:
    env = g.cfg.ai_env()
    provider = (payload.get("ai_provider") or env.get("AI_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    overrides = {"AI_PROVIDER": provider}
    preset = PROVIDERS[provider]
    if (payload.get("ai_api_key") or "").strip():
        overrides[preset["key_env"]] = payload["ai_api_key"].strip()[:500]
    if (payload.get("ai_base_url") or "").strip():
        overrides[f"{provider.upper()}_BASE_URL"] = payload["ai_base_url"].strip()[:300]
    if (payload.get("ai_model") or "").strip():
        overrides["AI_MODEL"] = payload["ai_model"].strip()[:120]
    return resolve_ai_settings({**env, **overrides})


@app.post("/api/models")
def api_models():
    if security.rate_limited(f"models:{g.user['id']}", 20, 600):
        return jsonify({"ok": False, "message": "Too many requests — wait a few minutes.", "models": []}), 429
    ai = _ai_settings_from_request(request.get_json(silent=True) or {})
    models, error = list_provider_models(ai["base_url"], ai["api_key"])
    if error:
        return jsonify({"ok": False, "message": error, "models": []})
    return jsonify({"ok": True, "models": models[:500],
                    "message": f"{len(models)} model(s) available on {ai['label']}."})


@app.post("/api/validate-ai")
def api_validate_ai():
    if security.rate_limited(f"validate-ai:{g.user['id']}", 20, 600):
        return jsonify({"ok": False, "message": "Too many tests — wait a few minutes."}), 429
    ai = _ai_settings_from_request(request.get_json(silent=True) or {})
    portal = PROVIDERS[ai["provider"]]["key_portal"]
    if not ai["api_key"]:
        return jsonify({"ok": False, "message": f"Paste your {ai['label']} API key first"
                                                + (f" (get one at {portal})." if portal else ".")})
    if not ai["base_url"]:
        return jsonify({"ok": False, "message": "Set the provider's base URL first."})
    if not ai["model"]:
        return jsonify({"ok": False, "message": "Pick a model first (use Load models)."})
    from ai_client import RateLimiter
    client = CompatibleAIClient(ai["api_key"], ai["base_url"], rate_limiter=RateLimiter(60))
    try:
        client.messages.create(model=ai["model"], max_tokens=200, system="Reply with one word.",
                               messages=[{"role": "user", "content": "Say ok"}],
                               max_attempts=1, temperature=0.0)
    except RuntimeError as exc:
        text = str(exc)
        if "401" in text or "403" in text:
            return jsonify({"ok": False, "message": f"{ai['label']} rejected the key (401/403)."})
        if "404" in text:
            return jsonify({"ok": False, "message": f"Model '{ai['model']}' not found on {ai['label']} (404)."})
        return jsonify({"ok": False, "message": f"{ai['label']} error: {text[:220]}"})
    return jsonify({"ok": True, "message": f"Works — {ai['label']} answered with model '{ai['model']}'."})


# ---------------------------------------------------------------------------
# AI providers, one card each: connect a key, tick models, pick which goes first
# ---------------------------------------------------------------------------

def _provider_base_url(pid: str) -> str:
    env = g.cfg.ai_env()
    return (str(env.get(f"{pid.upper()}_BASE_URL") or "").strip() or PROVIDERS[pid]["base_url"]).rstrip("/")


def _provider_cards() -> list:
    """What the AI section shows for each provider."""
    from ai_client import PROVIDER_GUIDES, PROVIDER_ORDER
    from model_router import pool_entries
    env = g.cfg.ai_env()
    saved = set(g.cfg.saved_secret_names())
    cards = []
    for pid in PROVIDER_ORDER:
        preset, guide = PROVIDERS[pid], PROVIDER_GUIDES[pid]
        connected = preset["key_env"] in saved
        cards.append({"id": pid, "label": preset["label"], "portal": preset["key_portal"], **guide,
                      "connected": connected, "base_url": _provider_base_url(pid) if pid == "custom" else "",
                      "models": [m for m, *_ in pool_entries(pid, env)] if connected else []})
    return cards


def _model_choices(pid: str, live: list) -> list:
    from ai_client import chat_model_ids, recommended_models
    from model_router import selected_models
    models = chat_model_ids(live)
    recommended = recommended_models(pid, models)
    chosen = selected_models(pid, g.cfg.ai_env()) or recommended
    # A model picked earlier that the provider no longer lists stays visible
    # (and selected) so nothing changes behind the user's back.
    models = sorted(set(models) | set(chosen))
    order = {m: i for i, m in enumerate(recommended)}
    models.sort(key=lambda m: (m not in order, order.get(m, 0), m))
    return [{"id": m, "recommended": m in recommended, "selected": m in chosen} for m in models]


def _ensure_primary(cfg) -> None:
    """The model tried first must belong to a connected provider and to its
    ticked models; otherwise the first connected one takes its place."""
    from model_router import pool_entries
    env = cfg.ai_env()
    saved = set(cfg.saved_secret_names())
    current = (env.get("AI_PROVIDER") or "").strip().lower()
    connected = [pid for pid in PROVIDERS if PROVIDERS[pid]["key_env"] in saved]
    if not connected:
        return
    if current in connected:
        models = [m for m, *_ in pool_entries(current, env)]
        if not models or env.get("AI_MODEL") in models:
            return
        cfg.set_many({"AI_MODEL": models[0]})
        return
    pid = connected[0]
    models = [m for m, *_ in pool_entries(pid, env)]
    cfg.set_many({"AI_PROVIDER": pid, "AI_MODEL": models[0] if models else PROVIDERS[pid]["default_model"]})


def _provider_or_400(pid: str):
    if pid not in PROVIDERS:
        abort(404)
    if security.rate_limited(f"ai-providers:{g.user['id']}", 60, 600):
        abort(429)
    return PROVIDERS[pid]


@app.post("/api/ai/<pid>/connect")
def api_ai_connect(pid):
    """Check a key by asking the provider for its models; save it only when
    the provider accepts it."""
    import safe_http
    preset = _provider_or_400(pid)
    payload = request.get_json(silent=True) or {}
    key = str(payload.get("api_key") or "").strip()
    if not key:
        return jsonify({"ok": False, "message": "Paste your key first."}), 400
    if len(key) > 500:
        return jsonify({"ok": False, "message": "That key is too long."}), 400
    base_url = preset["base_url"]
    if pid == "custom":
        base_url = str(payload.get("base_url") or "").strip().rstrip("/")[:300]
        try:
            safe_http.check_url(base_url)
            if config.is_production() and not base_url.startswith("https://"):
                raise safe_http.BlockedURL("Use an https:// address.")
        except Exception as exc:
            return jsonify({"ok": False, "message": f"That address can't be used: {exc}"}), 400
    live, error = list_provider_models(base_url, key)
    if error:
        hint = f" Copy it again from {preset['label']}'s site." if "rejected" in error else ""
        return jsonify({"ok": False, "message": error + hint}), 400
    g.cfg.set_secret(preset["key_env"], key)
    if pid == "custom":
        g.cfg.set_many({"CUSTOM_BASE_URL": base_url})
    choices = _model_choices(pid, live)
    g.cfg.set_many({f"AI_MODELS_{pid.upper()}": ",".join(c["id"] for c in choices if c["selected"])})
    _ensure_primary(g.cfg)
    accounts.audit("ai_provider_connected", actor=g.user["id"], detail={"provider": pid})
    picked = sum(1 for c in choices if c["selected"])
    return jsonify({"ok": True, "models": choices,
                    "message": f"Connected — {len(choices)} models available, {picked} recommended ones selected."})


@app.get("/api/ai/<pid>/models")
def api_ai_models(pid):
    preset = _provider_or_400(pid)
    key = g.cfg.secret(preset["key_env"])
    if not key:
        return jsonify({"ok": False, "message": "Not connected.", "models": []})
    live, error = list_provider_models(_provider_base_url(pid), key)
    if error:
        from model_router import selected_models
        chosen = selected_models(pid, g.cfg.ai_env())
        return jsonify({"ok": False, "message": error, "rejected": "rejected" in error,
                        "models": [{"id": m, "recommended": False, "selected": True} for m in chosen]})
    return jsonify({"ok": True, "models": _model_choices(pid, live)})


@app.post("/api/ai/<pid>/models")
def api_ai_select_models(pid):
    preset = _provider_or_400(pid)
    if not g.cfg.secret(preset["key_env"]):
        return jsonify({"ok": False, "message": "Connect this provider first."}), 400
    raw = (request.get_json(silent=True) or {}).get("models") or []
    models = list(dict.fromkeys(str(m).strip()[:120] for m in raw if str(m).strip()))[:30]
    if not models:
        return jsonify({"ok": False, "message": "Keep at least one model ticked, or remove the key."}), 400
    g.cfg.set_many({f"AI_MODELS_{pid.upper()}": ",".join(models)})
    _ensure_primary(g.cfg)
    return jsonify({"ok": True, "message": f"Saved — {len(models)} {preset['label']} model"
                                           f"{'s' if len(models) != 1 else ''} in your pool."})


@app.post("/api/ai/<pid>/disconnect")
def api_ai_disconnect(pid):
    preset = _provider_or_400(pid)
    g.cfg.delete_secret(preset["key_env"])
    g.cfg.set_many({f"AI_MODELS_{pid.upper()}": ""})
    _ensure_primary(g.cfg)
    accounts.audit("ai_provider_disconnected", actor=g.user["id"], detail={"provider": pid})
    return jsonify({"ok": True, "message": f"{preset['label']} removed."})


@app.post("/api/ai/primary")
def api_ai_primary():
    """Which model is tried first (the others take over when it's busy)."""
    from model_router import pool_entries
    name = str((request.get_json(silent=True) or {}).get("model") or "")
    pid, _, model = name.partition("/")
    if pid not in PROVIDERS or model not in [m for m, *_ in pool_entries(pid, g.cfg.ai_env())]:
        return jsonify({"ok": False, "message": "Pick one of the models in your pool."}), 400
    g.cfg.set_many({"AI_PROVIDER": pid, "AI_MODEL": model})
    return jsonify({"ok": True, "message": f"{model} is tried first."})


def _pool_rows() -> list:
    from model_router import build_router
    rows = []
    for row in build_router(g.cfg.ai_env()).snapshot():
        row["roles"] = ", ".join(f"{task} (tier {tier})" if tier else f"{task} (preferred)"
                                 for task, tier in sorted(row["tiers"].items()))
        rows.append(row)
    return rows


def _probe_deployment(d) -> dict:
    import time as _time
    from ai_client import AIProviderError
    started = _time.perf_counter()
    try:
        d.client.messages.create(model=d.model, max_tokens=200, system="Reply with one word.",
                                 messages=[{"role": "user", "content": "Say ok"}], max_attempts=1,
                                 temperature=0.0, reasoning_effort="low" if d.reasoning else None)
        return {"state": "ok", "detail": f"Answered in {(_time.perf_counter() - started) * 1000:.0f} ms."}
    except AIProviderError as error:
        headers = error.headers or {}
        if error.kind == "bad_response":
            return {"state": "ok", "detail": "Reachable (the probe's short reply came back empty)."}
        if str(headers.get("x-ratelimit-limit-req-minute")) == "0":
            return {"state": "inactive", "detail": "The account's plan allows 0 requests a minute."}
        if error.kind == "auth" and error.status == 401:
            return {"state": "bad_key", "detail": "The API key was rejected."}
        if error.kind == "auth":
            return {"state": "unavailable", "detail": "Not available on this key's plan."}
        if error.kind == "not_found":
            return {"state": "unavailable", "detail": "This provider doesn't serve that model."}
        if error.kind == "rate_limit":
            return {"state": "busy", "detail": "Rate-limited right now."}
        return {"state": "error", "detail": str(error)[:160]}
    except Exception as error:
        return {"state": "error", "detail": str(error)[:160]}


@app.post("/api/ai-health")
def api_ai_health():
    from concurrent.futures import ThreadPoolExecutor
    from model_router import build_router
    if security.rate_limited(f"ai-health:{g.user['id']}", 5, 600):
        return jsonify({"ok": False, "rows": [], "message": "Checked very recently — wait a few minutes."}), 429
    router = build_router(g.cfg.ai_env())
    if not router.deployments:
        return jsonify({"ok": False, "rows": [], "message": "No AI provider has a key yet. Add one above."})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_probe_deployment, router.deployments))
    rows = [{"name": d.name, "tiers": d.tiers, **result} for d, result in zip(router.deployments, results)]
    gaps = [task for task in ("research", "translation")
            if not [r for r in rows if task in r["tiers"] and r["state"] in ("ok", "busy")]]
    message = ("Every task has at least one working model." if not gaps else
               f"No working model for: {', '.join(gaps)}. Fix a key or plan above.")
    return jsonify({"ok": not gaps, "rows": rows, "message": message})


def main():
    database.init_schema()
    host = config.get("HOST", "127.0.0.1")
    port = config.int_setting("PORT", 5050)
    if config.embedded_worker():
        import worker
        worker.ensure_embedded()
    print(f"Dashboard running at http://{host}:{port}")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
