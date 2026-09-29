"""Delivery tracking: bounce notices mark sends as not delivered, the worker
scans each user's inbox on its own schedule, and the dashboard shows them in red."""

from datetime import datetime, timedelta, timezone

import pytest

import bounce_checker
import sender_worker
import worker as worker_module
from user_config import UserConfig

GMAIL_BOUNCE = """Address not found

Your message wasn't delivered to jobs@acme.com because the address couldn't be found.

Final-Recipient: rfc822; jobs@acme.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does not exist.
"""


def _sent(make_app, when=None, **kw):
    return make_app(status="sent", sent_at=(when or datetime.now(timezone.utc)).isoformat(), **kw)


def test_bounce_marks_a_sent_row_not_delivered_with_the_reason(make_app, data):
    app_id = _sent(make_app)
    assert bounce_checker._record_bounce_if_sent(
        data, "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "Delivery Status Notification (Failure)", GMAIL_BOUNCE)
    row = data.get_application_by_id(app_id)
    assert row["status"] == "bounced"
    assert "550-5.1.1" in row["error_message"] and row["error_message"].startswith("Not delivered")


def test_a_bounce_in_my_inbox_never_touches_another_users_row(make_app, make_user):
    """Two users applied to the same company; only the one whose inbox holds
    the bounce gets it."""
    import db
    other = make_user("other@example.com")
    theirs = _sent(make_app, owner=other)
    me = make_user("me@example.com")
    mine = _sent(make_app, owner=me)
    assert bounce_checker._record_bounce_if_sent(
        db.for_user(me), "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)", GMAIL_BOUNCE)
    assert db.for_user(me).get_application_by_id(mine)["status"] == "bounced"
    assert db.for_user(other).get_application_by_id(theirs)["status"] == "sent"


def test_run_check_records_result_and_never_raises(user_id, monkeypatch):
    cfg = UserConfig(user_id)
    monkeypatch.setattr(bounce_checker, "check_bounces", lambda cfg, days_back=3: 2)
    assert bounce_checker.run_check(cfg)["updated"] == 2
    assert bounce_checker.last_check(user_id)["updated"] == 2

    def boom(cfg, days_back=3):
        raise RuntimeError("IMAP login failed")
    monkeypatch.setattr(bounce_checker, "check_bounces", boom)
    result = bounce_checker.run_check(cfg, trigger="auto")
    assert result["error"] == "IMAP login failed"
    assert bounce_checker.last_check(user_id)["trigger"] == "auto"


@pytest.fixture
def probe(user_id, monkeypatch):
    """Runs the worker's bounce scheduling synchronously; returns the calls."""
    calls = []
    monkeypatch.setattr(bounce_checker, "credentials_available", lambda cfg: True)

    def fake_run_check(cfg, days_back=3, trigger="manual"):
        calls.append((cfg.user_id, days_back))
        import db
        result = {"at": db.now(), "trigger": trigger, "updated": 0, "error": None}
        db.for_user(cfg.user_id).set_meta(bounce_checker.LAST_CHECK_KEY, result)
        return result
    monkeypatch.setattr(bounce_checker, "run_check", fake_run_check)

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False
    monkeypatch.setattr(worker_module.threading, "Thread", InlineThread)
    UserConfig(user_id).set_many({"BOUNCE_CHECK_MINUTES": "30"})
    w = worker_module.Worker("test")

    def run():
        w._next_bounce_probe = 0
        w._probe_bounces()
    return calls, run


def test_worker_checks_automatically_after_recent_sends(probe, make_app, data, user_id):
    calls, run = probe
    run()
    assert calls == []          # nothing sent → nothing to check
    _sent(make_app)
    run()
    assert calls == [(user_id, sender_worker.BOUNCE_WINDOW_DAYS)]
    run()                       # checked a moment ago → wait for the interval
    assert len(calls) == 1
    stale = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    data.set_meta(bounce_checker.LAST_CHECK_KEY, {"at": stale})
    run()
    assert len(calls) == 2


def test_worker_ignores_old_sends_and_can_be_disabled(probe, make_app, user_id):
    calls, run = probe
    _sent(make_app, when=datetime.now(timezone.utc) - timedelta(days=5))
    run()
    assert calls == []
    _sent(make_app, company="Beta", email="hr@beta.io")
    UserConfig(user_id).set_many({"BOUNCE_CHECK_MINUTES": "0"})
    run()
    assert calls == []


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
