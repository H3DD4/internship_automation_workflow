"""Send queue: claiming rows, the daily cap, crash recovery, error routing —
and that none of it ever crosses from one user to another."""

import threading
from unittest.mock import patch

import db
import mail_service
import sender_worker
from user_config import UserConfig


def test_create_send_job_queues_sendable_row(make_app, data):
    app_id = make_app(status="ready")
    job_id, queued = data.create_send_job([app_id])
    assert job_id and queued == [app_id]
    assert data.get_application_by_id(app_id)["status"] == "queued"


def test_second_job_cannot_claim_the_same_row(make_app, data):
    """Regression: a double-click used to create two jobs for one company."""
    app_id = make_app(status="ready")
    _, first_ids = data.create_send_job([app_id])
    second_job, second_ids = data.create_send_job([app_id])
    assert first_ids == [app_id]
    assert second_ids == [] and second_job is None


def test_rows_without_a_draft_are_not_queued(make_app, data):
    app_id = make_app(status="ready", subject="", body="")
    assert data.create_send_job([app_id]) == (None, [])


def test_sent_rows_cannot_be_requeued(make_app, data):
    app_id = make_app(status="sent")
    assert data.create_send_job([app_id])[1] == []


def test_a_user_cannot_queue_another_users_row(make_app, make_user):
    other = make_user("other@example.com")
    foreign = make_app(owner=other, company="Theirs", email="t@x.com")
    mine = db.for_user(other)
    stranger = db.for_user(make_user("stranger@example.com"))
    assert stranger.create_send_job([foreign]) == (None, [])
    assert mine.get_application_by_id(foreign)["status"] == "ready"


def _sender(user_id, settings=None):
    UserConfig(user_id).set_many({"MIN_DELAY_SECONDS": "0", "MAX_DELAY_SECONDS": "0",
                                  "MAX_EMAILS_PER_DAY": "100", **(settings or {})})
    return sender_worker.UserSender(user_id, "user", "test-worker", threading.Event())


def _run_job(sender, job_id, send_result):
    assert db.claim_send_job(job_id, "test-worker", sender_worker.stale_cutoff())
    with patch.object(mail_service, "send", return_value=send_result), patch("builtins.print"):
        sender.process_job(job_id)


def test_successful_send_records_message_id(make_app, data, user_id):
    app_id = make_app()
    job_id, _ = data.create_send_job([app_id])
    _run_job(_sender(user_id), job_id, mail_service.SendResult(
        success=True, message="sent", message_id="<abc@mail>", provider="smtp"))
    row = data.get_application_by_id(app_id)
    assert row["status"] == "sent"
    assert row["message_id"] == "<abc@mail>"
    assert data.get_send_job(job_id)["status"] == "completed"


def test_daily_cap_returns_remaining_rows_to_ready(make_app, data, user_id):
    """Regression: hitting the cap used to leave rows 'queued' forever."""
    first = make_app(company="A", email="a@x.com")
    second = make_app(company="B", email="b@x.com")
    job_id, queued = data.create_send_job([first, second])
    assert len(queued) == 2
    _run_job(_sender(user_id, {"MAX_EMAILS_PER_DAY": "1"}), job_id,
             mail_service.SendResult(success=True, message="sent", provider="smtp"))
    statuses = {data.get_application_by_id(i)["status"] for i in (first, second)}
    assert statuses == {"sent", "ready"}, statuses
    reverted = next(data.get_application_by_id(i) for i in (first, second)
                    if data.get_application_by_id(i)["status"] == "ready")
    assert "cap" in (reverted["error_message"] or "").lower()


def test_daily_cap_is_per_user(make_app, make_user, data, user_id):
    """Another account's sends never count against yours."""
    other = make_user("busy@example.com")
    busy = db.for_user(other)
    for i in range(3):
        make_app(owner=other, company=f"O{i}", email=f"o{i}@x.com", status="sent", sent_at=db.now())
    assert busy.count_sent_today() == 3
    assert data.count_sent_today() == 0
    app_id = make_app()
    job_id, _ = data.create_send_job([app_id])
    _run_job(_sender(user_id, {"MAX_EMAILS_PER_DAY": "1"}), job_id,
             mail_service.SendResult(success=True, message="sent", provider="smtp"))
    assert data.get_application_by_id(app_id)["status"] == "sent"


