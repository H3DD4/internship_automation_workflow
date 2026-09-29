"""Routes every AI call across all configured providers, so a task keeps
working when one provider is rate-limited, over its daily quota, down, or
has a bad key.

Why not LiteLLM: its PyPI package was compromised in March 2026 (1.82.7 /
1.82.8 shipped a credential stealer), and this machine holds Gmail
credentials. More to the point, its fallbacks are generic — they fall to any
model that answers. Here quality has to govern the order: a weaker model
turned "Lötsysteme" into "welding" systems with no error to show for it.

The policy, in order of precedence:

1. Tiers per task. Tier 1 is every model that has proven good enough for the
   task. gpt-oss-20b is tier 2 for research and never used for translation.
2. Within a tier, balance: the least-recently-used model that is ready goes
   next, so two good models on two providers share the load (and double the
   throughput) instead of one queuing behind its own rate limit.
3. A model is ready when its cooldown has passed, its provider has token and
   request budget for the call (from the provider's own rate-limit headers),
   and its minimum spacing has elapsed.
4. Drop a tier only when every model in the better one is unavailable for
   longer than `degrade_after` seconds. A 15-second wait for the good model
   beats a mistranslation.
5. When nothing is ready, wait for whatever recovers first, up to `max_wait`
   — availability first — and only then give up.

Health is per model: a transient failure cools that model down with
exponential backoff and it is tried again when the cooldown expires (a
half-open circuit); a rejected key (401) retires every model on that
provider; a 403/404 retires just that model ("not available in your
subscription tier" is a 403 on a perfectly good key).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from ai_client import (
    PROVIDERS,
    AIProviderError,
    CompatibleAIClient,
    _parse_duration,
    estimate_tokens,
    resolve_ai_settings,
)

# ---------------------------------------------------------------------------
# The pool: which models each provider contributes, and at what tier per task
# ---------------------------------------------------------------------------
# Tiers are judgments backed by today's measurements, so they live in code
# with their reasons rather than in a settings form.
#   (model, {task: tier}, sends reasoning_effort, min seconds between calls)
POOL = {
    "groq": [
        # Researched and translated all of today's live batches accurately.
        ("openai/gpt-oss-120b", {"research": 1, "translation": 1}, True, 2.0),
        # Fine at extracting a hook; mistranslated ("welding" for soldering)
        # and dropped a product name, so never used for translation.
        ("openai/gpt-oss-20b", {"research": 2}, True, 2.0),
    ],
    "mistral": [
        # Strong on French/German/Italian, which is most of this list. Free
        # tier is ~1 request/second per the published limits.
        ("mistral-medium-latest", {"research": 1, "translation": 1}, False, 1.1),
        ("mistral-small-latest", {"research": 2, "translation": 2}, False, 1.1),
    ],
    "gemini": [
        # Measured on today's hard cases: "Lötsyteme" -> soldering (despite the
        # site's typo), kept "Frontier Engine", translated Finnish correctly,
        # and its "cutting-edge" was refused by the guard. Availability is the
        # weakness — 503 "high demand" and a tight per-minute free quota — so
        # it is paced at one call every 6 s and cooled down whenever it balks.
        ("gemini-3.6-flash", {"research": 1, "translation": 1}, True, 6.0),
        # Unmeasured (503 "high demand" at test time). Research only, where the
        # hook is verified against the site anyway; never trusted to translate.
        ("gemini-3.8-flash", {"research": 2}, True, 6.0),
        ("gemini-3.7-flash", {"research": 2}, True, 6.0),
    ],
    "opencode": [
        ("muse-spark-1.3", {"research": 1, "translation": 1}, False, 1.0),
    ],
}

# Park a whole provider this long when its plan allows zero requests (a
# Mistral workspace before a plan is activated reports a limit of 0).
PLAN_INACTIVE_COOLDOWN = 15 * 60


class AllModelsUnavailable(RuntimeError):
    """No model for the task could take the call within the allowed wait."""


class RoutingCancelled(RuntimeError):
    """The run was asked to stop while this call was waiting for a model.

    Deliberately NOT an AllModelsUnavailable: callers treat "no model" as a
    reason to fall back to standard wording, but a stop must not produce a
    degraded draft — the company goes back in the queue untouched."""


@dataclass
class Deployment:
    """One model on one provider, with its live health."""

    provider: str
    model: str
    client: object
    tiers: dict
    reasoning: bool = False
    min_interval: float = 0.0
    order: int = 0
    # --- health, mutated under the router's lock ---
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    disabled_reason: str = ""
    consecutive_failures: int = 0
    last_used_at: float = 0.0
    successes: int = 0
    failures: int = 0
    last_error: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class RouterResult:
    text: str
    deployment: str
    provider: str
    model: str
    tier: int
    content: list = field(default_factory=list)  # messages.create()-compatible shape


class ModelRouter:
    is_router = True

    def __init__(self, deployments: list, *, degrade_after: float = 25.0,
                 max_wait: float = 900.0, clock=time.monotonic, sleep=time.sleep,
                 log=print, should_stop=None):
        self.deployments = list(deployments)
        self.degrade_after = degrade_after
        self.max_wait = max_wait
        self._clock = clock
        self._sleep = sleep
        self._log = log
        self._lock = threading.Lock()
        # Set by the pipeline. Checked before every call and every second of
        # every wait: a router that sleeps through "Stop" makes Stop look broken
        # (a worker could sit in a 15-minute wait with the stop flag ignored).
        self.should_stop = should_stop

    def _stopping(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def _nap(self, seconds: float) -> None:
        """Sleep in slices of at most a second, bailing out on a stop request."""
        remaining = seconds
        while remaining > 0:
            if self._stopping():
                raise RoutingCancelled("stop requested")
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step

    # -- readiness -----------------------------------------------------------

    def _wait_for(self, d: Deployment, tokens: int, now: float) -> float:
        waits = [d.cooldown_until - now, d.last_used_at + d.min_interval - now]
        budget = getattr(d.client, "token_budget", None)
        base_url = getattr(d.client, "base_url", None)
        if budget is not None and base_url is not None:
            waits.append(budget.available_in(
                (base_url, d.model, getattr(d.client, "budget_scope", "")), tokens))
        return max(0.0, *waits)

    def _pick(self, task: str, tokens: int, exclude: set) -> tuple:
        """(deployment or None, seconds to wait). Called under the lock."""
        now = self._clock()
        candidates = [d for d in self.deployments
                      if task in d.tiers and not d.disabled_reason and d.name not in exclude]
        if not candidates:
            return None, None
        waits = {d.name: self._wait_for(d, tokens, now) for d in candidates}
        for tier in sorted({d.tiers[task] for d in candidates}):
            in_tier = [d for d in candidates if d.tiers[task] == tier]
            best = min(in_tier, key=lambda d: (waits[d.name], d.last_used_at, d.order))
            if waits[best.name] <= self.degrade_after:
                return best, waits[best.name]
        # Every tier is far off: take whatever frees up first, better tier on ties.
        best = min(candidates, key=lambda d: (waits[d.name], d.tiers[task], d.order))
        return best, waits[best.name]

    # -- health updates ------------------------------------------------------

    def _succeeded(self, d: Deployment, headers) -> None:
        d.successes += 1
        d.consecutive_failures = 0
        d.cooldown_reason = ""
        self._apply_request_budget(d, headers)

    def _apply_request_budget(self, d: Deployment, headers) -> None:
        """Read request quotas from response headers and park the model when
        one is spent: Groq's per-day ("x-ratelimit-remaining-requests") and
        Mistral's per-minute ("x-ratelimit-remaining-req-minute")."""
        headers = headers or {}
        now = self._clock()
        remaining = _int(headers.get("x-ratelimit-remaining-requests"))
        if remaining is not None and remaining <= 0:
            reset = _parse_duration(headers.get("x-ratelimit-reset-requests") or "") or 60.0
            self._cool(d, now + reset, "daily request quota used up")
        per_minute = _int(headers.get("x-ratelimit-remaining-req-minute"))
        if per_minute is not None and per_minute <= 0:
            self._cool(d, now + 60.0, "per-minute request quota used up")

    def _cool(self, d: Deployment, until: float, reason: str) -> None:
        if until > d.cooldown_until:
            d.cooldown_until = until
            d.cooldown_reason = reason

    def _failed(self, d: Deployment, error: AIProviderError) -> None:
        now = self._clock()
        d.failures += 1
        d.last_error = str(error)[:200]
        headers = error.headers or {}

        if _int(headers.get("x-ratelimit-limit-req-minute")) == 0:
            # The account allows zero requests: nothing will work until a plan
            # is activated, on any of this provider's models.
            for other in self._same_provider(d):
                self._cool(other, now + PLAN_INACTIVE_COOLDOWN,
                           "provider plan allows 0 requests/minute — activate a plan")
            return
        if error.kind == "auth" and error.status == 401:
            for other in self._same_provider(d):
                other.disabled_reason = "API key rejected (401)"
            return
        if error.kind == "auth":        # 403: this model only
            d.disabled_reason = "not available on this key (403)"
            return
        if error.kind == "not_found":
            d.disabled_reason = "model not found (404)"
            return
        if error.kind == "rate_limit":
            wait = error.retry_after
            if wait is None:
                self._apply_request_budget(d, headers)
                wait = 20.0 * (2 ** min(d.consecutive_failures, 4))
            d.consecutive_failures += 1
            self._cool(d, now + wait, "rate-limited")
            return
        # server / network / bad_response / bad_request: transient — back off.
        d.consecutive_failures += 1
        backoff = min(5.0 * (2 ** (d.consecutive_failures - 1)), 300.0)
        self._cool(d, now + backoff, f"{error.kind} error, retrying in {backoff:.0f}s")

    def _same_provider(self, d: Deployment) -> list:
        return [o for o in self.deployments if o.provider == d.provider]

    # -- the call ------------------------------------------------------------

    def complete(self, *, task: str, system: str, messages: list, max_tokens: int,
                 temperature: float | None = None, json_mode: bool = False,
                 reasoning_effort: str | None = None, avoid=(),
                 max_wait: float | None = None) -> RouterResult:
        """Run one call on the best available model for `task`. `avoid`
        names deployments the caller already tried for this item (e.g. one
        whose answer turned out unusable). `max_wait` overrides how long this
        call may wait for a model — translation, which has a safe fallback,
        waits far less than research."""
        tokens = estimate_tokens(system, messages)
        exclude = set(avoid)
        deadline = self._clock() + (self.max_wait if max_wait is None else max_wait)
        previous_choice = None

        while True:
            if self._stopping():
                raise RoutingCancelled("stop requested")
            with self._lock:
                d, wait = self._pick(task, tokens, exclude)
                if d is None:
                    raise AllModelsUnavailable(self._explain(task, exclude))
                if wait <= 0:
                    d.last_used_at = self._clock()  # claim it before releasing the lock
            if wait > 0:
                if self._clock() + wait > deadline:
                    raise AllModelsUnavailable(self._explain(task, exclude))
                self._nap(min(wait, 30.0) + 0.05)
                continue

            if previous_choice and previous_choice != d.name:
                self._log(f"    [router] {task}: switching {previous_choice} -> {d.name}")
            previous_choice = d.name

            try:
                response = d.client.messages.create(
                    model=d.model, max_tokens=max_tokens, system=system, messages=messages,
                    max_attempts=1, temperature=temperature, json_mode=json_mode,
                    reasoning_effort=reasoning_effort if d.reasoning else None,
                )
            except AIProviderError as error:
                with self._lock:
                    self._failed(d, error)
                    state = d.disabled_reason or d.cooldown_reason
                self._log(f"    [router] {d.name} failed ({error.kind}) — {state}")
                continue
            except Exception as error:  # a client bug must not take the task down
                with self._lock:
                    self._failed(d, AIProviderError(str(error), kind="server"))
                self._log(f"    [router] {d.name} raised {error!r} — trying another model")
                continue

            with self._lock:
                self._succeeded(d, getattr(response, "headers", None))
            text = response.content[0].text
            return RouterResult(text=text, deployment=d.name, provider=d.provider,
                                model=d.model, tier=d.tiers[task],
                                content=response.content)

    def _explain(self, task: str, exclude: set) -> str:
        now = self._clock()
        parts = []
        for d in self.deployments:
            if task not in d.tiers:
                continue
            if d.disabled_reason:
                parts.append(f"{d.name}: {d.disabled_reason}")
            elif d.name in exclude:
                parts.append(f"{d.name}: already tried for this item")
            elif d.cooldown_until > now:
                parts.append(f"{d.name}: {d.cooldown_reason} "
                             f"({d.cooldown_until - now:.0f}s left)")
        return f"No model available for {task}. " + ("; ".join(parts) or "none configured")

    # -- visibility ----------------------------------------------------------

    def snapshot(self) -> list:
        """Per-model health, for the dashboard and the run log."""
        now = self._clock()
        with self._lock:
            rows = []
            for d in self.deployments:
                if d.disabled_reason:
                    state, reason = "disabled", d.disabled_reason
                elif d.cooldown_until > now:
                    state = "cooling down"
                    reason = f"{d.cooldown_reason} ({d.cooldown_until - now:.0f}s left)"
                else:
                    state, reason = "ready", ""
                rows.append({"name": d.name, "provider": d.provider, "model": d.model,
                             "tiers": dict(d.tiers), "state": state, "reason": reason,
                             "successes": d.successes, "failures": d.failures,
                             "last_error": d.last_error})
            return rows


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usable_key(value: str) -> bool:
    value = (value or "").strip()
    return bool(value) and "your_" not in value.lower() and "_here" not in value.lower()


