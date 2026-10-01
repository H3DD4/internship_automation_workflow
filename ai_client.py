"""Small OpenAI-compatible chat client used by both pipeline agents.

Includes:
- Thread-safe global rate limiter (configurable RPM)
- Retry with exponential backoff + jitter on 429, 5xx, empty body, bad JSON
- Status-code check BEFORE json parsing (fixes the crash loop)
"""

import json
import os
import re
import time
import random
import threading
from types import SimpleNamespace

import hashlib

import requests

import safe_http


def extract_json_object(text: str) -> str | None:
    """Return the last balanced {...} JSON object found in `text`, or None.

    Reasoning models sometimes wrap the JSON in prose/markdown, or only emit
    it inside a "thinking" field — this scans for balanced braces (tracking
    string/escape state so braces inside string values don't confuse it)
    instead of relying on a schema-specific regex.
    """
    if not text:
        return None

    best = None
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    best = text[start:i + 1]
    return best


def _extract_message_text(message: dict) -> str:
    """Pull assistant text from OpenAI-compatible responses, including reasoning models."""
    if not message:
        return ""

    content = (message.get("content") or "").strip()
    if content and content not in ('{"subject": "", "body": ""}', '{"subject":"","body":""}'):
        return content

    # Providers disagree on the field name: OpenAI-compatible servers use
    # "reasoning_content", Groq uses "reasoning". Check both.
    reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
    if reasoning:
        # Reasoning models sometimes put the final JSON only in reasoning_content.
        block = extract_json_object(reasoning)
        if block:
            return block
        if not content and len(reasoning) > 50:
            return reasoning

    # Legacy text field on some providers
    return (message.get("text") or content or "").strip()


class RateLimiter:
    """Thread-safe token-bucket-style rate limiter. One global instance shared
    across all worker threads — no matter how many research/writer workers,
    total API calls never exceed max_per_minute."""

    def __init__(self, max_per_minute: int = 800):
        self.interval = 60.0 / max_per_minute
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self._last_call = time.time()


# Single global instance — configurable via AI_MAX_RPM env var at startup
_global_rate_limiter = None


def get_global_rate_limiter(max_rpm: int = None) -> RateLimiter:
    global _global_rate_limiter
    if _global_rate_limiter is None:
        import os
        rpm = max_rpm or int(os.getenv("AI_MAX_RPM", "800"))
        _global_rate_limiter = RateLimiter(max_per_minute=rpm)
    return _global_rate_limiter


# ---------------------------------------------------------------------------
# Token budget, paced from the provider's own rate-limit headers
# ---------------------------------------------------------------------------
# RateLimiter above caps requests per minute, but that is not the limit that
# bites: Groq's free tier allows 8,000 TOKENS per minute per model, and one
# research call is ~2,200. Firing blind meant 84 rate-limit errors in a
# 50-company run, each followed by a 2-4 s backoff and, after three, a
# downgrade to a weaker model — which is how a hook came out as "solving
# systems" instead of "soldering systems".
#
# Every Groq response carries x-ratelimit-limit-tokens and
# x-ratelimit-remaining-tokens, and the bucket refills continuously at
# limit/60 tokens per second. So each call first reserves its estimated tokens
# against that bucket and, if they aren't there yet, waits exactly as long as
# the refill needs. Providers that send no such headers are never paced.

_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def _parse_duration(text: str) -> float | None:
    """Seconds in a Go-style duration ("577ms", "6.48s", "1m2.3s"), or None."""
    parts = _DURATION_PART_RE.findall(text or "")
    if not parts:
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(value) * scale[unit] for value, unit in parts)


def estimate_tokens(system: str, messages: list) -> int:
    """A deliberately high estimate of a request's prompt tokens. Scraped
    European text tokenises denser than English prose, and under-reserving is
    what leads straight back into a 429."""
    chars = len(system or "") + sum(len(str(m.get("content", ""))) for m in messages)
    return int(chars / 3.2) + 40


