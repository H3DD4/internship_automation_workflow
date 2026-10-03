"""First-time setup: one question per page, with a tracker.

A new account lands here instead of the full app. Each step's tick comes from
real data (a CV is stored, a key is connected…), so leaving and coming back
resumes exactly where the person stopped. Optional steps can be skipped, and
"Skip setup" always leads to the app — nobody is trapped.

Why a wizard: testers found the first setup hard to follow. One task per
screen with visible progress, an early win (the CV score needs no AI key),
and progress already earned at the start ("Account ✓") are the patterns
that get people through a multi-step setup.
"""

from __future__ import annotations

import json
from datetime import date

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

import accounts
import config
import cv_ats
import db
import email_templates
import profiles
from dashboard import security
from utils import COUNTRY_BY_TLD

bp = Blueprint("onboarding", __name__)

STEPS = [
    {"id": "you", "title": "About you", "short": "You"},
    {"id": "cv", "title": "Your CV", "short": "CV"},
    {"id": "places", "title": "Where", "short": "Where", "optional": True},
    {"id": "ai", "title": "AI key", "short": "AI key"},
    {"id": "profile", "title": "Your profile", "short": "Profile"},
    {"id": "style", "title": "Email style", "short": "Style"},
    {"id": "mail", "title": "Your mailbox", "short": "Mailbox", "optional": True},
]
STEP_IDS = [s["id"] for s in STEPS]

# Shown first: where this platform's students apply most.
POPULAR_COUNTRIES = ["France", "Switzerland", "Belgium", "Luxembourg", "Germany", "Canada",
                     "United Kingdom", "Netherlands", "Tunisia", "Morocco"]
ALL_COUNTRIES = POPULAR_COUNTRIES + sorted(set(COUNTRY_BY_TLD.values()) - set(POPULAR_COUNTRIES))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(cfg) -> dict:
    try:
        state = json.loads(cfg.get("ONBOARDING") or "{}")
    except ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


def save_state(cfg, **changes) -> dict:
    state = {**load_state(cfg), **changes}
    cfg.set_many({"ONBOARDING": json.dumps(state)})
    return state


def target_countries(cfg) -> list:
    return [c for c in (cfg.get("TARGET_COUNTRIES") or "").split("|") if c]


def _has_ai_key(cfg) -> bool:
    from ai_client import PROVIDERS
    return bool(cfg.saved_secret_names() & {p["key_env"] for p in PROVIDERS.values()})


def _has_mail(cfg) -> bool:
    from dashboard.app import _setup_state
    return bool(_setup_state()["has_gmail"])


def progress(cfg, user_id: int) -> list:
    """The steps with what is really done, in order."""
    state = load_state(cfg)
    skipped, seen = set(state.get("skipped") or []), set(state.get("seen") or [])
    profile = profiles.load(user_id) or {}
    has_facts = bool(profile.get("facts")) or profile.get("mode") == "custom"
    done = {
        "you": bool(state.get("dates")) or has_facts,
        "cv": bool(cfg.cv_info()),
        "places": bool(target_countries(cfg)) or "places" in skipped,
        "ai": _has_ai_key(cfg),
        "profile": has_facts,
        "style": has_facts and "style" in seen,
        "mail": "mail" in skipped or _has_mail(cfg),
    }
    return [{**step, "done": done[step["id"]], "skipped": step["id"] in skipped} for step in STEPS]


def needed(user: dict, cfg) -> bool:
    """A brand-new student account that hasn't finished (or skipped) setup.
    Accounts that already have companies or a profile are left alone."""
    if not user or user["role"] == "admin":
        return False
    if load_state(cfg).get("finished"):
        return False
    data = db.for_user(user["id"])
    profile = profiles.load(user["id"]) or {}
    has_profile = bool(profile.get("facts")) or profile.get("mode") == "custom"
    if has_profile and data.get_grouped_stats():
        return False                      # an account already in use
    return True


def _first_open(steps: list) -> str:
    return next((s["id"] for s in steps if not s["done"]), "finish")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@bp.get("/welcome")
def wizard():
    cfg = g.cfg
    steps = progress(cfg, g.user["id"])
    wanted = request.args.get("step", "")
    current = wanted if wanted in STEP_IDS + ["finish"] else _first_open(steps)
    session["in_setup"] = True        # mailbox sign-ins come back here, not to Settings
    state = load_state(cfg)
    profile = profiles.load(g.user["id"]) or {}
    facts = profile.get("facts") or {}
    index = STEP_IDS.index(current) if current in STEP_IDS else len(STEP_IDS)
    context = {
        "steps": steps, "current": current, "index": index,
        "done_count": 1 + sum(1 for s in steps if s["done"]), "total": 1 + len(steps),
        "previous": STEP_IDS[index - 1] if 0 < index <= len(STEP_IDS) else None,
        "following": STEP_IDS[index + 1] if index + 1 < len(STEP_IDS) else "finish",
        "state": state, "facts": facts, "profile": profile,
        "this_year": date.today().year, "months": profiles.MONTHS["en"], "kinds": profiles.INTERNSHIP_KINDS,
        "dates": state.get("dates") or facts.get("internship") or {},
        "full_name": facts.get("full_name") or cfg.get("YOUR_NAME") or g.user.get("full_name") or "",
    }
    if current == "cv":
        stored = cfg.cv()
        context["report"] = cv_ats.check(stored["filename"], stored["content"]) if stored else None
    elif current == "places":
        context.update(countries=ALL_COUNTRIES, popular=POPULAR_COUNTRIES, chosen=target_countries(cfg))
    elif current == "ai":
        from dashboard.app import _provider_cards
        cards = _provider_cards()
        context["cards"] = [c for c in cards if c["id"] in ("groq", "gemini", "mistral")] or cards[:3]
        context["connected"] = [c for c in cards if c.get("connected")]
    elif current == "profile":
        context["flags"] = (profiles.check_profile(facts, profile.get("cv_text") or "")
                            if facts and profile.get("cv_text") else [])
    elif current == "style":
        context.update(templates=email_templates.template_choices("en"),
                       template_id=profile.get("template_id") or email_templates.DEFAULT_TEMPLATE,
                       language_mode=profile.get("language_mode") or "auto")
    elif current == "mail":
        from dashboard.app import _setup_state
        context["setup"] = _setup_state()
    elif current == "finish":
        context.update(ntern_rows=db.catalog_count(), chosen=target_countries(cfg),
                       ready=all(s["done"] for s in steps if not s.get("optional")))
    return render_template("onboarding.html", **context)


