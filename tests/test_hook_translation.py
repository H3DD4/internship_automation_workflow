"""Hook translation: a verified foreign phrase is rendered into English.

The list is largely European, so a hook copied from a company's own site is
often French, Italian or German. Quoting it verbatim stayed truthful but
grafted a foreign clause into English prose:

    "...is your work on soluzioni personalizzate per un'infrastruttura IT..."

The safety argument is the ORDER: the hook is verified against the site in
its original language, and only then translated. These tests pin that order
and pin what happens when a translation can't be trusted.
"""

import json
from types import SimpleNamespace

from agents.research_agent import (
    looks_already_english,
    still_looks_foreign,
    translate_hook,
    verify_hook,
)

FRENCH = "Optimisation FinOps continue, pas un audit ponctuel"
ITALIAN = "soluzioni personalizzate per un'infrastruttura IT performante"
GERMAN = "Frontier Engine: die KI-Plattform für autonome AI-Agenten im Unternehmen"
ENGLISH = "merge queue that batches pull requests and tests them together"


class FakeClient:
    """Returns a scripted translation; records whether it was called at all."""

    def __init__(self, english):
        self.english = english
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        payload = json.dumps({"english": self.english})
        return SimpleNamespace(content=[SimpleNamespace(text=payload)])


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def test_foreign_phrases_are_detected():
    """What drives the decision to translate is looks_already_english(), which
    needs positive evidence of English. still_looks_foreign() needs positive
    evidence of another language, and a phrase can be foreign with neither —
    ITALIAN below holds no accent and no function word from either list."""
    for phrase in (FRENCH, ITALIAN, GERMAN):
        assert not looks_already_english(phrase), phrase
    for phrase in (FRENCH, GERMAN):
        assert still_looks_foreign(phrase), phrase


def test_an_unchanged_answer_is_not_mistaken_for_a_translation():
    """The gap the asymmetry above opens: a model that echoes the phrase back
    would otherwise be accepted, because nothing in it looks foreign."""
    client = FakeClient(ITALIAN)
    hook, note = translate_hook(client, "m", ITALIAN, "site text")
    assert hook == ITALIAN
    assert "did not reach English" in note


def test_english_phrases_are_left_alone():
    for phrase in (ENGLISH,
                   "GPU-as-a-Service solutions that maximize intelligence per Watt",
                   "agentic AI platform built specifically for regulated industries",
                   "systematic and independent evaluation of your AI solution"):
        assert looks_already_english(phrase), phrase
        assert not still_looks_foreign(phrase), phrase


def test_a_good_translation_need_not_contain_english_function_words():
    """"continuous FinOps optimisation, not a one-off audit" holds none of the
    markers that mark a phrase as English up front. Judging a finished
    translation by that test put the French phrase back untranslated."""
    translated = "continuous FinOps optimisation, not a one-off audit"
    assert not looks_already_english(translated)   # no positive marker…
    assert not still_looks_foreign(translated)     # …but nothing foreign left


def test_an_english_hook_never_costs_a_model_call():
    client = FakeClient("should not be used")
    hook, note = translate_hook(client, "m", ENGLISH, "site text")
    assert hook == ENGLISH and note == "" and client.calls == 0


# ---------------------------------------------------------------------------
# Translation, and what happens when it can't be trusted
# ---------------------------------------------------------------------------

def test_a_foreign_hook_is_translated():
    client = FakeClient("custom solutions for a high-performance, secure IT infrastructure")
    hook, note = translate_hook(client, "m", ITALIAN, "site text")
    assert hook == "custom solutions for a high-performance, secure IT infrastructure"
    assert note == "translated into English"
    assert client.calls == 1


def test_proper_names_survive_translation():
    client = FakeClient("the Frontier Engine AI platform for autonomous AI agents in the enterprise")
    hook, _ = translate_hook(client, "m", GERMAN, "site text")
    assert "Frontier Engine" in hook


def test_a_translation_that_invents_a_number_is_discarded():
    """The one thing translation must never do: add a claim. The original is
    already verified against the site, so falling back to it is always safe."""
    client = FakeClient("continuous FinOps optimisation for over 500 clients")
    hook, note = translate_hook(client, "m", FRENCH, "site text")
    assert hook == FRENCH
    assert "invented a number" in note


def test_a_translation_that_is_still_foreign_is_discarded():
    client = FakeClient("optimisation continue, pas un audit")
    hook, note = translate_hook(client, "m", FRENCH, "site text")
    assert hook == FRENCH
    assert "did not reach English" in note


def test_a_translation_that_adds_banned_flattery_is_discarded():
    client = FakeClient("continuous FinOps optimisation from an industry leader")
    hook, note = translate_hook(client, "m", FRENCH, "site text")
    assert hook == FRENCH
    assert "industry leader" in note


