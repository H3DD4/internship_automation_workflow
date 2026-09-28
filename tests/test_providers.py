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
    groq = resolve_ai_settings({"AI_PROVIDER": "groq"})["fallbacks"]
    opencode = resolve_ai_settings({"AI_PROVIDER": "opencode"})["fallbacks"]
    assert groq and opencode
    assert not set(groq) & set(opencode)
    assert resolve_ai_settings({"AI_PROVIDER": "custom"})["fallbacks"] == []
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
    page = client.get("/settings").data.decode()
    assert "super-secret-groq-key" not in page
    assert "key saved" in page
    # The tracker no longer carries the credential form at all.
    assert "super-secret-groq-key" not in client.get("/").data.decode()


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

def test_tracker_links_to_settings_instead_of_inlining_the_form(client):
    """The credential/file/pacing fields used to sit in a <details> at the
    bottom of the tracker. They belong to /settings now."""
    page = client.get("/").data.decode()
    assert 'href="/settings"' in page
    assert 'id="workspace-setup"' not in page
    assert 'name="ai_api_key"' not in page

    settings = client.get("/settings")
    assert settings.status_code == 200
    body = settings.data.decode()
    for field in ('name="ai_api_key"', 'name="companies_file"', 'name="cv_file"',
                  'name="max_per_day"', 'name="research_workers"', 'name="ai_fallbacks"'):
        assert field in body, field


def test_settings_saves_the_advanced_fields(client, isolated):
    companies = io.BytesIO(b"company_name,email\nAcme,jobs@acme.com\n")
    response = client.post("/setup", headers=client.origin,
                           content_type="multipart/form-data",
                           data={"return_to": "settings",
                                 "ai_provider": "groq", "ai_api_key": "k",
                                 "ai_model": "openai/gpt-oss-120b",
                                 "ai_fallbacks": "alt-one,  alt-two ",
                                 "your_name": "Mohamed Hedda", "target_role": "Internship",
                                 "max_per_day": "12", "bounce_minutes": "15",
                                 "research_workers": "5", "writer_workers": "4",
                                 "ai_max_rpm": "200",
                                 "companies_file": (companies, "companies.csv")})
    # Saving from /settings comes back to /settings, not to the tracker.
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")

    env = (isolated / ".env").read_text()
    for line in ('MAX_EMAILS_PER_DAY="12"', 'BOUNCE_CHECK_MINUTES="15"',
                 'RESEARCH_WORKERS="5"', 'WRITER_WORKERS="4"', 'AI_MAX_RPM="200"',
                 'AI_FALLBACK_MODELS="alt-one,alt-two"'):
        assert line in env, line


def test_a_bad_advanced_value_is_ignored_rather_than_saved(client, isolated):
    """The sender worker int()-parses these; writing "many" would crash it."""
    companies = io.BytesIO(b"company_name,email\nAcme,jobs@acme.com\n")
    client.post("/setup", headers=client.origin, content_type="multipart/form-data",
                data={"ai_provider": "groq", "ai_api_key": "k",
                      "ai_model": "openai/gpt-oss-120b",
                      "your_name": "Mohamed Hedda", "target_role": "Internship",
                      "research_workers": "many", "ai_max_rpm": "-4",
                      "companies_file": (companies, "companies.csv")})
    env = (isolated / ".env").read_text()
    assert "many" not in env
    assert 'AI_MAX_RPM="-4"' not in env


def test_setup_return_to_cannot_be_pointed_anywhere_else(client, isolated):
    """`return_to` picks between two known pages — never an arbitrary URL."""
    companies = io.BytesIO(b"company_name,email\nAcme,jobs@acme.com\n")
    response = client.post("/setup", headers=client.origin,
                           content_type="multipart/form-data",
                           data={"return_to": "https://evil.example/steal",
                                 "ai_provider": "groq", "ai_api_key": "k",
                                 "ai_model": "openai/gpt-oss-120b",
                                 "your_name": "Mohamed Hedda", "target_role": "Internship",
                                 "companies_file": (companies, "companies.csv")})
    assert response.headers["Location"].endswith("/")


def test_opencode_preset_targets_the_live_zen_endpoint():
    """muse-spark-1.3 is served from opencode.ai/zen/v1 — the '-free' ids on
    that endpoint are refused outside the OpenCode client, so none is a
    default or a fallback here."""
    preset = PROVIDERS["opencode"]
    assert preset["base_url"] == "https://opencode.ai/zen/v1"
    assert preset["default_model"] == "muse-spark-1.3"
    picks = [preset["default_model"], *preset["fallbacks"], *preset["suggested_models"]]
    assert not [m for m in picks if m.endswith("-free")]


# ---------------------------------------------------------------------------
# Model pool in Settings
# ---------------------------------------------------------------------------

# Derived from PROVIDERS, so adding a provider can't leak its real key into a
# test (hard-coding the list is exactly how the Gemini key got through).
REAL_KEY_VARS = tuple({p["key_env"] for p in PROVIDERS.values()}
                      | {"AI_API_KEY", "ANTHROPIC_API_KEY"})


@pytest.fixture
def only_fake_provider(monkeypatch, fake_provider):
    """A pool of exactly one model on the local fake server — never a real
    provider, whatever keys the developer's shell happens to hold."""
    for var in REAL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "custom")
    monkeypatch.setenv("CUSTOM_API_KEY", "good-key")
    monkeypatch.setenv("CUSTOM_BASE_URL", fake_provider)
    monkeypatch.setenv("AI_MODEL", "model-a")


def test_settings_lists_the_model_pool(client, only_fake_provider):
    page = client.get("/settings").data.decode()
    assert 'id="s-pool"' in page
    assert "custom/model-a" in page
    assert "research (tier 1)" in page
    assert 'id="check-pool-btn"' in page


def test_the_pool_check_reports_each_model_live(client, only_fake_provider):
    data = client.post("/api/ai-health", json={}, headers=client.origin).get_json()
    assert data["ok"], data
    assert data["rows"][0]["name"] == "custom/model-a"
    assert data["rows"][0]["state"] == "ok"


def test_the_pool_check_says_when_a_task_has_no_working_model(client, only_fake_provider, monkeypatch):
    monkeypatch.setenv("CUSTOM_API_KEY", "bad-key")
    data = client.post("/api/ai-health", json={}, headers=client.origin).get_json()
    assert not data["ok"]
    assert data["rows"][0]["state"] == "bad_key"
    assert "No working model for: research, translation" in data["message"]


def test_the_pool_never_puts_a_key_on_the_page(client, only_fake_provider):
    assert "good-key" not in client.get("/settings").data.decode()
