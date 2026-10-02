"""Profile: the CV-grounded wording every email is built from.

Flow for a new user: upload CV + a few answers -> the AI drafts the profile
(both languages) -> every claim is checked against the CV -> the user edits
and confirms -> a template turns it into their emails. A live preview shows
the exact email in English and French before anything is prepared.
"""

from __future__ import annotations

import json
from datetime import date

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

import accounts
import config
import db
import email_templates
import pipeline
import profiles
from agents.composer import compose_email
from agents.draft_guard import GuardRejection
from dashboard import security
from language import research_for_language
from utils import build_greeting

bp = Blueprint("profile", __name__)

SAMPLE = {
    "company": "Northwind Labs",
    "contact": "",
    "research": {"company_hook": "helping mid-sized companies modernise the way they operate",
                 "hook_original": "l'accompagnement des PME dans la modernisation de leur fonctionnement",
                 "site_language": ""},
}


def _profile_context(profile: dict | None) -> dict:
    facts = (profile or {}).get("facts")
    return {
        "profile": profile or {},
        "facts": facts,
        "mode": (profile or {}).get("mode") or "template",
        "template_id": (profile or {}).get("template_id") or email_templates.DEFAULT_TEMPLATE,
        "language_mode": (profile or {}).get("language_mode") or "auto",
        "templates": email_templates.template_choices("en"),
        "proven_rules": email_templates.proven_rules("en"),
        "cv": g.cfg.cv_info(),
        "has_ai_key": bool(g.cfg.saved_secret_names() & {
            p["key_env"] for p in __import__("ai_client").PROVIDERS.values()}),
        "kinds": profiles.INTERNSHIP_KINDS,
        "this_year": date.today().year,
        "months": profiles.MONTHS["en"],
        "ready_count": db.for_user(g.user["id"]).get_grouped_stats().get("ready", 0),
        "own_template": (profile or {}).get("own_template"),
        "own_editor": {"template": (profile or {}).get("own_template"), "blank": profiles.blank_own_template(),
                       "fieldBlanks": email_templates.OWN_FIELD_BLANKS},
    }


@bp.get("/profile")
def page():
    profile = profiles.load(g.user["id"])
    context = _profile_context(profile)
    context["flags"] = (profiles.check_profile(profile["facts"], profile.get("cv_text") or "")
                        if profile and profile.get("facts") and profile.get("cv_text") else [])
    return render_template("profile.html", **context)


@bp.post("/profile/analyze")
def analyze():
    """Read the CV, collect the request answers, let the AI draft the rest."""
    from ai_client import RateLimiter
    from model_router import build_router

    if security.rate_limited(f"analyze:{g.user['id']}", 8, 3600):
        return security.flash_and_back("You've analysed your CV many times this hour — edit the "
                                       "profile by hand, or try again later.", "error", "profile.page")
    upload = request.files.get("cv_file")
    try:
        if upload and upload.filename:
            content = upload.read(config.MAX_CV_BYTES + 1)
            info = g.cfg.save_cv(upload.filename, content)
            filename = info["filename"]
        else:
            stored = g.cfg.cv()
            if not stored:
                return security.flash_and_back("Upload your CV first.", "error", "profile.page")
            content, filename = stored["content"], stored["filename"]
        cv_text = profiles.extract_cv_text(filename, content)

        kind = request.form.get("kind", "end_of_study")
        month = int(request.form.get("start_month") or 0)
        year = int(request.form.get("start_year") or 0)
        duration = int(request.form.get("duration") or 0) or None
        open_to_hire = request.form.get("open_to_hire") == "on"
        ask = profiles.internship_ask(kind=kind, month=month, year=year, duration_months=duration,
                                      degree_context=None, open_to_hire=open_to_hire)

        env = g.cfg.ai_env()
        router = build_router(env, rate_limiter=RateLimiter(60))
        if not router.deployments:
            return security.flash_and_back("Add an AI provider key in Settings first — the "
                                           "analysis runs on your own key.", "error", "settings_page")
        facts = profiles.draft_profile_with_ai(router, cv_text)
    except (profiles.ProfileError, ValueError) as exc:
        return security.flash_and_back(str(exc), "error", "profile.page")

    info = profiles.INTERNSHIP_KINDS.get(kind) or profiles.INTERNSHIP_KINDS["internship"]
    facts["full_name"] = (request.form.get("full_name") or g.user.get("full_name") or "").strip()[:120]
    facts["internship_ask"] = ask
    facts["start_date"] = {lang: profiles.start_date_text(month, year, lang) for lang in profiles.LANGS}
    facts["target_role"] = {"en": info["role_en"], "fr": info["role_fr"]}
    facts["internship"] = {"kind": kind, "month": month, "year": year, "duration": duration,
                           "open_to_hire": open_to_hire}
    current = profiles.load(g.user["id"]) or {}
    profiles.save(g.user["id"], facts=facts, cv_text=cv_text,
                  mode=current.get("mode") if current.get("spec_en") and current.get("mode") == "custom" else "template")
    accounts.audit("profile_analyzed", actor=g.user["id"])
    flash("Your profile was drafted from your CV. Read every section, fix anything that isn't "
          "exactly right, then save.", "success")
    return redirect(url_for("profile.page") + "#editor")


