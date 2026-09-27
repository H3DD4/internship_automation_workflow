"""
Research agent — the only step that uses an AI model.

It asks the model two narrow questions about a company's website, then
VERIFIES both answers in code against the website text before anything
reaches the email:

  1. areas — which (up to two) of the applicant's CV areas match what this
     company does. Closed list; an answer is kept only if the site actually
     contains that area's keywords.
  2. hook  — one short phrase describing what the company does, copied from
     its own words. Kept only if its words are really on the site, it
     carries no number or claim the site doesn't, and the quoted evidence
     sentence is found there too.

Anything that fails verification is dropped, and the email falls back to its
standard wording. A weak model can therefore make the email less specific,
but it cannot make it say something untrue.
"""

import json
import re

import requests
from bs4 import BeautifulSoup

from retry import with_retry
from ai_client import extract_json_object, build_model_fallback_list
from agents.draft_guard import BANNED_PHRASES

# Transient network errors worth retrying a fetch for. Explicitly excludes
# HTTP-status errors like 404 (raise_for_status -> HTTPError) since retrying
# a page that genuinely doesn't exist just wastes time.
_FETCH_RETRY_ON = (requests.ConnectionError, requests.Timeout)

RESEARCH_SYSTEM_PROMPT_TEMPLATE = """You read a company's website text and answer two questions.
You COPY from the text. You do not create, guess or improve.

QUESTION 1 — "areas": which of these describe what the company actually does?
Give 0, 1 or 2 ids, the best match first. Give [] if none clearly applies.
{areas_block}

QUESTION 2 — "hook": one concrete thing this company does, as a short noun
phrase of 5 to 20 words, built from the website's OWN words. It will be
placed after the words "What interests me most about <Company> is your work on".
  good: "continuous penetration testing delivered through your own platform"
  good: "managed detection and response for mid-sized businesses"
  bad:  "innovative cutting-edge solutions"      (vague)
  bad:  "being the market leader with 500 clients" (not in the text)
Then copy, word for word, ONE sentence from the website that supports it,
as "hook_evidence".
If the text doesn't describe what they do (cookie banner, login page, error
page, or too little text), set both "hook" and "hook_evidence" to "".

Answer with ONLY this JSON object and nothing else:
{{"areas": [], "hook": "", "hook_evidence": "", "industry": "", "summary": ""}}

"industry": 2-5 words. "summary": one sentence on what they do, from the text.
Rules: never add a number, name, product, client, award or date that is not
in the text. Write "your", never "their" or "our".
"""

