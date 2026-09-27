"""Send queue: claiming rows, the daily cap, crash recovery, error routing."""

import os
from unittest.mock import patch

import db
import mail_service
import sender_worker


def test_create_send_job_queues_sendable_row(make_app):
    app_id = make_app(status="ready")
    job_id, queued = db.create_send_job([app_id])
    assert job_id and queued == [app_id]
    assert db.get_application_by_id(app_id)["status"] == "queued"


def test_second_job_cannot_claim_the_same_row(make_app):
    """Regression: a double-click (or row Send + Send selected at once) used
    to create two jobs for one company, sending the same email twice."""
    app_id = make_app(status="ready")
    first_job, first_ids = db.create_send_job([app_id])
    second_job, second_ids = db.create_send_job([app_id])

    assert first_ids == [app_id]
    assert second_ids == []
    assert second_job is None


def test_rows_without_a_draft_are_not_queued(make_app):
    app_id = make_app(status="ready", subject="", body="")
    job_id, queued = db.create_send_job([app_id])
    assert queued == []
    assert job_id is None


def test_sent_rows_cannot_be_requeued(make_app):
    app_id = make_app(status="sent")
    _, queued = db.create_send_job([app_id])
    assert queued == []


def _worker(env=None):
    worker = sender_worker.SenderWorker()
    defaults = {"MIN_DELAY_SECONDS": "0", "MAX_DELAY_SECONDS": "0", "MAX_EMAILS_PER_DAY": "100"}
    defaults.update(env or {})
    return worker, defaults


def _run_job(worker, env, job_id, send_result):
    with patch.object(mail_service, "send", return_value=send_result), \
         patch("builtins.print"), patch("sender_worker.load_dotenv"), \
         patch.dict(os.environ, env, clear=False):
        worker._process_job(job_id)


def test_successful_send_records_message_id(make_app):
    app_id = make_app()
    job_id, _ = db.create_send_job([app_id])
    worker, env = _worker()
    _run_job(worker, env, job_id, mail_service.SendResult(
        success=True, message="sent", message_id="<abc@mail>", provider="smtp"))

    row = db.get_application_by_id(app_id)
    assert row["status"] == "sent"
    assert row["message_id"] == "<abc@mail>"
    assert db.get_send_job(job_id)["status"] == "completed"


def test_daily_cap_returns_remaining_rows_to_ready(make_app):
    """Regression: on hitting the cap the loop just broke, leaving rows
    'queued' forever — a status that is neither sendable nor editable."""
    first = make_app(company="A", email="a@x.com")
    second = make_app(company="B", email="b@x.com")
    job_id, queued = db.create_send_job([first, second])
    assert len(queued) == 2

    worker, env = _worker({"MAX_EMAILS_PER_DAY": "1"})
    _run_job(worker, env, job_id, mail_service.SendResult(
        success=True, message="sent", provider="smtp"))

    statuses = {db.get_application_by_id(i)["status"] for i in (first, second)}
    assert statuses == {"sent", "ready"}, statuses
    reverted = next(db.get_application_by_id(i) for i in (first, second)
                     if db.get_application_by_id(i)["status"] == "ready")
    assert "cap" in (reverted["error_message"] or "").lower()


def test_auth_failure_halts_job_and_frees_rows(make_app):
    first = make_app(company="A", email="a@x.com")
    second = make_app(company="B", email="b@x.com")
    job_id, _ = db.create_send_job([first, second])

    worker, env = _worker()
    _run_job(worker, env, job_id, mail_service.SendResult(
        success=False, retryable=False, error_code="auth_failed",
        message="Gmail authentication failed", provider="smtp"))

    # Neither company is stuck queued; nothing was silently marked sent.
    for app_id in (first, second):
        assert db.get_application_by_id(app_id)["status"] == "ready"


def test_recovers_rows_left_sending_by_a_crash(make_app):
    """Regression: 'sending' rows were picked up by nothing after a restart
    (the sender only selects 'queued'), so they were stuck permanently."""
    app_id = make_app()
    job_id, _ = db.create_send_job([app_id])
    item = db.get_next_queued_item(job_id)
    db.update_send_job_item(item["id"], status="sending")
    db.update_application(app_id, status="sending")

    recovered = db.recover_interrupted_sends()

    assert recovered == 1
    row = db.get_application_by_id(app_id)
    assert row["status"] == "retry_wait"
    assert "Sent folder" in row["error_message"]


def test_recovery_frees_orphaned_queued_rows(make_app):
    app_id = make_app(status="queued")  # no job row at all
    db.recover_interrupted_sends()
    assert db.get_application_by_id(app_id)["status"] == "ready"


def test_pacing_is_read_from_env_per_job():
    """Regression: these were read at import, before .env was loaded, so the
    configured values never applied."""
    worker = sender_worker.SenderWorker()
    with patch("sender_worker.load_dotenv"), \
         patch.dict(os.environ, {"MIN_DELAY_SECONDS": "7", "MAX_EMAILS_PER_DAY": "3"}, clear=False):
        worker._load_settings()
    assert worker._min_delay == 7
    assert worker._max_per_day == 3


def test_reversed_delay_bounds_do_not_crash():
    worker = sender_worker.SenderWorker()
    with patch("sender_worker.load_dotenv"), \
         patch.dict(os.environ, {"MIN_DELAY_SECONDS": "120", "MAX_DELAY_SECONDS": "45"}, clear=False):
        worker._load_settings()
    assert worker._min_delay <= worker._max_delay


def test_invalid_pacing_value_falls_back_to_default():
    worker = sender_worker.SenderWorker()
    with patch("sender_worker.load_dotenv"), \
         patch.dict(os.environ, {"MAX_EMAILS_PER_DAY": "twenty"}, clear=False):
        worker._load_settings()
    assert worker._max_per_day == 20


def test_daily_cap_still_counts_a_message_that_later_bounced(make_app):
    """The cap limits what left the account today; a later bounce doesn't
    give back a send."""
    app_id = make_app(status="sent", sent_at=db.now())
    assert db.count_sent_today() == 1
    db.update_application(app_id, status="bounced")
    assert db.count_sent_today() == 1
