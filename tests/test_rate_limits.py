"""Pacing AI calls to the provider's token budget, and routing translation.

Groq's free tier allows 8,000 tokens per minute per model and one research
call is ~2,200. Firing blind produced 84 rate-limit errors in a 50-company run,
each with a guessed 2-4 s backoff and, after three, a downgrade to a weaker
model — which is how a hook came out as "solving systems" for soldering.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_client import (
    CompatibleAIClient,
    TokenBudget,
    _parse_duration,
    estimate_tokens,
    resolve_ai_settings,
)


class FakeClock:
    """A clock that only moves when something sleeps, so waits are exact."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


GROQ_HEADERS = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "8000"}
KEY = ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def budget(clock):
    return TokenBudget(clock=clock, sleep=clock.sleep)


# ---------------------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------------------

def test_an_endpoint_that_sends_no_budget_headers_is_never_paced(budget, clock):
    assert budget.reserve(KEY, 5000) == 0
    assert clock.slept == []


def test_a_call_that_fits_goes_straight_through(budget, clock):
    budget.observe(KEY, GROQ_HEADERS)
    assert budget.reserve(KEY, 2200) == 0
    assert clock.slept == []


def test_a_call_that_doesnt_fit_waits_only_for_the_refill_it_needs(budget, clock):
    budget.observe(KEY, {"x-ratelimit-limit-tokens": "8000",
                         "x-ratelimit-remaining-tokens": "1000"})
    budget.reserve(KEY, 2200)
    # 1,200 missing tokens at 8000/60 per second = 9 s (+ a 50 ms margin).
    assert sum(clock.slept) == pytest.approx(9.05, abs=0.01)


def test_concurrent_workers_queue_instead_of_all_firing(budget, clock):
    """Three research workers each seeing "7,000 left" and all firing is how
    the old client burned through the budget into a 429."""
    budget.observe(KEY, {"x-ratelimit-limit-tokens": "8000",
                         "x-ratelimit-remaining-tokens": "7000"})
    budget.reserve(KEY, 2200)
    budget.reserve(KEY, 2200)
    budget.reserve(KEY, 2200)
    assert clock.slept == []            # 6,600 of 7,000: all three fit
    budget.reserve(KEY, 2200)           # the fourth has to wait
    assert sum(clock.slept) > 0


def test_a_rate_limit_is_waited_out_once_not_twice(budget, clock):
    """"Try again in 6.5s" already means "your request fits after 6.5 s".
    Sleeping that long AND then reserving the tokens again would double it."""
    budget.observe(KEY, GROQ_HEADERS)
    assert budget.exhausted(KEY, 6.5, needed=2200) is True
    budget.reserve(KEY, 2200)
    assert sum(clock.slept) == pytest.approx(6.55, abs=0.01)


def test_a_rate_limit_without_a_stated_wait_falls_back_to_backoff(budget):
    budget.observe(KEY, GROQ_HEADERS)
    assert budget.exhausted(KEY, None, needed=2200) is False


def test_an_oversized_request_waits_for_a_full_bucket_rather_than_forever(budget, clock):
    budget.observe(KEY, {"x-ratelimit-limit-tokens": "8000",
                         "x-ratelimit-remaining-tokens": "0"})
    budget.reserve(KEY, 50_000)
    assert sum(clock.slept) <= TokenBudget.MAX_WAIT + 1


def test_models_have_separate_budgets(budget, clock):
    """The provider limits per model, so one model's empty bucket must not
    stall calls to another."""
    other = (KEY[0], "openai/gpt-oss-20b")
    budget.observe(KEY, {"x-ratelimit-limit-tokens": "8000",
                         "x-ratelimit-remaining-tokens": "0"})
    budget.observe(other, GROQ_HEADERS)
    budget.reserve(other, 2200)
    assert clock.slept == []


@pytest.mark.parametrize("text, seconds", [
    ("577ms", 0.577), ("6.48s", 6.48), ("1m2.3s", 62.3), ("50m24s", 3024.0), ("", None),
])
def test_provider_durations_parse(text, seconds):
    parsed = _parse_duration(text)
    assert parsed == (pytest.approx(seconds) if seconds is not None else None)