_STOPWORDS = {
    "that", "this", "with", "from", "your", "their", "they", "them", "have", "will",
    "into", "about", "more", "most", "such", "each", "every", "also", "which", "what",
    "when", "where", "while", "through", "across", "over", "than", "then", "these",
    "those", "been", "being", "were", "work", "works", "offer", "offers", "provide",
    "provides", "providing", "help", "helps", "helping", "based", "using", "used",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_FIRST_PERSON_RE = re.compile(r"\b(i|i'm|i've|my|me|we|we're|us)\b", re.IGNORECASE)


def _build_areas_block(areas: list) -> str:
    return "\n".join(f'- {area["id"]}: {area["match_description"]}' for area in areas)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _fetch_page_text(url: str, timeout: int) -> str:
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
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def fetch_website_text(url: str, timeout: int = 10, max_chars: int = 6000) -> str:
    """Best-effort visible text of a company's site. When the homepage is
    thin (a hero banner and a login button), the About page usually says
    what the company actually does, so it's appended."""
    if not url or not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    text = _fetch_page_text(url, timeout)
    if len(text) < 1500:
        base = url.rstrip("/")
        for path in ("/about", "/about-us"):
            extra = _fetch_page_text(base + path, timeout)
            if extra:
                text = f"{text} {extra}".strip()
                break
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Verification — deterministic, so it can't hallucinate in turn
# ---------------------------------------------------------------------------

def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > 5 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _content_words(text: str) -> list:
    return [_stem(w) for w in _WORD_RE.findall((text or "").lower())
            if len(w) > 3 and w not in _STOPWORDS]


def support_ratio(phrase: str, site_text: str) -> float:
    """Share of the phrase's meaningful words that also occur on the site."""
    words = _content_words(phrase)
    if not words:
        return 0.0
    site = set(_content_words(site_text))
    return sum(1 for w in words if w in site) / len(words)


def _keyword_present(keyword: str, text: str) -> bool:
    # Short keywords ("soc", "rag", "aws") need a boundary on both sides or
    # they match inside unrelated words ("social", "storage", "laws").
    pattern = r"(?<![a-z0-9])" + re.escape(keyword)
    if len(keyword) <= 4:
        pattern += r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def keyword_hits(area: dict, site_text: str) -> int:
    text = (site_text or "").lower()
    required = area.get("requires_any")
    if required and not any(term in text for term in required):
        return 0
    return sum(1 for keyword in area.get("keywords", []) if _keyword_present(keyword, text))


def resolve_areas(model_areas: list, site_text: str, areas: list) -> tuple:
    """Keep the model's area picks only when the site's own words back them
    up, topping up from keyword evidence alone when it's strong. Returns
    (area_ids, how_each_was_decided)."""
    by_id = {area["id"]: area for area in areas}
    hits = {area["id"]: keyword_hits(area, site_text) for area in areas}

    chosen, notes = [], []
    for area_id in model_areas or []:
        if area_id in by_id and area_id not in chosen and hits[area_id] >= 1:
            chosen.append(area_id)
            notes.append(f"{area_id}: model, confirmed by {hits[area_id]} keyword(s) on the site")
        elif area_id in by_id:
            notes.append(f"{area_id}: model suggested it, but nothing on the site backs it — dropped")

    # When the model found something, only add a keyword-only area if the
    # evidence is strong; when the model gave nothing usable, accept
    # moderate evidence so a weak model doesn't cost the whole match.
    threshold = 3 if chosen else 2
    for area_id, count in sorted(hits.items(), key=lambda item: -item[1]):
        if len(chosen) >= 2:
            break
        if area_id not in chosen and count >= threshold:
            chosen.append(area_id)
            notes.append(f"{area_id}: {count} keyword(s) on the site")

    return chosen[:2], notes


def _normalise_hook(hook: str, site_text: str) -> str:
    hook = (hook or "").strip().strip("\"'“”").rstrip(".").strip()
    hook = re.sub(r"^(your work on|work on|your work in)\s+", "", hook, flags=re.IGNORECASE)
    hook = re.sub(r"\b(their|our)\b", "your", hook, flags=re.IGNORECASE)
    first = hook.split()[0] if hook else ""
    # The hook continues a sentence, so a capitalised first word ("Continuous")
    # is lowered — unless the site itself capitalises it mid-sentence, which
    # marks a proper noun ("Kubernetes") — and acronyms ("AI-driven") are kept.
    if first[:1].isupper() and first[1:].islower():
        proper_noun = re.search(r"[a-z,;:]\s+" + re.escape(first) + r"\b", site_text or "")
        if not proper_noun:
            hook = first.lower() + hook[len(first):]
    return hook


def verify_hook(hook: str, evidence: str, site_text: str) -> tuple:
    """Return (hook, "grounded") or ("", reason_it_was_rejected)."""
    hook = _normalise_hook(hook, site_text)
    if not hook:
        return "", "none offered"

    words = hook.split()
    if not 4 <= len(words) <= 25:
        return "", f"length {len(words)} words"
    if _FIRST_PERSON_RE.search(hook):
        return "", "written in the first person"
    lowered = hook.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            return "", f"unverifiable phrase {phrase!r}"

    site_numbers = set(_NUMBER_RE.findall(site_text or ""))
    for number in _NUMBER_RE.findall(hook):
        if number not in site_numbers:
            return "", f"number {number!r} is not on the site"

    ratio = support_ratio(hook, site_text)
    if ratio < 0.6:
        return "", f"only {ratio:.0%} of its words appear on the site"

    if len(_content_words(evidence)) < 3 or support_ratio(evidence, site_text) < 0.8:
        return "", "its quoted evidence isn't on the site"

    return hook, "grounded"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def get_company_context(client, model: str, company_name: str, website_url: str,
                         areas: list) -> dict:
    """Research one company. Always returns a usable context: when the site
    can't be read or the model fails, `areas` comes from keywords alone (or
    is empty) and `company_hook` is "", and the email uses standard wording."""
    site_text = fetch_website_text(website_url)

    context = {
        "industry": "unknown",
        "mission_or_focus": "",
        "company_hook": "",
        "hook_evidence": "",
        "hook_status": "no website text",
        "areas": [],
        "area_notes": [],
        "site_chars": len(site_text),
    }
    if not site_text:
        return _with_display_fields(context)

    system_prompt = RESEARCH_SYSTEM_PROMPT_TEMPLATE.format(areas_block=_build_areas_block(areas))
    user_prompt = f"Company name: {company_name}\nWebsite text:\n\n{site_text}"

    answer = {}
    # Transport errors are retried inside CompatibleAIClient; here a model that
    # answered with something unparsable falls through to the next one.
    for attempt_model in build_model_fallback_list(model):
        try:
            response = client.messages.create(
                model=attempt_model,
                max_tokens=1500,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0.0,
                json_mode=True,
                reasoning_effort="low",
            )
            raw = response.content[0].text.strip()
            answer = json.loads(extract_json_object(raw) or raw)
            if not isinstance(answer, dict):
                raise ValueError("answer is not a JSON object")
            if attempt_model != model:
                print(f"    [research] used fallback model {attempt_model} for {company_name}")
            break
        except Exception as e:
            print(f"    [research] model {attempt_model} failed for {company_name}: {e}")
            answer = {}

    model_areas = answer.get("areas") or []
    if isinstance(model_areas, str):
        model_areas = [model_areas]
    context["areas"], context["area_notes"] = resolve_areas(model_areas, site_text, areas)

    hook, status = verify_hook(str(answer.get("hook") or ""),
                               str(answer.get("hook_evidence") or ""), site_text)
    context["company_hook"] = hook
    context["hook_evidence"] = str(answer.get("hook_evidence") or "") if hook else ""
    context["hook_status"] = status if answer else "model gave no usable answer"
    if not hook and status != "none offered":
        print(f"    [research] hook for {company_name} rejected ({status}) — using standard wording")

    for key, target in (("industry", "industry"), ("summary", "mission_or_focus")):
        value = str(answer.get(key) or "").strip()
        if value and support_ratio(value, site_text) >= 0.5:
            context[target] = value

    return _with_display_fields(context)


def _with_display_fields(context: dict) -> dict:
    """Fields the dashboard and DB already understand."""
    context["tone_of_voice"] = context.get("tone_of_voice", "unknown")
    context["talking_points"] = [context["company_hook"]] if context["company_hook"] else []
    context["matched_extra_mentions"] = list(context["areas"])
    context["match_reasons"] = {
        note.split(":", 1)[0]: note.split(":", 1)[1].strip()
        for note in context.get("area_notes", []) if ":" in note
        and note.split(":", 1)[0] in context["areas"]
    }
    return context
