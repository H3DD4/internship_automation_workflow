"""The guard that stops an invented claim going out under your name."""

import json
from pathlib import Path

import pytest

from agents.draft_guard import GuardRejection, check_draft

SPEC = json.loads((Path(__file__).parent.parent / "specializations.json").read_text())
FACTS = SPEC["core_identity"]["verified_facts"]

RESEARCH = {
    "industry": "offensive security",
    "mission_or_focus": "Vulnerability management platform with a dedicated consultant.",
    "working_axes": ["continuous penetration testing through their own platform"],
    "evidence": ['"continuous penetration testing"'],
    "talking_points": ["Platform plus human expertise"],
}

GREETING = "Dear Rootshell Security Team,"

GOOD_BODY = f"""{GREETING}

I'm a final-year Computer Engineering student at ENSIT (Tunis), specialised in cybersecurity and agentic AI engineering, and I'm looking for an end-of-study internship starting February 2027.

What draws me to Rootshell Security specifically is how you've paired continuous penetration testing with a dedicated consultant guiding every client through remediation — it's exactly the platform-plus-expertise model I want to learn to build.

My GitHub holds 20+ projects, but the one that best proves my expertise is RedBox — an autonomous AI agent that finds vulnerabilities in live, deployed applications and solves CTF challenges. It has solved 60+ CTF challenges automatically and reported 3 real vulnerabilities found in production environments.

My interest in cybersecurity started from wanting to understand and break down black box systems, which is what first pulled me toward technology. I've competed in numerous CTFs, winning several national and international competitions, and built deep experience in reverse engineering, offensive security, log analysis, digital forensics, and SIEM tooling.

I interned at three companies where this came together in practice: at Forvis Mazars I built a RAG-based LLM assistant for pentesters; at Talan I contributed to AI-driven pipelines; and at TDS Global by Nomios I helped build an ML-powered SOC platform. My path into AI grew out of wanting to automate my own security methodologies.

My CV is attached with further detail. I'd be genuinely glad to bring this background to Rootshell Security and grow with the team — I look forward to hearing from you.

Best regards,
Mohamed Hedda"""


def guard(body, *, research=RESEARCH, greeting=GREETING, extras=None, subject="Application"):
    check_draft({"subject": subject, "body": body}, facts=FACTS, research=research,
                company_name="Rootshell Security", greeting=greeting,
                applicant_name="Mohamed Hedda", extra_sentences=extras)


def test_a_clean_draft_passes():
    guard(GOOD_BODY)


@pytest.mark.parametrize("invented,description", [
    ("With over 500 enterprise clients, you clearly lead the space.", "client count"),
    ("Since your Series B in 2019 you have grown fast.", "funding round and year"),
    ("Founded in 1998, your team of 250 engineers is impressive.", "founding date and headcount"),
])
def test_invented_numbers_are_rejected(invented, description):
    body = GOOD_BODY.replace("What draws me to", f"{invented} What draws me to")
    with pytest.raises(GuardRejection) as excinfo:
        guard(body)
    assert "does not appear" in str(excinfo.value), description


@pytest.mark.parametrize("phrase", [
    "industry leader", "award-winning", "world-class", "fast-growing",
    "I have long admired", "I hope this email finds you well",
    "I am writing to express my interest",
])
def test_unverifiable_flattery_and_filler_are_rejected(phrase):
    body = GOOD_BODY.replace("What draws me to", f"You are an {phrase}. What draws me to")
    with pytest.raises(GuardRejection):
        guard(body)


def test_altered_cv_numbers_are_rejected():
    """The exact failure mode that matters: a model 'improving' 60+ into 70+
    puts a claim in the email that the attached CV contradicts."""
    with pytest.raises(GuardRejection) as excinfo:
        guard(GOOD_BODY.replace("60+ CTF challenges", "70+ CTF challenges"))
    assert "'70'" in str(excinfo.value)


def test_numbers_quoted_from_the_research_are_allowed():
    research = dict(RESEARCH, working_axes=["a 24/7 SOC staffed by 40 analysts"])
    body = GOOD_BODY.replace(
        "What draws me to Rootshell Security specifically is how you've paired continuous penetration testing",
        "What draws me to Rootshell Security specifically is your 24/7 SOC staffed by 40 analysts, paired",
    )
    guard(body, research=research)


def test_wrong_greeting_is_rejected():
    with pytest.raises(GuardRejection) as excinfo:
        guard(GOOD_BODY.replace(GREETING, "Dear Hiring Manager,"))
    assert "first line" in str(excinfo.value).lower() or "dear hiring manager" in str(excinfo.value).lower()


def test_missing_signoff_is_rejected():
    with pytest.raises(GuardRejection):
        guard(GOOD_BODY.replace("Best regards,\nMohamed Hedda", "Best regards,"))


def test_leftover_placeholder_is_rejected():
    with pytest.raises(GuardRejection) as excinfo:
        guard(GOOD_BODY.replace("Rootshell Security specifically", "{company_name} specifically"))
    assert "placeholder" in str(excinfo.value).lower()


def test_too_short_is_rejected():
    with pytest.raises(GuardRejection) as excinfo:
        guard(f"{GREETING}\n\nPlease hire me.\n\nBest regards,\nMohamed Hedda")
    assert "words" in str(excinfo.value)


def test_dropped_extra_sentence_is_rejected():
    extra = "I also have hands-on experience with Docker and cloud deployment in production."
    with pytest.raises(GuardRejection) as excinfo:
        guard(GOOD_BODY, extras=[extra])
    assert "dropped" in str(excinfo.value).lower()


def test_company_paragraph_must_be_omitted_without_research():
    """With nothing found on the site, an enthusiastic paragraph about the
    company can only be invention."""
    empty = {"industry": "unknown", "working_axes": [], "evidence": [], "talking_points": []}
    with pytest.raises(GuardRejection) as excinfo:
        guard(GOOD_BODY, research=empty)
    assert "omitted" in str(excinfo.value).lower()


def test_draft_without_company_paragraph_passes_when_research_is_empty():
    empty = {"industry": "unknown", "working_axes": [], "evidence": [], "talking_points": []}
    body = "\n\n".join(p for p in GOOD_BODY.split("\n\n") if "draws me to" not in p)
    guard(body, research=empty)


def test_specializations_facts_match_the_cv():
    """Guards against the pitch drifting away from the attached CV again."""
    assert FACTS["ctf_challenges_solved_by_redbox"] == "60+"
    assert FACTS["school"] == "ENSIT"
    assert FACTS["production_vulnerabilities_reported"] == 3
    assert FACTS["internship_start"] == "February 2027"
    assert set(FACTS["employers"]) == {"Forvis Mazars", "Talan", "TDS Global by Nomios"}
