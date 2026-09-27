"""AI client: JSON extraction, retries, and the model fallback list."""

import json
from unittest.mock import patch

from ai_client import (
    CompatibleAIClient,
    RateLimiter,
    build_model_fallback_list,
    extract_json_object,
    _extract_message_text,
)


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _client():
    return CompatibleAIClient("key", "https://example.com", rate_limiter=RateLimiter(100000))


def test_extracts_json_from_reasoning_content():
    """Regression: this path used an undefined `re`, raising NameError for
    every reasoning-model reply — which is what the default model returns."""
    message = {
        "content": "",
        "reasoning_content": 'Let me think... I will answer with '
                              '{"subject": "Hello", "body": "Body text"} as the final result.',
    }
    text = _extract_message_text(message)
    assert json.loads(text) == {"subject": "Hello", "body": "Body text"}


def test_extracts_research_shaped_json_too():
    """Not just subject/body — the research agent's schema must survive the
    same path (the old regex only matched subject+body objects)."""
    payload = '{"industry": "fintech", "talking_points": ["a", "b"]}'
    message = {"content": "", "reasoning_content": f"thinking...\n{payload}"}
    assert json.loads(_extract_message_text(message))["industry"] == "fintech"


def test_extract_json_object_handles_braces_inside_strings():
    text = 'preamble {"body": "uses {curly} braces", "subject": "s"} trailing'
    assert json.loads(extract_json_object(text))["body"] == "uses {curly} braces"


def test_extract_json_object_returns_none_without_json():
    assert extract_json_object("no json here") is None
    assert extract_json_object("") is None


def test_retries_empty_body_then_succeeds():
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(200, "")
        return FakeResponse(200, json.dumps({"choices": [{"message": {"content": "hello"}}]}))

    with patch("ai_client.requests.post", side_effect=fake_post), \
         patch("ai_client.time.sleep"), patch("builtins.print"):
        result = _client().messages.create("m", 10, "sys", [{"role": "user", "content": "hi"}])

    assert result.content[0].text == "hello"
    assert calls["n"] == 2


def test_does_not_retry_auth_errors():
    """A 401 is not transient — retrying just delays a clear error."""
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse(401, "unauthorized")

    with patch("ai_client.requests.post", side_effect=fake_post), \
         patch("ai_client.time.sleep"), patch("builtins.print"):
        try:
            _client().messages.create("m", 10, "sys", [{"role": "user", "content": "hi"}])
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "401" in str(exc)
    assert calls["n"] == 1


def test_model_fallback_list_is_ordered_and_deduped():
    assert build_model_fallback_list("hy3")[0] == "hy3"
    models = build_model_fallback_list("qwen3.8-flash")
    assert models[0] == "qwen3.8-flash"
    assert len(models) == len(set(models))
