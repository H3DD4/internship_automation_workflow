"""AI provider settings: per-provider keys, model listing, and validation."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ai_client import PROVIDERS, list_provider_models, resolve_ai_settings


@pytest.fixture(scope="module")
def fake_provider():
    """A tiny OpenAI-compatible server: /models lists two ids, and chat
    completions accept only the key 'good-key' and the model 'model-a'."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self):
            return self.headers.get("Authorization") == "Bearer good-key"

        def do_GET(self):
            if not self._authorised():
                return self._send(401, {"error": "bad key"})
            self._send(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self._authorised():
                return self._send(401, {"error": "bad key"})
            if request["model"] != "model-a":
                return self._send(404, {"error": "no such model"})
            self._send(200, {"choices": [{"message": {"content": "ok"}}]})

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_each_provider_uses_its_own_key():
    env = {"AI_PROVIDER": "opencode", "GROQ_API_KEY": "g", "OPENCODE_API_KEY": "o",
           "AI_MODEL": "some-model"}
    settings = resolve_ai_settings(env)
    assert settings["api_key"] == "o"
    assert settings["base_url"] == PROVIDERS["opencode"]["base_url"]
    assert resolve_ai_settings({**env, "AI_PROVIDER": "groq"})["api_key"] == "g"


def test_base_url_can_be_overridden_per_provider():
    env = {"AI_PROVIDER": "opencode", "OPENCODE_API_KEY": "o",
           "OPENCODE_BASE_URL": "https://example.test/v1/"}
    assert resolve_ai_settings(env)["base_url"] == "https://example.test/v1"


def test_legacy_single_key_settings_still_work():
    settings = resolve_ai_settings({"AI_API_KEY": "legacy",
                                    "AI_BASE_URL": "https://api.groq.com/openai/v1"})
    assert settings["provider"] == "groq"
    assert settings["api_key"] == "legacy"


def test_fallbacks_are_provider_specific():
    """Groq's model ids don't exist on OpenCode; falling back to them there
    would only produce 404s."""
    assert resolve_ai_settings({"AI_PROVIDER": "opencode"})["fallbacks"] == []
    assert resolve_ai_settings({"AI_PROVIDER": "groq"})["fallbacks"]
    custom = resolve_ai_settings({"AI_PROVIDER": "opencode", "AI_FALLBACK_MODELS": "x, y"})
    assert custom["fallbacks"] == ["x", "y"]


def test_list_models(fake_provider):
    assert list_provider_models(fake_provider, "good-key") == (["model-a", "model-b"], None)
    models, error = list_provider_models(fake_provider, "bad-key")
    assert models == [] and "401" in error


def test_dashboard_loads_models_and_tests_the_key(client, fake_provider):
    payload = {"ai_provider": "custom", "ai_api_key": "good-key", "ai_base_url": fake_provider}

    models = client.post("/api/models", json=payload, headers=client.origin).get_json()
    assert models["ok"] and models["models"] == ["model-a", "model-b"]

    ok = client.post("/api/validate-ai", json={**payload, "ai_model": "model-a"},
                     headers=client.origin).get_json()
    assert ok["ok"], ok

    wrong_model = client.post("/api/validate-ai", json={**payload, "ai_model": "nope"},
                              headers=client.origin).get_json()
    assert not wrong_model["ok"] and "not found" in wrong_model["message"]

    wrong_key = client.post("/api/validate-ai",
                            json={**payload, "ai_api_key": "bad-key", "ai_model": "model-a"},
                            headers=client.origin).get_json()
    assert not wrong_key["ok"] and "rejected" in wrong_key["message"]


def test_saving_one_provider_keeps_the_other_providers_key(client, isolated):
    """Switching provider in the form must never wipe another provider's key."""
    companies = io.BytesIO(b"company_name,email\nAcme,jobs@acme.com\n")
    form = {"ai_provider": "groq", "ai_api_key": "groq-key", "ai_model": "openai/gpt-oss-120b",
            "your_name": "Mohamed Hedda", "target_role": "Internship",
            "companies_file": (companies, "companies.csv")}
    assert client.post("/setup", data=form, headers=client.origin,
                       content_type="multipart/form-data").status_code == 302

    form = {"ai_provider": "opencode", "ai_api_key": "opencode-key", "ai_model": "muse-spark",
            "your_name": "Mohamed Hedda", "target_role": "Internship"}
    client.post("/setup", data=form, headers=client.origin, content_type="multipart/form-data")

    env = (isolated / ".env").read_text()
    assert 'GROQ_API_KEY="groq-key"' in env
    assert 'OPENCODE_API_KEY="opencode-key"' in env
    assert 'AI_PROVIDER="opencode"' in env
    assert 'AI_MODEL="muse-spark"' in env


def test_keys_are_never_sent_back_to_the_browser(client, isolated, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-groq-key")
    page = client.get("/").data.decode()
    assert "super-secret-groq-key" not in page
    assert "key saved" in page
