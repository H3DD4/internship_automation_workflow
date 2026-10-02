"""A student writes their own template — from an email they paste, or in the
section editor — and it works like every other style."""

import json

import cache_store
import email_templates
import profiles
from agents.composer import compose_email
from test_dates import _dates
from test_templates_and_profiles import FACTS


def _own(**changes):
    own = profiles.blank_own_template()
    own["layout"] = ["intro", "match", "about", "strengths", "ask", "closing"]
    own["en"]["about"] = "Outside class I run the IHEC Finance Club."
    own["fr"]["about"] = "En dehors des cours, je préside le club Finance de l'IHEC."
    for lang, values in changes.items():
        own[lang].update(values)
    return own


def _draft(own, lang="en", hook="AI for logistics"):
    spec = email_templates.build_spec(FACTS, "own", lang, own=own)
    return compose_email(spec, {"company_hook": hook, "areas": [spec["areas"][0]["id"]]}, "Acme",
                         "Dear Team," if lang == "en" else "Bonjour,", "Jane Doe", "Internship", lang)


def test_the_blanks_are_filled_for_each_company_in_both_languages():
    en, fr = _draft(_own(), "en"), _draft(_own(), "fr")
    assert "Acme" in en["body"] and "AI for logistics" in en["body"] and "Outside class I run" in en["body"]
    assert "KPMG" in en["body"]                                    # the CV evidence after the match lead
    assert "February 2027" in en["subject"] and en["subject"][0].isupper()
    assert "En dehors des cours" in fr["body"] and "février 2027" in fr["subject"]


def test_without_a_hook_the_second_opening_is_used():
    body = _draft(_own(), "en", hook="")["body"]
    assert "about a possible internship" in body and "your work on" not in body


def test_the_sections_follow_the_students_order():
    own = _own()
    own["layout"] = ["intro", "about", "closing"]
    body = _draft(own)["body"]
    assert body.index("Outside class") < body.index("My CV is attached") and "KPMG" not in body


def test_unknown_or_misplaced_blanks_are_explained():
    bad = _own(en={"closing": "Thanks [boss] — [their work]", "intro_standard": "Hi [field]"})
    problems = " ".join(email_templates.own_template_problems(bad))
    assert "[boss]" in problems and "can't be used in the closing" in problems
    assert "can't be used in the second opening" in problems
    nointro = _own(); nointro["layout"] = ["match", "closing"]
    assert "Keep the opening section." in email_templates.own_template_problems(nointro)


def test_curly_braces_are_explained_and_cannot_break_the_composer():
    own = _own(en={"about": "I love {curly} braces and {company}."})
    assert any("curly braces" in p for p in email_templates.own_template_problems(own))
    spec = email_templates.build_spec(FACTS, "own", "en", own=own)
    assert "{{curly}}" in spec["email"]["about_text"]          # escaped, never a placeholder


def test_a_pasted_email_becomes_a_template(client, user_id, monkeypatch):
    profiles.apply_template(user_id, FACTS, "specialist")

    class Router:
        deployments = [object()]

        def complete(self, **kwargs):
            assert "EMAIL:" in kwargs["messages"][0]["content"]
            answer = {"layout": ["intro", "match", "closing"], "highlights": 1,
                      "en": {"subject": "Internship — [field]", "intro_with_hook": "Your work on [their work] caught my eye.",
                             "intro_standard": "I'd love to join [company].", "match_lead": "I work in [field].",
                             "closing": "Thanks!", "sign_off": "Best,\n[my name]", "unexpected": "dropped"},
                      "fr": {"subject": "Stage — [field]", "intro_with_hook": "Votre travail sur [their work] m'intéresse.",
                             "intro_standard": "J'aimerais rejoindre [company].", "match_lead": "Je travaille en [field].",
                             "closing": "Merci !", "sign_off": "Cordialement,\n[my name]"}}
            return type("R", (), {"text": json.dumps(answer)})()

    monkeypatch.setattr("model_router.build_router", lambda *a, **k: Router())
    example = "Dear team, " + "I am a student who would love to work with you on real problems. " * 4
    reply = client.post("/api/own-template/from-example", headers=client.origin, json={"example": example}).get_json()
    assert reply["ok"] and reply["template"]["layout"] == ["intro", "match", "closing"]
    assert "unexpected" not in reply["template"]["en"] and reply["problems"] == []


def test_a_too_short_example_is_refused(client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    reply = client.post("/api/own-template/from-example", headers=client.origin, json={"example": "Hi there"}).get_json()
    assert not reply["ok"]


def test_preview_save_and_use_it_as_the_profile_style(client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    preview = client.post("/api/own-template/preview", headers=client.origin, json={"template": _own()}).get_json()
    assert preview["ok"] and "Outside class" in preview["preview"]["en"]["body"] and preview["preview"]["fr"]["subject"]
    saved = client.post("/api/own-template/save", headers=client.origin, json={"template": _own(), "use": True}).get_json()
    assert saved["ok"]
    p = profiles.load(user_id)
    assert p["template_id"] == "own" and p["own_template"]["en"]["about"].startswith("Outside class")
    assert "Your template" in client.get("/profile").data.decode()


def test_an_invented_number_in_your_own_words_is_refused(client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    reply = client.post("/api/own-template/save", headers=client.origin,
                        json={"template": _own(en={"about": "I managed 4321 clients."})}).get_json()
    assert not reply["ok"] and "4321" in reply["message"]
    assert profiles.load(user_id).get("own_template") is None


def test_one_email_can_be_switched_to_your_template(client, user_id, make_app, data):
    profiles.apply_template(user_id, FACTS, "specialist")
    profiles.save(user_id, own_template=_own())
    app_id = make_app(company="Acme", email="jobs@acme.com", status="ready", language="en")
    cache_store.save_research(user_id, "jobs@acme.com", {"company_hook": "AI for logistics", "areas": [],
                                                         "hook_status": "verified", "site_language": "en"})
    assert client.post(f"/api/style/{app_id}", headers=client.origin, json={"style": "own"}).get_json()["ok"]
    row = data.get_application_by_id(app_id)
    assert row["template_id"] == "own" and "Outside class I run" in row["body"]


def test_your_dates_update_your_template(client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    client.post("/api/own-template/save", headers=client.origin, json={"template": _own(), "use": True})
    _dates(client, month=9, year=2028)
    assert "September 2028" in profiles.load(user_id)["spec_en"]["email"]["subject"]
