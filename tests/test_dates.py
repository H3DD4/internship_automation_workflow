"""Each user sets their own internship dates, separately from the CV
analysis — instant, no AI — and can bring their ready drafts up to date."""

import cache_store
import profiles
from test_templates_and_profiles import FACTS


def _dates(client, month=1, year=2027, duration="6", kind="end_of_study", hire="on"):
    form = {"csrf_token": client.csrf, "kind": kind, "start_month": str(month), "start_year": str(year),
            "duration": duration}
    if hire:
        form["open_to_hire"] = hire
    return client.post("/profile/dates", data=form, headers=client.origin)


def test_a_template_user_changes_their_dates(client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    assert _dates(client, month=3, year=2027).status_code == 302
    p = profiles.load(user_id)
    assert p["facts"]["start_date"] == {"en": "March 2027", "fr": "mars 2027"}
    assert "6 months starting March 2027" in p["facts"]["internship_ask"]["en"]
    assert "March 2027" in p["spec_en"]["email"]["subject"]
    assert p["spec_en"]["email"]["internship_ask"] == p["facts"]["internship_ask"]["en"]


def test_hand_written_wording_only_has_its_date_replaced(client, with_profile):
    facts = dict(FACTS, start_date={"en": "February 2027", "fr": "février 2027"})
    profiles.save(with_profile, facts=facts)
    p = profiles.load(with_profile)
    spec = dict(p["spec_en"]); spec["email"] = dict(spec["email"], subject="{target_role} from February 2027 — {topic}")
    profiles.save(with_profile, spec_en=spec)
    before = profiles.load(with_profile)["spec_en"]["email"]["intro_with_hook"]
    _dates(client, month=1, year=2027)
    after = profiles.load(with_profile)
    assert after["spec_en"]["email"]["subject"] == "{target_role} from January 2027 — {topic}"
    assert after["spec_en"]["email"]["intro_with_hook"] == before          # every other word kept
    assert after["mode"] == "custom"


def test_dates_are_per_user(client, user_id, make_user):
    profiles.apply_template(user_id, FACTS, "specialist")
    other = make_user("other@example.com")
    profiles.apply_template(other, FACTS, "specialist")
    _dates(client, month=9, year=2027)
    assert profiles.load(other)["facts"]["start_date"] == FACTS["start_date"]


def test_bad_dates_are_refused(client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    _dates(client, month=13, year=2027)
    assert profiles.load(user_id)["facts"]["start_date"] == FACTS["start_date"]


def test_ready_drafts_can_be_brought_up_to_date(client, user_id, make_app, data):
    profiles.apply_template(user_id, FACTS, "specialist")
    app_id = make_app(company="Acme", email="jobs@acme.com", status="ready", language="en",
                      subject="old subject", body="old body")
    sent = make_app(company="Sent", email="s@acme.com", status="sent", subject="kept", body="kept")
    cache_store.save_research(user_id, "jobs@acme.com", {"company_hook": "", "areas": [], "hook_status": "none offered"})
    _dates(client, month=5, year=2027)
    reply = client.post("/api/drafts/rebuild-ready", headers=client.origin, json={}).get_json()
    assert reply["ok"] and reply["updated"] == 1
    assert "May 2027" in data.get_application_by_id(app_id)["subject"]
    assert data.get_application_by_id(sent)["subject"] == "kept"


def test_the_profile_page_shows_the_dates_card(client, user_id, make_app):
    profiles.apply_template(user_id, FACTS, "specialist")
    make_app(company="Acme", email="jobs@acme.com", status="ready")
    page = client.get("/profile").data.decode()
    assert "Your internship dates" in page and "rebuild-ready-btn" in page