def build_router(env=None, rate_limiter=None, **kwargs) -> ModelRouter:
    """A router over every provider that has a key in the environment.

    The provider and model chosen in Settings go first on ties, so the
    existing "primary" choice still means something; AI_TRANSLATION_MODEL,
    when set, is preferred for translation. AI_POOL_EXCLUDE takes a
    comma-separated list of "provider/model" names to leave out.

    `env` is one user's settings (user_config.UserConfig.ai_env()), and
    `rate_limiter` that user's own request pacing, so accounts never share a
    key, a quota or a queue."""
    env = os.environ if env is None else env
    settings = resolve_ai_settings(env)
    primary_provider, primary_model = settings["provider"], settings["model"]
    excluded = {x.strip() for x in str(env.get("AI_POOL_EXCLUDE") or "").split(",") if x.strip()}

    provider_ids = [primary_provider] + [p for p in PROVIDERS if p != primary_provider]
    deployments, order = [], 0
    for pid in provider_ids:
        preset = PROVIDERS.get(pid)
        if not preset:
            continue
        key = str(env.get(preset["key_env"]) or "").strip()
        if pid == primary_provider and not key:
            key = settings["api_key"]
        base_url = (str(env.get(f"{pid.upper()}_BASE_URL") or "").strip() or preset["base_url"]).rstrip("/")
        if not _usable_key(key) or not base_url:
            continue
        client = CompatibleAIClient(key, base_url, rate_limiter=rate_limiter)
        entries = list(POOL.get(pid, []))
        known = {model for model, *_ in entries}
        if pid == primary_provider and primary_model and primary_model not in known:
            entries.insert(0, (primary_model, {"research": 1, "translation": 1},
                               primary_model.startswith("openai/gpt-oss"), 1.0))
        # The primary model goes first within its provider.
        entries.sort(key=lambda e: e[0] != primary_model)
        for model, tiers, reasoning, spacing in entries:
            if f"{pid}/{model}" in excluded:
                continue
            deployments.append(Deployment(provider=pid, model=model, client=client,
                                          tiers=dict(tiers), reasoning=reasoning,
                                          min_interval=spacing, order=order))
            order += 1

    translation_model = str(env.get("AI_TRANSLATION_MODEL") or "").strip()
    if translation_model:
        for d in deployments:
            if d.model == translation_model:
                d.tiers["translation"] = 0  # preferred over every other tier
    return ModelRouter(deployments, **kwargs)
