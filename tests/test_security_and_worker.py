"""Encryption binding, SSRF guards, uploads, the legacy import, and the
background worker running two users' preparation side by side."""

import json
import sqlite3

import pytest

import accounts
import db
import runs
import safe_http
import vault
from user_config import ConfigError, UserConfig, validate_cv


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def test_a_secret_copied_to_another_user_does_not_decrypt(make_user):
    a, b = make_user("a@example.com"), make_user("b@example.com")
    token = vault.encrypt("sk-live-123", vault.user_scope(a, "GROQ_API_KEY"))
    assert vault.decrypt(token, vault.user_scope(a, "GROQ_API_KEY")) == "sk-live-123"
    with pytest.raises(vault.VaultError):
        vault.decrypt(token, vault.user_scope(b, "GROQ_API_KEY"))
    with pytest.raises(vault.VaultError):
        vault.decrypt(token, vault.user_scope(a, "GMAIL_APP_PASSWORD"))


def test_a_tampered_secret_is_rejected(user_id):
    token = vault.encrypt("x", vault.user_scope(user_id, "GROQ_API_KEY"))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(vault.VaultError):
        vault.decrypt(tampered, vault.user_scope(user_id, "GROQ_API_KEY"))


def test_key_rotation_keeps_old_secrets_readable(user_id, monkeypatch):
    from cryptography.fernet import Fernet
    import os
    cfg = UserConfig(user_id)
    cfg.set_secret("GROQ_API_KEY", "keep-me")
    old = os.environ["ENCRYPTION_KEYS"]
    monkeypatch.setenv("ENCRYPTION_KEYS", f"{Fernet.generate_key().decode()},{old}")
    assert UserConfig(user_id).secret("GROQ_API_KEY") == "keep-me"


# ---------------------------------------------------------------------------
# Server-side request forgery
# ---------------------------------------------------------------------------

@pytest.fixture
def production_network(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:5432/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "gopher://x", "http://user:pw@example.com/",
])
def test_private_and_odd_urls_are_refused_in_production(production_network, url):
    with pytest.raises(safe_http.BlockedURL):
        safe_http.check_url(url)


def test_a_redirect_to_a_private_address_is_refused(production_network, monkeypatch):
    class Redirect:
        is_redirect, is_permanent_redirect = True, False
        headers = {"location": "http://169.254.169.254/latest/meta-data/"}

        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            return Redirect()
    monkeypatch.setattr(safe_http, "_session", lambda: Session())
    monkeypatch.setattr(safe_http.socket, "getaddrinfo",
                        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))]
                        if host == "example.com" else [(2, 1, 6, "", ("169.254.169.254", port))])
    with pytest.raises(safe_http.BlockedURL):
        safe_http.get("http://example.com/")


def test_mail_servers_on_private_networks_are_refused(production_network):
    with pytest.raises(safe_http.BlockedURL):
        safe_http.check_host("127.0.0.1", 25)
    with pytest.raises(safe_http.BlockedURL):
        safe_http.check_host("evil.com/../x", 25)


def test_research_never_fetches_a_private_address(production_network):
    from agents.research_agent import fetch_website_text
    assert fetch_website_text("http://169.254.169.254/") == ""


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,content", [
    ("cv.pdf", b"<html>not a pdf</html>"), ("cv.exe", b"MZ..."), ("cv.docx", b"%PDF-1.4"), ("cv.pdf", b""),
])
def test_only_real_cv_files_are_accepted(name, content):
    with pytest.raises(ConfigError):
        validate_cv(name, content)


def test_a_cv_filename_cannot_carry_a_path():
    assert validate_cv("../../etc/pass wd.pdf", b"%PDF-1.4")["filename"] == "pass wd.pdf"


