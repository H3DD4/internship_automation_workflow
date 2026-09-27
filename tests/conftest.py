"""Shared fixtures.

Every test gets its own temporary database, cache directory and .env, so a
test run can never read the real applications.db, write into the real cache/,
or pick up the developer's real credentials — the previous smoke-test script
did all three.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point db, cache_store and the dashboard's .env at tmp_path."""
    import cache_store
    import db
    import sender_worker

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "applications.db")
    monkeypatch.setattr(db, "_initialized_path", None)

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache_store, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache_store, "RESEARCH_DIR", cache_dir / "research")
    monkeypatch.setattr(cache_store, "DRAFTS_DIR", cache_dir / "drafts")

    env_path = tmp_path / ".env"
    env_path.write_text("")
    monkeypatch.setattr(sender_worker, "ENV_PATH", env_path)

    db.init_db()

    # Saving settings calls load_dotenv(override=True), which writes straight
    # into os.environ; restore it so one test's keys can't leak into the next.
    saved_environ = dict(os.environ)
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved_environ)


@pytest.fixture
def client(isolated, monkeypatch):
    """Flask test client wired to the isolated workspace.

    `origin` on the returned client is the header POSTs need to satisfy the
    dashboard's CSRF guard.
    """
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "ENV_PATH", isolated / ".env")
    monkeypatch.setattr(dashboard_app, "UPLOAD_DIR", isolated / "uploads")
    monkeypatch.setattr(dashboard_app, "RUN_LOG_PATH", isolated / "run.log")
    monkeypatch.setattr(dashboard_app, "STOP_FILE", isolated / "stop.flag")
    # Never start the real background sender thread during tests.
    monkeypatch.setattr(dashboard_app.sender_worker, "ensure_running", lambda: None)

    dashboard_app.app.config["TESTING"] = True
    test_client = dashboard_app.app.test_client()
    test_client.origin = {"Origin": "http://localhost"}
    return test_client


@pytest.fixture
def make_app(isolated):
    """Factory creating an application row in a given state."""
    import db

    def _make(company="Acme", email="jobs@acme.com", status="ready",
              subject="Subject", body="Body", **fields):
        app_id = db.get_or_create_application(company, email, "https://acme.com")
        db.update_application(app_id, status=status, subject=subject, body=body, **fields)
        return app_id

    return _make
