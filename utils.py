"""
Utilities for normalizing messy real-world contact list schemas into the
standard shape the pipeline needs: company_name, email, website, contact_name.

Also handles deciding whether a "name" field is a real person's name (use
"Hello <First Name>,") or not (use "Hello <Company> team," instead) — this
is resolved in code (deterministic, testable) rather than left to the AI
to guess, since AI guessing here would be inconsistent across 20k+ rows.
"""

import sys
import json
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Domains that are personal/free email providers, not a company's own site —
# scraping these as a "company website" would just return junk.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "gmx.com", "gmx.de", "protonmail.com", "live.com", "msn.com",
    "yahoo.fr", "orange.fr", "free.fr", "laposte.net", "web.de",
}

# Tokens that mean "this is not a real individual's name" when found as the
# whole name or a distinct word within it.
GENERIC_NAME_TOKENS = {
    "team", "recruiting", "recruitment", "hr", "human", "resources", "careers",
    "career", "jobs", "contact", "info", "support", "sales", "admin",
    "no-reply", "noreply", "service", "talent", "marketing", "n/a", "na",
    "unknown", "none", "undisclosed", "respectivly", "respectively",
}

LEGAL_SUFFIXES = {
    "gmbh", "inc", "ltd", "llc", "sarl", "sas", "corp", "plc", "srl", "bv",
    "ag", "kg", "spa", "nv", "oy", "ab", "co",
}


def is_valid_email(value) -> bool:
    return isinstance(value, str) and bool(EMAIL_RE.match(value.strip()))


def extract_company_name(attributes_raw, email: str) -> str:
    """
    Pulls the company name out of the 'attributes' JSON column
    (e.g. '{"Company": "Rtone"}'). Falls back to a capitalized guess from
    the email domain if parsing fails or the key is missing.
    """
    if isinstance(attributes_raw, str) and attributes_raw.strip():
        try:
            data = json.loads(attributes_raw)
            company = data.get("Company") or data.get("company")
            if company and isinstance(company, str) and company.strip():
                return company.strip()
        except (json.JSONDecodeError, AttributeError):
            pass

    return _company_name_from_domain(email)


def _company_name_from_domain(email: str) -> str:
    if not is_valid_email(email):
        return "your company"
    domain = email.split("@")[-1].split(".")[0]
    return domain.replace("-", " ").replace("_", " ").title()


def derive_website_from_email(email: str) -> str:
    """
    Best-effort website guess from the email domain, skipped entirely for
    personal email providers (gmail.com etc.) where there's nothing
    meaningful to scrape.
    """
    if not is_valid_email(email):
        return ""
    domain = email.split("@")[-1].strip().lower()
    if domain in PERSONAL_EMAIL_DOMAINS:
        return ""
    return f"https://{domain}"


def resolve_greeting_name(contact_name, company_name: str) -> str | None:
    """
    Returns the first name to greet with if `contact_name` looks like a real
    individual's name, otherwise None (caller should fall back to
    "<company> team").
    """
    if not contact_name or not isinstance(contact_name, str):
        return None

    name = contact_name.strip()
    if len(name) < 3:
        return None

    lower = name.lower()

    if lower in GENERIC_NAME_TOKENS:
        return None

    if "@" in name:
        return None

    if any(ch.isdigit() for ch in name):
        return None

    words = re.split(r"[\s/]+", lower)
    if any(w in GENERIC_NAME_TOKENS for w in words):
        return None
    if any(w.strip(".") in LEGAL_SUFFIXES for w in words):
        return None

    if company_name and lower == company_name.strip().lower():
        return None

    first_token = name.split()[0]
    # Title-case it in case the source data was all lower/upper case
    return first_token[0].upper() + first_token[1:] if first_token else None


# Mailbox providers: an address there says nothing about the company's name.
_MAIL_PROVIDERS = {"gmail", "googlemail", "yahoo", "hotmail", "outlook", "live", "icloud", "me",
                   "protonmail", "proton", "gmx", "orange", "laposte", "free", "wanadoo", "aol",
                   "web", "t-online", "bluewin", "hispeed", "sfr", "yandex", "mail", "zoho"}