def test_the_cv_can_only_be_downloaded_by_its_owner(client, login, make_user, user_id):
    UserConfig(user_id).save_cv("mine.pdf", b"%PDF-1.4 mine")
    response = client.get("/settings/cv")
    assert response.data == b"%PDF-1.4 mine" and "attachment" in response.headers["Content-Disposition"]
    stranger = login(make_user("stranger@example.com"))
    assert stranger.get("/settings/cv").status_code == 404


def test_companies_import_preview_then_append(client, data):
    import io
    upload = lambda: {"companies_file": (io.BytesIO(b"email,company\na@x.com,A\nbad,B\n"), "c.csv"),
                      "csrf_token": client.csrf}
    preview = client.post("/api/companies/preview", data=upload(), headers={"Origin": "http://localhost"},
                          content_type="multipart/form-data").get_json()
    assert preview["ok"] and preview["new"] == 1 and preview["invalid"] == 1
    assert data.company_list_count() == 0                # preview saves nothing
    result = client.post("/api/companies/import", data=upload(), headers={"Origin": "http://localhost"},
                         content_type="multipart/form-data").get_json()
    assert result["ok"] and data.company_list_count() == 1


# ---------------------------------------------------------------------------
# The legacy import
# ---------------------------------------------------------------------------

def _legacy_install(root, spec, spec_fr):
    (root / "cache" / "research").mkdir(parents=True)
    (root / "cache" / "drafts").mkdir(parents=True)
    (root / "specializations.json").write_text(json.dumps(spec), encoding="utf-8")
    (root / "specializations_fr.json").write_text(json.dumps(spec_fr), encoding="utf-8")
    (root / "companies.csv").write_text("company_name,email\nAcme,jobs@acme.com\nBeta,hr@beta.io\n")
    (root / "cv.pdf").write_bytes(b"%PDF-1.4 cv")
    (root / ".env").write_text('YOUR_NAME="Old Owner"\nYOUR_TARGET_ROLE="Internship"\nGROQ_API_KEY="gsk_real"\n'
                               'COMPANIES_FILE_PATH="./companies.csv"\nCV_FILE_PATH="./cv.pdf"\n'
                               'GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"\nMAX_EMAILS_PER_DAY="20"\n')
    (root / "cache" / "research" / "abc123.json").write_text('{"areas": []}')
    conn = sqlite3.connect(root / "applications.db")
    conn.executescript("""
        CREATE TABLE applications (id INTEGER PRIMARY KEY, company_name TEXT, email TEXT, website TEXT,
            contact_name TEXT, status TEXT, subject TEXT, body TEXT, created_at TEXT, updated_at TEXT,
            sent_at TEXT, favorite INTEGER DEFAULT 0);
        CREATE TABLE events (id INTEGER PRIMARY KEY, application_id INTEGER, timestamp TEXT, stage TEXT,
            message TEXT, detail TEXT);
        CREATE TABLE send_jobs (id INTEGER PRIMARY KEY, status TEXT, total_items INTEGER, sent_count INTEGER,
            failed_count INTEGER, created_at TEXT, started_at TEXT, completed_at TEXT);
        CREATE TABLE send_job_items (id INTEGER PRIMARY KEY, job_id INTEGER, application_id INTEGER,
            status TEXT, error_message TEXT, message_id TEXT, attempted_at TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO applications VALUES (41, 'Acme', 'jobs@acme.com', '', '', 'sent', 'S', 'B',
            '2026-01-01', '2026-01-01', '2026-01-02', 1);
        INSERT INTO applications VALUES (42, 'Beta', 'hr@beta.io', '', '', 'ready', 'S2', 'B2',
            '2026-01-01', '2026-01-01', NULL, 0);
        INSERT INTO events VALUES (7, 41, '2026-01-01', 'send', 'sent', NULL);
        INSERT INTO send_jobs VALUES (3, 'completed', 1, 1, 0, '2026-01-02', NULL, NULL);
        INSERT INTO send_job_items VALUES (5, 3, 41, 'sent', NULL, '<m>', NULL);
        INSERT INTO meta VALUES ('bounce_check', '{"at": "2026-01-03"}');
    """)
    conn.commit()
    conn.close()


