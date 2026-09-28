"""The model router: every AI call keeps working when a provider doesn't.

Each scenario here is one that actually happened on this project:
  - Groq's free tier allows 1,000 requests/day per model; ~1,700 are needed.
  - Groq's 8,000 tokens/minute produced 84 rate-limit errors in one run.
  - A weaker model mistranslated "soldering" as "welding" with no error.
  - OpenCode's key answers 401; Mistral's free plan answers 429 with a
    limit of 0 until activated; mistral-large answers 403 on the free tier.
"""

import json
from types import SimpleNamespace

import pytest

from ai_client import AIProviderError
from model_router import AllModelsUnavailable, Deployment, ModelRouter, build_router


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeProvider:
    """A provider whose calls follow a script: an exception to raise, a
    string to answer with, or a (text, headers) pair."""

    def __init__(self, script=None, default="ok"):
        self.script = list(script or [])
        self.default = default
        self.calls = []
        self.messages = self

    def create(self, *, model, reasoning_effort=None, **kwargs):
        self.calls.append({"model": model, "reasoning_effort": reasoning_effort})
        step = self.script.pop(0) if self.script else self.default
        if isinstance(step, Exception):
            raise step
        text, headers = step if isinstance(step, tuple) else (step, {})
        return SimpleNamespace(content=[SimpleNamespace(text=text)], headers=headers)


def rate_limited(retry_after=None, headers=None):
    return AIProviderError("429", kind="rate_limit", status=429,
                           retry_after=retry_after, headers=headers or {})


def dep(provider, model, client, tiers, **kw):
    return Deployment(provider=provider, model=model, client=client, tiers=tiers, **kw)


def router(deployments, clock, **kw):
    return ModelRouter(deployments, clock=clock, sleep=clock.sleep, log=lambda *_: None, **kw)


def call(r, task="research", avoid=()):
    return r.complete(task=task, system="s", messages=[{"role": "user", "content": "x"}],
                      max_tokens=50, avoid=avoid)


@pytest.fixture
def clock():
    return Clock()


# ---------------------------------------------------------------------------
# Choosing a model
# ---------------------------------------------------------------------------

def test_the_best_tier_is_used_while_it_is_available(clock):
    groq, weak = FakeProvider(), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1}),
                dep("groq", "gpt-oss-20b", weak, {"research": 2})], clock)
    for _ in range(3):
        assert call(r).model == "gpt-oss-120b"
        clock.now += 5
    assert weak.calls == []


def test_two_good_models_on_two_providers_share_the_load(clock):
    """Balancing across providers is what doubles throughput: neither model
    queues behind its own rate limit while the other sits idle."""
    groq, mistral = FakeProvider(), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1}),
                dep("mistral", "mistral-medium", mistral, {"research": 1})], clock)
    used = []
    for _ in range(4):
        used.append(call(r).provider)
        clock.now += 0.1
    assert used == ["groq", "mistral", "groq", "mistral"]


def test_a_task_only_uses_models_cleared_for_it(clock):
    """gpt-oss-20b turned "Lötsyteme" into "welding" — it is research-only."""
    good, weak = FakeProvider(), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", good, {"research": 1, "translation": 1}),
                dep("groq", "gpt-oss-20b", weak, {"research": 2})], clock)
    good.script = [rate_limited(retry_after=600)]
    with pytest.raises(AllModelsUnavailable):
        ModelRouter(r.deployments, clock=clock, sleep=clock.sleep, log=lambda *_: None,
                    max_wait=60).complete(task="translation", system="s",
                                          messages=[{"role": "user", "content": "x"}],
                                          max_tokens=50)
    assert weak.calls == []


def test_reasoning_effort_only_goes_to_models_that_accept_it(clock):
    groq, mistral = FakeProvider(), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1}, reasoning=True),
                dep("mistral", "mistral-medium", mistral, {"research": 1})], clock)
    r.complete(task="research", system="s", messages=[], max_tokens=5, reasoning_effort="low")
    clock.now += 1
    r.complete(task="research", system="s", messages=[], max_tokens=5, reasoning_effort="low")
    assert groq.calls[0]["reasoning_effort"] == "low"
    assert mistral.calls[0]["reasoning_effort"] is None