def _squash(text: str) -> str:
    import unicodedata
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", plain)


def _domain_label(website: str, email: str) -> str:
    """The distinctive part of the company's domain: 'connect-i' for
    https://www.connect-i.ch, or for aminck@connect-i.ch when there's no site."""
    host = re.sub(r"^[a-z]+://", "", (website or "").strip().lower()).split("/")[0].split(":")[0]
    if not host and "@" in (email or ""):
        host = email.rsplit("@", 1)[1].strip().lower()
    labels = [part for part in host.split(".") if part and part != "www"]
    if len(labels) < 2:
        return ""
    # example.co.uk -> example; example.ch -> example
    label = labels[-3] if len(labels) >= 3 and len(labels[-2]) <= 3 and len(labels[-1]) == 2 else labels[-2]
    return "" if label in _MAIL_PROVIDERS else label


def _matches_domain(name: str, label: str) -> bool:
    name_key, label_key = _squash(name), _squash(label)
    if not name_key or len(label_key) < 2:
        return False
    if label_key in name_key or name_key in label_key:
        return True
    words = [_squash(w) for w in re.split(r"[\s\-&,./()+]+", name or "")]
    words = [w for w in words if w and w not in _NAME_FILLER]
    if any(len(w) >= 3 and w in label_key for w in words):
        return True
    # "Euro Tech Conseil" -> etcinfo.fr
    initials = "".join(w[0] for w in words)
    return len(initials) >= 2 and label_key.startswith(initials)


_NAME_FILLER = {"the", "and", "for", "des", "les", "and", "und", "der", "die", "das", "von", "van",
                "of", "de", "du", "la", "le", "et", "in", "en", "sa", "ag", "gmbh", "sas", "sarl",
                "ltd", "inc", "llc", "bv", "nv", "srl", "spa", "group", "groupe", "france"}


def name_for_email(list_name: str, site_name: str | None, website: str = "", email: str = "") -> str:
    """The company name to write in the email.

    Usually the name from the user's own list. But lists gathered by scanning
    sometimes hold a product instead of the company ("Opigno LMS" for
    Connect-i, whose product it is), and the email then praises the product
    for the company's other work. When the website names its owner, that
    name matches the domain, and the list's name doesn't, the website wins.
    """
    list_name = (list_name or "").strip()
    site_name = (site_name or "").strip()
    if not site_name or not list_name:
        return list_name or site_name
    if _squash(site_name) == _squash(list_name):
        return list_name
    label = _domain_label(website, email)
    if label and _matches_domain(site_name, label) and not _matches_domain(list_name, label):
        return site_name
    return list_name


def build_greeting(contact_name, company_name: str, lang: str = "en") -> str:
    """Returns the exact opening line to use, e.g. 'Dear Charly,' or
    'Dear Rtone Team,' — or, in French, 'Bonjour Charly,' / 'Madame, Monsieur,'."""
    first_name = resolve_greeting_name(contact_name, company_name)
    if lang == "fr":
        return f"Bonjour {first_name}," if first_name else "Madame, Monsieur,"
    if first_name:
        return f"Dear {first_name},"
    return f"Dear {company_name} Team,"


def make_console_encoding_safe():
    """Never let an unprintable character in company data kill a run.

    Windows consoles default to a legacy code page (cp1252 here), which has no
    mapping for characters that turn up in scraped company names all the time —
    a zero-width space, a CJK character, an emoji. `print()`-ing one raises
    UnicodeEncodeError from deep inside a worker thread, and because the
    pipeline's own error handler prints the same name, the handler raises too
    and the company disappears from the run without ever being counted.

    Switching the streams to errors="replace" keeps the console's encoding
    (so nothing else about the output changes) and turns an unmappable
    character into "?" instead of an exception.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # redirected to something that isn't a TextIOWrapper
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
