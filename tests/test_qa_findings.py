"""Regressions for the external QA review of 97c9628: each test reproduces
one finding that was confirmed against the code."""

import copy
from datetime import datetime, timedelta, timezone

import accounts
import email_templates
import profiles
from agents import research_agent as ra
from agents.composer import compose_email
from test_dates import _dates
from test_templates_and_profiles import FACTS


# --- Account takeover through a password set before the owner proved the address

def test_signing_in_with_google_removes_a_password_someone_else_chose(user_id):
    """Anyone could register victim@gmail.com with their own password; when
    the real owner later signed in with Google they landed in that account
    and the stranger's password kept working."""
    assert accounts.authenticate("user@example.com", "correct-horse-battery-9").ok
    accounts.create_session(user_id, "127.0.0.1", "attacker")
    assert accounts.claim_by_oauth(user_id) is True
    assert not accounts.authenticate("user@example.com", "correct-horse-battery-9").ok
    assert accounts.get_user(user_id)["email_verified_at"]
    assert accounts.claim_by_oauth(user_id) is False      # only the first time


def test_accounts_made_by_google_have_no_password_until_their_owner_sets_one(isolated, login):
    uid = accounts.create_user("g@example.com", None, status="active", email_verified=True)
    assert not accounts.has_password(accounts.get_user(uid))
    assert accounts.claim_by_oauth(uid) is False          # already proven: nothing to remove
    client = login(uid)
    assert b"current_password" not in client.get("/account/password").data
    reply = client.post("/account/password", headers=client.origin, data={
        "csrf_token": client.csrf, "new_password": "a-brand-new-pass-77", "new_password_confirm": "a-brand-new-pass-77"})
    assert reply.status_code == 302
    assert accounts.authenticate("g@example.com", "a-brand-new-pass-77").ok


# --- Daily cap: one user's emails never go out from two workers at once

def test_a_second_worker_cannot_send_for_the_same_user_at_the_same_time(make_app, data):
    import db
    first, _ = data.create_send_job([make_app(company="A", email="a@a.com")])
    second, _ = data.create_send_job([make_app(company="B", email="b@b.com")])
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert db.claim_send_job(first, "worker-1", stale)
    assert not db.claim_send_job(second, "worker-2", stale)
    assert db.claim_send_job(second, "worker-1", stale)    # the same worker goes on in order


# --- Research: no invented names, real quotes, quotes that are about the area

SITE = ("Synthetic Culture Lab runs bacterial growth assays for teaching laboratories and "
        "measures their repeatability across sessions. ") * 3


def test_a_hook_naming_a_client_the_site_never_mentions_is_rejected():
    hook, why = ra.verify_hook("bacterial growth assays for Nexalume teaching laboratories",
                               "runs bacterial growth assays for teaching laboratories", SITE)
    assert hook == "" and "Nexalume" in why


def test_the_hook_evidence_must_be_quoted_word_for_word():
    hook, why = ra.verify_hook("bacterial growth assays for teaching laboratories",
                               "teaching laboratories growth assays bacterial measures", SITE)
    assert hook == "" and "word for word" in why
    hook, _ = ra.verify_hook("bacterial growth assays for teaching laboratories",
                             "runs bacterial growth assays for teaching laboratories", SITE)
    assert hook


def test_a_translation_that_adds_a_name_is_not_used():
    assert ra.unsupported_names("clinical cancer diagnostics for Nexalume hospitals",
                                "tests de croissance bactérienne", "site") == ["Nexalume"]


def test_a_quote_unrelated_to_the_area_does_not_match_it():
    site = "Meadow Goods makes woven baskets for Sunday picnics and delivers them. " * 3
    areas = [{"id": "application_security", "label": "Application security",
              "keywords": ["owasp", "secure code review"], "keywords_fr": []}]
    chosen, notes = ra.resolve_areas(["application_security"], site, areas, "en",
                                     {"application_security": "woven baskets for Sunday picnics"})
    assert chosen == [] and "isn't about this area" in notes[0]


# --- Wording follows the dates card