class TokenBudget:
    """Per-(endpoint, model) token bucket, fed by response headers and shared
    by every thread, since the provider's limit is per model, not per worker."""

    MAX_WAIT = 65.0  # a full bucket refills in 60 s; never sleep longer

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self._lock = threading.Lock()
        self._state: dict = {}
        self._clock = clock
        self._sleep = sleep

    def _current(self, state: dict, now: float) -> float:
        rate = state["limit"] / 60.0
        return min(state["limit"], state["remaining"] + rate * (now - state["at"]))

    def available_in(self, key, tokens: int) -> float:
        """Seconds until `tokens` would be available — without taking them.
        Lets a router send a call to whichever model can take it soonest
        instead of queueing behind a busy one."""
        with self._lock:
            state = self._state.get(key)
            if state is None:
                return 0.0
            needed = min(tokens, state["limit"])
            current = self._current(state, self._clock())
            if current >= needed:
                return 0.0
            return (needed - current) / (state["limit"] / 60.0)

    def reserve(self, key, tokens: int) -> float:
        """Block until `tokens` are available, then take them. Returns how long
        it waited, in seconds."""
        waited = 0.0
        while True:
            with self._lock:
                state = self._state.get(key)
                if state is None:
                    return waited  # nothing known about this endpoint: don't pace
                now = self._clock()
                needed = min(tokens, state["limit"])  # an oversized call waits for a full bucket
                current = self._current(state, now)
                if current >= needed:
                    state.update(remaining=current - needed, at=now)
                    return waited
                delay = min((needed - current) / (state["limit"] / 60.0), self.MAX_WAIT)
            self._sleep(delay + 0.05)
            waited += delay + 0.05

    def observe(self, key, headers) -> None:
        """Adopt the provider's authoritative numbers from a response."""
        if not headers:
            return
        try:
            limit = int(headers.get("x-ratelimit-limit-tokens"))
            remaining = int(headers.get("x-ratelimit-remaining-tokens"))
        except (TypeError, ValueError):
            return
        if limit <= 0:
            return
        with self._lock:
            self._state[key] = {"limit": limit, "remaining": max(0, remaining),
                                "at": self._clock()}

    def exhausted(self, key, retry_after: float | None, needed: int) -> bool:
        """A 429 arrived. The provider's "try again in X" means a request of
        this size fits after X, so set the bucket to exactly that — every
        thread then waits the stated time in reserve(), once, instead of
        firing into it again. Returns False when there is nothing to pace
        with (no headers seen, or no stated wait), so the caller backs off."""
        if retry_after is None:
            return False
        with self._lock:
            state = self._state.get(key)
            if state is None:
                return False
            rate = state["limit"] / 60.0
            state.update(remaining=min(needed, state["limit"]) - retry_after * rate,
                         at=self._clock())
        return True


_global_token_budget = TokenBudget()


def _retry_after_seconds(response) -> float | None:
    """How long the provider asked us to wait, from a header or the body."""
    header = (getattr(response, "headers", None) or {}).get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    body = response.text or ""
    # Each provider words it differently: Groq "Please try again in 6.48s",
    # Gemini "Please retry in 35.9s" plus a structured "retryDelay": "36s".
    for pattern in (r"try again in ([0-9hms.]+)", r"retry in ([0-9hms.]+)",
                    r'"retryDelay"\s*:\s*"([0-9.]+s)"'):
        match = re.search(pattern, body)
        if match:
            return _parse_duration(match.group(1))
    return None


