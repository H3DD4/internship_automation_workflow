"""Re-scan: research and draft chosen companies again from scratch — one
from its page, or many selected in the table — e.g. after an API error."""

import cache_store
import runs


def _researched(make_app, data, user_id, company, email, status="ready"):
    app_id = make_app(company=company, email=email, status=status, hook_status="verified",
                      matched_extra_mentions="[]", industry="unknown", company_hook="x")
    cache_store.save_research(user_id, email, {"areas": [], "company_hook": "x"})
    cache_store.save_draft(user_id, email, {"subject": "S", "body": "B"})
    return app_id


def test_a_reset_forgets_research_and_draft(make_app, data, user_id):
    app_id = _researched(make_app, data, user_id, "Acme", "jobs@acme.com")
    assert data.reset_for_rescan([app_id]) == [app_id]
    row = data.get_application_by_id(app_id)
    assert row["status"] == "pending"
    assert not row["subject"] and not row["body"] and not row["hook_status"] and not row["industry"]
    assert cache_store.load_research(user_id, "jobs@acme.com") is None
    assert cache_store.load_draft(user_id, "jobs@acme.com") is None


def test_sent_queued_and_skipped_companies_are_never_reset(make_app, data):
    sent = make_app(company="S", email="s@x.com", status="sent")
    queued = make_app(company="Q", email="q@x.com", status="queued")
    skipped = make_app(company="K", email="k@x.com", status="skipped")
    assert data.reset_for_rescan([sent, queued, skipped]) == []
    assert data.get_application_by_id(sent)["status"] == "sent"
    assert data.get_application_by_id(sent)["subject"] == "Subject"


def test_rescanning_starts_a_run_for_just_those_companies(client, make_app, data, user_id, monkeypatch):
    from dashboard import app as app_module
    monkeypatch.setattr(app_module, "_setup_state", lambda: {"prep_ready": True})
    a = _researched(make_app, data, user_id, "A", "a@x.com")
    b = _researched(make_app, data, user_id, "B", "b@x.com", status="failed")
    _researched(make_app, data, user_id, "C", "c@x.com")                 # not selected
    reply = client.post("/api/rescan", headers=client.origin, json={"app_ids": [a, b]}).get_json()
    assert reply["ok"] and reply["started"] and reply["reset"] == 2
    run = runs.latest(user_id)
    assert run["status"] == "requested" and runs.targets_of(run) == sorted([a, b])
    assert [r[1] for r in data.rows_for_ids(runs.targets_of(run))] == ["a@x.com", "b@x.com"]


def test_while_a_run_is_going_the_companies_are_reset_for_the_next_one(client, make_app, data, user_id,
                                                                        monkeypatch):
    from dashboard import app as app_module
    monkeypatch.setattr(app_module, "_setup_state", lambda: {"prep_ready": True})
    runs.request_run(user_id)
    a = _researched(make_app, data, user_id, "A", "a@x.com")
    reply = client.post("/api/rescan", headers=client.origin, json={"app_ids": [a]}).get_json()
    assert reply["ok"] and not reply["started"] and "next one" in reply["message"]
    assert data.get_application_by_id(a)["status"] == "pending"


def test_a_targeted_run_only_prepares_its_companies(user_id, make_app, data, monkeypatch):
    import worker as worker_module
    seen = {}

    def fake_execute(uid, role, **kwargs):
        seen.update(kwargs)
        return {}
    monkeypatch.setattr(worker_module, "execute_prep_run", fake_execute)
    a = make_app(company="A", email="a@x.com", status="pending", subject="", body="")
    runs.request_run(user_id, targets=[a])
    worker_module.Worker("w")._execute_run(runs.claim_next("w"))
    assert seen["app_ids"] == [a]


def test_rescan_needs_a_working_setup(client, make_app):
    a = make_app(company="A", email="a@x.com")
    reply = client.post("/api/rescan", headers=client.origin, json={"app_ids": [a]})
    assert reply.status_code == 400 and "Settings" in reply.get_json()["message"]


def test_every_rescannable_row_can_be_selected(client, make_app):
    make_app(company="Pend", email="p@x.com", status="pending", subject="", body="")
    rows = client.get("/api/overview").get_json()["rows_html"]
    assert 'class="row-select-cb"' in rows and 'data-sendable=""' in rows


def test_the_company_page_offers_a_rescan(client, make_app):
    a = make_app(company="Acme", email="jobs@acme.com", status="failed")
    assert "rescan-detail-btn" in client.get(f"/company/{a}").data.decode()
    sent = make_app(company="Sent", email="s@acme.com", status="sent")
    assert "rescan-detail-btn" not in client.get(f"/company/{sent}").data.decode()
