"""Matching a company to the CV, and what research keeps when models are busy.

Seen on a real list: the model picked the right areas for "Klart AI"
(autonomous AI employees) and a Dutch cybersecurity consultancy, but each
area's short English/French keyword list found nothing on the page, so the
picks were dropped and the companies were filed as "no CV match".
"""

from agents import research_agent as ra

AREAS = [
    {"id": "agentic_ai", "keywords": ["llm", "agentic", "ai agent"], "keywords_fr": []},
    {"id": "offensive_security", "keywords": ["pentest", "red team"], "keywords_fr": []},
]
KLART = ("Klart AI provides enterprise-grade autonomous AI employees to automate repetitive "
         "workflows across operations, finance and support. ") * 3


def test_a_pick_backed_by_the_sites_own_words_is_kept():
    chosen, notes = ra.resolve_areas(["agentic_ai"], KLART, AREAS, "en",
                                     {"agentic_ai": "autonomous AI employees to automate repetitive workflows"})
    assert chosen == ["agentic_ai"] and "site's own words" in notes[0]


def test_a_quote_that_is_not_on_the_site_does_not_count():
    """Still no invention: the evidence must be on the page, word for word."""
    chosen, _ = ra.resolve_areas(["offensive_security"], KLART, AREAS, "en",
                                 {"offensive_security": "world-class penetration testing for banks"})
    assert chosen == []


def test_the_quote_works_in_any_language():
    site = "BA is een consultancy bedrijf voor cybersecurity: wij testen de beveiliging van netwerken. " * 3
    chosen, _ = ra.resolve_areas(["offensive_security"], site, AREAS, "other",
                                 {"offensive_security": "wij testen de beveiliging van netwerken"})
    assert chosen == ["offensive_security"]


def test_keywords_still_confirm_a_pick_without_a_quote():
    chosen, _ = ra.resolve_areas(["offensive_security"], "We run red team exercises. " * 3, AREAS, "en")
    assert chosen == ["offensive_security"]


def test_quote_matching_ignores_case_spacing_and_dash_style():
    assert ra.quote_on_site("Connect-i develops  Opigno", "connect‑i DEVELOPS opigno enterprise")
    assert not ra.quote_on_site("AI", "AI everywhere")            # too short to mean anything


def test_the_prompt_asks_for_evidence_per_area():
    prompt = ra.RESEARCH_SYSTEM_PROMPT_TEMPLATE.format(areas_block="- x: y")
    assert '"area_evidence"' in prompt and "exactly as written" in prompt


def test_a_hook_nobody_could_translate_still_serves_the_french_email(monkeypatch):
    """No translator free: the English email uses standard wording, but the
    verified French phrase is kept for the French email instead of lost."""
    import language
    site = ("Hortis rassemble aujourd'hui une équipe de plus de soixante collaborateurs et a construit "
            "par expérience une offre d'accompagnement globale de ses clients. ") * 3
    monkeypatch.setattr(ra, "fetch_website_text", lambda url: site)
    monkeypatch.setattr(ra, "ask_json", lambda *a, **k: (
        {"areas": [], "hook": "offre d'accompagnement globale de vos clients",
         "hook_evidence": "a construit par expérience une offre d'accompagnement globale de ses clients",
         "industry": "", "summary": "", "organisation": "Hortis"}, "m"))
    monkeypatch.setattr(ra, "translate_hook", lambda *a, **k: (
        "", "dropped: no translation model was available — standard wording"))
    context = ra.get_company_context(None, "m", "HORTIS SA", "https://hortis.ch", [])
    assert context["company_hook"] == ""
    assert "accompagnement" in context["hook_original"]
    assert "accompagnement" in language.research_for_language(context, "fr")["company_hook"]


def test_a_site_that_could_not_be_read_is_not_filed_as_no_match(client, make_app):
    make_app(company="Unread", email="a@unread.com", status="ready",
             hook_status="no website text", matched_extra_mentions="[]")
    data = client.get("/api/overview").get_json()
    assert "Unread" in data["rows_html"] and "Not checked" in data["rows_html"]
    assert data["no_match_count"] == 0