def _facts(kind="apprenticeship", month=4, year=2028, duration=18):
    facts = copy.deepcopy(FACTS)
    facts["internship"] = {"kind": kind, "month": month, "year": year, "duration": duration, "open_to_hire": False}
    facts["start_date"] = {"en": profiles.start_date_text(month, year, "en"),
                           "fr": profiles.start_date_text(month, year, "fr")}
    facts["internship_ask"] = profiles.internship_ask(kind=kind, month=month, year=year, duration_months=duration,
                                                      degree_context=None, open_to_hire=False)
    facts["target_role"] = {"en": profiles.INTERNSHIP_KINDS[kind]["role_en"],
                            "fr": profiles.INTERNSHIP_KINDS[kind]["role_fr"]}
    return facts


def _draft(facts, style, lang, research=None):
    spec = email_templates.build_spec(facts, style, lang)
    return compose_email(spec, research or {"company_hook": "", "areas": []}, "Acme",
                         "Dear Team," if lang == "en" else "Madame, Monsieur,", "Jane Doe",
                         facts["target_role"][lang], lang)


def test_every_style_asks_for_what_the_dates_card_says():
    facts = _facts()
    for style in email_templates.TEMPLATES:
        for lang in ("en", "fr"):
            text = (lambda d: d["subject"] + d["body"])(_draft(facts, style, lang))
            assert "internship" not in text.lower() or style == "research_lab", (style, lang)
            assert " un stage" not in text, (style, lang)


def test_the_call_request_says_how_long():
    assert "18 months" in _draft(_facts(), "conversation", "en")["body"]
    assert "18 mois" in _draft(_facts(), "conversation", "fr")["body"]


def test_french_elides_before_a_vowel():
    for style in email_templates.TEMPLATES:
        draft = _draft(_facts(month=4), style, "fr")
        assert "de avril" not in draft["subject"] + draft["body"], style
    assert "d'avril" in profiles.internship_ask(kind="internship", month=4, year=2027, duration_months=None,
                                                degree_context=None, open_to_hire=False)["fr"]


def test_research_is_not_said_twice_in_the_subject():
    draft = _draft(_facts(kind="research"), "research_lab", "en")
    assert "Research Research" not in draft["subject"]
    assert "recherche en recherche" not in _draft(_facts(kind="research"), "research_lab", "fr")["subject"]


def test_the_research_style_does_not_claim_the_company_does_research():
    for lang in ("en", "fr"):
        body = _draft(_facts(kind="internship"), "research_lab", lang)["body"].lower()
        assert "research carried out at" not in body and "recherches menées" not in body


def test_hand_written_call_request_gets_the_new_date(client, with_profile):
    facts = dict(FACTS, start_date={"en": "February 2027", "fr": "février 2027"})
    profiles.save(with_profile, facts=facts)
    p = profiles.load(with_profile)
    spec = copy.deepcopy(p["spec_en"])
    spec["email"]["ask_text"] = "Could we talk about an internship from February 2027?"
    profiles.save(with_profile, spec_en=spec)
    _dates(client, month=9, year=2028)
    assert profiles.load(with_profile)["spec_en"]["email"]["ask_text"] == \
        "Could we talk about an internship from September 2028?"


# --- Confirm dialog: Enter on Cancel must not send

def test_enter_confirms_only_on_the_send_button():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "dashboard" / "static"
    for name in ("app.js", "detail.js"):
        source = (root / name).read_text(encoding="utf-8")
        assert 'if (e.key === "Enter") done(true)' not in source, name
        assert 'document.activeElement === $("confirm-ok")' in source, name


# --- Daily limit: say so before sending, not "Send failed" afterwards

def test_sending_past_todays_limit_is_refused_with_the_reason(client, make_app, data, user_id):
    from user_config import UserConfig
    import db
    UserConfig(user_id).set_many({"MAX_EMAILS_PER_DAY": "1"})
    UserConfig(user_id).save_cv("cv.pdf", b"%PDF-1.4 test")
    data.update_application(make_app(company="Done", email="d@d.com", status="sent"), sent_at=db.now())
    target = make_app(company="Next", email="n@n.com")
    reply = client.post("/api/send-job", headers=client.origin, json={"app_ids": [target]}).get_json()
    assert not reply["ok"] and "today's limit of 1" in reply["message"]
    assert data.get_application_by_id(target)["status"] == "ready"
