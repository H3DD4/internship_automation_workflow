"""Moving between dashboard pages must stay fast.

Every tracker and Settings load renders _setup_state(), and two parts of it
were expensive for values that almost never change:
  - the companies row count streamed the whole spreadsheet (22k rows), and
  - the Google sign-in check called Google to refresh an expired token — and
    when that refresh can't succeed, it failed over the network again on
    every page load.
These tests pin the caching that removed both, plus the cheaper wins around
it (static caching, a timeline index, hover preloading).
"""

import os
import time

import pytest

import db


def _app_module():
    from dashboard import app as app_module
    return app_module


# ---------------------------------------------------------------------------
# Companies row count
# ---------------------------------------------------------------------------

def test_the_companies_file_is_read_once_not_on_every_page_load(isolated, monkeypatch):
    app_module = _app_module()
    path = isolated / "companies.csv"
    path.write_text("company_name,email\nA,a@a.com\nB,b@b.com\n")

    calls = []
    real = app_module._count_companies_rows_uncached
    monkeypatch.setattr(app_module, "_count_companies_rows_uncached",
                        lambda p: calls.append(p) or real(p))

    for _ in range(5):
        assert app_module._count_companies_rows(path) == 2
    assert len(calls) == 1


def test_uploading_a_new_companies_file_is_picked_up_immediately(isolated):
    """The cache is keyed on the file itself, so it can never show the count
    of a file that has since been replaced."""
    app_module = _app_module()
    path = isolated / "companies.csv"
    path.write_text("company_name,email\nA,a@a.com\n")
    assert app_module._count_companies_rows(path) == 1

    path.write_text("company_name,email\nA,a@a.com\nB,b@b.com\nC,c@c.com\n")
    # Different size, so a different cache key even on a coarse-mtime filesystem.
    assert app_module._count_companies_rows(path) == 3


def test_a_missing_companies_file_counts_as_zero(isolated):
    assert _app_module()._count_companies_rows(isolated / "nope.xlsx") == 0


# ---------------------------------------------------------------------------
# Google sign-in check
# ---------------------------------------------------------------------------

@pytest.fixture
def token_file(isolated, monkeypatch):
    import google_auth_helper
    path = isolated / "token.json"
    monkeypatch.setattr(google_auth_helper, "TOKEN_PATH", path)
    _app_module()._invalidate_oauth_status()
    yield path
    _app_module()._invalidate_oauth_status()


def test_a_dead_google_token_is_not_retried_on_every_page_load(token_file, monkeypatch):
    """An expired Testing-mode token can't refresh. Before caching, every
    single page load paid a failing network round trip to Google for it."""
    import google_auth_helper
    token_file.write_text('{"token": "old"}')
    calls = []
    monkeypatch.setattr(google_auth_helper, "get_credentials",
                        lambda: calls.append(1) or None)

    app_module = _app_module()
    for _ in range(5):
        configured, connected, email, expired = app_module._oauth_status()
        assert expired and not connected
    assert len(calls) == 1


def test_the_google_check_is_retried_once_its_ttl_passes(token_file, monkeypatch):
    import google_auth_helper
    token_file.write_text('{"token": "old"}')
    calls = []
    monkeypatch.setattr(google_auth_helper, "get_credentials",
                        lambda: calls.append(1) or None)
    app_module = _app_module()
    app_module._oauth_status()

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic",
                        lambda: real_monotonic() + app_module._OAUTH_STATUS_TTL + 1)
    app_module._oauth_status()
    assert len(calls) == 2


def test_signing_in_is_reflected_on_the_very_next_load(token_file, monkeypatch):
    """A new token.json is a new cache key, so connecting never waits out the
    TTL behind a stale "not connected"."""
    import google_auth_helper
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda: object())
    app_module = _app_module()

    assert app_module._oauth_status()[1] is False      # no token yet
    token_file.write_text('{"token": "new", "email": "me@gmail.com"}')
    configured, connected, email, expired = app_module._oauth_status()
    assert connected and email == "me@gmail.com"


def test_disconnecting_is_reflected_on_the_very_next_load(client, token_file, monkeypatch):
    import google_auth_helper
    token_file.write_text('{"token": "t", "email": "me@gmail.com"}')
    monkeypatch.setattr(google_auth_helper, "get_credentials", lambda: object())
    app_module = _app_module()
    assert app_module._oauth_status()[1] is True

    client.post("/oauth/disconnect", headers=client.origin)
    assert app_module._oauth_status()[1] is False


def test_google_sign_in_outcomes_land_on_the_gmail_settings(client):
    """The Gmail controls moved to Settings; landing on the tracker showed the
    result on a page with nothing to act on."""
    response = client.post("/oauth/disconnect", headers=client.origin)
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
    """Long caching is only safe because an edited file gets a new URL."""
    from pathlib import Path
    app_module = _app_module()
    css = Path(app_module.app.static_folder) / "style.css"
    original = css.stat().st_mtime
    try:
        with app_module.app.test_request_context():
            before = app_module._inject_asset_urls()["asset"]("style.css")
            os.utime(css, (original + 10, original + 10))
            after = app_module._inject_asset_urls()["asset"]("style.css")
        assert before != after
    finally:
        os.utime(css, (original, original))


# ---------------------------------------------------------------------------
# Hover preloading
# ---------------------------------------------------------------------------

def test_pages_preload_links_on_hover_but_never_the_google_sign_in(client):
    page = client.get("/").data.decode()
    assert '<script type="speculationrules">' in page
    rules = page.split('<script type="speculationrules">', 1)[1].split("</script>", 1)[0]
    # Preloading /oauth/start would begin a Google sign-in on mere hover.
    assert '"/oauth/*"' in rules
    assert rules.index('"not"') < rules.index('"/oauth/*"')


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def test_a_companys_timeline_is_read_from_an_index(isolated):
    """Every company page loads its events; without an index that is a full
    scan of a table growing by several rows per company per run."""
    db.init_db()
    conn = db.get_connection()
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE application_id = ? "
        "ORDER BY timestamp ASC", (1,)))
    conn.close()
    assert "idx_events_application" in plan
    assert "SCAN events" not in plan


def test_counting_todays_sends_uses_an_index(isolated):
    db.init_db()
    conn = db.get_connection()
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM applications "
        "WHERE sent_at >= ? AND sent_at < ?", ("2026-09-28", "2026-09-29")))
    conn.close()
    assert "idx_applications_sent_at" in plan


def test_todays_send_count_still_matches_exactly_the_same_rows(isolated, make_app):
    """The range replaced LIKE 'YYYY-MM-DD%'; it must count the same rows,
    including the edge of midnight on both sides."""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    make_app(email="a@x.com", status="sent", sent_at=f"{today}T00:00:00.000000+00:00")
    make_app(email="b@x.com", status="sent", sent_at=f"{today}T23:59:59.999999+00:00")
    make_app(email="c@x.com", status="bounced", sent_at=f"{today}T12:00:00+00:00")
    make_app(email="d@x.com", status="sent", sent_at=f"{yesterday}T23:59:59+00:00")
    make_app(email="e@x.com", status="sent", sent_at=f"{tomorrow}T00:00:00+00:00")

    # a, b and the bounced c — sent_at counts, not status (see count_sent_today).
    assert db.count_sent_today() == 3
