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


# What Gmail actually sends to a French-language account: the readable part
# is French, and the failed address sits in the delivery report part (which
# Python parses into header blocks) and in X-Failed-Recipients.
GMAIL_FR_NOTICE = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Failure)
X-Failed-Recipients: {address}
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain; charset="UTF-8"
Content-Transfer-Encoding: 8bit

** Adresse introuvable **
Votre message n'est pas parvenu à {address}, car l'adresse est introuvable.
La réponse était : 550 5.1.1 The email account that you tried to reach does not exist.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; {address}
Action: failed
Status: 5.1.1

--b1--
"""


def _notice(address, header=None):
    import email
    raw = GMAIL_FR_NOTICE.format(address=address)
    if header:
        raw = raw.replace(f"X-Failed-Recipients: {address}", f"X-Failed-Recipients: {header}")
    return email.message_from_bytes(raw.encode("utf-8"))


@pytest.mark.parametrize("strip", ["nothing", "header", "header+text"])
def test_a_real_gmail_notice_in_french_is_recognised(make_app, data, strip):
    """Every notice in a real inbox went unrecognised: the report part was
    never decoded and the text patterns were English only. Any one of the
    three places the address appears is now enough."""
    app_id = _sent(make_app, email="ismael@modeo.ai")
    msg = _notice("ismael@modeo.ai")
    if strip != "nothing":
        del msg["X-Failed-Recipients"]
    body = bounce_checker._get_body_text(msg)
    if strip == "header+text":
        body = body.replace("parvenu à ismael@modeo.ai", "parvenu")
    assert "Final-Recipient: rfc822; ismael@modeo.ai" in bounce_checker._get_body_text(msg)
    assert bounce_checker._record_bounce_if_sent(data, msg["From"], msg["Subject"], body, msg)
    row = data.get_application_by_id(app_id)
    assert row["status"] == "bounced" and "550 5.1.1" in row["error_message"]


def test_an_address_with_styled_letters_still_matches(make_app, data):
    """An address copied from a web page in 𝐛𝐨𝐥𝐝 letters is stored folded."""
    app_id = _sent(make_app, email="contact@mobileguard.fr")
    from email.header import Header
    styled = "𝐜𝐨𝐧𝐭𝐚𝐜𝐭@mobileguard.fr"
    msg = _notice(styled, header=Header(styled, "utf-8").encode())   # encoded, as Gmail sends it
    assert bounce_checker._record_bounce_if_sent(data, msg["From"], msg["Subject"],
                                                 bounce_checker._get_body_text(msg), msg)
    assert data.get_application_by_id(app_id)["status"] == "bounced"


def test_the_button_looks_back_further_than_the_automatic_check(client, monkeypatch):
    seen = {}

    def fake_run_check(cfg, days_back=3, trigger="manual"):
        seen["days"] = days_back
        return {"at": "now", "trigger": trigger, "updated": 0, "error": None}
    monkeypatch.setattr(bounce_checker, "run_check", fake_run_check)
    client.post("/api/check-bounces", headers=client.origin, json={})
    assert seen["days"] == sender_worker.MANUAL_BOUNCE_WINDOW_DAYS > sender_worker.BOUNCE_WINDOW_DAYS


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
