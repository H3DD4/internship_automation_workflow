"""Shared fixtures.

Every test gets its own empty database, and the environment is set BEFORE
any app module is imported — so the suite can never read or write the real
database, the real .env, or real credentials.

By default tests run on a throwaway SQLite file per test. To run the same
suite on PostgreSQL:

    TEST_DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5433/internship_test pytest -q
"""

import os
import sys
from pathlib import Path

# ---- Environment first: nothing below may see the developer's .env values.
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite://"            # replaced per test
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use-000000000000"
os.environ["ENCRYPTION_KEYS"] = "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcy0hISE="
os.environ["EMBEDDED_WORKER"] = "0"
os.environ["ALLOW_PRIVATE_URLS"] = "1"
os.environ["SIGNUP_MODE"] = "approval"

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_SECRET_FILES = [ROOT / "token.json", ROOT / "credentials.json",
                     *ROOT.glob("client_secret_*.json"), ROOT / ".env", ROOT / "applications.db"]


def _fingerprint(paths):
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in paths}


@pytest.fixture(scope="session", autouse=True)
def real_files_are_never_touched():
    """Tripwire: fail the run if any test changed the real .env, database,
    token or OAuth client file."""
    before = _fingerprint(REAL_SECRET_FILES)
    yield
    after = _fingerprint(REAL_SECRET_FILES)
    changed = [path for path in before if before[path] != after[path]]
    assert not changed, f"a test modified real files: {changed}"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A fresh, empty database for this test."""
    import database
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        monkeypatch.setenv("DATABASE_URL", url)
        database.dispose_engine()
        engine = database.get_engine()
        database.metadata.drop_all(engine)
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS schema_version"))
        database._initialized.clear()
    else:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
        database.dispose_engine()
    database.init_schema()
    yield tmp_path
    database.dispose_engine()


def _make_user(email="user@example.com", role="user", status="active", name="Test User"):
    import accounts
    return accounts.create_user(email, "correct-horse-battery-9", full_name=name, role=role, status=status)


@pytest.fixture
def user_id(isolated):
    return _make_user()


@pytest.fixture
def data(user_id):
    import db
    return db.for_user(user_id)


@pytest.fixture
def make_user(isolated):
    return _make_user


@pytest.fixture
def make_app(data):
    """Factory creating an application row (for the default user) in a given state."""
    def _make(company="Acme", email="jobs@acme.com", status="ready", subject="Subject", body="Body",
              owner=None, **fields):
        import db
        target = db.for_user(owner) if owner else data
        app_id = target.get_or_create_application(company, email, "https://acme.com")
        target.update_application(app_id, status=status, subject=subject, body=body, **fields)
        return app_id
    return _make


def _login(test_client, uid):
    import accounts
    from dashboard import security
    token, session_id = accounts.create_session(uid, "127.0.0.1", "pytest")
    test_client.set_cookie(security.COOKIE_NAME, token, domain="localhost")
    from sqlalchemy import select
    import database
    with database.read() as conn:
        csrf = conn.execute(select(database.auth_sessions.c.csrf_token)
                            .where(database.auth_sessions.c.id == session_id)).scalar_one()
    test_client.origin = {"Origin": "http://localhost", "X-CSRF-Token": csrf}
    test_client.csrf = csrf
    test_client.user_id = uid
    return test_client


@pytest.fixture
def app_module(isolated):
    import dashboard.app as dashboard_app
    dashboard_app.app.config["TESTING"] = True
    return dashboard_app


@pytest.fixture
def anon_client(app_module):
    test_client = app_module.app.test_client()
    test_client.origin = {"Origin": "http://localhost"}
    return test_client


@pytest.fixture
def client(app_module, user_id):
    """A test client signed in as the default user. `client.origin` holds the
    headers a POST needs (same Origin + the session's CSRF token)."""
    return _login(app_module.app.test_client(), user_id)


@pytest.fixture
def login(app_module):
    """login(user_id) -> a new client signed in as that user."""
    return lambda uid: _login(app_module.app.test_client(), uid)


@pytest.fixture
def admin_client(app_module, make_user):
    uid = make_user("admin@example.com", role="admin", name="Admin")
    return _login(app_module.app.test_client(), uid)


SAMPLE_SPEC = None


@pytest.fixture
def spec():
    import json
    return json.loads((ROOT / "specializations.json").read_text(encoding="utf-8"))


@pytest.fixture
def spec_fr():
    import json
    return json.loads((ROOT / "specializations_fr.json").read_text(encoding="utf-8"))


@pytest.fixture
def with_profile(user_id, spec, spec_fr):
    """Give the default user the original hand-written wording (EN + FR)."""
    import profiles
    import user_config
    profiles.save(user_id, mode="custom", template_id="custom", language_mode="auto",
                  spec_en=spec, spec_fr=spec_fr)
    user_config.UserConfig(user_id).set_many({"YOUR_NAME": "Mohamed Hedda",
                                              "YOUR_TARGET_ROLE": "End-of-Study Internship"})
    return user_id
