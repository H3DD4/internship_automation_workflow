"""Email styles in English and French, the CV-grounded profile, languages."""

import re

import pytest

import email_templates
import language
import profiles
from agents.composer import compose_email
from utils import build_greeting

# A profile from a completely different discipline than the original account:
# the templates must work for anyone.
FINANCE_CV = """Lina Ben Salah — Master's student in Corporate Finance, IHEC Carthage (Tunis)
Experience: Analyst intern, KPMG Tunisia (2025): built a valuation model for 12 SMEs.
Project: Credit-risk scoring model on 40,000 loans, AUC 0.87.
President of the IHEC Finance Club. Winner of the 2024 CFA Research Challenge (national round)."""

FACTS = profiles.normalize_facts({
    "full_name": "Lina Ben Salah",
    "identity": {"en": "a Master's student in Corporate Finance at IHEC Carthage in Tunis",
                 "fr": "en Master de finance d'entreprise à l'IHEC Carthage (Tunis)"},
    "default_topic": {"en": "Corporate Finance", "fr": "Finance d'entreprise"},
    "motivation": {"en": "I enjoy turning messy financial data into decisions.",
                   "fr": "J'aime transformer des données financières complexes en décisions."},
    "internship_ask": profiles.internship_ask(kind="end_of_study", month=2, year=2027, duration_months=6,
                                              degree_context=None, open_to_hire=True),
    "start_date": {"en": "February 2027", "fr": "février 2027"},
    "target_role": {"en": "End-of-Study Internship", "fr": "Stage de fin d'études"},
    "areas": [
        {"id": "valuation", "label": {"en": "company valuation", "fr": "évaluation d'entreprises"},
         "topic": {"en": "Valuation", "fr": "Évaluation"}, "match_description": "M&A, valuation, advisory",
         "keywords": ["valuation", "m&a"], "keywords_fr": ["évaluation", "fusions"],
         "evidence": {"en": "At KPMG Tunisia I built a valuation model for 12 SMEs.",
                      "fr": "Chez KPMG Tunisia, j'ai construit un modèle d'évaluation pour 12 PME."},
         "sources": ["kpmg"]},
        {"id": "credit_risk", "label": {"en": "credit risk", "fr": "risque de crédit"},
         "topic": {"en": "Credit Risk", "fr": "Risque de crédit"}, "match_description": "banks, lending",
         "keywords": ["credit risk"], "keywords_fr": ["risque de crédit"],
         "evidence": {"en": "I built a credit-risk scoring model on 40,000 loans with an AUC of 0.87.",
                      "fr": "J'ai construit un modèle de scoring du risque de crédit sur 40 000 prêts, avec un AUC de 0,87."},
         "sources": ["scoring"]},
    ],
    "strengths": [
        {"id": "cfa", "text": {"en": "I won the national round of the 2024 CFA Research Challenge.",
                               "fr": "J'ai remporté la manche nationale du CFA Research Challenge 2024."},
         "sources": ["cfa"]},
        {"id": "club", "text": {"en": "As President of the IHEC Finance Club I organise events for 200 members.",
                                "fr": "À la présidence du club Finance de l'IHEC, j'organise des événements."},
         "sources": ["club"]},
    ],
})


@pytest.mark.parametrize("template_id", list(email_templates.TEMPLATES))
@pytest.mark.parametrize("lang", ["en", "fr"])
@pytest.mark.parametrize("hook", ["", "helping mid-sized companies raise capital"])
def test_every_style_builds_a_valid_email_in_both_languages(template_id, lang, hook):
    spec = email_templates.build_spec(FACTS, template_id, lang)
    research = {"company_hook": hook, "hook_original": "l'accompagnement des PME dans leurs levées de fonds" if hook else "",
                "areas": ["valuation"]}
    research = language.research_for_language(research, lang)
    draft = compose_email(spec, research, "Atlas Capital", build_greeting("", "Atlas Capital", lang),
                          "Lina Ben Salah", spec["target_role"], lang)
    body = draft["body"]
    assert "<<" not in body and "{" not in body and "{" not in draft["subject"]
    assert body.rstrip().endswith("Lina Ben Salah")
    assert "Atlas Capital" in body
    if lang == "fr":
        assert body.startswith("Madame, Monsieur,")
        assert "Dear" not in body and "Best regards" not in body
    else:
        assert body.startswith("Dear Atlas Capital Team,")


FRENCH_GENDERED = re.compile(r"\b(ravie?|heureuse?|motivée?|étudiante?|passionnée?|convaincue?|prête?)\b",
                             re.IGNORECASE)