class AIProviderError(RuntimeError):
    """A failed call, classified so a router can react correctly:

      auth        401/403 — this key will never work; stop using the deployment
      not_found   404 — the model id doesn't exist here; stop using it
      rate_limit  429 — come back after `retry_after` seconds
      server      5xx — transient; back off
      network     connection error / timeout — transient; back off
      bad_request 400 — this request can't be served here
      bad_response a 200 that carried nothing usable

    Subclasses RuntimeError with the same messages as before, so every caller
    that caught RuntimeError or matched on the text still works."""

    def __init__(self, message: str, *, kind: str, status: int | None = None,
                 retry_after: float | None = None, headers=None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.headers = headers or {}


def _classify_status(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return "bad_request"


# Every call to a provider goes through the SSRF-guarded session: a "custom"
# base URL is user input, and on a server it must not reach private addresses.
_http = None


def _http_post(url: str, **kwargs):
    global _http
    if _http is None:
        _http = safe_http.session()
    safe_http.check_url(url)
    return _http.post(url, **kwargs)


def _http_get(url: str, **kwargs):
    global _http
    if _http is None:
        _http = safe_http.session()
    safe_http.check_url(url)
    return _http.get(url, **kwargs)


def key_fingerprint(api_key: str) -> str:
    """Identifies a key without holding it: two users on the same provider
    and model have separate quotas, so their token buckets must be separate."""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:12]


class CompatibleAIClient:
    def __init__(self, api_key: str, base_url: str, rate_limiter: RateLimiter = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or get_global_rate_limiter()
        # Shared across clients of the same key: the provider limits the
        # model per account, not per client object.
        self.token_budget = _global_token_budget
        self.budget_scope = key_fingerprint(api_key)
        self.messages = _Messages(self)


class _Messages:
    def __init__(self, client: CompatibleAIClient):
        self.client = client

    def create(self, model: str, max_tokens: int, system: str, messages: list,
               max_attempts: int = 3, temperature: float = None,
               json_mode: bool = False, reasoning_effort: str = None):
        """`temperature` matters a lot here: unset, providers default to 1.0,
        which invites a model to embroider facts in what is meant to be near-
        verbatim assembly. Both agents pass a low value.

        `json_mode` asks the provider for guaranteed-parsable JSON, and
        `reasoning_effort` caps how long a reasoning model thinks (the writing
        task needs no deliberation, and long reasoning eats the token budget).
        Both are ignored by providers that don't support them, so the raw
        response is still parsed defensively downstream.
        """
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        budget_key = (self.client.base_url, model, self.client.budget_scope)
        needed = estimate_tokens(system, payload["messages"][1:])
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            # Wait for token budget first (the limit that actually bites),
            # then for the request-rate limiter.
            self.client.token_budget.reserve(budget_key, needed)
            self.client.rate_limiter.wait()

            # --- Network-level errors ---
            try:
                response = _http_post(
                    f"{self.client.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.client.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=120,
                )
            except safe_http.BlockedURL as e:
                raise AIProviderError(f"AI endpoint refused: {e}", kind="auth") from e
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == max_attempts:
                    raise AIProviderError(
                        f"AI provider unreachable after {max_attempts} attempts: {e}",
                        kind="network",
                    ) from e
                self._backoff(attempt, f"connection error: {e}")
                continue

            headers = getattr(response, "headers", None) or {}
            self.client.token_budget.observe(budget_key, headers)

            # --- Retryable HTTP status codes (429 rate-limit, 5xx server error) ---
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = (_retry_after_seconds(response)
                               if response.status_code == 429 else None)
                if attempt == max_attempts:
                    raise AIProviderError(
                        f"AI provider error {response.status_code} after "
                        f"{max_attempts} attempts: {response.text[:300]}",
                        kind=_classify_status(response.status_code),
                        status=response.status_code, retry_after=retry_after,
                        headers=headers,
                    )
                if response.status_code == 429:
                    # Wait as long as the provider says, not a guessed 2-4 s:
                    # guessing short just earns another 429.
                    if retry_after is not None:
                        print(f"    [ai-retry] {model} rate-limited — waiting "
                              f"{retry_after:.1f}s as the provider asked")
                        if not self.client.token_budget.exhausted(budget_key, retry_after, needed):
                            # No headers to pace with: sleep the stated time here.
                            time.sleep(min(retry_after, TokenBudget.MAX_WAIT)
                                       + random.uniform(0.05, 0.3))
                        continue
                self._backoff(attempt, f"HTTP {response.status_code}")
                continue

            # --- Optional params the model may not accept ---
            # Not every model supports JSON mode or reasoning_effort, and a
            # rejected optional param comes back as a fatal 400. Drop it and
            # retry once rather than failing the company over a nicety.
            if response.status_code == 400:
                dropped = self._drop_unsupported_param(payload, response.text)
                if dropped:
                    print(f"    [ai] {model} rejected '{dropped}' — retrying without it")
                    # Not a failure of the model, so it doesn't use up an attempt
                    # (with max_attempts=1 it used to end the call right here).
                    # Bounded: each optional param can only be dropped once.
                    attempt -= 1
                    continue

            # --- Non-retryable HTTP errors (401, 403, 404, etc.) ---
            if response.status_code != 200:
                raise AIProviderError(
                    f"AI provider error {response.status_code}: {response.text[:500]}",
                    kind=_classify_status(response.status_code),
                    status=response.status_code, headers=headers,
                )

            # --- Safe JSON parsing (guard against empty 200 body) ---
            body_text = response.text.strip()
            if not body_text:
                if attempt == max_attempts:
                    raise AIProviderError(
                        "AI provider returned empty response body after retries",
                        kind="bad_response", status=200, headers=headers,
                    )
                self._backoff(attempt, "empty response body")
                continue

            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == max_attempts:
                    raise AIProviderError(
                        f"AI provider returned invalid JSON: {e}\n"
                        f"Body: {body_text[:300]}",
                        kind="bad_response", status=200, headers=headers,
                    ) from e
                self._backoff(attempt, f"bad JSON: {e}")
                continue

            # --- Extract content (some reasoning models fill reasoning_content only) ---
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError) as e:
                if attempt == max_attempts:
                    raise AIProviderError(
                        f"AI response missing expected fields: {e}\n"
                        f"Data: {json.dumps(data)[:300]}",
                        kind="bad_response", status=200, headers=headers,
                    ) from e
                self._backoff(attempt, f"malformed response: {e}")
                continue

            content = _extract_message_text(message)
            if not content:
                if attempt == max_attempts:
                    raise AIProviderError(
                        "AI provider returned empty message content after retries",
                        kind="bad_response", status=200, headers=headers,
                    )
                self._backoff(attempt, "empty message content")
                continue

            # headers/model ride along for the router's health and budget
            # tracking; existing callers only ever read .content.
            return SimpleNamespace(content=[SimpleNamespace(text=content)],
                                   headers=headers, model=model)

        raise AIProviderError("AI call failed: exhausted all retry attempts", kind="server")

    @staticmethod
    def _drop_unsupported_param(payload: dict, error_text: str) -> str | None:
        """Remove one optional param the provider complained about, if any.
        Returns the removed key so the caller can retry and log it."""
        lowered = (error_text or "").lower()
        for key in ("response_format", "reasoning_effort"):
            if key in payload and key in lowered:
                payload.pop(key)
                return key
        return None

    @staticmethod
    def _backoff(attempt: int, reason: str):
        delay = (2 ** attempt) + random.uniform(0, 1)
        print(f"    [ai-retry] attempt {attempt} failed ({reason}) — "
              f"retrying in {delay:.1f}s")
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
# Every provider here speaks the same OpenAI-compatible /chat/completions API,
# so switching is only a matter of which key, base URL and model are used.
# Each provider keeps its OWN key in .env (GROQ_API_KEY, OPENCODE_API_KEY, …),
# so switching back and forth in the dashboard never loses a key.
PROVIDERS = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "key_portal": "https://console.groq.com/keys",
        # gpt-oss-120b follows a strict extraction prompt most reliably of
        # Groq's line-up; the fallbacks are a different architecture so they
        # tend to fail differently rather than the same way.
        "default_model": "openai/gpt-oss-120b",
        "fallbacks": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
        # Only the chat models Groq currently serves — the older Llama ids
        # were retired, and "Load models" in Settings is the live list.
        "suggested_models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b",
                              "qwen/qwen3.8-27b"],
    },
    "opencode": {
        "label": "OpenCode Zen",
        # Verified against the live endpoint: /models answers here, and
        # /chat/completions takes an "Authorization: Bearer oc_sk_..." key.
        "base_url": "https://opencode.ai/zen/v1",
        "key_env": "OPENCODE_API_KEY",
        "key_portal": "https://opencode.ai/auth",
        "default_model": "muse-spark-1.3",
        # Different vendors behind one endpoint, so a fallback fails
        # differently from the primary rather than the same way.
        "fallbacks": ["claude-haiku-4-5", "gemini-3.5-flash"],
        # A short, useful subset; "Load models" fetches the full live list.
        # The "*-free" ids are refused outside the OpenCode client itself.
        "suggested_models": ["muse-spark-1.3", "muse-spark-1.2",
                              "claude-haiku-4-5", "claude-sonnet-5",
                              "gemini-3.5-flash", "gpt-5.4-mini",
                              "deepseek-v4-flash", "qwen3.8-flash"],
    },
    "mistral": {
        "label": "Mistral",
        # Verified live: /models answers with a valid key. The free
        # "Experiment" plan must be activated in the console first — until
        # then every call returns 429 with x-ratelimit-limit-req-minute: 0.
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "key_portal": "https://console.mistral.ai/api-keys",
        "default_model": "mistral-medium-latest",
        "fallbacks": ["mistral-small-latest"],
        # mistral-large-latest answers 403 "not available in your
        # subscription tier" on the free plan, so it isn't offered.
        "suggested_models": ["mistral-medium-latest", "mistral-small-latest",
                              "magistral-medium-latest", "ministral-14b-latest"],
    },
    "gemini": {
        "label": "Google Gemini",
        # Google's OpenAI-compatible endpoint (the native /v1beta is not).
        # Verified live 2026-09-28: gemini-3.6-flash answers; most other
        # Flash models answered 503 "high demand", and the 2.5 family 404
        # "no longer available to new users".
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "key_portal": "https://aistudio.google.com/apikey",
        "default_model": "gemini-3.6-flash",
        "fallbacks": ["gemini-3.8-flash", "gemini-3.7-flash"],
        "suggested_models": ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.7-flash",
                              "gemini-3.5-flash", "gemini-flash-latest"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "key_portal": "https://openrouter.ai/keys",
        "default_model": "",
        "fallbacks": [],
        "suggested_models": [],
    },
    "custom": {
        "label": "Custom (any OpenAI-compatible endpoint)",
        "base_url": "",
        "key_env": "CUSTOM_API_KEY",
        "key_portal": "",
        "default_model": "",
        "fallbacks": [],
        "suggested_models": [],
    },
}