def test_no_translator_available_means_standard_wording_not_a_foreign_clause():
    """With no model free to translate, the email falls back to its standard
    English wording. Keeping the French would put a foreign clause in an
    English email — exactly what translation exists to prevent."""
    class Exploding:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("provider down")

    hook, note = translate_hook(Exploding(), "m", FRENCH, "site text")
    assert hook == ""
    assert "no translation model was available" in note


# ---------------------------------------------------------------------------
# The order that makes it safe
# ---------------------------------------------------------------------------

def test_verification_still_runs_against_the_original_language():
    """Grounding is established on the company's own words, before any
    translation exists — so a hook the site doesn't support is dropped
    whatever language it is in."""
    site = "Nous proposons une optimisation FinOps continue, pas un audit ponctuel."
    kept, status = verify_hook(FRENCH, site, site)
    assert kept and status == "grounded"

    invented = "leadership mondial sur le marché du cloud souverain"
    dropped, why = verify_hook(invented, site, site)
    assert dropped == "" and "appear on the site" in why


# ---------------------------------------------------------------------------
# The research prompt must not invite a translation
# ---------------------------------------------------------------------------

def test_the_research_prompt_demands_the_websites_own_language():
    """Measured on 6 previously-rejected companies: 5 recovered after this
    instruction was added.

    verify_hook() scores the hook's words against the site text, so a hook the
    model helpfully translated into English scores near zero against a German
    or French site and is discarded — "only 0% of its words appear on the
    site" for a hook that was perfectly accurate. Answering in the site's own
    language is what makes verification possible; translate_hook() then renders
    it into English AFTER it has been verified.
    """
    from agents.research_agent import RESEARCH_SYSTEM_PROMPT_TEMPLATE

    prompt = RESEARCH_SYSTEM_PROMPT_TEMPLATE.lower()
    assert "website's own language" in prompt
    assert "do not translate" in prompt
    # It must also say WHY, so the rule survives a future prompt edit.
    assert "checked" in prompt and "thrown away" in prompt


def test_verification_and_translation_run_in_that_order():
    """The safety property the two halves depend on: grounding is established
    on the site's own words, and translation only ever rewrites a phrase that
    already passed."""
    import inspect
    from agents.research_agent import get_company_context

    source = inspect.getsource(get_company_context)
    assert source.index("verify_hook(") < source.index("translate_hook(")


# ---------------------------------------------------------------------------
# Numbers across languages
# ---------------------------------------------------------------------------

def test_a_number_written_the_french_way_survives_translation():
    """French writes "26 000" (with a narrow no-break space), English "26,000".
    Comparing raw strings saw "26,000" as a new number and threw away a
    correct translation, leaving the French in the email."""
    french = "déploiement de 26 000 bornes de recharge"
    client = FakeClient("deployment of 26,000 charging stations")
    hook, note = translate_hook(client, "m", french, "site text")
    assert hook == "deployment of 26,000 charging stations"
    assert note == "translated into English"


def test_thousands_separators_compare_by_value():
    from agents.research_agent import _number_values
    for written in ("26 000", "26 000", "26 000", "26.000", "26,000", "26'000"):
        assert _number_values(written) == {"26000"}, written


def test_a_genuinely_new_number_is_still_caught_after_normalising():
    """Normalising the format must not open a hole: a different quantity is
    still an invented claim."""
    french = "déploiement de 26 000 bornes de recharge"
    client = FakeClient("deployment of 27,000 charging stations")
    hook, note = translate_hook(client, "m", french, "site text")
    assert hook == french
    assert "invented a number" in note


def test_separate_numbers_are_not_merged_into_one():
    from agents.research_agent import _number_values
    assert _number_values("founded 2014, 12 offices") == {"2014", "12"}


# ---------------------------------------------------------------------------
# Banned phrases can't hide behind typography
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

LOOKALIKE_SPELLINGS = [
    "cutting‑edge",   # non-breaking hyphen — what actually reached a draft
    "cutting‐edge",   # hyphen
    "cutting–edge",   # en dash
    "Cutting-Edge",        # plain, capitalised
]


@pytest.mark.parametrize("spelling", LOOKALIKE_SPELLINGS)
def test_a_banned_phrase_is_caught_however_it_is_typeset(spelling):
    from agents.draft_guard import find_banned_phrase
    assert find_banned_phrase(f"trained in {spelling} practices") == "cutting-edge"


def test_a_curly_apostrophe_does_not_hide_filler():
    from agents.draft_guard import find_banned_phrase
    assert find_banned_phrase("I’ve long admired your team") == "i've long admired"


def test_the_translation_that_leaked_it_is_now_refused():
    """The Jetdev case, verbatim: "technologies de pointe" came back as
    "cutting‑edge" with U+2011, and the plain-hyphen check let it through."""
    french = ("développeurs impliqués et un vrai collectif formé aux pratiques "
              "et technologies de pointe")
    client = FakeClient("engaged developers and a true collective trained in "
                        "cutting‑edge practices and technologies")
    hook, note = translate_hook(client, "m", french, "site text")
    assert hook == french
    assert "cutting-edge" in note


