"""Composition, verification, and the guard.

Covers the three promises the email pipeline makes:
  1. A company with no usable website data gets the standard email — a plain
     interest line with its name, and no claim about its work.
  2. A company with data gets an interest line built from its own words and
     a match paragraph tied to the right parts of the CV.
  3. Nothing the model says is used unless the site backs it up.
"""

import json
import re
from pathlib import Path

import pytest

from agents.composer import compose_email
from agents.draft_guard import GuardRejection, check_draft
from agents.research_agent import resolve_areas, verify_hook

ROOT = Path(__file__).parent.parent
SPEC = json.loads((ROOT / "specializations.json").read_text(encoding="utf-8"))
AREAS = SPEC["areas"]
NAME = "Mohamed Hedda"
ROLE = "End-of-Study Internship"

SOC_SITE = ("Northwatch Security provides managed detection and response for mid-sized "
            "businesses. Our analysts monitor your environment around the clock from our "
            "security operations centre, correlating SIEM alerts and leading incident response "
            "when a threat is confirmed. We also run threat hunting across endpoints.")


def compose(company, research=None):
    return compose_email(SPEC, research or {}, company, f"Dear {company} Team,", NAME, ROLE)


# ---------------------------------------------------------------------------
# 1. No data -> standard email
# ---------------------------------------------------------------------------

def test_no_research_gives_the_standard_email():
    draft = compose("Acme Corp")
    body = draft["body"]
    assert body.startswith("Dear Acme Corp Team,")
    assert "Acme Corp" in body.split("\n\n")[1], "the interest line must name the company"
    assert "interested" in body.lower() or "like to join" in body.lower()
    assert "your work on" not in body and "Your focus on" not in body, \
        "without data the email must not claim anything about their work"
    assert "February 2027" in body and "Bac+5" in body
    assert "CV is attached" in body
    assert body.rstrip().endswith(NAME)


def test_standard_email_still_shows_the_strongest_cv_points():
    body = compose("Acme Corp")["body"]
    assert "RedBox" in body and "60+" in body
    assert "Forvis Mazars" in body and "Securinets" in body


def test_standard_emails_vary_between_companies_but_are_stable_per_company():
    bodies = {compose(f"Company {i}")["body"].split("\n\n")[1] for i in range(12)}
    assert len(bodies) > 1, "identical bodies at volume look like bulk mail"
    assert compose("Stable Co")["body"] == compose("Stable Co")["body"]


# ---------------------------------------------------------------------------
# 2. Data -> tailored email
# ---------------------------------------------------------------------------

def test_verified_hook_and_areas_produce_a_tailored_email():
    research = {"company_hook": "managed detection and response for mid-sized businesses",
                "hook_evidence": "provides managed detection and response for mid-sized businesses",
                "areas": ["soc_blue_team", "agentic_ai"]}
    draft = compose("Northwatch Security", research)
    body = draft["body"]
    assert "What interests me most about Northwatch Security is your work on " \
           "managed detection and response for mid-sized businesses." in body
    assert "Your focus on security operations and threat detection and agentic AI" in body
    assert "97%" in body, "the SOC evidence from TDS Global should be used"
    assert "Security Operations" in draft["subject"]


def test_a_project_is_never_cited_twice():
    """offensive_security's evidence is RedBox, so the general strengths must
    not repeat RedBox."""
    body = compose("Breakpoint", {"areas": ["offensive_security"]})["body"]
    assert body.count("RedBox") == 1


def test_two_areas_sharing_a_source_fall_back_to_alternative_evidence():
    # Both SOC and ML evidence come from the TDS Global internship.
    body = compose("DataSec", {"areas": ["soc_blue_team", "machine_learning"]})["body"]
    assert body.count("97%") == 1
    assert "NVIDIA" in body


def test_no_tool_names_in_any_email():
    """The email talks about what was built; the CV lists the tools."""
    tools = ["burp", "ghidra", "splunk", "ida pro", "x64dbg", "wireshark", "suricata",
             "snort", "nmap", "sqlmap", "caido", "frida", "jadx", "qdrant", "langgraph",
             "fastapi", "react", "neo4j", "xgboost", "pytorch", "tensorflow"]
    for area_id in [a["id"] for a in AREAS] + [None]:
        body = compose("Tooltest", {"areas": [area_id]} if area_id else {})["body"].lower()
        for tool in tools:
            assert tool not in body, f"{tool!r} appears in the {area_id or 'standard'} email"


@pytest.mark.parametrize("area_id", [a["id"] for a in AREAS])
def test_every_area_composes_a_valid_email(area_id):
    draft = compose("Example Co", {"areas": [area_id]})
    words = len(draft["body"].split())
    assert 120 <= words <= 400, f"{area_id}: {words} words"