DEFAULT_PROVIDER = "groq"
DEFAULT_BASE_URL = PROVIDERS[DEFAULT_PROVIDER]["base_url"]
DEFAULT_MODEL = PROVIDERS[DEFAULT_PROVIDER]["default_model"]
KEY_PORTAL_URL = PROVIDERS[DEFAULT_PROVIDER]["key_portal"]
SUPPORTED_MODELS = PROVIDERS[DEFAULT_PROVIDER]["suggested_models"]


def _env(env, name: str) -> str:
    return str(env.get(name) or "").strip()


def resolve_ai_settings(env=None) -> dict:
    """The provider, key, base URL, model and fallbacks to use, from .env.

    AI_PROVIDER picks the provider; its key comes from that provider's own
    variable, and its base URL can be overridden with <PROVIDER>_BASE_URL.
    Older single-provider settings (AI_API_KEY / AI_BASE_URL) still work."""
    env = os.environ if env is None else env
    provider = _env(env, "AI_PROVIDER").lower()

    if provider in PROVIDERS:
        preset = PROVIDERS[provider]
        api_key = _env(env, preset["key_env"]) or _env(env, "AI_API_KEY")
        base_url = _env(env, f"{provider.upper()}_BASE_URL") or preset["base_url"]
        if provider == "custom":
            base_url = base_url or _env(env, "AI_BASE_URL")
    else:
        # Legacy .env: one key and one base URL. Treat it as the provider
        # whose base URL it matches, else as a custom endpoint.
        base_url = _env(env, "AI_BASE_URL") or DEFAULT_BASE_URL
        provider = next((pid for pid, p in PROVIDERS.items()
                         if p["base_url"] and p["base_url"] == base_url.rstrip("/")), "custom")
        preset = PROVIDERS[provider]
        api_key = _env(env, "AI_API_KEY") or _env(env, "ANTHROPIC_API_KEY")

    model = _env(env, "AI_MODEL") or preset["default_model"]
    configured_fallbacks = [m.strip() for m in _env(env, "AI_FALLBACK_MODELS").split(",") if m.strip()]
    return {
        "provider": provider,
        "label": preset["label"],
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        # Translating a verified hook is a second, much smaller call. Giving it
        # its own model gives it its own per-model rate-limit budget — but on
        # Groq's free tier the smaller models mistranslated ("welding" for
        # soldering) and dropped product names, so it defaults to the research
        # model and the split is opt-in.
        "translation_model": _env(env, "AI_TRANSLATION_MODEL") or model,
        "fallbacks": configured_fallbacks or list(preset["fallbacks"]),
    }


