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
from agents.draft_guard import BANNED_PHRASES, find_banned_phrase
from model_router import RoutingCancelled
from language import detect_language
import safe_http

# Transient network errors worth retrying a fetch for. Explicitly excludes
# HTTP-status errors like 404 (raise_for_status -> HTTPError) since retrying
# a page that genuinely doesn't exist just wastes time.
_FETCH_RETRY_ON = (requests.ConnectionError, requests.Timeout)

RESEARCH_SYSTEM_PROMPT_TEMPLATE = """You read a company's website text and answer two questions.
You COPY from the text. You do not create, guess or improve.

QUESTION 1 — "areas": which of these describe what the company actually does?
Give 0, 1 or 2 ids, the best match first. Give [] if none clearly applies.
{areas_block}
For each id you give, copy into "area_evidence" the words from the website
(3 to 15 words, exactly as written, in the website's language) that show it:
  {{"agentic_ai": "autonomous AI employees that automate repetitive workflows"}}

QUESTION 2 — "hook": one concrete thing this company does, as a short noun
phrase of 5 to 20 words, built from the website's OWN words. It will be
placed after the words "What interests me most about <Company> is your work on".
  good: "continuous penetration testing delivered through your own platform"
  good: "managed detection and response for mid-sized businesses"
  bad:  "innovative cutting-edge solutions"      (vague)
  bad:  "being the market leader with 500 clients" (not in the text)

Answer in the WEBSITE'S OWN LANGUAGE. If the text is German, answer in German;
if French, in French. Do NOT translate it into English. The phrase is checked
word by word against the website text, so a translated phrase is thrown away
even when it is perfectly accurate — it is translated later, after checking.
  good: "soluzioni personalizzate per un'infrastruttura IT performante"
  bad:  "custom solutions for high-performance IT" (translated, will be thrown away)
Then copy, word for word, ONE sentence from the website that supports it,
as "hook_evidence".
If the text doesn't describe what they do (cookie banner, login page, error
page, or too little text), set both "hook" and "hook_evidence" to "".

Answer with ONLY this JSON object and nothing else:
{{"areas": [], "area_evidence": {{}}, "hook": "", "hook_evidence": "", "industry": "", "summary": "", "organisation": "", "city": "", "country": ""}}

"industry": 2-5 words. "summary": one sentence on what they do, from the text.
"organisation": the name of the company that owns this website, copied exactly
as the text writes it. If the site presents a product, give the company behind
the product (e.g. "Connect-i", not its product "Opigno"). "" if the text doesn't
say.
"city" and "country": where the company is based (its office or headquarters),
only when the text states it — the city as written (e.g. "Lyon", "Genève"), the
country in English (e.g. "France", "Switzerland"). "" if the text doesn't say.
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


def _parse_json_object(text: str) -> dict:
    raw = (text or "").strip()
    answer = json.loads(extract_json_object(raw) or raw)
    if not isinstance(answer, dict):
        raise ValueError("answer is not a JSON object")
    return answer


def ask_json(client, model: str, *, task: str, system: str, user: str, max_tokens: int,
             label: str = "", validate=None, attempts: int = 3,
             max_wait: float | None = None) -> tuple:
    """Ask for a JSON object, moving to another model when an answer comes back
    unusable. Returns (answer, "provider/model" or model id) — or ({}, None)
    when no model produced a usable answer.

    With a ModelRouter, availability is the router's job — rate limits,
    outages, bad keys and daily quotas are handled across providers inside
    complete() — so this only has to reject bad answers, excluding the model
    that gave one. With a single client (tests, legacy callers) it walks the
    provider's model fallback list as before."""
    messages = [{"role": "user", "content": user}]

    if getattr(client, "is_router", False):
        tried: set = set()
        for _ in range(attempts):
            try:
                result = client.complete(task=task, system=system, messages=messages,
                                         max_tokens=max_tokens, temperature=0.0,
                                         json_mode=True, reasoning_effort="low", avoid=tried,
                                         max_wait=max_wait)
            except RoutingCancelled:
                raise  # a stop is not "no model": the pipeline puts the company back
            except Exception as exc:  # AllModelsUnavailable: nothing left to try
                print(f"    [research] {task} for {label}: {exc}")
                return {}, None
            try:
                answer = _parse_json_object(result.text)
                if validate:
                    validate(answer)
                return answer, result.deployment
            except Exception as exc:
                print(f"    [research] {result.deployment} gave an unusable {task} "
                      f"answer for {label}: {exc}")
                tried.add(result.deployment)
        return {}, None

    # Transport errors are retried inside CompatibleAIClient; here a model that
    # answered with something unparsable falls through to the next one.
    for attempt_model in build_model_fallback_list(model):
        try:
            response = client.messages.create(
                model=attempt_model, max_tokens=max_tokens, system=system,
                messages=messages, temperature=0.0, json_mode=True, reasoning_effort="low",
            )
            answer = _parse_json_object(response.content[0].text)
            if validate:
                validate(answer)
            if attempt_model != model:
                print(f"    [research] used fallback model {attempt_model} for {label}")
            return answer, attempt_model
        except Exception as exc:
            print(f"    [research] model {attempt_model} failed for {label}: {exc}")
    return {}, None


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
        # safe_http refuses private/loopback/metadata addresses (the company
        # list is user input on a shared server), caps the page size and
        # re-checks every redirect.
        resp = with_retry(
            lambda: safe_http.get(url, headers=headers, timeout=timeout),
            attempts=2, base_delay=1.0, retry_on=_FETCH_RETRY_ON,
            what=f"fetching {url}", quiet=True,
        )
        resp.raise_for_status()
    except (requests.RequestException, safe_http.BlockedURL) as e:
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