def test_the_legacy_import_carries_everything_over(isolated, tmp_path, spec, spec_fr):
    import migrate_legacy
    import profiles
    root = tmp_path / "legacy"
    root.mkdir()
    _legacy_install(root, spec, spec_fr)
    result = migrate_legacy.run(email="Owner@Example.com", root=root, password="a-long-legacy-passphrase")
    uid = result["user_id"]
    user = accounts.get_user(uid)
    assert user["role"] == "user" and user["email"] == "owner@example.com"
    assert user["must_change_password"] == 0
    data = db.for_user(uid)
    assert data.get_application_by_id(41)["status"] == "sent"      # ids preserved
    assert data.get_application_by_id(42)["subject"] == "S2"
    assert [e["message"] for e in data.get_events(41)] == ["sent"]
    assert data.get_meta("bounce_check") == {"at": "2026-01-03"}
    assert data.company_list_count() == 2
    assert data.cache_get("research", "abc123") == {"areas": []}
    cfg = UserConfig(uid)
    assert cfg.secret("GROQ_API_KEY") == "gsk_real"
    assert cfg.secret("GMAIL_APP_PASSWORD") == ""                  # the placeholder is not imported
    assert cfg.cv()["content"] == b"%PDF-1.4 cv"
    profile = profiles.load(uid)
    assert profile["mode"] == "custom" and profile["spec_en"] == spec and profile["spec_fr"] == spec_fr
    # New applications continue after the imported ids.
    assert data.get_or_create_application("New", "new@x.com", "") > 42
    # The import refuses to run twice.
    with pytest.raises(migrate_legacy.MigrationError):
        migrate_legacy.run(email="owner@example.com", root=root)


# ---------------------------------------------------------------------------
# The worker: two users preparing at once, each with their own log
# ---------------------------------------------------------------------------

class FakeRouter:
    is_router = True
    should_stop = None
    deployments = [object()]

    def snapshot(self):
        return []

    def complete(self, **kwargs):
        answer = {"areas": ["offensive_security"], "hook": "", "hook_evidence": "",
                  "industry": "security", "summary": ""}
        return type("R", (), {"text": json.dumps(answer), "deployment": "fake/model"})()


def test_two_users_prepare_side_by_side_with_separate_logs(make_user, spec, spec_fr, monkeypatch):
    import profiles
    import worker as worker_module
    monkeypatch.setattr("model_router.build_router", lambda *a, **k: FakeRouter())
    monkeypatch.setattr("agents.research_agent.fetch_website_text",
                        lambda url: "We run penetration testing and vulnerability research for banks. " * 5)
    users = []
    for email, name in (("alice@example.com", "Alice Martin"), ("bob@example.com", "Bob Durand")):
        uid = make_user(email, name=name)
        profiles.save(uid, mode="custom", template_id="custom", spec_en=spec, spec_fr=spec_fr)
        cfg = UserConfig(uid)
        cfg.set_many({"YOUR_NAME": name, "YOUR_TARGET_ROLE": "Internship"})
        cfg.set_secret("GROQ_API_KEY", f"key-{uid}")
        db.for_user(uid).add_companies([{"email": f"jobs@{name.split()[0].lower()}corp.com",
                                         "company_name": f"{name.split()[0]} Corp", "website": "https://x.com",
                                         "contact_name": ""}])
        users.append(uid)
        runs.request_run(uid)

    w = worker_module.Worker("test")
    for _ in range(2):
        run = runs.claim_next("test")
        w._execute_run(run)

    for uid, other in ((users[0], users[1]), (users[0 + 1], users[0])):
        run = runs.latest(uid)
        assert run["status"] == "done", run["log"]
        rows = db.for_user(uid).get_all_applications()
        assert len(rows) == 1 and rows[0]["status"] == "ready"
        me = accounts.get_user(uid)["full_name"]
        assert rows[0]["body"].rstrip().endswith(me)
        other_company = accounts.get_user(other)["full_name"].split()[0] + " Corp"
        assert other_company not in run["log"]                  # each log holds only its own run