@bp.post("/profile/dates")
def save_dates():
    """Your internship dates on their own — instant, no AI, no re-analysis."""
    try:
        result = profiles.update_dates(
            g.user["id"], kind=request.form.get("kind", "end_of_study"),
            month=int(request.form.get("start_month") or 0), year=int(request.form.get("start_year") or 0),
            duration_months=int(request.form.get("duration") or 0) or None,
            open_to_hire=request.form.get("open_to_hire") == "on")
    except (profiles.ProfileError, ValueError) as exc:
        return security.flash_and_back(str(exc), "error", "profile.page")
    accounts.audit("profile_dates_changed", actor=g.user["id"])
    flash(f"Saved — new emails now say {result['new']['en']}.", "success")
    return redirect(url_for("profile.page") + "#dates")


@bp.post("/api/drafts/rebuild-ready")
def rebuild_ready():
    """Rewrite every draft waiting for review with the current profile (new
    dates, new wording) — instant, from the saved research, no AI."""
    import cache_store
    import drafting
    import pipeline
    from agents.draft_guard import GuardRejection
    try:
        dcfg = drafting.load_config(g.user["id"], g.cfg)
    except drafting.NotReady as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    d = db.for_user(g.user["id"])
    rows, _ = d.get_applications_paginated(status="ready", limit=5000)
    done = failed = 0
    for app in rows:
        research = pipeline._load_research(g.user["id"], app["email"], app) or {}
        try:
            draft = drafting.compose_for(dcfg, app, research, lang=app.get("language") or None)
        except (GuardRejection, drafting.NotReady, KeyError, ValueError):
            failed += 1
            continue
        cache_store.save_draft(g.user["id"], app["email"], draft)
        d.update_application(app["id"], subject=draft["subject"], body=draft["body"],
                             language=draft["language"], template_id=draft.get("template_id"))
        done += 1
    accounts.audit("drafts_rebuilt", actor=g.user["id"], detail={"done": done, "failed": failed})
    message = f"{done} draft{'s' if done != 1 else ''} updated."
    if failed:
        message += f" {failed} couldn't be rebuilt and were left as they were."
    return jsonify({"ok": True, "updated": done, "failed": failed, "message": message})


@bp.post("/api/profile/save")
def save():
    payload = request.get_json(silent=True) or {}
    facts = profiles.normalize_facts(payload.get("facts") or {})
    profile = profiles.load(g.user["id"]) or {}
    cv_text = profile.get("cv_text") or ""
    problems = profiles.completeness_problems(facts)
    flags = profiles.check_profile(facts, cv_text) if cv_text else []
    if problems:
        return jsonify({"ok": False, "problems": problems, "flags": flags,
                        "message": "Still missing: " + ", ".join(problems) + "."})
    if flags:
        return jsonify({"ok": False, "flags": flags, "problems": [],
                        "message": f"{len(flags)} sentence(s) mention something your CV doesn't. "
                                   "Fix them, or tick “This is true” if your CV just words it differently."})
    template_id = payload.get("template_id") or profile.get("template_id") or email_templates.DEFAULT_TEMPLATE
    try:
        specs = profiles.apply_template(g.user["id"], facts, template_id)
    except profiles.ProfileError as exc:
        return jsonify({"ok": False, "message": str(exc)})
    missing = email_templates.spec_problems(specs["en"])
    accounts.audit("profile_saved", actor=g.user["id"])
    return jsonify({"ok": True, "message": "Profile saved — your emails now use it." if not missing
                    else "Saved, but still missing: " + ", ".join(missing) + "."})


