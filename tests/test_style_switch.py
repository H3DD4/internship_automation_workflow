"""Switching the style of one email while reviewing it — same research, same
approved facts, instant — and the evidence each style shows."""

import pytest

import cache_store
import email_templates
import profiles
from test_templates_and_profiles import FACTS

RESEARCH = {"company_hook": "helping mid-sized companies raise capital", "areas": [],
            "hook_status": "verified", "site_language": "en"}


@pytest.fixture
def template_user(user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    return user_id


@pytest.fixture
def ready_app(make_app, template_user):
    app_id = make_app(company="Acme", email="jobs@acme.com", status="ready", language="en",
                      hook_status="verified", matched_extra_mentions="[]")
    cache_store.save_research(template_user, "jobs@acme.com", RESEARCH)
    return app_id


def _switch(client, app_id, style):
    return client.post(f"/api/style/{app_id}", headers=client.origin, json={"style": style})


def test_the_two_new_styles_are_short_and_end_on_a_question():
    for style in ("conversation", "spontaneous"):
        for lang in ("en", "fr"):
            spec = email_templates.build_spec(FACTS, style, lang)
            from agents.composer import compose_email
            draft = compose_email(spec, RESEARCH, "Acme", "Dear Team," if lang == "en" else "Madame, Monsieur,",
                                  "Jane Doe", spec["target_role"] or "Internship", lang)
            assert len(draft["body"].split()) <= 135, (style, lang)
            assert "?" in draft["body"], (style, lang)
            assert len(draft["subject"].split()) <= 9, (style, lang)


def test_every_style_cites_published_evidence_only():
    for choice in email_templates.template_choices("en"):
        assert choice["best_for"], choice["id"]
        for e in choice["evidence"]:
            assert e["url"].startswith("https://") and e["source"]


def test_switching_one_email_rewrites_it_in_that_style(client, ready_app, data):
    reply = _switch(client, ready_app, "conversation").get_json()
    assert reply["ok"]
    row = data.get_application_by_id(ready_app)
    assert row["template_id"] == "conversation" and "15 minutes" in row["body"]


def test_the_chosen_style_survives_a_rebuild_and_a_language_switch(client, ready_app, data):
    _switch(client, ready_app, "spontaneous")
    assert client.post(f"/api/regenerate/{ready_app}", headers=client.origin, json={}).get_json()["ok"]
    assert data.get_application_by_id(ready_app)["template_id"] == "spontaneous"
    assert client.post(f"/api/language/{ready_app}", headers=client.origin, json={"language": "fr"}).get_json()["ok"]
    row = data.get_application_by_id(ready_app)
    assert row["template_id"] == "spontaneous" and "Candidature spontanée" in row["subject"]


def test_switching_back_to_the_profile_style_clears_the_override(client, ready_app, data):
    _switch(client, ready_app, "concise")
    _switch(client, ready_app, "specialist")
    assert data.get_application_by_id(ready_app)["template_id"] is None


def test_an_unknown_style_is_refused(client, ready_app):
    assert _switch(client, ready_app, "made-up").status_code == 400


def test_a_sent_email_cannot_be_restyled(client, make_app, template_user):
    sent = make_app(company="Old", email="o@x.com", status="sent")
    assert not _switch(client, sent, "concise").get_json()["ok"]


def test_the_company_page_offers_the_switch_with_its_evidence(client, ready_app):
    page = client.get(f"/company/{ready_app}").data.decode()
    assert 'id="style-select"' in page and "Short call request" in page and "Jason Chen" in page


def test_hand_written_wording_users_can_switch_to_templates_and_back(client, with_profile, make_app, data):
    profiles.save(with_profile, facts=FACTS)
    app_id = make_app(company="Acme", email="jobs@acme.com", status="ready", language="en")
    cache_store.save_research(with_profile, "jobs@acme.com", RESEARCH)
    assert _switch(client, app_id, "conversation").get_json()["ok"]
    assert _switch(client, app_id, "custom").get_json()["ok"]
    assert data.get_application_by_id(app_id)["template_id"] is None


def test_a_rescan_keeps_the_chosen_style(client, ready_app, data):
    """Re-scanning redoes the research, not the user's choice of wording."""
    _switch(client, ready_app, "conversation")
    data.reset_for_rescan([ready_app])
    row = data.get_application_by_id(ready_app)
    assert row["template_id"] == "conversation" and row["status"] == "pending"


def test_a_rescanned_company_is_written_again_in_its_chosen_style(client, ready_app, data, template_user):
    import drafting
    from user_config import UserConfig
    _switch(client, ready_app, "conversation")
    data.reset_for_rescan([ready_app])
    app = data.get_application_by_id(ready_app)
    draft = drafting.compose_for(drafting.load_config(template_user, UserConfig(template_user)), app,
                                 {"company_hook": "", "areas": []})
    assert "15 minutes" in draft["body"]


def test_the_profile_page_lists_the_proven_rules(client, template_user):
    page = client.get("/profile").data.decode()
    assert "What the data says works" in page and "Backlinko" in page and "Best for:" in page