def test_a_run_without_a_profile_fails_with_a_clear_message(user_id):
    import worker as worker_module
    runs.request_run(user_id)
    worker_module.Worker("test")._execute_run(runs.claim_next("test"))
    run = runs.latest(user_id)
    assert run["status"] == "failed" and "profile" in run["log"].lower()


def _go_quiet(run_id):
    """Make a running run look like its worker died an hour ago."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    import database
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with database.tx() as conn:
        conn.execute(update(database.prep_runs).where(database.prep_runs.c.id == run_id).values(heartbeat_at=old))


def test_a_dead_workers_run_continues_and_its_rows_are_put_back(make_app, data, user_id):
    run_id = runs.request_run(user_id)
    runs.claim_next("dead-worker")
    _go_quiet(run_id)
    researching = make_app(company="A", email="a@x.com", status="researching", subject="", body="")
    writing = make_app(company="B", email="b@x.com", status="writing", subject="", body="")
    import worker as worker_module
    worker_module.Worker("live")._recover_runs()
    run = runs.latest(user_id)
    assert run["id"] == run_id and run["status"] == "requested"        # queued again, same run
    assert "Continuing where it left off" in run["log"]
    assert data.get_application_by_id(researching)["status"] == "pending"
    assert data.get_application_by_id(writing)["status"] == "researched"  # research kept
    assert runs.claim_next("live")["id"] == run_id                       # a worker picks it up


def test_a_run_the_user_was_stopping_is_not_resumed(user_id):
    run_id = runs.request_run(user_id)
    runs.claim_next("dead-worker")
    runs.request_stop(user_id)
    _go_quiet(run_id)
    assert runs.recover_stale_runs() == [user_id]
    assert runs.latest(user_id)["status"] == "stopped"


def test_a_run_that_keeps_dying_is_closed_after_a_few_tries(user_id):
    run_id = runs.request_run(user_id)
    for _ in range(runs.MAX_RESUMES):
        runs.claim_next("w")
        _go_quiet(run_id)
        runs.recover_stale_runs()
        assert runs.latest(user_id)["status"] == "requested"
    runs.claim_next("w")
    _go_quiet(run_id)
    runs.recover_stale_runs()
    run = runs.latest(user_id)
    assert run["status"] == "failed" and "start it again" in run["summary"]


def test_a_restart_puts_the_run_back_in_the_queue(user_id, monkeypatch, make_app, data):
    import worker as worker_module
    w = worker_module.Worker("restarting")
    stuck = make_app(company="C", email="c@x.com", status="writing", subject="", body="")

    def interrupted_run(*args, **kwargs):
        w.stop_event.set()              # SIGTERM arrives mid-run
        return {"skipped": 1}
    monkeypatch.setattr(worker_module, "execute_prep_run", interrupted_run)
    runs.request_run(user_id)
    w._execute_run(runs.claim_next("restarting"))
    assert runs.latest(user_id)["status"] == "requested"
    assert data.get_application_by_id(stuck)["status"] == "researched"


def test_a_user_stop_ends_the_run_with_nothing_left_half_way(user_id, monkeypatch, make_app, data):
    import worker as worker_module
    w = worker_module.Worker("w")
    stuck = make_app(company="D", email="d@x.com", status="researching", subject="", body="")

    def stopped_run(*args, **kwargs):
        runs.request_stop(user_id)
        return {"skipped": 1}
    monkeypatch.setattr(worker_module, "execute_prep_run", stopped_run)
    runs.request_run(user_id)
    w._execute_run(runs.claim_next("w"))
    assert runs.latest(user_id)["status"] == "stopped"
    assert data.get_application_by_id(stuck)["status"] == "pending"
