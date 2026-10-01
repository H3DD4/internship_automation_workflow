"""The AI section: a card per provider with a "Get a key" link, a key that is
checked before it's saved, and models ticked from the provider's live list —
never typed. Every connected provider and ticked model joins the pool."""

import pytest

import ai_client
from user_config import UserConfig

GROQ_LIVE = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b", "allam-2-7b",
             "whisper-large-v3", "meta-llama/llama-prompt-guard-2-22m"]
GEMINI_LIVE = ["models/gemini-3.6-flash", "models/gemini-2.5-flash", "models/gemini-embedding-001",
               "models/veo-3.1-generate-preview", "models/gemini-3.8-live"]


@pytest.fixture
def live(monkeypatch):
    """Fake providers: Groq and Gemini accept "good-key"; anything else is rejected."""
    from dashboard import app as app_module

    def fake_list(base_url, api_key, timeout=20):
        if api_key != "good-key":
            return [], "The provider rejected this API key (401/403)."
        return (GEMINI_LIVE if "googleapis" in base_url else GROQ_LIVE), None
    monkeypatch.setattr(app_module, "list_provider_models", fake_list)


def _connect(client, pid, key="good-key"):
    return client.post(f"/api/ai/{pid}/connect", headers=client.origin, json={"api_key": key})


def test_only_chat_models_are_offered():
    assert ai_client.chat_model_ids(GROQ_LIVE) == ["allam-2-7b", "openai/gpt-oss-120b",
                                                   "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
    assert ai_client.chat_model_ids(GEMINI_LIVE) == ["gemini-2.5-flash", "gemini-3.6-flash"]


def test_every_provider_has_a_card_with_a_way_to_get_a_key(client):
    page = client.get("/settings").data.decode()
    for pid, preset in ai_client.PROVIDERS.items():
        assert f'data-provider="{pid}"' in page
        if preset["key_portal"]:
            assert preset["key_portal"] in page
    assert "Connect more than one" in page and "Get a free key" in page
    assert 'name="ai_model"' not in page and 'name="ai_fallbacks"' not in page   # nothing typed by hand


def test_a_rejected_key_is_not_saved(client, live, user_id):
    reply = _connect(client, "groq", "wrong-key")
    assert reply.status_code == 400 and "rejected" in reply.get_json()["message"]
    assert not UserConfig(user_id).secret("GROQ_API_KEY")


def test_connecting_saves_the_key_and_ticks_the_recommended_models(client, live, user_id):
    data = _connect(client, "groq").get_json()
    assert data["ok"]
    chosen = [m["id"] for m in data["models"] if m["selected"]]
    assert "openai/gpt-oss-120b" in chosen and "allam-2-7b" not in chosen
    assert "whisper-large-v3" not in [m["id"] for m in data["models"]]
    cfg = UserConfig(user_id)
    assert cfg.secret("GROQ_API_KEY") == "good-key"
    assert cfg.get("AI_PROVIDER") == "groq" and cfg.get("AI_MODEL") in chosen


def test_a_second_provider_joins_the_pool(client, live, user_id):
    from model_router import build_router
    _connect(client, "groq")
    _connect(client, "gemini")
    names = {d.name for d in build_router(UserConfig(user_id).ai_env()).deployments}
    assert "groq/openai/gpt-oss-120b" in names and "gemini/gemini-3.6-flash" in names
    assert UserConfig(user_id).get("AI_PROVIDER") == "groq"          # the first one stays first


def test_ticked_models_are_the_pool(client, live, user_id):
    from model_router import build_router
    _connect(client, "groq")
    reply = client.post("/api/ai/groq/models", headers=client.origin,
                        json={"models": ["openai/gpt-oss-120b", "allam-2-7b"]}).get_json()
    assert reply["ok"]
    groq = [d.model for d in build_router(UserConfig(user_id).ai_env()).deployments if d.provider == "groq"]
    assert groq == ["openai/gpt-oss-120b", "allam-2-7b"]
    empty = client.post("/api/ai/groq/models", headers=client.origin, json={"models": []})
    assert empty.status_code == 400


def test_try_first_must_be_a_model_in_the_pool(client, live, user_id):
    _connect(client, "groq")
    _connect(client, "gemini")
    assert client.post("/api/ai/primary", headers=client.origin,
                       json={"model": "gemini/gemini-3.6-flash"}).get_json()["ok"]
    assert UserConfig(user_id).get("AI_PROVIDER") == "gemini"
    bad = client.post("/api/ai/primary", headers=client.origin, json={"model": "groq/made-up"})
    assert bad.status_code == 400


def test_removing_the_first_provider_hands_over_to_the_next(client, live, user_id):
    _connect(client, "groq")
    _connect(client, "gemini")
    assert client.post("/api/ai/groq/disconnect", headers=client.origin, json={}).get_json()["ok"]
    cfg = UserConfig(user_id)
    assert not cfg.secret("GROQ_API_KEY")
    assert cfg.get("AI_PROVIDER") == "gemini" and cfg.get("AI_MODEL") == "gemini-3.6-flash"


def test_any_connected_provider_counts_as_having_a_key(client, live, user_id):
    UserConfig(user_id).set_many({"AI_PROVIDER": "groq"})     # first choice without a key
    _connect(client, "gemini")
    page = client.get("/settings").data.decode()
    assert "1</strong> provider connected" in page
    assert 'class="checklist-item checklist-item--done"' in client.get("/").data.decode()
