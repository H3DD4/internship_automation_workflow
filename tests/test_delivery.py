"""Delivery tracking: bounce notices mark sends as not delivered, the worker
scans for them on its own, and the dashboard shows them in red."""

from datetime import datetime, timedelta, timezone

import pytest

import bounce_checker
import db
import sender_worker

GMAIL_BOUNCE = """Address not found

Your message wasn't delivered to jobs@acme.com because the address couldn't be found.

Final-Recipient: rfc822; jobs@acme.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does not exist.
"""


def _sent(make_app, when=None, **kw):
    return make_app(status="sent", sent_at=(when or datetime.now(timezone.utc)).isoformat(), **kw)


def test_bounce_marks_a_sent_row_not_delivered_with_the_reason(make_app):
    app_id = _sent(make_app)
    assert bounce_checker._record_bounce_if_sent(
        "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)",
        GMAIL_BOUNCE)
    row = db.get_application_by_id(app_id)
    assert row["status"] == "bounced"
    assert "550-5.1.1" in row["error_message"] and row["error_message"].startswith("Not delivered")


def test_run_check_records_result_and_never_raises(isolated, monkeypatch):
    monkeypatch.setattr(bounce_checker, "check_bounces", lambda days_back=3: 2)
    assert bounce_checker.run_check()["updated"] == 2
    assert bounce_checker.last_check()["updated"] == 2

    def boom(days_back=3):
        raise RuntimeError("IMAP login failed")
    monkeypatch.setattr(bounce_checker, "check_bounces", boom)
    result = bounce_checker.run_check(trigger="auto")
    assert result["error"] == "IMAP login failed"
    assert bounce_checker.last_check()["trigger"] == "auto"


@pytest.fixture
def worker_env(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(bounce_checker, "credentials_available", lambda: True)
    monkeypatch.setattr(bounce_checker, "check_bounces", lambda days_back=3: calls.append(days_back) or 0)
    monkeypatch.setenv("BOUNCE_CHECK_MINUTES", "30")
    return calls


def test_worker_checks_automatically_after_recent_sends(worker_env, make_app):
    worker = sender_worker.SenderWorker()
    worker._maybe_check_bounces()
    assert worker_env == []          # nothing sent → nothing to check

    _sent(make_app)
    worker._next_bounce_probe = 0
    worker._maybe_check_bounces()
    assert worker_env == [sender_worker.BOUNCE_WINDOW_DAYS]

    worker._next_bounce_probe = 0    # checked a moment ago → wait for the interval
    worker._maybe_check_bounces()
    assert len(worker_env) == 1

    stale = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    db.set_meta(bounce_checker.LAST_CHECK_KEY, {"at": stale})
    worker._next_bounce_probe = 0
    worker._maybe_check_bounces()
    assert len(worker_env) == 2


def test_worker_ignores_old_sends_and_can_be_disabled(worker_env, make_app, monkeypatch):
    _sent(make_app, when=datetime.now(timezone.utc) - timedelta(days=5))
    worker = sender_worker.SenderWorker()
    worker._maybe_check_bounces()
    assert worker_env == []

    _sent(make_app, company="Beta", email="hr@beta.io")
    monkeypatch.setenv("BOUNCE_CHECK_MINUTES", "0")
    worker._next_bounce_probe = 0
    worker._maybe_check_bounces()
    assert worker_env == []


def test_dashboard_shows_undelivered_in_red(client, make_app):
    make_app(status="bounced", error_message="Not delivered — 550 no such user")
    make_app(company="Beta", email="hr@beta.io", status="sent")

    html = client.get("/").data.decode()
    assert "Not delivered" in html
    assert 'class="row--problem"' in html
    alert = html[html.index('id="delivery-alert"'):]
    assert "hidden" not in alert[:alert.index(">")]

    overview = client.get("/api/overview").get_json()
    assert overview["bounced_count"] == 1
    assert "auto_minutes" in overview["bounce_check"]


def test_alert_hidden_when_everything_was_delivered(client, make_app):
    make_app(status="sent")
    html = client.get("/").data.decode()
    alert = html[html.index('id="delivery-alert"'):]
    assert "hidden" in alert[:alert.index(">")]