def list_provider_models(base_url: str, api_key: str, timeout: int = 20) -> tuple:
    """(model_ids, error_message) from the provider's /models endpoint."""
    if not base_url:
        return [], "No base URL set for this provider."
    if not api_key:
        return [], "No API key for this provider yet."
    url = base_url.rstrip("/") + "/models"
    try:
        response = _http_get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    except requests.RequestException as exc:
        return [], f"Could not reach {url} ({exc})."
    if response.status_code in (401, 403):
        return [], "The provider rejected this API key (401/403)."
    if response.status_code != 200:
        return [], f"{url} answered {response.status_code}: {response.text[:200]}"
    try:
        payload = response.json()
    except ValueError:
        return [], f"{url} did not return JSON."
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return [], f"Unexpected /models response from {url}."
    return sorted({str(item.get("id", "")) if isinstance(item, dict) else str(item)
                   for item in items} - {""}), None


def build_model_fallback_list(primary: str, fallbacks=None) -> list[str]:
    """Ordered, de-duplicated [primary, *fallbacks]. By default the fallbacks
    are the current provider's (or AI_FALLBACK_MODELS), because another
    provider's model ids don't exist on this one. Transport-level errors are
    already retried inside CompatibleAIClient; this list is for answers that
    came back unusable."""
    if fallbacks is None:
        fallbacks = resolve_ai_settings()["fallbacks"]
    models = []
    for m in (primary, *fallbacks):
        if m and m not in models:
            models.append(m)
    return models