@pytest.mark.parametrize("template_id", list(email_templates.TEMPLATES))
def test_french_template_wording_never_assumes_a_gender(template_id):
    wording = email_templates.TEMPLATES[template_id]["fr"]
    text = " ".join(v if isinstance(v, str) else " ".join(v) for v in wording.values())
    assert not FRENCH_GENDERED.search(text), FRENCH_GENDERED.search(text)


def test_the_original_accounts_french_wording_is_gender_neutral_too(spec_fr):
    email = spec_fr["email"]
    text = " ".join(v if isinstance(v, str) else " ".join(v) for v in email.values() if isinstance(v, (str, list)))
    text += " ".join(a["evidence"] for a in spec_fr["areas"]) + " ".join(s["text"] for s in spec_fr["strengths"])
    assert not FRENCH_GENDERED.search(text), FRENCH_GENDERED.search(text)


def test_braces_in_a_cv_cannot_become_format_fields():
    facts = dict(FACTS, identity={"en": "a student of {company.__class__} and C{++}", "fr": "en {x}"})
    spec = email_templates.build_spec(facts, "specialist", "en")
    draft = compose_email(spec, {"areas": []}, "Acme", "Dear Acme Team,", "Lina", "Internship")
    assert "{company.__class__}" in draft["body"] or "company.__class__" in draft["body"]
    assert "<class" not in draft["body"]


def test_only_numbers_from_the_approved_profile_are_allowed():
    spec = email_templates.build_spec(FACTS, "specialist", "en")
    assert {"12", "40000", "0.87", "2024", "2027"} <= set(spec["verified_facts"]["allowed_numbers"])


def test_the_internship_request_is_built_from_answers_not_ai():
    ask = profiles.internship_ask(kind="summer", month=7, year=2026, duration_months=2,
                                  degree_context=None, open_to_hire=False)
    assert ask["en"] == "I'm looking for a summer internship for 2 months starting July 2026."
    assert ask["fr"] == "Je recherche un stage d'été d'une durée de 2 mois à partir de juillet 2026."


def test_claims_not_in_the_cv_are_flagged():
    facts = profiles.normalize_facts(FACTS)
    facts["strengths"][0]["text"]["en"] = "I led 45 analysts at Goldman Sachs."
    flags = profiles.check_profile(facts, FINANCE_CV)
    flagged = {f["key"]: f["reason"] for f in flags}
    assert "strength.cfa.en" in flagged
    assert "45" in flagged["strength.cfa.en"] and "Goldman" in flagged["strength.cfa.en"]
    # Sentences backed by the CV aren't flagged.
    assert "area.valuation.evidence.en" not in flagged


def test_a_flag_can_be_confirmed_by_the_user():
    facts = profiles.normalize_facts(FACTS)
    facts["strengths"][0]["text"]["en"] = "I led 45 analysts at Goldman Sachs."
    facts["confirmed"] = ["strength.cfa.en"]
    assert "strength.cfa.en" not in {f["key"] for f in profiles.check_profile(facts, FINANCE_CV)}


def test_normalize_drops_unknown_fields_and_caps_sizes():
    raw = {"identity": {"en": "x", "fr": "y"}, "evil": "<script>", "areas": [{"id": "A B!"}] * 20}
    facts = profiles.normalize_facts(raw)
    assert "evil" not in facts
    assert len(facts["areas"]) == profiles.MAX_AREAS
    assert facts["areas"][0]["id"] == "a_b"


# ---------------------------------------------------------------------------
# Language decisions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("override,mode,site,email,expected", [
    ("fr", "en", "en", "a@b.com", "fr"),          # the per-company switch wins
    (None, "en", "fr", "a@b.fr", "en"),           # then "always English"
    (None, "auto", "fr", "a@b.com", "fr"),        # then the site's language
    (None, "auto", "en", "a@b.fr", "en"),
    (None, "auto", "other", "a@b.de", "en"),      # German site -> English
    (None, "auto", "", "rh@acme.fr", "fr"),       # unreadable site, .fr domain
    (None, "auto", "", "rh@acme.com", "en"),
])
def test_language_choice(override, mode, site, email, expected):
    assert language.choose_language(override=override, default_mode=mode, context={"site_language": site},
                                    email=email, website="") == expected


def test_french_is_never_chosen_without_french_wording():
    assert language.choose_language(override="fr", default_mode="auto", context={"site_language": "fr"},
                                    email="a@b.fr", website="", available=("en",)) == "en"


