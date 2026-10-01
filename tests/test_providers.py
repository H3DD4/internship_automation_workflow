"""AI provider settings: per-provider keys, model listing, and validation."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ai_client import PROVIDERS, list_provider_models, resolve_ai_settings
from user_config import UserConfig


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


def _save(client, section, **fields):
    return client.post("/settings", data={"section": section, "csrf_token": client.csrf, **fields},
                       headers={"Origin": "http://localhost"})


def test_saving_one_provider_keeps_the_other_providers_key(client, user_id):
    """Switching provider in the form must never wipe another provider's key."""
    assert _save(client, "ai", ai_provider="groq", ai_api_key="groq-key",
                 ai_model="openai/gpt-oss-120b").status_code == 302
    _save(client, "ai", ai_provider="opencode", ai_api_key="opencode-key", ai_model="muse-spark")
    cfg = UserConfig(user_id)
    assert cfg.secret("GROQ_API_KEY") == "groq-key"
    assert cfg.secret("OPENCODE_API_KEY") == "opencode-key"
    assert cfg.get("AI_PROVIDER") == "opencode"
    assert cfg.get("AI_MODEL") == "muse-spark"


def test_keys_are_stored_encrypted_and_per_user(client, user_id, make_user):
    import database
    from sqlalchemy import select
    _save(client, "ai", ai_provider="groq", ai_api_key="super-secret-groq-key", ai_model="m")
    with database.read() as conn:
        stored = conn.execute(select(database.user_secrets.c.ciphertext)).scalar_one()
    assert "super-secret-groq-key" not in stored
    other = make_user("other@example.com")
    assert UserConfig(other).secret("GROQ_API_KEY") == ""


def test_keys_are_never_sent_back_to_the_browser(client, user_id):
    UserConfig(user_id).set_secret("GROQ_API_KEY", "super-secret-groq-key")
    page = client.get("/settings").data.decode()
    assert "super-secret-groq-key" not in page
    assert 'data-provider="groq" data-label="Groq"' in page and "Connected" in page
    assert "super-secret-groq-key" not in client.get("/").data.decode()


def test_a_key_can_be_deleted(client, user_id):
    UserConfig(user_id).set_secret("GROQ_API_KEY", "k")
    _save(client, "ai", ai_provider="groq", ai_model="m", remove_key="on")
    assert UserConfig(user_id).secret("GROQ_API_KEY") == ""


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

def test_tracker_links_to_settings_instead_of_inlining_the_form(client):
    page = client.get("/").data.decode()
    assert 'href="/settings' in page
    assert 'name="ai_api_key"' not in page
    body = client.get("/settings").data.decode()
    for field in ('data-provider="groq"', 'id="companies-file"', 'name="cv_file"',
                  'name="max_per_day"', 'name="research_workers"', 'name="mail_method"'):
        assert field in body, field


def test_settings_saves_the_advanced_fields(client, user_id):
    _save(client, "ai", ai_provider="groq", ai_api_key="k", ai_model="openai/gpt-oss-120b",
          ai_fallbacks="alt-one,  alt-two ")
    _save(client, "sending", max_per_day="12", bounce_minutes="15", min_delay="30", max_delay="90")
    response = _save(client, "advanced", research_workers="3", writer_workers="2", ai_max_rpm="200")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings#s-advanced")
    cfg = UserConfig(user_id)
    assert cfg.get("MAX_EMAILS_PER_DAY") == "12"
    assert cfg.get("BOUNCE_CHECK_MINUTES") == "15"
    assert cfg.get("RESEARCH_WORKERS") == "3"
    assert cfg.get("AI_MAX_RPM") == "200"
    assert cfg.get("AI_FALLBACK_MODELS") == "alt-one,alt-two"


def test_a_bad_advanced_value_is_ignored_rather_than_saved(client, user_id):
    """The sender int()-parses these; writing "many" would crash it."""
    _save(client, "advanced", research_workers="many", ai_max_rpm="-4")
    cfg = UserConfig(user_id)
    assert cfg.get("RESEARCH_WORKERS") == "3"
    assert cfg.get("AI_MAX_RPM") == "800"


def test_an_unknown_section_is_refused(client):
    assert _save(client, "https://evil.example/steal").status_code == 400


def test_a_custom_ai_address_on_a_private_network_is_refused_in_production(client, user_id, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", __import__("os").environ["DATABASE_URL"])
    response = _save(client, "ai", ai_provider="custom", ai_base_url="https://169.254.169.254/v1",
                     ai_model="m")
    assert response.status_code == 302
    assert UserConfig(user_id).get("CUSTOM_BASE_URL") == ""


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

@pytest.fixture
def only_fake_provider(user_id, fake_provider):
    """A pool of exactly one model on the local fake server."""
    cfg = UserConfig(user_id)
    cfg.set_many({"AI_PROVIDER": "custom", "CUSTOM_BASE_URL": fake_provider, "AI_MODEL": "model-a"})
    cfg.set_secret("CUSTOM_API_KEY", "good-key")
    return cfg


def test_settings_lists_the_model_pool(client, only_fake_provider):
    page = client.get("/settings").data.decode()
    assert "custom/model-a" in page
    assert "research (tier 1)" in page
    assert 'id="check-pool-btn"' in page


def test_the_pool_check_reports_each_model_live(client, only_fake_provider):
    data = client.post("/api/ai-health", json={}, headers=client.origin).get_json()
    assert data["ok"], data
    assert data["rows"][0]["name"] == "custom/model-a"
    assert data["rows"][0]["state"] == "ok"


def test_the_pool_check_says_when_a_task_has_no_working_model(client, only_fake_provider):
    only_fake_provider.set_secret("CUSTOM_API_KEY", "bad-key")
    data = client.post("/api/ai-health", json={}, headers=client.origin).get_json()
    assert not data["ok"]
    assert data["rows"][0]["state"] == "bad_key"
    assert "No working model for: research, translation" in data["message"]


def test_the_pool_never_puts_a_key_on_the_page(client, only_fake_provider):
    assert "good-key" not in client.get("/settings").data.decode()


def test_one_users_pool_is_built_from_their_own_keys_only(only_fake_provider, make_user):
    from model_router import build_router
    other = make_user("other@example.com")
    assert build_router(UserConfig(other).ai_env()).deployments == []
    assert [d.name for d in build_router(only_fake_provider.ai_env()).deployments] == ["custom/model-a"]