@bp.post("/api/profile/settings")
def save_settings():
    """Template and language choice."""
    payload = request.get_json(silent=True) or {}
    profile = profiles.load(g.user["id"])
    if not profile:
        return jsonify({"ok": False, "message": "Create your profile first."})
    language_mode = payload.get("language_mode", profile.get("language_mode") or "auto")
    if language_mode not in ("auto", "en", "fr"):
        return jsonify({"ok": False, "message": "Unknown language setting."}), 400
    template_id = payload.get("template_id") or profile["template_id"]
    if template_id == "custom":
        if not (profile.get("spec_en") and profile.get("mode") == "custom"):
            return jsonify({"ok": False, "message": "No hand-written wording to switch to."})
        profiles.save(g.user["id"], language_mode=language_mode)
        return jsonify({"ok": True, "message": "Saved."})
    if template_id == email_templates.OWN_ID:
        if not (profile.get("own_template") and profile.get("facts")):
            return jsonify({"ok": False, "message": "Create your own template first (below)."})
    elif template_id not in email_templates.TEMPLATES:
        return jsonify({"ok": False, "message": "Unknown template."}), 400
    if not profile.get("facts"):
        profiles.save(g.user["id"], language_mode=language_mode)
        return jsonify({"ok": False, "message": "Templates need a CV profile — analyse your CV first."})
    profiles.save(g.user["id"], language_mode=language_mode)
    profiles.apply_template(g.user["id"], profile["facts"], template_id)
    return jsonify({"ok": True, "message": "Saved — new drafts use this style. Use “Rebuild draft” "
                                           "to restyle ones already prepared."})


@bp.post("/api/profile/preview")
def preview():
    """The same sample company, rendered with a template (or the current
    wording), in both languages."""
    payload = request.get_json(silent=True) or {}
    profile = profiles.load(g.user["id"]) or {}
    template_id = payload.get("template_id") or profile.get("template_id")
    facts = profiles.normalize_facts(payload["facts"]) if payload.get("facts") else profile.get("facts")
    name = ((facts or {}).get("full_name") or g.cfg.get("YOUR_NAME") or g.user.get("full_name") or "You")

    if template_id == "custom" or (not facts and profile.get("mode") == "custom"):
        specs = {"en": profile.get("spec_en"), "fr": profile.get("spec_fr")}
    elif facts:
        specs = {lang: email_templates.build_spec(facts, template_id, lang) for lang in profiles.LANGS}
    else:
        return jsonify({"ok": False, "message": "Create your profile first."})

    company, research = SAMPLE["company"], dict(SAMPLE["research"])
    sample_app = _real_sample()
    if sample_app:
        company, research = sample_app
    first_area = (specs.get("en") or specs.get("fr") or {}).get("areas", [{}])
    research["areas"] = [first_area[0]["id"]] if first_area and first_area[0].get("id") else []

    out = {}
    for lang in profiles.LANGS:
        spec = specs.get(lang)
        if not spec:
            continue
        role = spec.get("target_role") or g.cfg.get("YOUR_TARGET_ROLE") or "Internship"
        try:
            draft = compose_email(spec, research_for_language(research, lang), company,
                                  build_greeting("", company, lang), name, role, lang)
            out[lang] = {"subject": draft["subject"], "body": draft["body"],
                         "words": len(draft["body"].split())}
        except (GuardRejection, KeyError, IndexError, ValueError) as exc:
            out[lang] = {"error": str(exc)[:300]}
    return jsonify({"ok": True, "company": company, "preview": out})


def _real_sample():
    """A company from the user's own list with verified research, so the
    preview shows a real hook instead of the made-up one."""
    data = db.for_user(g.user["id"])
    rows, _ = data.get_applications_paginated(status="ready", limit=20)
    for app in rows:
        research = pipeline._load_research(g.user["id"], app["email"], app)
        if research and research.get("company_hook"):
            return app["company_name"], dict(research)
    return None


@bp.post("/api/profile/custom-spec")
def save_custom_spec():
    """Hand-written wording (the original specializations.json format), for
    users who want full control. Validated by composing a sample email."""
    payload = request.get_json(silent=True) or {}
    specs = {}
    for lang in profiles.LANGS:
        raw = payload.get(f"spec_{lang}")
        if raw in (None, ""):
            specs[lang] = None
            continue
        try:
            spec = json.loads(raw) if isinstance(raw, str) else raw
            for key in ("verified_facts", "email", "areas", "strengths"):
                if key not in spec:
                    raise ValueError(f"missing '{key}'")
            compose_email(spec, {"company_hook": "", "areas": [spec["areas"][0]["id"]] if spec["areas"] else []},
                          "Northwind Labs", build_greeting("", "Northwind Labs", lang),
                          g.cfg.get("YOUR_NAME") or "You", spec.get("target_role") or "Internship", lang)
        except (ValueError, KeyError, IndexError, TypeError, GuardRejection) as exc:
            return jsonify({"ok": False, "message": f"{lang.upper()} wording is invalid: {str(exc)[:200]}"})
        specs[lang] = spec
    if not specs["en"]:
        return jsonify({"ok": False, "message": "The English wording is required."})
    profiles.save(g.user["id"], mode="custom", template_id="custom",
                  spec_en=specs["en"], spec_fr=specs["fr"])
    accounts.audit("profile_custom_saved", actor=g.user["id"])
    return jsonify({"ok": True, "message": "Hand-written wording saved."})



