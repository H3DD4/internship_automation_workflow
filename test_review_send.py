"""
Smoke tests for the Review & Send queue architecture.
Run: python test_review_send.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def ok(name: str):
    global passed
    passed += 1
    print(f"  OK  {name}")


def fail(name: str, detail: str):
    global failed
    failed += 1
    print(f"  FAIL {name}: {detail}")


def test_imports():
    from ai_client import CompatibleAIClient, RateLimiter, get_global_rate_limiter
    from mail_service import send, SendResult
    from sender_worker import ensure_running, is_running, SenderWorker
    from pipeline import Pipeline
    ok("module imports")


def test_ai_client_retry_on_empty_body():
    from ai_client import CompatibleAIClient, RateLimiter

    class FakeResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

        def json(self):
            return json.loads(self.text)

    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(200, "")
        return FakeResponse(200, json.dumps({
            "choices": [{"message": {"content": "hello"}}]
        }))

    client = CompatibleAIClient("key", "https://example.com", rate_limiter=RateLimiter(10000))
    with patch("ai_client.requests.post", side_effect=fake_post), \
         patch("ai_client.time.sleep"), \
         patch("builtins.print"):
        result = client.messages.create("m", 10, "sys", [{"role": "user", "content": "hi"}])
    assert result.content[0].text == "hello"
    assert calls["n"] == 2
    ok("ai_client retries empty body then succeeds")


def test_db_schema_and_migrations(tmp_db: Path):
    import db
    db.DB_PATH = tmp_db
    db.init_db()

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "send_jobs" in tables
    assert "send_job_items" in tables

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
    for col in ("send_attempts", "last_attempt_at", "message_id", "error_code"):
        assert col in cols, f"missing column {col}"

    conn.execute(
        "INSERT INTO applications (company_name, email, status, created_at, updated_at) "
        "VALUES ('Old Co', 'old@example.com', 'drafted', 't', 't')"
    )
    conn.commit()
    conn.close()

    db.init_db()
    row = db.get_application_by_email("old@example.com")
    assert row["status"] == "ready", f"expected ready, got {row['status']}"
    ok("db schema + drafted-to-ready migration")


def test_send_job_lifecycle(tmp_db: Path):
    import db
    db.DB_PATH = tmp_db
    db.init_db()

    app_id = db.get_or_create_application("Acme", "acme@test.com", "https://acme.com")
    db.update_application(app_id, status="ready", subject="Hi", body="Body text")

    job_id = db.create_send_job([app_id])
    assert job_id > 0

    app = db.get_application_by_id(app_id)
    assert app["status"] == "queued"

    job = db.get_send_job(job_id)
    assert job["total_items"] == 1
    assert job["queued"] == 1
    assert len(job["items"]) == 1

    item = db.get_next_queued_item(job_id)
    assert item["application_id"] == app_id
    assert item["subject"] == "Hi"

    db.update_send_job_item(item["id"], status="sent", message_id="msg-1")
    db.update_application(app_id, status="sent", sent_at=db.now())
    db.update_send_job(job_id, status="completed", completed_at=db.now())

    job = db.get_send_job(job_id)
    assert job["sent_count"] == 1
    assert db.get_next_queued_item(job_id) is None
    ok("send job create + poll lifecycle")


def test_mail_service_no_draft():
    from mail_service import send, SendResult
    result = send({"email": "x@test.com", "subject": "", "body": ""})
    assert isinstance(result, SendResult)
    assert not result.success
    assert result.error_code == "no_draft"
    ok("mail_service rejects missing draft")


def test_mail_service_success_mock():
    from mail_service import send, SendResult
    with patch("mail_service.send_email") as mock_send:
        with patch.dict(os.environ, {"CV_FILE_PATH": "/tmp/cv.pdf"}, clear=False):
            result = send({
                "email": "ok@test.com",
                "subject": "Subject",
                "body": "Body",
            })
    assert result.success
    assert mock_send.called
    ok("mail_service send success path")


def test_sender_worker_processes_job(tmp_db: Path):
    import db
    import mail_service
    import sender_worker

    db.DB_PATH = tmp_db
    db.init_db()

    app_id = db.get_or_create_application("SendCo", "send@test.com", "")
    db.update_application(app_id, status="ready", subject="S", body="B")
    job_id = db.create_send_job([app_id])

    worker = sender_worker.SenderWorker()
    worker._min_delay = 0
    worker._max_delay = 0
    worker._max_per_day = 100

    with patch.object(mail_service, "send", return_value=mail_service.SendResult(
        success=True, message="sent", provider="smtp"
    )), patch("builtins.print"):
        worker._process_job(job_id)

    app = db.get_application_by_id(app_id)
    assert app["status"] == "sent", f"expected sent, got {app['status']}"
    job = db.get_send_job(job_id)
    assert job["status"] == "completed"
    assert job["sent_count"] == 1
    ok("sender_worker processes job to sent")


def test_needs_preparation_skips_done(tmp_db: Path):
    import db
    from pipeline import needs_preparation

    db.DB_PATH = tmp_db
    db.init_db()
    app_id = db.get_or_create_application("Done", "done@test.com", "")
    db.update_application(app_id, status="ready", subject="S", body="B")
    app = db.get_application_by_id(app_id)
    assert needs_preparation(app) is False

    app_id2 = db.get_or_create_application("Need", "need@test.com", "")
    app2 = db.get_application_by_id(app_id2)
    assert needs_preparation(app2) is True
    ok("needs_preparation skips ready rows with drafts")


def test_research_from_db_fallback(tmp_db: Path):
    import db
    from pipeline import _load_research

    db.DB_PATH = tmp_db
    db.init_db()
    app_id = db.get_or_create_application("R", "r@test.com", "")
    db.update_application(
        app_id, status="researched", industry="Tech",
        talking_points='["cloud"]',
        matched_extra_mentions='["cloud"]',
        match_reasons='{"cloud": "they use aws"}',
    )
    app = db.get_application_by_id(app_id)
    ctx = _load_research("r@test.com", app)
    assert ctx is not None
    assert ctx["industry"] == "Tech"
    ok("research context rebuilt from DB when cache missing")


def test_pipeline_writer_marks_ready(tmp_db: Path):
    import db
    import cache_store
    from pipeline import Pipeline

    db.DB_PATH = tmp_db
    db.init_db()

    email = "pipeline@test.com"
    cache_store.save_draft(email, {"subject": "Cached sub", "body": "Cached body"})
    cache_store.save_research(email, {
        "industry": "tech",
        "mission_or_focus": "stuff",
        "tone_of_voice": "formal",
        "talking_points": [],
        "matched_extra_mentions": [],
        "match_reasons": {},
    })

    app_id = db.get_or_create_application("PipeCo", email, "https://pipe.com")

    class FakeClient:
        pass

    cfg = {
        "extra_mentions": {},
        "core_identity": "id",
        "applicant_name": "Me",
        "target_role": "Intern",
    }
    pipeline = Pipeline(FakeClient(), cfg, "model", dry_run=False,
                        research_workers=1, writer_workers=1)

    with patch("builtins.print"):
        pipeline._writer_task(
            app_id, "PipeCo", email, "https://pipe.com", "",
            cache_store.load_research(email),
        )

    app = db.get_application_by_id(app_id)
    assert app["status"] == "ready"
    assert app["subject"] == "Cached sub"
    assert pipeline.results["ready"] == 1
    ok("pipeline writer marks ready (cached draft)")


def test_dashboard_api_routes(tmp_db: Path):
    import db
    db.DB_PATH = tmp_db
    db.init_db()

    app_id = db.get_or_create_application("DashCo", "dash@test.com", "")
    db.update_application(app_id, status="ready", subject="Sub", body="Body")

    from dashboard.app import app as flask_app
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()

    # Edit protection on sent
    db.update_application(app_id, status="sent")
    resp = client.post("/api/update-draft", json={
        "app_id": app_id, "subject": "X", "body": "Y"
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert "Cannot edit" in data["message"]

    db.update_application(app_id, status="ready", subject="Sub", body="Body")

    resp = client.post("/api/send-job", json={"app_ids": [app_id]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    job_id = data["job_id"]

    resp = client.get(f"/api/send-job/{job_id}")
    assert resp.status_code == 200
    poll = resp.get_json()
    assert poll["ok"] is True
    assert poll["total"] == 1
    assert poll["queued"] == 1

    resp = client.get("/api/applications?status=queued")
    assert resp.status_code == 200
    apps = resp.get_json()
    assert apps["total"] >= 1
    ok("dashboard API routes (send-job, poll, applications, edit guard)")


def main():
    print("\nReview & Send smoke tests\n" + "=" * 40)

    tmp = Path(tempfile.mkdtemp(prefix="review_send_test_"))
    try:
        tests = [
            ("imports", lambda: test_imports()),
            ("ai retry", lambda: test_ai_client_retry_on_empty_body()),
            ("db schema", lambda: test_db_schema_and_migrations(tmp / "schema.db")),
            ("send job", lambda: test_send_job_lifecycle(tmp / "job.db")),
            ("mail no draft", lambda: test_mail_service_no_draft()),
            ("mail mock send", lambda: test_mail_service_success_mock()),
            ("sender worker", lambda: test_sender_worker_processes_job(tmp / "worker.db")),
            ("needs prep skip", lambda: test_needs_preparation_skips_done(tmp / "need.db")),
            ("research from db", lambda: test_research_from_db_fallback(tmp / "rdb.db")),
            ("pipeline ready", lambda: test_pipeline_writer_marks_ready(tmp / "pipe.db")),
            ("dashboard API", lambda: test_dashboard_api_routes(tmp / "dash.db")),
        ]

        for name, fn in tests:
            try:
                fn()
            except Exception as e:
                fail(name, str(e))
    finally:
        import gc
        gc.collect()
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass

    print("=" * 40)
    print(f"Results: {passed} passed, {failed} failed\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