# ---------------------------------------------------------------------------
# Failing over — staying on quality
# ---------------------------------------------------------------------------

def test_a_rate_limited_model_hands_over_to_the_other_good_model_not_a_weaker_one(clock):
    groq, mistral, weak = FakeProvider([rate_limited(retry_after=60)]), FakeProvider(), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1}),
                dep("mistral", "mistral-medium", mistral, {"research": 1}),
                dep("groq", "gpt-oss-20b", weak, {"research": 2})], clock)
    result = call(r)
    assert result.deployment == "mistral/mistral-medium"
    assert weak.calls == []


def test_groqs_daily_quota_moves_work_to_the_other_provider(clock):
    """The 1,000-requests/day wall: the success that spends the last request
    reports remaining 0, and the model is parked until the stated reset."""
    spent = ("ok", {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2h0m0s"})
    groq, mistral = FakeProvider([spent]), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1}),
                dep("mistral", "mistral-medium", mistral, {"research": 1})], clock)
    call(r)                                  # groq, spending its last request
    for _ in range(3):
        clock.now += 1
        assert call(r).provider == "mistral"
    assert [row["state"] for row in r.snapshot()] == ["cooling down", "ready"]


def test_a_short_wait_for_the_good_model_beats_degrading(clock):
    """A 10-second token-budget wait is cheaper than a mistranslation."""
    good = FakeProvider([rate_limited(retry_after=10), "ok"])
    weak = FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", good, {"research": 1}),
                dep("groq", "gpt-oss-20b", weak, {"research": 2})], clock, degrade_after=25)
    assert call(r).model == "gpt-oss-120b"
    assert weak.calls == []
    assert sum(clock.slept) == pytest.approx(10.05, abs=0.1)


def test_degrading_happens_only_when_every_good_model_is_out_for_long(clock):
    good = FakeProvider([rate_limited(retry_after=3600)])
    weak = FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", good, {"research": 1}),
                dep("groq", "gpt-oss-20b", weak, {"research": 2})], clock, degrade_after=25)
    assert call(r).model == "gpt-oss-20b"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_a_rejected_key_retires_every_model_on_that_provider(clock):
    """OpenCode today: 401 "Invalid credential" on every model."""
    bad = FakeProvider([AIProviderError("401", kind="auth", status=401)])
    good = FakeProvider()
    r = router([dep("opencode", "muse-spark-1.3", bad, {"research": 1}),
                dep("opencode", "other", bad, {"research": 1}),
                dep("groq", "gpt-oss-120b", good, {"research": 1})], clock)
    assert call(r).provider == "groq"
    states = {row["name"]: row["state"] for row in r.snapshot()}
    assert states["opencode/muse-spark-1.3"] == "disabled"
    assert states["opencode/other"] == "disabled"
    assert len(bad.calls) == 1              # never hammered again


def test_a_403_retires_only_that_model(clock):
    """mistral-large on the free plan: 403 "not available in your
    subscription tier" — on a key that works for the other models."""
    mistral = FakeProvider([AIProviderError("403", kind="auth", status=403), "ok"])
    r = router([dep("mistral", "mistral-large", mistral, {"research": 1}),
                dep("mistral", "mistral-medium", mistral, {"research": 1})], clock)
    assert call(r).model == "mistral-medium"
    states = {row["name"]: row["state"] for row in r.snapshot()}
    assert states == {"mistral/mistral-large": "disabled", "mistral/mistral-medium": "ready"}