# ---------------------------------------------------------------------------
# The student's own template
# ---------------------------------------------------------------------------

def _own_from_payload(payload: dict) -> dict:
    """The editor's template, cleaned the same way as an AI draft."""
    raw = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    own = profiles._own_clean(raw)
    # A language left completely empty in the editor is simply not offered.
    for lang in profiles.LANGS:
        if not any(own[lang].values()):
            own[lang] = {}
    return own


def _own_preview(own: dict, facts: dict) -> dict:
    """Both languages on one sample company — through the same composer and
    checks every real email goes through."""
    name = (facts.get("full_name") or g.cfg.get("YOUR_NAME") or g.user.get("full_name") or "You")
    company, research = SAMPLE["company"], dict(SAMPLE["research"])
    sample_app = _real_sample()
    if sample_app:
        company, research = sample_app
    out = {}
    for lang in profiles.LANGS:
        if not own.get(lang):
            continue
        try:
            spec = email_templates.build_spec(facts, email_templates.OWN_ID, lang, own=own)
            areas = spec.get("areas") or []
            research["areas"] = [areas[0]["id"]] if areas else []
            role = spec.get("target_role") or "Internship"
            draft = compose_email(spec, research_for_language(research, lang), company,
                                  build_greeting("", company, lang), name, role, lang)
            out[lang] = {"subject": draft["subject"], "body": draft["body"], "words": len(draft["body"].split())}
        except (GuardRejection, KeyError, IndexError, ValueError) as exc:
            out[lang] = {"error": str(exc).strip("'\"")[:300]}
    return {"company": company, "preview": out}


@bp.post("/api/own-template/from-example")
def own_template_from_example():
    """Paste an email you like -> your template, split into sections."""
    from ai_client import RateLimiter
    from model_router import build_router
    if security.rate_limited(f"own-template:{g.user['id']}", 15, 3600):
        return jsonify({"ok": False, "message": "You've converted many emails this hour — edit the sections by hand, or try later."})
    profile = profiles.load(g.user["id"]) or {}
    if not profile.get("facts"):
        return jsonify({"ok": False, "message": "Analyse your CV first (step 1) — your template is filled with it."})
    router = build_router(g.cfg.ai_env(), rate_limiter=RateLimiter(60))
    if not router.deployments:
        return jsonify({"ok": False, "message": "Add an AI provider key in Settings first."})
    payload = request.get_json(silent=True) or {}
    try:
        own = profiles.own_template_from_example(router, str(payload.get("example") or "")[:8000],
                                                 profile.get("cv_text") or "")
    except profiles.ProfileError as exc:
        return jsonify({"ok": False, "message": str(exc)})
    return jsonify({"ok": True, "template": own, "problems": email_templates.own_template_problems(own),
                    "message": "Here's your template — check each section, then preview and save."})


@bp.post("/api/own-template/preview")
def own_template_preview():
    profile = profiles.load(g.user["id"]) or {}
    if not profile.get("facts"):
        return jsonify({"ok": False, "message": "Analyse your CV first (step 1)."})
    own = _own_from_payload(request.get_json(silent=True) or {})
    problems = email_templates.own_template_problems(own)
    if problems:
        return jsonify({"ok": False, "problems": problems, "message": problems[0]})
    return jsonify({"ok": True, **_own_preview(own, profile["facts"])})


@bp.post("/api/own-template/save")
def own_template_save():
    profile = profiles.load(g.user["id"]) or {}
    if not profile.get("facts"):
        return jsonify({"ok": False, "message": "Analyse your CV first (step 1)."})
    payload = request.get_json(silent=True) or {}
    own = _own_from_payload(payload)
    problems = email_templates.own_template_problems(own)
    if problems:
        return jsonify({"ok": False, "problems": problems, "message": problems[0]})
    preview = _own_preview(own, profile["facts"])
    broken = [f"{lang.upper()}: {p['error']}" for lang, p in preview["preview"].items() if "error" in p]
    if broken:
        return jsonify({"ok": False, "message": "The sample email didn't pass the checks — " + broken[0], **preview})
    profiles.save(g.user["id"], own_template=own)
    use = bool(payload.get("use"))
    if use or profile.get("template_id") == email_templates.OWN_ID:
        profiles.apply_template(g.user["id"], profile["facts"], email_templates.OWN_ID)
    accounts.audit("own_template_saved", actor=g.user["id"], detail={"default": use})
    return jsonify({"ok": True, **preview,
                    "message": "Saved — new emails use your template. Use “Update them with my current profile” "
                               "to rewrite the ones already ready." if use else
                               "Saved — pick “Your template” as your style, or switch any single email to it."})
