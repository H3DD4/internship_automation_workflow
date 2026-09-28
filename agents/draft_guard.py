"""Deterministic checks on a generated draft, run before it is ever shown
as "ready to send".

The system prompt asks the model not to invent things; this module verifies
it actually didn't. Everything here is mechanical — no second LLM call, no
judgement — so it behaves identically on every run and can't itself
hallucinate. A rejection is fed back to the writer as a concrete complaint
and the draft is regenerated.

The expensive failure this exists to prevent is a confident invented claim
("with over 500 enterprise clients", "since your Series B") going out under
the applicant's name to a company that knows the truth.
"""

import re


class GuardRejection(Exception):
    """A draft violated a verifiable rule. The message is fed back to the model."""


# Phrases that are either unverifiable flattery or standard filler. Matching
# is case-insensitive and on word boundaries.
BANNED_PHRASES = [
    "industry leader", "industry-leading", "market leader", "world-class",
    "award-winning", "renowned", "fast-growing", "cutting-edge", "best-in-class",
    "i have long admired", "i have always admired", "i've long admired",
    "i am writing to express", "i hope this email finds you well",
    "to whom it may concern", "dear hiring manager", "dear sir or madam",
    "your recent funding", "series a", "series b", "series c",
    "i have used your", "i've used your", "i am a user of",
    "as a longtime follower", "i follow your blog", "i saw your job posting",
    "i read your recent blog",
]

# Typographic look-alikes folded to their ASCII form before phrase matching.
# Models (and websites) routinely write "cutting‑edge" with a non-breaking
# hyphen (U+2011) or an en dash, which the plain "cutting-edge" never matched
# — a banned phrase reached a live draft that way. Apostrophes fold too, so
# "i’ve long admired" is caught like "i've long admired".
_LOOKALIKES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", "­": "",
    "‘": "'", "’": "'", "ʼ": "'",
    " ": " ", " ": " ", " ": " ",
})


def fold_for_matching(text: str) -> str:
    """Lower-case `text` with look-alike punctuation reduced to ASCII and
    runs of whitespace collapsed, so a phrase can't slip past a check by
    being typed with a different hyphen, apostrophe or space."""
    return re.sub(r"\s+", " ", (text or "").translate(_LOOKALIKES).lower())


def find_banned_phrase(text: str) -> str | None:
    """The first banned phrase in `text`, however it is typeset, or None."""
    folded = fold_for_matching(text)
    return next((phrase for phrase in BANNED_PHRASES if phrase in folded), None)


# Numbers that legitimately appear as ordinary prose rather than a claim.
_ALWAYS_ALLOWED_NUMBERS = {"1", "2", "24", "7"}

_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}|\[[A-Za-z ]+\]|XXX|TODO|Lorem ipsum", re.IGNORECASE)


def _numbers_in(text: str) -> set:
    """Bare digit strings in `text`, normalised (commas stripped, trailing
    '.0' style decimals kept as written)."""
    return {match.group(0).replace(",", "").rstrip(".") for match in _NUMBER_RE.finditer(text)}


def _allowed_numbers(facts: dict, research: dict) -> set:
    """Every number the draft is permitted to contain: the applicant's own
    verified figures, plus any number that actually appeared in the research
    (so quoting the company's own '24/7 SOC' is fine)."""
    allowed = set(_ALWAYS_ALLOWED_NUMBERS)
    allowed.update(str(n) for n in facts.get("allowed_numbers", []))

    for value in facts.values():
        if isinstance(value, (str, int)):
            allowed.update(_numbers_in(str(value)))
        elif isinstance(value, list):
            for item in value:
                allowed.update(_numbers_in(str(item)))

    # The hook was already verified against the site text, so a number it
    # quotes (e.g. "24/7 monitoring") is the company's own, not an invention.
    for key in ("company_hook", "hook_evidence"):
        allowed.update(_numbers_in(str(research.get(key, "") or "")))

    return allowed


def check_draft(draft: dict, *, facts: dict, research: dict, company_name: str,
                greeting: str, applicant_name: str) -> None:
    """Raise GuardRejection with a specific, actionable complaint, or return
    None if the draft is clean."""
    subject = (draft.get("subject") or "").strip()
    body = (draft.get("body") or "").strip()
    problems = []

    if not subject:
        problems.append("The subject line is empty.")
    if not body:
        raise GuardRejection("The email body is empty.")

    # --- Structure ---
    if not body.startswith(greeting):
        problems.append(
            f"The first line must be exactly {greeting!r}, but the body starts "
            f"{body.splitlines()[0][:60]!r}."
        )
    if applicant_name and applicant_name.split()[0].lower() not in body.lower()[-160:]:
        problems.append(f"The email must be signed off with {applicant_name!r} at the end.")

    # --- Leftover template scaffolding ---
    placeholder = _PLACEHOLDER_RE.search(body) or _PLACEHOLDER_RE.search(subject)
    if placeholder:
        problems.append(
            f"Unfilled placeholder {placeholder.group(0)!r} was left in the text."
        )

    # --- Length ---
    word_count = len(body.split())
    if word_count < 120:
        problems.append(f"The body is only {word_count} words; it must be at least 120.")
    elif word_count > 400:
        problems.append(f"The body is {word_count} words; it must stay under 400.")

    # --- Unverifiable flattery and filler ---
    lowered = body.lower()
    folded = fold_for_matching(body)
    for phrase in BANNED_PHRASES:
        if phrase in folded:
            problems.append(
                f"The phrase {phrase!r} is not supported by the research and must be removed."
            )

    # --- Invented numbers ---
    # Digits in the company's own name ("42 Labs", "1Password") or in the
    # greeting's contact name aren't claims.
    allowed = _allowed_numbers(facts, research) | _numbers_in(company_name) | _numbers_in(greeting)
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        for number in _numbers_in(sentence):
            if number not in allowed:
                problems.append(
                    f"The number {number!r} does not appear in the applicant's verified facts "
                    f"or in the company research, so it cannot be stated. Offending sentence: "
                    f"{sentence.strip()[:130]!r}"
                )

    # --- A claim of interest in their specific work needs a verified hook ---
    hook = (research.get("company_hook") or "").strip()
    if not hook and f"interests me most about {company_name.lower()}" in lowered:
        problems.append(
            "No verified phrase from the company's website was available, so the email "
            "must not claim a specific interest in their work."
        )

    if problems:
        raise GuardRejection(" ".join(problems))