def test_a_plan_with_a_zero_limit_parks_the_whole_provider(clock):
    """Mistral before plan activation: 429 with x-ratelimit-limit-req-minute: 0.
    Retrying is pointless for every model on the account."""
    zero = rate_limited(headers={"x-ratelimit-limit-req-minute": "0",
                                 "x-ratelimit-remaining-req-minute": "0"})
    mistral, groq = FakeProvider([zero]), FakeProvider()
    r = router([dep("mistral", "mistral-medium", mistral, {"research": 1}),
                dep("mistral", "mistral-small", mistral, {"research": 2}),
                dep("groq", "gpt-oss-120b", groq, {"research": 1})], clock)
    assert call(r).provider == "groq"
    for _ in range(3):
        clock.now += 1
        call(r)
    assert len(mistral.calls) == 1
    reasons = {row["name"]: row["reason"] for row in r.snapshot()}
    assert "activate a plan" in reasons["mistral/mistral-small"]


def test_an_outage_backs_off_and_the_model_is_tried_again_later(clock):
    flaky = FakeProvider([AIProviderError("503", kind="server", status=503), "ok"])
    backup = FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", flaky, {"research": 1}),
                dep("mistral", "mistral-medium", backup, {"research": 1})], clock)
    assert call(r).provider == "mistral"      # during the outage
    clock.now += 10                           # past the 5 s first backoff
    backup.default = rate_limited(retry_after=600)
    assert call(r).provider == "groq"         # half-open: tried again, recovered
    assert r.snapshot()[0]["state"] == "ready"


def test_repeated_failures_back_off_exponentially(clock):
    flaky = FakeProvider([AIProviderError("503", kind="server", status=503)] * 3)
    r = router([dep("groq", "gpt-oss-120b", flaky, {"research": 1})], clock)
    d = r.deployments[0]
    cooldowns = []
    for _ in range(3):
        start = clock.now
        try:
            ModelRouter([d], clock=clock, sleep=clock.sleep, log=lambda *_: None,
                        max_wait=0.01).complete(task="research", system="s",
                                                messages=[], max_tokens=5)
        except AllModelsUnavailable:
            pass
        cooldowns.append(d.cooldown_until - start)
        clock.now = d.cooldown_until
    assert cooldowns == [5.0, 10.0, 20.0]


# ---------------------------------------------------------------------------
# Availability first
# ---------------------------------------------------------------------------

def test_when_everything_is_busy_the_call_waits_instead_of_failing(clock):
    groq = FakeProvider([rate_limited(retry_after=90), "ok"])
    mistral = FakeProvider([rate_limited(retry_after=120)])
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1}),
                dep("mistral", "mistral-medium", mistral, {"research": 1})], clock, max_wait=900)
    assert call(r).provider == "groq"
    assert 90 <= sum(clock.slept) <= 100


def test_it_gives_up_only_past_the_wait_limit_and_says_why(clock):
    groq = FakeProvider([rate_limited(retry_after=3600)])
    r = router([dep("groq", "gpt-oss-120b", groq, {"research": 1})], clock, max_wait=60)
    with pytest.raises(AllModelsUnavailable, match="groq/gpt-oss-120b: rate-limited"):
        call(r)


def test_an_unusable_answer_can_be_retried_on_a_different_model(clock):
    a, b = FakeProvider(), FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", a, {"research": 1}),
                dep("mistral", "mistral-medium", b, {"research": 1})], clock)
    first = call(r)
    second = call(r, avoid={first.deployment})
    assert second.deployment != first.deployment


def test_a_misbehaving_client_cannot_take_the_task_down(clock):
    broken = FakeProvider([KeyError("bug in a client")])
    backup = FakeProvider()
    r = router([dep("x", "m1", broken, {"research": 1}),
                dep("y", "m2", backup, {"research": 1})], clock)
    assert call(r).provider == "y"


# ---------------------------------------------------------------------------
# The research agent on top of the router
# ---------------------------------------------------------------------------