def test_every_number_in_the_spec_is_declared():
    """Adding a figure to specializations.json without declaring it would
    make the guard reject every email that uses it."""
    allowed = set(SPEC["verified_facts"]["allowed_numbers"])
    text = json.dumps({k: v for k, v in SPEC.items() if k != "verified_facts"})
    found = set(re.findall(r"\b\d[\d,.]*\b", text.replace("\\n", " ")))
    assert found <= allowed, f"undeclared numbers: {found - allowed}"


# ---------------------------------------------------------------------------
# 3. Verification of what the model returns
# ---------------------------------------------------------------------------

def test_hook_copied_from_the_site_is_accepted():
    hook, status = verify_hook("managed detection and response for mid-sized businesses",
                               "Northwatch Security provides managed detection and response "
                               "for mid-sized businesses.", SOC_SITE)
    assert status == "grounded"
    assert hook == "managed detection and response for mid-sized businesses"


@pytest.mark.parametrize("hook,reason", [
    ("award-winning quantum cryptography for global banks", "not on the site"),
    ("detection and response for over 500 enterprise clients", "invented number"),
    ("innovative solutions", "too short/vague"),
    ("our managed detection and response which I love", "first person"),
])
def test_hooks_the_site_does_not_support_are_rejected(hook, reason):
    result, status = verify_hook(hook, "managed detection and response for mid-sized businesses",
                                 SOC_SITE)
    assert result == "", f"{reason}: {status}"


def test_hook_with_invented_evidence_is_rejected():
    _, status = verify_hook("managed detection and response",
                            "We were named Gartner's leading provider in quantum security.",
                            SOC_SITE)
    assert "evidence" in status


def test_nothing_is_invented_for_an_empty_page():
    cookie_page = "We use cookies. Accept all. Sign in. Email. Password."
    assert verify_hook("security consulting services for enterprises",
                       "We use cookies.", cookie_page)[0] == ""
    assert resolve_areas(["offensive_security"], cookie_page, AREAS)[0] == []


def test_model_area_without_site_evidence_is_dropped():
    areas, notes = resolve_areas(["cloud"], SOC_SITE, AREAS)
    assert "cloud" not in areas
    assert any("dropped" in note for note in notes)


def test_keywords_rescue_areas_when_the_model_returns_nothing():
    areas, _ = resolve_areas([], SOC_SITE, AREAS)
    assert areas and areas[0] == "soc_blue_team"


def test_short_keywords_need_word_boundaries():
    """'soc' must not match 'social', nor 'rag' 'storage'."""
    text = "A social media storage company with great coverage."
    assert resolve_areas([], text, AREAS)[0] == []


def test_hook_capitalisation_keeps_proper_nouns():
    site = "We run managed Kubernetes platforms for startups. Continuous delivery for teams."
    assert verify_hook("Kubernetes platforms for startups",
                       "We run managed Kubernetes platforms for startups.", site)[0] \
        .startswith("Kubernetes")
    assert verify_hook("Continuous delivery for teams",
                       "Continuous delivery for teams.", site)[0].startswith("continuous")


# ---------------------------------------------------------------------------
# The guard itself (last line of defence)
# ---------------------------------------------------------------------------

def _guard(body, research=None):
    check_draft({"subject": "s", "body": body}, facts=SPEC["verified_facts"],
                research=research or {}, company_name="Acme", greeting="Dear Acme Team,",
                applicant_name=NAME)


BASE = compose("Acme")["body"]


def test_guard_accepts_a_composed_email():
    _guard(BASE)


def test_guard_rejects_invented_numbers():
    with pytest.raises(GuardRejection, match="'500'"):
        _guard(BASE.replace("Best regards", "You serve 500 clients.\n\nBest regards"))


def test_guard_rejects_an_altered_cv_figure():
    with pytest.raises(GuardRejection, match="'70'"):
        _guard(BASE.replace("60+", "70+"))


def test_guard_rejects_interest_claim_without_a_verified_hook():
    body = BASE.replace("\n\n", "\n\nWhat interests me most about Acme is your work on AI. ", 1)
    with pytest.raises(GuardRejection, match="verified phrase"):
        _guard(body)


def test_guard_rejects_leftover_placeholders():
    with pytest.raises(GuardRejection, match="placeholder"):
        _guard(BASE.replace("Acme", "{company}", 2))


def test_guard_rejects_flattery():
    with pytest.raises(GuardRejection, match="industry leader"):
        _guard(BASE.replace("Best regards", "You are the industry leader.\n\nBest regards"))