def keyword_hits(area: dict, site_text: str, lang: str = "") -> int:
    """How many of the area's keywords the site uses. On a French site the
    area's French keywords count too ("test d'intrusion", "vulnérabilité"),
    which the English list alone never matched."""
    text = (site_text or "").lower()
    keywords = list(area.get("keywords", []))
    required = list(area.get("requires_any") or [])
    if lang == "fr":
        keywords += area.get("keywords_fr", [])
        required += area.get("requires_any_fr", []) if required else []
    if required and not any(term in text for term in required):
        return 0
    return sum(1 for keyword in keywords if _keyword_present(keyword, text))


_QUOTE_FOLD = str.maketrans({**{c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"},
                              **{c: "'" for c in "\u2018\u2019\u02bc"}, **{c: '"' for c in "\u201c\u201d"}})


def quote_on_site(quote: str, site_text: str, min_words: int = 3) -> bool:
    """True when `quote` appears in the site text word for word (ignoring
    case, spacing and the kind of dash or quote mark)."""
    def fold(text):
        return " ".join((text or "").translate(_QUOTE_FOLD).lower().split())
    quote = fold(quote).strip(" .,;:\"'")
    return len(quote.split()) >= min_words and quote in fold(site_text)


def _fold_words(text: str) -> list:
    return re.findall(r"[^\W\d_]+", (text or "").translate(_QUOTE_FOLD).lower())


def quote_fits_area(quote: str, area: dict) -> bool:
    """A quote found on the site proves the site says it, not that it is
    about this area: a basket maker's "woven baskets for Sunday picnics" is
    on its site but says nothing about application security. The quote must
    share a word root with the area's own vocabulary (its name, topic and
    keywords, English and French) — a loose test, so "wij testen de
    beveiliging van netwerken" still fits penetration testing (test, netw…),
    but an unrelated passage does not."""
    vocab = set()
    for text in [area.get("id", "").replace("_", " "), area.get("label", ""), area.get("topic", ""),
                 *(area.get("keywords") or []), *(area.get("keywords_fr") or [])]:
        vocab.update(_fold_words(text))
    if not vocab:
        return True
    for word in _fold_words(quote):
        if word in _STOPWORDS:
            continue
        if len(word) >= 4 and any(word[:4] in v for v in vocab if len(v) >= 4):
            return True
        if 2 <= len(word) <= 3 and word in vocab:
            return True
    return False


def resolve_areas(model_areas: list, site_text: str, areas: list, lang: str = "",
                  evidence: dict | None = None) -> tuple:
    """Keep the model's area picks only when the site's own words back them
    up — one of the area's keywords, or the passage the model quoted for it,
    found word for word on the site. The quote is what makes this work in any
    language and for any field: the keyword lists are short, English/French,
    and missed "autonomous AI employees" for an agentic-AI company.
    Tops up from keyword evidence alone when it's strong. Returns
    (area_ids, how_each_was_decided)."""
    by_id = {area["id"]: area for area in areas}
    hits = {area["id"]: keyword_hits(area, site_text, lang) for area in areas}
    evidence = evidence if isinstance(evidence, dict) else {}

    chosen, notes = [], []
    for area_id in model_areas or []:
        if area_id not in by_id or area_id in chosen:
            continue
        quote = str(evidence.get(area_id) or "").strip()
        if hits[area_id] >= 1:
            chosen.append(area_id)
            notes.append(f"{area_id}: model, confirmed by {hits[area_id]} keyword(s) on the site")
        elif quote and quote_on_site(quote, site_text) and quote_fits_area(quote, by_id[area_id]):
            chosen.append(area_id)
            notes.append(f"{area_id}: model, backed by the site's own words: \"{quote[:120]}\"")
        elif quote and quote_on_site(quote, site_text):
            notes.append(f"{area_id}: the quoted passage is on the site but isn't about this area — dropped")
        else:
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
    # A model sometimes returns a sentence — "Factory 3D is a powerful software
    # platform to …" — which reads as broken grammar after "your work on". A
    # short named subject plus "is a/an/the" becomes an appositive: "Factory 3D,
    # a powerful software platform to …". Same words, same claim.
    appositive = re.match(r"^([A-Z0-9][^\s,]*(?:\s+[^\s,]+){0,3}?)\s+(?:is|are)\s+(an?|the)\s+(.+)$",
                          hook)
    if appositive:
        subject, article, rest = appositive.groups()
        # The subject of "X is a …" is the name of the thing, so its capital
        # stays: "Factory 3D", never "factory 3D".
        return f"{subject}, {article} {rest}"
    # The French and Spanish "IA" is AI; left as-is it reads as a typo.
    hook = re.sub(r"\bIA\b", "AI", hook)

    words = hook.split()
    first = words[0] if words else ""

    def capitalised(word: str) -> bool:
        return word[:1].isupper() and (word[1:] == "" or word[1:].islower())

    def proper_noun_on_site(word: str) -> bool:
        # Capitalised mid-sentence on their own site marks a name ("Kubernetes").
        return bool(re.search(r"[a-z,;:]\s+" + re.escape(word) + r"\b", site_text or ""))

    # The hook continues a sentence, so a capitalised first word ("Continuous")
    # is lowered — unless it is a proper noun — and acronyms ("AI-driven") are
    # kept. A lone capital ("A no-code lab") counts as capitalised too.
    if capitalised(first) and not proper_noun_on_site(first):
        # A site heading comes back in Title Case — "Penetration Testing &
        # Vulnerability Assessments" — and lowering only its first word left
        # "penetration Testing & Vulnerability Assessments". When every content
        # word is capitalised the whole thing is a heading, so it is lowered
        # throughout, still sparing acronyms and names the site capitalises.
        content = [w for w in words if w[:1].isalpha() and w.lower() not in _HEADING_SMALL_WORDS]
        is_heading = len(content) >= 3 and all(w[:1].isupper() for w in content)
        lowered = []
        for index, word in enumerate(words):
            if index == 0 or (is_heading and capitalised(word) and not proper_noun_on_site(word)):
                word = word.lower()
            lowered.append(word)
        hook = " ".join(lowered)
    return hook


_HEADING_SMALL_WORDS = {"a", "an", "and", "or", "of", "for", "the", "to", "in", "on",
                        "with", "as", "at", "by", "&", "de", "du", "des", "et"}


# A hook completes "What interests me most about <Company> is your work on …",
# so it has to describe work. An offer ("free trial month for internet
# subscriptions") or a bare quantity ("more than 26,000 references") can be
# perfectly true and still read as nonsense there.
# Deliberately narrow: "24/7 monitoring", "5G networks", "hands-free" and
# "free software" are all fine and must not match.
_NOT_WORK_RE = re.compile(
    r"^(?:more than|over|up to|at least|nearly|almost)\s+\d"
    r"|\bfree\s+(?:trial|month|months|shipping|delivery|demo|consultation|quote)\b"
    r"|\btrial\s+(?:month|period)\b"
    r"|\b(?:discount|coupon|voucher|promo|promotion)s?\b"
    r"|\d+\s*%\s*off\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Translation — runs only AFTER the hook is verified, never before
# ---------------------------------------------------------------------------
# Most of this list is European, so a hook copied from a company's own site is
# often French, Italian or German. Quoting it verbatim kept the email truthful
# but grafted a foreign clause into English prose, which reads like a bot:
#   "...is your work on soluzioni personalizzate per un'infrastruttura IT..."
#
# The order here is the whole safety argument. The hook is verified against the
# site in its ORIGINAL language first, so grounding is established on the
# company's own words. Translation is a rendering step applied to an already-
# trusted phrase — it can never introduce a claim, because a claim that wasn't
# in the verified original is caught below and the original is kept instead.

# Seconds a translation may wait for a free translation model before the email
# falls back to standard wording.
TRANSLATION_MAX_WAIT = 60.0

TRANSLATE_SYSTEM_PROMPT = """You translate one short phrase into English.

Keep every proper name exactly as written: company names, product names,
brand names, technologies and acronyms (Frontier Engine, FinOps, Kubernetes,
GPU, SaaS). Translate everything around them.

Translate ONLY. Add nothing, drop nothing, explain nothing. Never add a
number, client, award or date. If the phrase is already English, return it
unchanged.

The result continues the sentence "What interests me most about <Company> is
your work on ...", so return a noun phrase, not a sentence, with no final
full stop.

Answer with ONLY this JSON object:
{"english": ""}"""

# Function words common enough that one of them means the phrase already reads
# as English. Deliberately permissive: a needless translation call is cheap,
# and translating an English phrase is a no-op, but MISSING a foreign phrase
# is the bug being fixed.
_ENGLISH_MARKERS = {
    "the", "and", "that", "with", "your", "for", "from", "this", "their", "its",
    "into", "across", "through", "over", "between", "without", "our", "you",
    "who", "what", "which", "are", "is", "be", "of", "to", "in", "on", "by",
    "as", "at", "all", "every", "more", "than", "using", "based", "built",
}
# Unambiguous non-English function words — none of these is also an English
# word, so a single hit is a reliable signal.
_NON_ENGLISH_MARKERS = {
    # German
    "die", "der", "das", "und", "für", "fur", "im", "ein", "eine", "von", "mit",
    "zu", "den", "dem", "aus", "bei", "auf", "nicht", "sind", "werden", "unsere",
    "ihre", "ihr", "wir", "sie", "sein", "seine", "durch", "oder",
    # French
    "les", "des", "pour", "avec", "sur", "dans", "aux", "leur", "nos", "vos",
    "est", "sont", "chez", "sans", "vers", "notre", "votre", "qui", "que", "pas",
    "ce", "cet", "cette", "ses", "ces", "nous", "vous", "leurs",
    # Italian
    "della", "delle", "dei", "degli", "che", "sono", "gli", "alla", "nel", "nella",
    "nostro", "nostra", "suo", "sua", "una", "uno", "un", "il", "lo", "al", "nei", "sul",
    # Spanish / Portuguese
    "los", "las", "para", "por", "nuestra", "nuestro", "sus", "como", "não",
    # Dutch
    "voor", "van", "het", "onze", "wij", "een",
}
_ACCENTED_RE = re.compile(r"[àâäáãçèéêëìíîïñòóôöõùúûüýÿßœæ]", re.IGNORECASE)


# A thousands-grouped number in any European convention — "26 000" (with a
# plain, no-break, narrow no-break or thin space), "26.000", "26,000", "26'000"
# — or a plain number with an optional decimal part.
_LOCALE_NUMBER_RE = re.compile(
    r"\d{1,3}(?:[    .,'’]\d{3})+(?!\d)|\d+(?:[.,]\d+)?"
)


def _number_values(text: str) -> set:
    """The numbers in `text` as bare digit strings, so the same quantity
    written under different conventions compares equal."""
    return {re.sub(r"\D", "", match) for match in _LOCALE_NUMBER_RE.findall(text or "")}


def _phrase_tokens(text: str) -> set:
    return set(re.findall(r"[a-zà-ÿ]+", (text or "").lower()))


def still_looks_foreign(text: str) -> bool:
    """True when the phrase carries positive evidence of another language."""
    return bool(_ACCENTED_RE.search(text or "")
                or _phrase_tokens(text) & _NON_ENGLISH_MARKERS)


def looks_already_english(hook: str) -> bool:
    """True when the phrase plainly reads as English, so no call is needed.

    Deciding to SKIP needs positive evidence of English, because the cost of
    being wrong is a foreign phrase going out untranslated. Judging a finished
    translation is the opposite question — see translate_hook.
    """
    if still_looks_foreign(hook):
        return False
    return bool(_phrase_tokens(hook) & _ENGLISH_MARKERS)


def translate_hook(client, model: str, hook: str, site_text: str) -> tuple:
    """(hook_in_english, note). Falls back to the verified original on any
    doubt: it is truthful either way, so a failed translation must cost
    readability, never accuracy."""
    if not hook or looks_already_english(hook):
        return hook, ""

    def has_translation(answer: dict) -> None:
        if not str(answer.get("english") or "").strip():
            raise ValueError('no "english" in the answer')

    # A short wait: translation has a safe fallback, and waiting the router's
    # full 15 minutes for a translator held every research worker hostage —
    # the whole run stalled until someone closed the dashboard.
    answer, _ = ask_json(client, model, task="translation", system=TRANSLATE_SYSTEM_PROMPT,
                         user=hook, max_tokens=300, label="translation",
                         validate=has_translation, max_wait=TRANSLATION_MAX_WAIT)
    english = _normalise_hook(str(answer.get("english") or "").strip(), site_text)
    if not english:
        # No translator free: the standard English wording beats a French or
        # German clause grafted into an English email. (The other safeguards
        # below still fall back to the verified original — those are cases
        # where a translation came back and was worse, not missing.)
        return "", "dropped: no translation model was available — standard wording"

    # The translated phrase is re-checked as if it were new, because it is:
    # only the ORIGINAL was verified against the site.
    words = english.split()
    if not 4 <= len(words) <= 25:
        return hook, "kept in the original language (translation was the wrong length)"
    if _FIRST_PERSON_RE.search(english):
        return hook, "kept in the original language (translation used the first person)"
    banned = find_banned_phrase(english)
    if banned:
        return hook, f"kept in the original language (translation added {banned!r})"
    # A number absent from the verified original is an invented claim, which is
    # exactly what the whole pipeline exists to prevent. Compared by value, not
    # by formatting: French writes "26 000", English "26,000", and comparing the
    # raw strings discarded a correct translation as if it had made one up.
    if _number_values(english) - _number_values(hook):
        return hook, "kept in the original language (translation invented a number)"
    # Same for a name: a translation that brings in a company, client or
    # place the original never mentioned is making a new claim.
    if unsupported_names(english, hook, site_text):
        return hook, "kept in the original language (translation added a name)"
    # "plus de 26 000 références" passes the English-only check on the
    # original; only the translation shows it is a quantity, not their work.
    # Keeping the French wouldn't help, so the hook is dropped entirely.
    if _NOT_WORK_RE.search(english):
        return "", "dropped: describes an offer or a quantity, not their work"
    # Deliberately not looks_already_english(): a correct translation need not
    # contain any of the function words that mark a phrase as English up front
    # ("continuous FinOps optimisation, not a one-off audit" has none), and
    # rejecting it for that put the French phrase back untranslated. What
    # matters here is only that no trace of the source language is left.
    if still_looks_foreign(english) or english.strip().lower() == hook.strip().lower():
        return hook, "kept in the original language (translation did not reach English)"

    return english, "translated into English"


_NAME_RE = re.compile(r"(?<![\w'’-])[A-Z][\w&'’.-]*[A-Z0-9][\w&'’.-]*|(?<=\s)[A-Z][a-zà-ÿ]+(?:[A-Z][\w]*)?")


def unsupported_names(phrase: str, *sources: str) -> list:
    """Capitalised words (names of companies, products, clients, places) in
    `phrase` that none of `sources` contains. The first word is skipped —
    any sentence starts with a capital. An invented client ("Nexalume
    hospitals") is the most damaging thing a hook can say, and the
    word-overlap ratio alone let it through."""
    haystack = " ".join(_fold_words(" ".join(sources)))
    names = []
    for match in _NAME_RE.finditer(phrase or ""):
        if match.start() == 0:
            continue
        word = match.group(0).strip(".'’-")
        folded = " ".join(_fold_words(word))
        if folded and f" {folded} " not in f" {haystack} ":
            names.append(word)
    return names


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
    banned = find_banned_phrase(hook)
    if banned:
        return "", f"unverifiable phrase {banned!r}"
    if _NOT_WORK_RE.search(hook):
        return "", "describes an offer or a quantity, not their work"

    site_numbers = set(_NUMBER_RE.findall(site_text or ""))
    for number in _NUMBER_RE.findall(hook):
        if number not in site_numbers:
            return "", f"number {number!r} is not on the site"

    ratio = support_ratio(hook, site_text)
    if ratio < 0.6:
        return "", f"only {ratio:.0%} of its words appear on the site"

    names = unsupported_names(hook, site_text)
    if names:
        return "", f"names {', '.join(names[:3])} that the site doesn't mention"

    # The evidence must be a real quotation — a passage that shares most of
    # its words with the site but was put together by the model proves
    # nothing.
    if len(_content_words(evidence)) < 3 or not quote_on_site(evidence, site_text):
        return "", "its quoted evidence isn't on the site word for word"

    return hook, "grounded"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def get_company_context(client, model: str, company_name: str, website_url: str,
                         areas: list, translation_model: str | None = None) -> dict:
    """Research one company. Always returns a usable context: when the site
    can't be read or the model fails, `areas` comes from keywords alone (or
    is empty) and `company_hook` is "", and the email uses standard wording."""
    site_text = fetch_website_text(website_url)

    context = {
        "industry": "unknown",
        "mission_or_focus": "",
        "company_hook": "",
        "hook_original": "",
        "research_model": "",
        "hook_evidence": "",
        "hook_status": "no website text",
        "areas": [],
        "area_notes": [],
        "site_chars": len(site_text),
        # en | fr | other | "" — decides which language the email is written in.
        "site_language": detect_language(site_text),
        # The company's own name as its website writes it ("" when unsure);
        # see utils.name_for_email.
        "site_company_name": "",
    }
    if not site_text:
        return _with_display_fields(context)

    system_prompt = RESEARCH_SYSTEM_PROMPT_TEMPLATE.format(areas_block=_build_areas_block(areas))
    user_prompt = f"Company name: {company_name}\nWebsite text:\n\n{site_text}"

    answer, used_model = ask_json(client, model, task="research", system=system_prompt,
                                  user=user_prompt, max_tokens=1500, label=company_name)
    context["research_model"] = used_model or ""

    model_areas = answer.get("areas") or []
    if isinstance(model_areas, str):
        model_areas = [model_areas]
    context["areas"], context["area_notes"] = resolve_areas(model_areas, site_text, areas,
                                                            context["site_language"],
                                                            answer.get("area_evidence"))

    hook, status = verify_hook(str(answer.get("hook") or ""),
                               str(answer.get("hook_evidence") or ""), site_text)

    # Verified first, translated second — see TRANSLATE_SYSTEM_PROMPT above.
    original = hook
    if hook:
        hook, note = translate_hook(client, translation_model or model, hook, site_text)
        if note:
            status = f"{status}, {note}"
            if hook and hook != original:
                print(f"    [research] hook for {company_name} translated into English")

    context["company_hook"] = hook
    # The phrase actually found on the site, kept so the dashboard can show
    # what was quoted and any translation stays auditable. When no translator
    # was free, the English email goes without it, but the verified original
    # is kept: a French company's French email uses it as written.
    untranslated = bool(original and not hook and "no translation model" in status)
    context["hook_original"] = original if (hook and original != hook) or untranslated else ""
    context["hook_evidence"] = str(answer.get("hook_evidence") or "") if hook or untranslated else ""
    context["hook_status"] = status if answer else "model gave no usable answer"
    if not hook and status != "none offered":
        print(f"    [research] hook for {company_name} rejected ({status}) — using standard wording")

    for key, target in (("industry", "industry"), ("summary", "mission_or_focus")):
        value = str(answer.get(key) or "").strip()
        if value and support_ratio(value, site_text) >= 0.5:
            context[target] = value

    context["site_company_name"] = verify_site_name(str(answer.get("organisation") or ""), site_text)
    context["city"], context["country"] = verify_location(str(answer.get("city") or ""),
                                                          str(answer.get("country") or ""), site_text)
    return _with_display_fields(context)


# The country names a site may use for the English name the model returns.
_COUNTRY_ALIASES = {
    "switzerland": ("switzerland", "suisse", "schweiz", "svizzera"),
    "france": ("france",), "germany": ("germany", "allemagne", "deutschland"),
    "belgium": ("belgium", "belgique", "belgië", "belgien"), "luxembourg": ("luxembourg", "luxemburg"),
    "netherlands": ("netherlands", "pays-bas", "nederland"), "spain": ("spain", "espagne", "españa"),
    "italy": ("italy", "italie", "italia"), "united kingdom": ("united kingdom", "uk", "royaume-uni", "england"),
    "united states": ("united states", "usa", "états-unis"), "canada": ("canada",),
    "tunisia": ("tunisia", "tunisie"), "morocco": ("morocco", "maroc"), "austria": ("austria", "autriche", "österreich"),
}


def verify_location(city: str, country: str, site_text: str) -> tuple:
    """(city, country), each kept only when the site's own text names it — a
    location the model guessed is worse than none."""
    folded = " " + " ".join(_fold_words(site_text)) + " "
    def on_site(name: str) -> bool:
        words = " ".join(_fold_words(name))
        return bool(words) and f" {words} " in folded
    city = " ".join(city.split())[:60]
    city = city if city and len(city.split()) <= 4 and on_site(city) else ""
    country = " ".join(country.split())[:60]
    names = _COUNTRY_ALIASES.get(country.lower(), (country,))
    country = country if country and (city or any(on_site(n) for n in names)) else ""
    return city, country


def verify_site_name(name: str, site_text: str) -> str:
    """The website's own name for its company, kept only when it appears
    word for word in the text — never a name the model made up."""
    dashes = str.maketrans({c: "-" for c in "‐‑‒–—−"})
    name = " ".join(name.translate(dashes).split()).strip(" .,;:\"'")
    if not name or len(name) > 60 or len(name.split()) > 6:
        return ""
    page = " ".join((site_text or "").translate(dashes).split()).lower()
    if name.lower() in page:
        return name
    # "Hortis SA" on a site that only ever says "Hortis".
    bare = re.sub(r"[\s,]+(sa|ag|gmbh|sas|sarl|srl|bv|nv|ltd|llc|inc|plc|s\.a\.|s\.a\.s\.)\.?$", "",
                  name, flags=re.IGNORECASE).strip()
    return bare if bare != name and len(bare) >= 2 and bare.lower() in page else ""


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