def test_a_french_email_uses_the_hook_as_verified_on_the_french_site():
    research = {"company_hook": "continuous penetration testing for SMEs",
                "hook_original": "des tests d'intrusion continus pour nos clients PME"}
    assert language.research_for_language(research, "fr")["company_hook"] == \
        "des tests d'intrusion continus pour vos clients PME"
    assert language.research_for_language(research, "en")["company_hook"] == research["company_hook"]


def test_an_english_only_hook_is_left_out_of_a_french_email():
    research = {"company_hook": "continuous penetration testing for SMEs", "hook_original": ""}
    assert language.research_for_language(research, "fr")["company_hook"] == ""


def test_french_elision():
    assert language.french_de("Airbus") == "d'Airbus"
    assert language.french_de("Talan") == "de Talan"


# ---------------------------------------------------------------------------
# The profile pages
# ---------------------------------------------------------------------------

def test_profile_save_refuses_unconfirmed_invented_claims(client, user_id):
    profiles.save(user_id, cv_text=FINANCE_CV, facts=FACTS)
    facts = profiles.normalize_facts(FACTS)
    facts["strengths"][0]["text"]["en"] = "I led 45 analysts at Goldman Sachs."
    result = client.post("/api/profile/save", json={"facts": facts}, headers=client.origin).get_json()
    assert result["ok"] is False and result["flags"]
    assert not profiles.load(user_id).get("spec_en")

    facts["confirmed"] = [f["key"] for f in result["flags"]]
    result = client.post("/api/profile/save", json={"facts": facts, "template_id": "concise"},
                         headers=client.origin).get_json()
    assert result["ok"], result
    saved = profiles.load(user_id)
    assert saved["template_id"] == "concise" and saved["spec_fr"]["email"]["sign_off"].startswith("Cordialement")


def test_switching_style_and_language(client, user_id):
    profiles.save(user_id, cv_text=FINANCE_CV, facts=FACTS)
    profiles.apply_template(user_id, FACTS, "specialist")
    result = client.post("/api/profile/settings", json={"template_id": "formal", "language_mode": "fr"},
                         headers=client.origin).get_json()
    assert result["ok"]
    saved = profiles.load(user_id)
    assert saved["template_id"] == "formal" and saved["language_mode"] == "fr"
    assert saved["spec_en"]["email"]["sign_off"].startswith("Kind regards")


def test_preview_renders_both_languages(client, user_id):
    profiles.save(user_id, cv_text=FINANCE_CV, facts=FACTS)
    result = client.post("/api/profile/preview", json={"template_id": "project_led"},
                         headers=client.origin).get_json()
    assert result["ok"]
    assert result["preview"]["en"]["body"].startswith("Dear ")
    assert result["preview"]["fr"]["body"].startswith("Madame, Monsieur,")
    # "Project first" opens with the flagship strength.
    assert result["preview"]["en"]["body"].split("\n\n")[1].startswith("I won the national round")


def test_the_original_hand_written_wording_is_kept_and_previewable(client, with_profile):
    page = client.get("/profile").data.decode()
    assert "Your own wording" in page
    result = client.post("/api/profile/preview", json={"template_id": "custom"}, headers=client.origin).get_json()
    assert result["ok"] and "ENSIT" in result["preview"]["en"]["body"]
    assert "Je suis en dernière année" in result["preview"]["fr"]["body"]


def test_invalid_custom_wording_is_refused(client, with_profile):
    result = client.post("/api/profile/custom-spec", json={"spec_en": "{\"email\": {}}"},
                         headers=client.origin).get_json()
    assert result["ok"] is False
    assert profiles.load(with_profile)["spec_en"]["verified_facts"]


def test_cv_text_extraction_from_docx():
    import io
    import zipfile
    buffer = io.BytesIO()
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in FINANCE_CV.split("\n") * 3)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    text = profiles.extract_cv_text("cv.docx", buffer.getvalue())
    assert "KPMG Tunisia" in text


def test_an_ai_profile_draft_is_normalised(monkeypatch):
    import json

    class FakeRouter:
        def complete(self, **kwargs):
            return type("R", (), {"text": json.dumps({
                "identity": {"en": "a student", "fr": "en master"},
                "areas": [{"id": "Valuation!!", "label": {"en": "valuation", "fr": "évaluation"},
                           "evidence": {"en": "I valued 12 SMEs.", "fr": "J'ai évalué 12 PME."}}],
                "strengths": [{"id": "cfa", "text": {"en": "I won a prize.", "fr": "J'ai gagné un prix."}}],
            })})()
    facts = profiles.draft_profile_with_ai(FakeRouter(), FINANCE_CV)
    assert facts["areas"][0]["id"] == "valuation"
    assert facts["strengths"][0]["text"]["fr"] == "J'ai gagné un prix."