def test_research_moves_to_another_model_when_an_answer_is_unusable(clock, monkeypatch):
    import agents.research_agent as research_agent
    site = "We deliver penetration testing and red teaming for regional banks."
    monkeypatch.setattr(research_agent, "fetch_website_text", lambda url: site)
    good_answer = json.dumps({"areas": [], "hook": "penetration testing and red teaming for regional banks",
                              "hook_evidence": site, "industry": "", "summary": ""})
    broken, good = FakeProvider(["not json at all"]), FakeProvider([good_answer])
    r = router([dep("groq", "gpt-oss-120b", broken, {"research": 1}),
                dep("mistral", "mistral-medium", good, {"research": 1})], clock)

    context = research_agent.get_company_context(r, "ignored", "Acme", "https://acme.example", [])
    assert context["company_hook"] == "penetration testing and red teaming for regional banks"
    assert context["research_model"] == "mistral/mistral-medium"


# ---------------------------------------------------------------------------
# Building the pool from settings
# ---------------------------------------------------------------------------

def test_the_pool_includes_every_provider_with_a_key():
    env = {"AI_PROVIDER": "groq", "AI_MODEL": "openai/gpt-oss-120b",
           "GROQ_API_KEY": "gsk_x", "MISTRAL_API_KEY": "mstrl_x",
           "OPENCODE_API_KEY": "your_opencode_api_key_here"}   # placeholder: left out
    names = [d.name for d in build_router(env).deployments]
    assert names[0] == "groq/openai/gpt-oss-120b"               # primary first
    assert "mistral/mistral-medium-latest" in names
    assert not any(n.startswith("opencode/") for n in names)


def test_the_weaker_groq_model_never_translates():
    env = {"AI_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x"}
    tiers = {d.name: d.tiers for d in build_router(env).deployments}
    assert "translation" not in tiers["groq/openai/gpt-oss-20b"]
    assert tiers["groq/openai/gpt-oss-120b"] == {"research": 1, "translation": 1}


def test_a_chosen_translation_model_is_preferred_for_translation():
    env = {"AI_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x", "MISTRAL_API_KEY": "mstrl_x",
           "AI_TRANSLATION_MODEL": "mistral-medium-latest"}
    tiers = {d.name: d.tiers for d in build_router(env).deployments}
    assert tiers["mistral/mistral-medium-latest"]["translation"] == 0


def test_models_can_be_excluded_from_the_pool():
    env = {"AI_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x",
           "AI_POOL_EXCLUDE": "groq/openai/gpt-oss-20b"}
    assert "groq/openai/gpt-oss-20b" not in [d.name for d in build_router(env).deployments]


# ---------------------------------------------------------------------------
# Gemini, as it behaved live on 2026-09-28
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body, seconds", [
    ('{"error": {"message": "Please try again in 6.48s. Need more tokens?"}}', 6.48),  # Groq
    ('{"error": {"message": "Quota exceeded. Please retry in 35.9s."}}', 35.9),        # Gemini text
    ('[{"error": {"details": [{"retryDelay": "36s"}]}}]', 36.0),                        # Gemini RetryInfo
])
def test_every_providers_wording_of_the_wait_is_understood(body, seconds):
    from ai_client import _retry_after_seconds
    response = SimpleNamespace(headers={}, text=body)
    assert _retry_after_seconds(response) == pytest.approx(seconds)


def test_gemini_joins_the_pool_with_measured_tiers():
    env = {"AI_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x", "GEMINI_API_KEY": "AQ.x"}
    tiers = {d.name: d.tiers for d in build_router(env).deployments}
    assert tiers["gemini/gemini-3.6-flash"] == {"research": 1, "translation": 1}
    # Unmeasured Gemini models may research (the hook is verified against the
    # site) but are never trusted to translate.
    assert tiers["gemini/gemini-3.8-flash"] == {"research": 2}


def test_a_model_retired_for_new_users_is_dropped_for_good(clock):
    """Gemini 2.5 Flash today: 404 "no longer available to new users"."""
    gone = FakeProvider([AIProviderError("404", kind="not_found", status=404)])
    live = FakeProvider()
    r = router([dep("gemini", "gemini-2.5-flash", gone, {"research": 1}),
                dep("gemini", "gemini-3.6-flash", live, {"research": 1})], clock)
    assert call(r).model == "gemini-3.6-flash"
    clock.now += 3600
    call(r)
    assert len(gone.calls) == 1