def test_verify_hook_refuses_a_lookalike_banned_phrase():
    site = "We build cutting‑edge fraud detection for regional banks."
    kept, why = verify_hook("cutting‑edge fraud detection for regional banks", site, site)
    assert kept == "" and "cutting-edge" in why


def test_the_full_draft_guard_refuses_a_lookalike_banned_phrase():
    """The last line of defence before "ready" has to be as strict as the
    earlier ones, or a phrase that dodged them sails through here too."""
    from agents.draft_guard import GuardRejection, check_draft
    body = ("Dear Ana,\n\n" + "I build detection tooling for security teams. " * 20
            + "Your cutting‑edge platform interests me.\n\nBest regards,\nMohamed Hedda")
    with pytest.raises(GuardRejection, match="cutting-edge"):
        check_draft({"subject": "Internship", "body": body}, facts={}, research={},
                    company_name="Acme", greeting="Dear Ana,",
                    applicant_name="Mohamed Hedda")


# ---------------------------------------------------------------------------
# The hook has to read as a phrase after "your work on"
# ---------------------------------------------------------------------------

from agents.research_agent import _normalise_hook  # noqa: E402


def test_a_named_sentence_becomes_an_appositive():
    """Real hook: "…is your work on Factory 3D is a powerful software platform"."""
    hook = "Factory 3D is a powerful software platform to streamline the Definition process"
    assert _normalise_hook(hook, "") == (
        "Factory 3D, a powerful software platform to streamline the Definition process")


def test_a_lone_capital_article_is_lowered():
    """Real hook: "…is your work on A no-code lab for machine vision"."""
    assert _normalise_hook("A no-code lab for machine vision", "") == "a no-code lab for machine vision"


@pytest.mark.parametrize("hook", [
    "managed detection and response for mid-sized businesses",   # already a phrase
    "a platform that is a joy to use for engineers",             # "is a" not after a name
    "Kubernetes operators for regulated industries",             # no copula at all
])
def test_phrases_that_already_read_well_are_untouched(hook):
    assert _normalise_hook(hook, "we run Kubernetes daily") == hook


def test_the_appositive_keeps_every_word_of_the_claim():
    """The repair is grammatical only: nothing the site said is dropped, so
    grounding checked on the original still holds."""
    hook = "ReefIQ is the biological data substrate for discovery teams"
    fixed = _normalise_hook(hook, "")
    assert fixed == "ReefIQ, the biological data substrate for discovery teams"
    assert set(fixed.replace(",", "").split()) == set(hook.split()) - {"is"}


# ---------------------------------------------------------------------------
# Found by reading every hook in the live drafts
# ---------------------------------------------------------------------------

def test_a_title_case_heading_is_lowered_throughout():
    """Was: "penetration Testing & Vulnerability Assessments"."""
    assert _normalise_hook("Penetration Testing & Vulnerability Assessments", "") == \
        "penetration testing & vulnerability assessments"


def test_a_heading_keeps_names_and_acronyms():
    site = "we deliver managed services on Kubernetes for every client"
    assert _normalise_hook("Managed Kubernetes Services For SAP Teams", site) == \
        "managed Kubernetes services for SAP teams"


def test_a_normal_phrase_with_one_name_is_not_treated_as_a_heading():
    """Only a phrase where EVERY content word is capitalised is a heading;
    otherwise the capitals inside are someone's names and stay."""
    assert _normalise_hook("Consulting for Red Hat and Google Cloud users", "") == \
        "consulting for Red Hat and Google Cloud users"


def test_french_ia_becomes_ai():
    assert _normalise_hook("pure-player of IA transformation", "") == "pure-player of AI transformation"


@pytest.mark.parametrize("hook", [
    "free trial month for internet subscriptions",
    "more than 26,000 references all around the cable",
    "20% off managed hosting plans this month",
    "discount on annual cloud subscriptions for startups",
])
def test_an_offer_or_a_quantity_is_not_their_work(hook):
    kept, why = verify_hook(hook, hook, hook)
    assert kept == "" and "not their work" in why


@pytest.mark.parametrize("hook", [
    "24/7 monitoring and security maintenance for client systems",
    "hands-free voice interfaces for industrial workers",
    "free and open source platform for collaborative pedagogy",
    "effortless deployment of private 5G networks",
])
def test_real_work_that_merely_contains_a_number_or_free_is_kept(hook):
    kept, why = verify_hook(hook, hook, hook)
    assert kept, why


def test_a_translation_that_turns_out_to_be_a_quantity_is_dropped():
    """Checked on the English, since the French original has no English words
    to match — and keeping the French wouldn't make it any better."""
    french = "plus de 26\u202f000 références tout autour du câble"
    client = FakeClient("more than 26,000 references all around the cable")
    hook, note = translate_hook(client, "m", french, "site text")
    assert hook == "" and "not their work" in note