def test_auth_failure_halts_job_and_frees_rows(make_app, data, user_id):
    first = make_app(company="A", email="a@x.com")
    second = make_app(company="B", email="b@x.com")
    job_id, _ = data.create_send_job([first, second])
    _run_job(_sender(user_id), job_id, mail_service.SendResult(
        success=False, retryable=False, error_code="auth_failed",
        message="Gmail authentication failed", provider="smtp"))
    for app_id in (first, second):
        assert data.get_application_by_id(app_id)["status"] == "ready"


def test_an_item_claimed_elsewhere_is_never_sent_twice(make_app, data, user_id):
    app_id = make_app()
    job_id, _ = data.create_send_job([app_id])
    item = data.get_next_queued_item(job_id)
    assert data.claim_job_item(item["id"]) is True
    assert data.claim_job_item(item["id"]) is False


def test_a_job_owned_by_a_live_worker_cannot_be_claimed(make_app, data):
    app_id = make_app()
    job_id, _ = data.create_send_job([app_id])
    assert db.claim_send_job(job_id, "worker-a", sender_worker.stale_cutoff())
    assert not db.claim_send_job(job_id, "worker-b", sender_worker.stale_cutoff())


def test_recovers_rows_left_sending_by_a_crash(make_app, data):
    """A 'sending' row after a restart: we can't know if it went out, so it
    becomes retry_wait with a warning, never re-sent automatically."""
    app_id = make_app()
    job_id, _ = data.create_send_job([app_id])
    item = data.get_next_queued_item(job_id)
    data.update_send_job_item(item["id"], status="sending")
    data.update_application(app_id, status="sending")
    assert data.recover_interrupted_sends() == 1
    row = data.get_application_by_id(app_id)
    assert row["status"] == "retry_wait"
    assert "Sent folder" in row["error_message"]


def test_recovering_a_dead_workers_job(make_app, data):
    app_id = make_app()
    job_id, _ = data.create_send_job([app_id])
    item = data.get_next_queued_item(job_id)
    data.update_send_job_item(item["id"], status="sending")
    assert db.recover_job_items(job_id) == 1
    assert data.get_application_by_id(app_id)["status"] == "retry_wait"


def test_recovery_frees_orphaned_queued_rows(make_app, data):
    app_id = make_app(status="queued")  # no job row at all
    data.recover_interrupted_sends()
    assert data.get_application_by_id(app_id)["status"] == "ready"


def test_pacing_is_read_per_job(user_id):
    sender = _sender(user_id, {"MIN_DELAY_SECONDS": "7", "MAX_DELAY_SECONDS": "9",
                               "MAX_EMAILS_PER_DAY": "3"})
    sender._load_settings()
    assert sender._min_delay == 7
    assert sender._max_per_day == 3


def test_reversed_delay_bounds_do_not_crash(user_id):
    sender = _sender(user_id, {"MIN_DELAY_SECONDS": "120", "MAX_DELAY_SECONDS": "45"})
    sender._load_settings()
    assert sender._min_delay <= sender._max_delay


def test_invalid_pacing_value_falls_back_to_default(user_id):
    sender = _sender(user_id, {"MAX_EMAILS_PER_DAY": "twenty"})
    sender._load_settings()
    assert sender._max_per_day == 20


def test_a_regular_user_cannot_raise_the_cap_above_the_platform_ceiling(user_id):
    import config
    sender = _sender(user_id, {"MAX_EMAILS_PER_DAY": "100000"})
    sender._load_settings()
    assert sender._max_per_day == config.USER_MAX_EMAILS_PER_DAY


def test_daily_cap_still_counts_a_message_that_later_bounced(make_app, data):
    app_id = make_app(status="sent", sent_at=db.now())
    assert data.count_sent_today() == 1
    data.update_application(app_id, status="bounced")
    assert data.count_sent_today() == 1
