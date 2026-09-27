"""Small OpenAI-compatible chat client used by both pipeline agents.

Includes:
- Thread-safe global rate limiter (configurable RPM)
- Retry with exponential backoff + jitter on 429, 5xx, empty body, bad JSON
- Status-code check BEFORE json parsing (fixes the crash loop)
"""

import json
import os
import time
import random
import threading
from types import SimpleNamespace

import requests


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

    reasoning = (message.get("reasoning_content") or "").strip()
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


class CompatibleAIClient:
    def __init__(self, api_key: str, base_url: str, rate_limiter: RateLimiter = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or get_global_rate_limiter()
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

        for attempt in range(1, max_attempts + 1):
            # Rate-limit before every call
            self.client.rate_limiter.wait()

            # --- Network-level errors ---
            try:
                response = requests.post(
                    f"{self.client.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.client.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=120,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI provider unreachable after {max_attempts} attempts: {e}"
                    ) from e
                self._backoff(attempt, f"connection error: {e}")
                continue

            # --- Retryable HTTP status codes (429 rate-limit, 5xx server error) ---
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI provider error {response.status_code} after "
                        f"{max_attempts} attempts: {response.text[:300]}"
                    )
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
                    continue

            # --- Non-retryable HTTP errors (401, 403, 404, etc.) ---
            if response.status_code != 200:
                raise RuntimeError(
                    f"AI provider error {response.status_code}: {response.text[:500]}"
                )

            # --- Safe JSON parsing (guard against empty 200 body) ---
            body_text = response.text.strip()
            if not body_text:
                if attempt == max_attempts:
                    raise RuntimeError(
                        "AI provider returned empty response body after retries"
                    )
                self._backoff(attempt, "empty response body")
                continue

            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI provider returned invalid JSON: {e}\n"
                        f"Body: {body_text[:300]}"
                    ) from e
                self._backoff(attempt, f"bad JSON: {e}")
                continue

            # --- Extract content (some reasoning models fill reasoning_content only) ---
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError) as e:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"AI response missing expected fields: {e}\n"
                        f"Data: {json.dumps(data)[:300]}"
                    ) from e
                self._backoff(attempt, f"malformed response: {e}")
                continue

            content = _extract_message_text(message)
            if not content:
                if attempt == max_attempts:
                    raise RuntimeError(
                        "AI provider returned empty message content after retries"
                    )
                self._backoff(attempt, "empty message content")
                continue

            return SimpleNamespace(content=[SimpleNamespace(text=content)])

        raise RuntimeError("AI call failed: exhausted all retry attempts")

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
        "fallbacks": ["llama-3.3-70b-versatile", "openai/gpt-oss-20b"],
        "suggested_models": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile",
                              "openai/gpt-oss-20b", "moonshotai/kimi-k2-instruct",
                              "qwen/qwen3-32b", "llama-3.1-8b-instant"],
    },
    "opencode": {
        "label": "OpenCode Zen",
        # Not verified from this project's build environment (the host was
        # unreachable); "Load models" in the dashboard confirms it, and the
        # base URL is editable if OpenCode documents a different one.
        "base_url": "https://opencode.ai/zen/v1",
        "key_env": "OPENCODE_API_KEY",
        "key_portal": "https://opencode.ai",
        "default_model": "",
        "fallbacks": [],
        "suggested_models": [],
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
        response = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
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
