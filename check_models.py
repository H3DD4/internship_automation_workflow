"""Discover and compare AI models for this pipeline.

Any OpenAI-compatible provider works (Groq, OpenCode, OpenRouter, Together,
a local Ollama…). The model's only job is research: pick which of your CV
areas fit a company, and copy one phrase describing what the company does
from its own site. This script measures exactly that on four fixed test
websites, including an empty cookie-banner page where the only correct
answer is to find nothing.

  python check_models.py --list              what models does my endpoint offer?
  python check_models.py                     test the model in my .env
  python check_models.py --all               compare every suggested model
  python check_models.py -m modelA,modelB    compare specific models
  python check_models.py --show              also print the emails it leads to

A model is scored on: right CV areas, a hook that survives verification
against the site, and — most important — inventing nothing when there's
nothing to find.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from ai_client import (  # noqa: E402
    PROVIDERS, CompatibleAIClient, RateLimiter, list_provider_models, resolve_ai_settings,
)
from agents import research_agent  # noqa: E402
from agents.composer import compose_email  # noqa: E402

# Fixed, fictional sites so every model faces the same test.
TEST_SITES = [
    {
        "name": "Northwatch Security",
        "expect_areas": {"soc_blue_team"},
        "expect_hook": True,
        "text": ("Northwatch Security provides managed detection and response for mid-sized "
                 "businesses. Our analysts monitor your environment around the clock from our "
                 "security operations centre, correlating SIEM alerts and leading incident "
                 "response when a threat is confirmed. We also run proactive threat hunting "
                 "across endpoints and cloud workloads, and publish threat intelligence reports "
                 "for our clients."),
    },
    {
        "name": "Lumen Agents",
        "expect_areas": {"agentic_ai"},
        "expect_hook": True,
        "text": ("Lumen Agents builds AI agents that automate back-office work for insurance "
                 "companies. Our multi-agent platform combines large language models with "
                 "retrieval-augmented generation over each client's documents, so claims are "
                 "triaged and summarised in minutes. Every agent's output is traced and reviewed "
                 "before it reaches a customer."),
    },
    {
        "name": "Breakpoint Labs",
        "expect_areas": {"offensive_security"},
        "expect_hook": True,
        "text": ("Breakpoint Labs is a penetration testing firm. We run web and mobile "
                 "application security assessments, red team engagements that emulate real "
                 "adversaries, and continuous vulnerability testing for SaaS companies. Each "
                 "engagement ends with a remediation workshop with your developers."),
    },
    {
        "name": "Portal Login",
        "expect_areas": set(),
        "expect_hook": False,
        "text": ("We use cookies to improve your experience. Accept all. Manage preferences. "
                 "Sign in. Email. Password. Forgot your password? Create account."),
    },
]


def list_models(base_url: str, api_key: str) -> list:
    models, error = list_provider_models(base_url, api_key)
    if error:
        print(f"  {error}")
    return models


def run_site(client, model: str, site: dict, areas: list) -> dict:
    """Research one test site with one model (no fallback, no network fetch)."""
    with patch.object(research_agent, "fetch_website_text", return_value=site["text"]), \
         patch.object(research_agent, "build_model_fallback_list", return_value=[model]), \
         patch("builtins.print"):
        started = time.time()
        context = research_agent.get_company_context(client, model, site["name"], "x", areas)
    context["seconds"] = time.time() - started

    problems = []
    got_areas = set(context["areas"])
    if site["expect_areas"] and not site["expect_areas"] & got_areas:
        problems.append(f"missed {'/'.join(sorted(site['expect_areas']))} (got {sorted(got_areas) or 'none'})")
    if not site["expect_areas"] and got_areas:
        problems.append(f"matched {sorted(got_areas)} on a page with nothing to match")

    status = context["hook_status"]
    if site["expect_hook"] and not context["company_hook"]:
        problems.append(f"no usable hook ({status})")
    if not site["expect_hook"] and status not in ("none offered", "model gave no usable answer"):
        problems.append(f"INVENTED a hook for an empty page ({status})")
    context["problems"] = problems
    return context


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list models the endpoint offers")
    parser.add_argument("--all", action="store_true", help="test every suggested model")
    parser.add_argument("-m", "--models", help="comma-separated models to test")
    parser.add_argument("--show", action="store_true", help="print the emails the best model produces")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=True)
    ai = resolve_ai_settings()
    api_key, base_url, configured = ai["api_key"], ai["base_url"], ai["model"]

    if not api_key:
        portal = PROVIDERS[ai["provider"]]["key_portal"]
        print(f"No API key for {ai['label']} in .env{f' (get one at {portal})' if portal else ''}.\n"
              f"Add it in the dashboard's setup form.")
        return 1

    print(f"provider : {ai['label']}")
    print(f"endpoint : {base_url}")
    print(f"key      : {api_key[:6]}…{api_key[-4:]}\n")

    available = list_models(base_url, api_key)
    if available:
        print(f"{len(available)} model(s) available:")
        for model in available:
            print(f"  {model}{'  <- configured' if model == configured else ''}")
    else:
        print("Could not list models from this endpoint (some providers don't expose /models).")
    print()
    if args.list:
        return 0

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.all:
        suggested = PROVIDERS[ai["provider"]]["suggested_models"]
        models = [m for m in suggested if not available or m in available] or available[:6]
    else:
        models = [configured]

    spec = json.loads((ROOT / "specializations.json").read_text())
    client = CompatibleAIClient(api_key, base_url, rate_limiter=RateLimiter(60))
    print(f"Researching {len(TEST_SITES)} test websites with each model "
          f"(the last one is an empty login page — the right answer there is 'nothing').\n")

    results = []
    for model in models:
        print(f"{model}")
        runs, seconds = [], 0.0
        for site in TEST_SITES:
            ctx = run_site(client, model, site, spec["areas"])
            runs.append((site, ctx))
            seconds += ctx["seconds"]
            mark = "ok " if not ctx["problems"] else "BAD"
            detail = ctx["company_hook"] or "—"
            print(f"   {mark} {site['name']:<20} areas={ctx['areas'] or '[]'}  hook={detail[:60]!r}")
            for problem in ctx["problems"]:
                print(f"         ✗ {problem}")
        failures = sum(len(ctx["problems"]) for _, ctx in runs)
        invented = any("INVENTED" in p for _, ctx in runs for p in ctx["problems"])
        results.append({"model": model, "failures": failures, "invented": invented,
                        "seconds": seconds, "runs": runs})
        print(f"   → {failures} problem(s), {seconds:.1f}s\n")

    safe = [r for r in results if not r["invented"]]
    print("=" * 64)
    if not safe:
        print("Every model invented something on the empty page. Don't use any of them\n"
              "as-is — or check the failures above for a wrong key or base URL (401/404).")
        return 1
    best = min(safe, key=lambda r: (r["failures"], r["seconds"]))
    print(f"RECOMMENDED: {best['model']}  ({best['failures']} problem(s), {best['seconds']:.1f}s)")
    print(f"Set it with:  AI_MODEL={best['model']}   (or pick it in the dashboard)")
    print("\nEven a weaker model is safe here: anything it can't back up with the site's own\n"
          "words is dropped, and the email falls back to its standard wording.")

    if args.show:
        for site, ctx in best["runs"]:
            draft = compose_email(spec, ctx, site["name"], f"Dear {site['name']} Team,",
                                  os.getenv("YOUR_NAME", "Mohamed Hedda"),
                                  os.getenv("YOUR_TARGET_ROLE", "End-of-Study Internship"))
            print("\n" + "-" * 64)
            print(f"Subject: {draft['subject']}\n\n{draft['body']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
