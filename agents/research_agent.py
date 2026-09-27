"""
Agent 1: Research Agent
Scrapes a company's website (best-effort) and asks Claude to distill it
into a small structured JSON blob describing the company, PLUS which of the
user's optional "extra mention" add-ons (cloud, software_dev, data, etc.)
genuinely fit this company. Zero, one, or several can match — there's no
forced single pick, and no match at all is a valid, common outcome.

This JSON is what Agent 2 (writer_agent) uses to personalize the email:
the user's core identity (agentic AI + cybersecurity) stays fixed and is
NOT driven by this file — only the small add-on mentions are.
"""

import json
import re
import requests
from bs4 import BeautifulSoup

from retry import with_retry
from ai_client import extract_json_object, build_model_fallback_list

# Transient network errors worth retrying a fetch for. Explicitly excludes
# HTTP-status errors like 404 (raise_for_status -> HTTPError) since retrying
# a page that genuinely doesn't exist just wastes time.
_FETCH_RETRY_ON = (requests.ConnectionError, requests.Timeout)

RESEARCH_SYSTEM_PROMPT_TEMPLATE = """You are a research assistant that reads raw, messy
website text and extracts a concise, structured summary of a company for use
in a personalized job application email.

You are also given a list of optional "extra mention" add-ons the applicant can
reference IN ADDITION to their fixed core pitch (their core pitch is separate and
NOT part of this task). Your job is to identify which of these add-ons, if any,
are genuinely supported by real signals in the company's text.

Extra mention add-ons available (id: match_description):
{extra_mentions_block}

Respond with ONLY a JSON object (no markdown fences, no preamble, no commentary).
Use this exact schema:

{{
  "industry": "short phrase, e.g. 'fintech / payments'",
  "company_size_guess": "startup | small | mid-size | large enterprise | unknown",
  "mission_or_focus": "1-2 sentence summary of what the company does / cares about",
  "tone_of_voice": "formal | casual | technical | mission-driven | unknown",
  "talking_points": ["2-4 short specific facts or values worth referencing in an email"],
  "notable_products_or_news": "1 sentence, or 'none found'",
  "matched_extra_mentions": ["0 or more ids from the list above that have REAL supporting evidence"],
  "match_reasons": {{"extra_mention_id": "1 short sentence: the SPECIFIC fact that justifies this match"}}
}}

It is completely normal and expected for matched_extra_mentions to be an empty list
if nothing clearly fits — do NOT force a match. A company can also genuinely match
more than one add-on at once if there's real evidence for each.

If the provided text is too thin to infer something, use "unknown" or "none found"
rather than inventing facts. Never fabricate specific numbers, funding rounds,
client names, or news that isn't in the text.
"""


def _build_extra_mentions_block(extra_mentions: list) -> str:
    lines = []
    for mention in extra_mentions:
        lines.append(f'- {mention["id"]}: {mention["match_description"]}')
    return "\n".join(lines)


def fetch_website_text(url: str, timeout: int = 10, max_chars: int = 6000) -> str:
    """Best-effort fetch + strip of a company website's visible text."""
    if not url or not isinstance(url, str) or not url.strip():
        return ""

    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; InternshipResearchBot/1.0; "
                      "+personal-use-application-assistant)"
    }

    try:
        resp = with_retry(
            lambda: requests.get(url, headers=headers, timeout=timeout),
            attempts=2, base_delay=1.0, retry_on=_FETCH_RETRY_ON,
            what=f"fetching {url}", quiet=True,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    [research] could not fetch {url}: {e}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()

    return text[:max_chars]


def get_company_context(client, model: str, company_name: str, website_url: str,
                         extra_mentions: list) -> dict:
    """
    Runs the research agent for one company.
    `extra_mentions` is the list loaded from specializations.json["extra_mentions"].
    Returns a dict with 0+ matched_extra_mentions — an empty list is a normal,
    valid outcome (just means: use the core pitch alone, no add-on).
    Falls back to a minimal "unknown" context if scraping/parsing fails,
    so the pipeline never hard-crashes on one bad company.
    """
    site_text = fetch_website_text(website_url)

    fallback = {
        "industry": "unknown",
        "company_size_guess": "unknown",
        "mission_or_focus": f"{company_name} — no additional context available.",
        "tone_of_voice": "formal",
        "talking_points": [],
        "notable_products_or_news": "none found",
        "matched_extra_mentions": [],
        "match_reasons": {},
    }

    if not site_text:
        return fallback

    system_prompt = RESEARCH_SYSTEM_PROMPT_TEMPLATE.format(
        extra_mentions_block=_build_extra_mentions_block(extra_mentions)
    )

    user_prompt = (
        f"Company name: {company_name}\n"
        f"Raw website text (truncated):\n\n{site_text}"
    )

    # Transport-level errors (network, 429, 5xx) are already retried inside
    # CompatibleAIClient.messages.create. Here we only fall through to another
    # model when a call succeeds at the HTTP level but returns unusable
    # content (empty/malformed JSON) — common with reasoning models under a
    # narrow max_tokens budget.
    last_err = None
    for attempt_model in build_model_fallback_list(model):
        try:
            response = client.messages.create(
                model=attempt_model,
                max_tokens=2000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()
            block = extract_json_object(raw) or raw
            data = json.loads(block)

            # make sure all expected keys exist
            for key in fallback:
                data.setdefault(key, fallback[key])

            # guard against the model inventing ids that aren't in our config
            known_ids = {m["id"] for m in extra_mentions}
            matches = data.get("matched_extra_mentions", [])
            if not isinstance(matches, list):
                matches = [matches]
            valid_matches = [m for m in matches if m in known_ids]

            data["matched_extra_mentions"] = valid_matches
            data["match_reasons"] = {
                k: v for k, v in data.get("match_reasons", {}).items() if k in valid_matches
            }

            if attempt_model != model:
                print(f"    [research] used fallback model {attempt_model} for {company_name}")
            return data
        except Exception as e:
            last_err = e
            print(f"    [research] model {attempt_model} failed for {company_name}: {e}")
            continue

    print(f"    [research] AI parsing failed for {company_name}: {last_err}")
    return fallback