def _go(step: str):
    return redirect(url_for("onboarding.wizard", step=step))


@bp.post("/welcome/you")
def save_you():
    name = (request.form.get("full_name") or "").strip()[:120]
    try:
        month, year = int(request.form.get("start_month") or 0), int(request.form.get("start_year") or 0)
        profiles.start_date_text(month, year, "en")
        duration = int(request.form.get("duration") or 0) or None
        if duration is not None and not 1 <= duration <= 36:
            raise profiles.ProfileError("Pick a duration between 1 and 36 months.")
    except (ValueError, profiles.ProfileError) as exc:
        flash(str(exc) if isinstance(exc, profiles.ProfileError) else "Pick a start month and year.", "error")
        return _go("you")
    if not name:
        flash("Enter your name — it signs your emails.", "error")
        return _go("you")
    kind = request.form.get("kind") if request.form.get("kind") in profiles.INTERNSHIP_KINDS else "internship"
    open_to_hire = request.form.get("open_to_hire") == "on"
    g.cfg.set_many({"YOUR_NAME": name})
    save_state(g.cfg, dates={"kind": kind, "month": month, "year": year, "duration": duration,
                             "open_to_hire": open_to_hire})
    # An existing profile gets the new dates at once.
    if (profiles.load(g.user["id"]) or {}).get("facts"):
        profiles.update_dates(g.user["id"], kind=kind, month=month, year=year,
                              duration_months=duration, open_to_hire=open_to_hire)
    return _go("cv")


@bp.post("/api/cv/check")
def cv_check():
    """Store the uploaded CV (when one is sent) and report how an ATS reads
    it. Rules only — the same file always gets the same report."""
    upload = request.files.get("cv_file")
    if upload and upload.filename:
        try:
            g.cfg.save_cv(upload.filename, upload.read(config.MAX_CV_BYTES + 1))
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)})
    stored = g.cfg.cv()
    if not stored:
        return jsonify({"ok": False, "message": "Choose your CV file first."})
    report = cv_ats.check(stored["filename"], stored["content"])
    if not report["blocking"]:
        try:      # keep the profile's CV text in step with the attached file
            text = profiles.extract_cv_text(stored["filename"], stored["content"])
            if profiles.load(g.user["id"]):
                profiles.save(g.user["id"], cv_text=text)
        except profiles.ProfileError:
            pass
    return jsonify({"ok": True, "report": report})


@bp.post("/welcome/places")
def save_places():
    chosen = [c for c in request.form.getlist("country") if c in ALL_COUNTRIES][:12]
    g.cfg.set_many({"TARGET_COUNTRIES": "|".join(chosen)})
    state = load_state(g.cfg)
    skipped = set(state.get("skipped") or [])
    skipped.discard("places") if chosen else skipped.add("places")
    save_state(g.cfg, skipped=sorted(skipped))
    if request.form.get("return") == "settings":
        flash("Target places saved.", "success")
        return redirect(url_for("profile.page") + "#places")
    return _go("ai")


@bp.post("/welcome/seen/<step>")
def mark_seen(step):
    if step not in STEP_IDS:
        return jsonify({"ok": False}), 400
    state = load_state(g.cfg)
    save_state(g.cfg, seen=sorted(set(state.get("seen") or []) | {step}))
    return jsonify({"ok": True})


@bp.post("/welcome/skip/<step>")
def skip(step):
    if step not in ("places", "mail"):
        return _go(step)
    state = load_state(g.cfg)
    save_state(g.cfg, skipped=sorted(set(state.get("skipped") or []) | {step}))
    return _go(STEP_IDS[STEP_IDS.index(step) + 1] if step != STEP_IDS[-1] else "finish")


@bp.post("/welcome/finish")
def finish():
    """Leave the setup for the app — done, or skipped for now."""
    save_state(g.cfg, finished=True)
    session.pop("in_setup", None)
    accounts.audit("onboarding_finished", actor=g.user["id"],
                   detail={"complete": all(s["done"] for s in progress(g.cfg, g.user["id"]))})
    if request.form.get("start") and all(s["done"] for s in progress(g.cfg, g.user["id"])
                                         if not s.get("optional")):
        flash("You're set up. Press “Start preparation” below — your first drafts arrive in a few minutes.", "success")
    return redirect(url_for("index"))