def test_the_token_estimate_errs_high():
    """Under-reserving leads straight back into a 429, so the estimate is
    deliberately above the ~4 characters per token of English."""
    text = "x" * 6400
    assert estimate_tokens("", [{"role": "user", "content": text}]) >= 6400 / 4


# ---------------------------------------------------------------------------
# The client, end to end through a real 429
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload, headers):
        self.status_code = status_code
        self.headers = headers
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)


def test_the_client_waits_out_a_429_exactly_as_long_as_the_provider_says(clock):
    ok = {"choices": [{"message": {"content": '{"ok": true}'}}]}
    limited = {"error": {"message": "Rate limit reached ... Please try again in 3.25s."}}
    responses = [
        FakeResponse(429, limited, {"x-ratelimit-limit-tokens": "8000",
                                    "x-ratelimit-remaining-tokens": "0"}),
        FakeResponse(200, ok, {"x-ratelimit-limit-tokens": "8000",
                               "x-ratelimit-remaining-tokens": "5000"}),
    ]
    # A no-op request limiter, so the only sleeping that can happen is the
    # budget's (on the fake clock) or a blind backoff (which must not).
    client = CompatibleAIClient("k", "https://api.groq.com/openai/v1",
                                rate_limiter=SimpleNamespace(wait=lambda: None))
    client.token_budget = TokenBudget(clock=clock, sleep=clock.sleep)

    with patch("ai_client._http_post", side_effect=responses) as post, \
         patch("ai_client.time.sleep") as blind_backoff:
        result = client.messages.create(model="openai/gpt-oss-120b", max_tokens=50,
                                        system="s", messages=[{"role": "user", "content": "hi"}])

    assert result.content[0].text == '{"ok": true}'
    assert post.call_count == 2
    # One wait, of the stated length — and no guessed exponential backoff.
    assert sum(clock.slept) == pytest.approx(3.30, abs=0.01)
    blind_backoff.assert_not_called()


# ---------------------------------------------------------------------------
# Translation model routing
# ---------------------------------------------------------------------------

def test_translation_defaults_to_the_research_model():
    """Measured on this account: the smaller Groq models turned "Lötsyteme"
    into "welding" and dropped the product name "Frontier Engine", so the
    split is opt-in."""
    settings = resolve_ai_settings({"AI_PROVIDER": "groq", "AI_MODEL": "openai/gpt-oss-120b"})
    assert settings["translation_model"] == "openai/gpt-oss-120b"


def test_translation_model_can_be_set_separately():
    settings = resolve_ai_settings({"AI_PROVIDER": "groq", "AI_MODEL": "openai/gpt-oss-120b",
                                    "AI_TRANSLATION_MODEL": "some/other-model"})
    assert settings["translation_model"] == "some/other-model"


def test_research_and_translation_go_to_their_own_models(monkeypatch):
    import agents.research_agent as research_agent

    site = "Nous proposons une optimisation FinOps continue, pas un audit ponctuel."
    monkeypatch.setattr(research_agent, "fetch_website_text", lambda url: site)

    calls = []

    class Recorder:
        def __init__(self):
            self.messages = self

        def create(self, model, messages, **kwargs):
            calls.append(model)
            if "Website text" in messages[0]["content"]:
                answer = {"areas": [], "hook": "optimisation FinOps continue, pas un audit ponctuel",
                          "hook_evidence": site, "industry": "", "summary": ""}
            else:
                answer = {"english": "continuous FinOps optimisation, not a one-off audit"}
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(answer))])

    context = research_agent.get_company_context(
        Recorder(), "research-model", "Acme", "https://acme.example", [],
        translation_model="translation-model")

    assert calls == ["research-model", "translation-model"]
    assert context["company_hook"] == "continuous FinOps optimisation, not a one-off audit"
    assert context["hook_original"] == "optimisation FinOps continue, pas un audit ponctuel"