def test_high_demand_503s_rotate_to_a_model_that_answers(clock):
    """Six of seven Gemini Flash models answered 503 "high demand" at once."""
    busy = [FakeProvider([AIProviderError("503", kind="server", status=503)]) for _ in range(3)]
    live = FakeProvider()
    deployments = [dep("gemini", f"busy-{i}", p, {"research": 1}) for i, p in enumerate(busy)]
    deployments.append(dep("gemini", "gemini-3.6-flash", live, {"research": 1}))
    r = router(deployments, clock)
    assert call(r).model == "gemini-3.6-flash"
    assert all(len(p.calls) == 1 for p in busy)


# ---------------------------------------------------------------------------
# Stop means stop
# ---------------------------------------------------------------------------
# Live 2026-09-28: every worker sat in the router's wait for a translation
# model; the stop flag was written at 17:38 and never read, and the run only
# ended when the dashboard was closed.

def test_stop_interrupts_a_long_wait_within_about_a_second(clock):
    from model_router import RoutingCancelled
    busy = FakeProvider([rate_limited(retry_after=600)])
    r = router([dep("groq", "gpt-oss-120b", busy, {"research": 1})], clock, max_wait=900)
    stop_at = clock.now + 5
    r.should_stop = lambda: clock.now >= stop_at
    with pytest.raises(RoutingCancelled):
        call(r)
    assert clock.now - stop_at <= 1.0          # noticed within one nap slice


def test_a_stopped_run_makes_no_further_calls(clock):
    from model_router import RoutingCancelled
    provider = FakeProvider()
    r = router([dep("groq", "gpt-oss-120b", provider, {"research": 1})], clock)
    r.should_stop = lambda: True
    with pytest.raises(RoutingCancelled):
        call(r)
    assert provider.calls == []


def test_a_call_can_wait_less_than_the_router_default(clock):
    """Translation waits 60 s, not the 15 minutes research may."""
    busy = FakeProvider([rate_limited(retry_after=300)])
    r = router([dep("groq", "gpt-oss-120b", busy, {"translation": 1})], clock, max_wait=900)
    started = clock.now
    with pytest.raises(AllModelsUnavailable):
        r.complete(task="translation", system="s", messages=[], max_tokens=5, max_wait=60)
    assert clock.now - started < 61


def test_a_stop_is_not_mistaken_for_no_model_available(clock):
    """ask_json turns "no model" into standard wording; a stop must pass
    straight through instead, or stopping would write degraded drafts."""
    from agents.research_agent import ask_json
    from model_router import RoutingCancelled
    r = router([dep("groq", "gpt-oss-120b", FakeProvider(), {"research": 1})], clock)
    r.should_stop = lambda: True
    with pytest.raises(RoutingCancelled):
        ask_json(r, "m", task="research", system="s", user="u", max_tokens=5)


def test_a_company_interrupted_by_stop_goes_back_untouched(isolated, monkeypatch):
    import db
    import pipeline as pipeline_module
    from model_router import RoutingCancelled

    class StoppingRouter:
        is_router = True
        should_stop = None

        def complete(self, **kwargs):
            raise RoutingCancelled("stop requested")

    monkeypatch.setattr("agents.research_agent.fetch_website_text",
                        lambda url: "We build detection engineering tooling for SOC teams.")
    cfg = {"spec": {"areas": []}, "applicant_name": "Me", "target_role": "Intern"}
    p = pipeline_module.Pipeline(StoppingRouter(), cfg, "m", research_workers=1, writer_workers=1)
    row = ("Acme", "jobs@acme.com", "https://acme.example", "")
    p._guard(p._research_task, "research stage for <jobs@acme.com>", row)

    app = db.get_application_by_email("jobs@acme.com")
    assert app["status"] == "pending"          # not "failed", not "researching"
    assert not app["subject"] and not app["body"]
    assert p.results["skipped"] == 1 and p.results["failed"] == 0
