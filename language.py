"""Which language each email is written in, and the French-specific pieces.

The rule, in order:
  1. The user's per-company choice (the EN/FR switch on a company's page).
  2. The user's default ("always English" / "always French"), if set.
  3. The language of the company's own website text, detected during
     research — a company that presents itself in French gets French.
  4. A French country domain (.fr) when the site couldn't be read.
  5. English.

Detection is deterministic (function-word counts), so it is free, instant,
testable, and never an extra AI call.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

LANGUAGES = ("en", "fr")

_WORD_RE = re.compile(r"[a-zàâäçéèêëîïôöùûüÿœæ']+")

_FRENCH = {
    "le", "la", "les", "des", "du", "et", "pour", "avec", "nous", "vous", "est", "sont",
    "une", "dans", "sur", "au", "aux", "notre", "nos", "votre", "vos", "qui", "que", "par",
    "ou", "ses", "ces", "cette", "leur", "leurs", "chez", "sans", "plus", "tous", "entreprise",
    "services", "solutions", "données", "sécurité", "équipe", "l'", "d'",
}
_ENGLISH = {
    "the", "and", "for", "with", "we", "our", "you", "your", "is", "are", "of", "to", "in",
    "on", "by", "this", "that", "from", "about", "more", "all", "company", "team", "solutions",
    "services", "security", "data",
}
_OTHER = {  # German, Italian, Spanish, Dutch — "not French, not English"
    "und", "der", "die", "das", "mit", "für", "wir", "ihre", "unsere", "della", "delle",
    "che", "sono", "il", "per", "los", "las", "para", "nuestra", "het", "voor", "onze", "een",
}
# Words both lists contain carry no signal.
_SHARED = _FRENCH & _ENGLISH

FRENCH_TLDS = {"fr"}


def detect_language(text: str) -> str:
    """"fr", "en", "other", or "" when there is too little text to say."""
    tokens = []
    for token in _WORD_RE.findall((text or "").lower()[:20000]):
        if token.startswith(("l'", "d'", "qu'", "j'", "n'", "s'", "c'")):
            tokens.append(token[:2])
            token = token[2:]
        tokens.append(token)
    fr = sum(1 for t in tokens if t in _FRENCH and t not in _SHARED)
    en = sum(1 for t in tokens if t in _ENGLISH and t not in _SHARED)
    other = sum(1 for t in tokens if t in _OTHER)
    best = max(fr, en, other)
    if best < 8:
        return ""
    if fr == best and fr >= 1.5 * max(en, other, 1):
        return "fr"
    if en == best and en >= 1.5 * max(fr, other, 1):
        return "en"
    if other == best and other >= 1.5 * max(fr, en, 1):
        return "other"
    return ""


def _tld(url_or_email: str) -> str:
    value = (url_or_email or "").strip().lower()
    if "@" in value and "://" not in value:
        host = value.rsplit("@", 1)[-1]
    else:
        host = urlparse(value if "://" in value else "https://" + value).hostname or ""
    return host.rsplit(".", 1)[-1] if "." in host else ""


def choose_language(*, override: str | None, default_mode: str, context: dict | None,
                    email: str, website: str, available=LANGUAGES) -> str:
    available = [lang for lang in LANGUAGES if lang in available] or ["en"]
    if override in available:
        return override
    if default_mode in available:
        return default_mode
    site = (context or {}).get("site_language") or ""
    if site == "fr" and "fr" in available:
        return "fr"
    if site in ("en", "other"):
        return "en" if "en" in available else available[0]
    if (_tld(website) in FRENCH_TLDS or _tld(email) in FRENCH_TLDS) and "fr" in available:
        return "fr"
    return "en" if "en" in available else available[0]


# ---------------------------------------------------------------------------
# French hooks
# ---------------------------------------------------------------------------

def _looks_french(phrase: str) -> bool:
    """A short phrase is French when it carries French function words (or
    French accents) and nothing marking another language."""
    words = set(_WORD_RE.findall((phrase or "").lower()))
    if words & _OTHER:
        return False
    return bool(words & (_FRENCH - _SHARED)) or bool(re.search(r"[éèêàçùœ]", phrase or ""))


def _frenchify(hook: str) -> str:
    """The verified phrase, re-addressed to the reader. Research normalises a
    hook for an English sentence ("IA" -> "AI", "our" -> "your"); a French
    sentence needs the French forms back."""
    hook = re.sub(r"\bAI\b", "IA", hook)
    hook = re.sub(r"\bnos\b", "vos", hook, flags=re.IGNORECASE)
    hook = re.sub(r"\bnotre\b", "votre", hook, flags=re.IGNORECASE)
    hook = re.sub(r"\bleurs\b", "vos", hook, flags=re.IGNORECASE)
    hook = re.sub(r"\bleur\b", "votre", hook, flags=re.IGNORECASE)
    return hook


def research_for_language(research: dict | None, lang: str) -> dict:
    """The research as the composer should see it for `lang`.

    English keeps the research untouched (it already holds the English hook).
    French uses the phrase exactly as verified on the company's French site;
    a hook that only exists in English is left out rather than dropped into a
    French sentence."""
    research = dict(research or {})
    if lang != "fr":
        return research
    candidates = [research.get("hook_original") or "", research.get("company_hook") or ""]
    french = next((c for c in candidates if c and _looks_french(c)), "")
    research["company_hook"] = _frenchify(french) if french else ""
    return research


# ---------------------------------------------------------------------------
# Greetings
# ---------------------------------------------------------------------------

def french_greeting(first_name: str | None) -> str:
    return f"Bonjour {first_name}," if first_name else "Madame, Monsieur,"


_VOWEL_START = re.compile(r"^[aeiouyàâäéèêëîïôöùûüœæ]", re.IGNORECASE)


def french_de(company: str) -> str:
    """"de Talan" / "d'Airbus" — French elides before a vowel sound."""
    company = (company or "").strip()
    return f"d'{company}" if _VOWEL_START.match(company) else f"de {company}"
