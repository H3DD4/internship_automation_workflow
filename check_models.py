"""Discover and compare AI models for this pipeline.

Any OpenAI-compatible provider works (Groq, OpenCode, OpenRouter, Together,
a local Ollama…) — the pipeline only ever speaks /chat/completions. What
actually matters is whether a given model can follow a long, rule-heavy
prompt without inventing facts, and that is a property of the model, not the
provider. This script measures exactly that.

  python check_models.py --list              what models does my endpoint offer?
  python check_models.py                     test the model in my .env
  python check_models.py --all               compare every suggested model
  python check_models.py -m modelA,modelB    compare specific models

Each tested model writes a real email for a fixed fake company, and the
result is scored by the same draft_guard.py the pipeline uses, plus a check
that the CV's facts survived intact. Pick the model that passes with the
lowest latency.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from ai_client import (  # noqa: E402
    DEFAULT_BASE_URL, DEFAULT_MODEL, KEY_PORTAL_URL, SUPPORTED_MODELS,
    CompatibleAIClient, RateLimiter,
)
from agents.draft_guard import GuardRejection, check_draft  # noqa: E402
from agents.writer_agent import generate_email  # noqa: E402
from utils import build_greeting  # noqa: E402

# A fixed company with deliberately *tempting* gaps: the research says nothing
# about size, funding, clients or awards, so any model that mentions them is
# inventing. Keeping it fixed makes model comparisons fair.
FAKE_COMPANY = "Rootshell Security"
FAKE_RESEARCH = {
    "industry": "offensive security / vulnerability management",
    "company_size_guess": "unknown",
    "mission_or_focus": ("They run a vulnerability management platform and pair every "
                          "client with a dedicated security consultant."),
    "tone_of_voice": "technical",
    "working_axes": [
        "continuous penetration testing delivered through their own platform",
        "threat intelligence feeds combined with AI-driven analysis",
        "a dedicated security consultant guiding each client from setup through remediation",
    ],
    "evidence": [
        "\"continuous penetration testing, managed in the Rootshell platform\"",
        "\"AI-driven analysis and threat intelligence\"",
        "\"a dedicated consultant supports you from onboarding to remediation\"",
    ],
    "talking_points": ["Platform plus human expertise", "Remediation-focused, not just scanning"],
    "notable_products_or_news": "none found",
    "matched_extra_mentions": ["ai_agents"],
    "match_reasons": {"ai_agents": "They describe AI-driven analysis in their platform."},
}


def load_spec() -> dict:
    with open(ROOT / "specializations.json") as f:
        return json.load(f)


def list_models(base_url: str, api_key: str) -> list:
    url = base_url.rstrip("/") + "/models"
    response = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    if response.status_code != 200:
        print(f"  {url} -> HTTP {response.status_code}: {response.text[:300]}")
        return []
    payload = response.json()
    items = payload.get("data", payload if isinstance(payload, list) else [])
    return sorted(str(item.get("id", item)) for item in items)


def score_draft(draft: dict, spec: dict, greeting: str, applicant_name: str) -> tuple:
    """Returns (list_of_problems, list_of_notes)."""
    problems, notes = [], []
    body = draft["body"]

    try:
        check_draft(draft, facts=spec["core_identity"].get("verified_facts", {}),
                    research=FAKE_RESEARCH, company_name=FAKE_COMPANY,
                    greeting=greeting, applicant_name=applicant_name,
                    extra_sentences=[])
    except GuardRejection as rejection:
        problems.append(str(rejection))

    lowered = body.lower()

    # The CV's facts must survive verbatim — a model that "improves" 60+ into
    # 70+ or ENSIT into a longer school name is worse than useless here.
    for needle, label in [
        ("ensit", "school (ENSIT)"),
        ("redbox", "flagship project (RedBox)"),
        ("60+", "CTF count (60+)"),
        ("february 2027", "start date (February 2027)"),
        ("forvis mazars", "employer (Forvis Mazars)"),
        ("talan", "employer (Talan)"),
        ("nomios", "employer (TDS Global by Nomios)"),
    ]:
        if needle not in lowered:
            problems.append(f"dropped or altered the {label}")

    # The company paragraph should exist and be grounded.
    if f"draws me to {FAKE_COMPANY.lower()}" not in lowered:
        notes.append("no 'what draws me to...' paragraph")
    grounded = any(term in lowered for term in
                    ("consultant", "penetration testing", "threat intelligence", "remediation"))
    if not grounded:
        problems.append("company paragraph cites nothing from the research")

    notes.append(f"{len(body.split())} words")
    return problems, notes


def test_model(model: str, client, spec: dict, applicant_name: str, target_role: str) -> dict:
    greeting = f"Dear {FAKE_COMPANY} Team,"
    started = time.time()
    try:
        draft = generate_email(
            client, model, spec["core_identity"], applicant_name,
            FAKE_RESEARCH, FAKE_COMPANY, spec["extra_mentions"], target_role, greeting,
            company_paragraph_rules=spec.get("company_paragraph"),
            allow_fallback=False,  # report on THIS model, not its stand-ins
        )
    except Exception as exc:
        return {"model": model, "ok": False, "seconds": time.time() - started,
                "problems": [f"failed to produce a usable draft: {str(exc)[:200]}"],
                "notes": [], "draft": None}

    seconds = time.time() - started
    problems, notes = score_draft(draft, spec, greeting, applicant_name)
    return {"model": model, "ok": not problems, "seconds": seconds,
            "problems": problems, "notes": notes, "draft": draft}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list models the endpoint offers")
    parser.add_argument("--all", action="store_true", help="test every suggested model")
    parser.add_argument("-m", "--models", help="comma-separated models to test")
    parser.add_argument("--show", action="store_true", help="print the winning email in full")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=True)
    api_key = (os.getenv("AI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip()
    base_url = (os.getenv("AI_BASE_URL") or DEFAULT_BASE_URL).strip()
    configured = (os.getenv("AI_MODEL") or DEFAULT_MODEL).strip()

    if not api_key:
        print(f"No AI_API_KEY in .env. Get a key ({KEY_PORTAL_URL} for Groq) and add it,\n"
              f"or set it in the dashboard's setup form.")
        return 1

    print(f"endpoint : {base_url}")
    print(f"key      : {api_key[:8]}…{api_key[-4:]}\n")

    available = list_models(base_url, api_key)
    if available:
        print(f"{len(available)} model(s) available:")
        for model in available:
            marker = "  <- configured" if model == configured else ""
            print(f"  {model}{marker}")
    else:
        print("Could not list models from this endpoint (some providers don't expose /models).")
    print()

    if args.list:
        return 0

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.all:
        models = [m for m in SUPPORTED_MODELS if not available or m in available]
        if not models:
            models = available[:6]
    else:
        models = [configured]

    unknown = [m for m in models if available and m not in available]
    if unknown:
        print(f"note: {', '.join(unknown)} not offered by this endpoint — testing anyway\n")

    spec = load_spec()
    applicant_name = os.getenv("YOUR_NAME", "Mohamed Hedda")
    target_role = os.getenv("YOUR_TARGET_ROLE", "End-of-Study Internship")
    client = CompatibleAIClient(api_key, base_url, rate_limiter=RateLimiter(60))

    print(f"Writing a test email for '{FAKE_COMPANY}' with each model.")
    print("Scored on: facts preserved, nothing invented, company paragraph grounded.\n")

    results = []
    for model in models:
        print(f"  {model} … ", end="", flush=True)
        result = test_model(model, client, spec, applicant_name, target_role)
        results.append(result)
        verdict = "PASS" if result["ok"] else "FAIL"
        print(f"{verdict}  ({result['seconds']:.1f}s, {', '.join(result['notes']) or '—'})")
        for problem in result["problems"]:
            print(f"      ✗ {problem[:190]}")

    passed = [r for r in results if r["ok"]]
    print("\n" + "=" * 60)
    if not passed:
        print("No model passed. Check the failures above — if they're all 401/404,\n"
              "the key or the base URL is wrong rather than the models.")
        return 1

    best = min(passed, key=lambda r: r["seconds"])
    print(f"RECOMMENDED: {best['model']}  ({best['seconds']:.1f}s)")
    print(f"\nSet it with:  AI_MODEL={best['model']}")
    print("or pick it in the dashboard's setup form.")

    if args.show:
        print("\n" + "-" * 60)
        print(f"Subject: {best['draft']['subject']}\n")
        print(best["draft"]["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
