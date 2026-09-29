"""Moving between dashboard pages must stay fast.

Pins: the Google sign-in check is cached per user (an expired token isn't
refreshed over the network on every page load), static assets are versioned
and cached, hover preloading never starts a Google sign-in, and the hot
queries are served from indexes.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

import database
from user_config import UserConfig


def _app_module():
    from dashboard import app as app_module
    return app_module


# ---------------------------------------------------------------------------
# Google sign-in check
# ---------------------------------------------------------------------------

@pytest.fixture
def with_token(user_id):
    cfg = UserConfig(user_id)
    cfg.set_secret("GOOGLE_TOKEN", json.dumps({"token": "old", "email": "me@gmail.com"}))
    _app_module()._oauth_cache.clear()
    yield cfg
    _app_module()._oauth_cache.clear()


def test_a_dead_google_token_is_not_retried_on_every_page_load(with_token, monkeypatch):
    import google_auth_helper
    calls = []
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda cfg: calls.append(1) or None)
    for _ in range(5):
        status = _app_module()._oauth_status(UserConfig(with_token.user_id))
        assert status["connected"] and not status["valid"]
    assert len(calls) == 1


def test_the_google_check_is_retried_once_its_ttl_passes(with_token, monkeypatch):
    import google_auth_helper
    calls = []
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda cfg: calls.append(1) or None)
    app_module = _app_module()
    app_module._oauth_status(with_token)
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + app_module._OAUTH_STATUS_TTL + 1)
    app_module._oauth_status(with_token)
    assert len(calls) == 2


def test_signing_in_again_is_reflected_on_the_very_next_load(with_token, monkeypatch):
    import google_auth_helper
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda cfg: None)
    assert _app_module()._oauth_status(with_token)["valid"] is False
    time.sleep(0.01)
    with_token.set_secret("GOOGLE_TOKEN", json.dumps({"token": "new", "email": "new@gmail.com"}))
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda cfg: object())
    status = _app_module()._oauth_status(UserConfig(with_token.user_id))
    assert status["valid"] and status["email"] == "new@gmail.com"


def test_disconnecting_is_reflected_on_the_very_next_load(client, with_token, monkeypatch):
    import google_auth_helper
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda cfg: object())
    assert _app_module()._oauth_status(with_token)["connected"] is True
    client.post("/oauth/disconnect", data={"csrf_token": client.csrf}, headers={"Origin": "http://localhost"})
    assert _app_module()._oauth_status(UserConfig(with_token.user_id))["connected"] is False


def test_google_sign_in_outcomes_land_on_the_email_settings(client):
    response = client.post("/oauth/disconnect", data={"csrf_token": client.csrf},
                           headers={"Origin": "http://localhost"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings#s-gmail")


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

def test_static_assets_are_versioned_so_they_can_be_cached(client):
    page = client.get("/").data.decode()
    assert "/static/style.css?v=" in page
    assert "/static/app.js?v=" in page


def test_static_assets_are_served_with_a_long_cache(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert "max-age=31536000" in response.headers.get("Cache-Control", "")


def test_editing_a_static_file_changes_its_url(client):
    from pathlib import Path
    app_module = _app_module()
    css = Path(app_module.app.static_folder) / "style.css"
    original = css.stat().st_mtime
    try:
        with app_module.app.test_request_context():
            before = app_module._inject_globals()["asset"]("style.css")
            os.utime(css, (original + 10, original + 10))
            after = app_module._inject_globals()["asset"]("style.css")
        assert before != after
    finally:
        os.utime(css, (original, original))


# ---------------------------------------------------------------------------
# Hover preloading
# ---------------------------------------------------------------------------

def test_pages_preload_links_on_hover_but_never_the_google_sign_in(client):
    page = client.get("/").data.decode()
    assert '<script type="speculationrules" nonce="' in page
    rules = page.split('<script type="speculationrules"', 1)[1].split("</script>", 1)[0]
    assert '"/oauth/*"' in rules
    assert rules.index('"not"') < rules.index('"/oauth/*"')


def test_the_preload_script_carries_the_pages_csp_nonce(client):
    response = client.get("/")
    page = response.data.decode()
    nonce = page.split('<script type="speculationrules" nonce="', 1)[1].split('"', 1)[0]
    assert f"'nonce-{nonce}'" in response.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _plan(sql, params):
    with database.read() as conn:
        return " ".join(str(tuple(r)) for r in conn.execute(text("EXPLAIN QUERY PLAN " + sql), params))


def test_a_companys_timeline_is_read_from_an_index(isolated):
    if database.is_postgres():
        pytest.skip("query plans checked on SQLite")
    plan = _plan("SELECT * FROM events WHERE application_id = :a ORDER BY timestamp ASC", {"a": 1})
    assert "idx_events_application" in plan
    assert "SCAN events" not in plan


def test_counting_todays_sends_uses_an_index(isolated):
    if database.is_postgres():
        pytest.skip("query plans checked on SQLite")
    plan = _plan("SELECT COUNT(*) FROM applications WHERE user_id = :u AND sent_at >= :a AND sent_at < :b",
                 {"u": 1, "a": "2026-09-28", "b": "2026-09-29"})
    assert "idx_applications_user_sent_at" in plan


def test_the_tracker_filters_by_user_and_status_from_an_index(isolated):
    if database.is_postgres():
        pytest.skip("query plans checked on SQLite")
    plan = _plan("SELECT COUNT(*) FROM applications WHERE user_id = :u AND status = :s", {"u": 1, "s": "ready"})
    assert "idx_applications_user_status" in plan


def test_todays_send_count_still_matches_exactly_the_same_rows(make_app, data):
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    make_app(email="a@x.com", status="sent", sent_at=f"{today}T00:00:00.000000+00:00")
    make_app(email="b@x.com", status="sent", sent_at=f"{today}T23:59:59.999999+00:00")
    make_app(email="c@x.com", status="bounced", sent_at=f"{today}T12:00:00+00:00")
    make_app(email="d@x.com", status="sent", sent_at=f"{yesterday}T23:59:59+00:00")
    make_app(email="e@x.com", status="sent", sent_at=f"{tomorrow}T00:00:00+00:00")
    assert data.count_sent_today() == 3
