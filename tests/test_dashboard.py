"""Dashboard routes: CSRF guard, filters, edit protection, skip, overview."""

import pytest
from markupsafe import escape

import db


def test_index_renders(client):
    assert client.get("/").status_code == 200


def test_cross_origin_post_is_blocked(client, make_app):
    """Regression: without this, any site you merely visited could POST to
    the local dashboard — rewriting AI_BASE_URL to steal the API key, or
    queueing real sends."""
    app_id = make_app()
    response = client.post("/api/send-job", json={"app_ids": [app_id]},
                           headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert db.get_application_by_id(app_id)["status"] == "ready"


def test_post_without_origin_or_referer_is_blocked(client):
    assert client.post("/setup", data={}).status_code == 403


def test_same_origin_post_is_allowed(client, make_app):
    app_id = make_app()
    response = client.post("/api/send-job", json={"app_ids": [app_id]},
                           headers=client.origin)
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_get_requests_are_exempt(client):
    assert client.get("/api/gmail-status", headers={"Origin": "http://evil.example"}).status_code == 200


def test_send_job_rejects_bad_ids(client):
    response = client.post("/api/send-job", json={"app_ids": ["abc"]}, headers=client.origin)
    assert response.status_code == 400


def test_cannot_edit_a_sent_email(client, make_app):
    app_id = make_app(status="sent")
    response = client.post("/api/update-draft",
                           json={"app_id": app_id, "subject": "X", "body": "Y"},
                           headers=client.origin)
    assert response.get_json()["ok"] is False
    assert db.get_application_by_id(app_id)["subject"] == "Subject"


def test_overview_returns_rendered_rows_and_grouped_counts(client, make_app):
    make_app(company="Ready Co", email="r@x.com", status="ready")
    make_app(company="Failed Co", email="f@x.com", status="failed")
    make_app(company="Sent Co", email="s@x.com", status="sent")

    data = client.get("/api/overview").get_json()

    assert data["ok"] is True
    assert data["rows_html"].count("<tr") == 3
    assert data["grouped"]["ready"] == 1
    assert data["grouped"]["problems"] == 1
    assert data["grouped"]["sent"] == 1


def test_group_filter_selects_a_whole_stage(client, make_app):
    make_app(company="F", email="f@x.com", status="failed")
    make_app(company="B", email="b@x.com", status="bounced")
    make_app(company="R", email="r@x.com", status="ready")

    assert client.get("/api/overview?status=problems").get_json()["table_total"] == 2
    assert client.get("/api/overview?status=ready").get_json()["table_total"] == 1


def test_bad_pagination_params_do_not_500(client):
    assert client.get("/api/applications?page=abc&limit=xyz").status_code == 200


def test_skip_and_unskip_round_trip(client, make_app):
    app_id = make_app(status="ready")

    assert client.post(f"/api/skip/{app_id}", json={}, headers=client.origin).get_json()["ok"]
    assert db.get_application_by_id(app_id)["status"] == "skipped"

    assert client.post(f"/api/skip/{app_id}", json={"unskip": True},
                       headers=client.origin).get_json()["ok"]
    assert db.get_application_by_id(app_id)["status"] == "ready"


def test_cannot_skip_a_sent_row(client, make_app):
    app_id = make_app(status="sent")
    response = client.post(f"/api/skip/{app_id}", json={}, headers=client.origin)
    assert response.get_json()["ok"] is False
    assert db.get_application_by_id(app_id)["status"] == "sent"


def test_detail_page_renders_preview_and_tabs(client, make_app):
    app_id = make_app(company="Preview Co", email="p@x.com")
    body = client.get(f"/company/{app_id}").data.decode()
    assert "detail-tab" in body
    assert "email-preview__headers" in body
    assert "Preview Co" in body


@pytest.mark.parametrize("original", [
    "optimisation des co\u00fbts cloud avec FinOps",
    '<script>alert("hook")</script> & FinOps',
])
def test_detail_shows_original_hook_with_translation_and_evidence(client, make_app, original):
    hook = "cloud cost optimization with FinOps"
    evidence = "FinOps pour optimiser les ressources cloud"
    app_id = make_app(company_hook=hook, hook_original=original, hook_evidence=evidence)

    response = client.get(f"/company/{app_id}")

    assert response.status_code == 200
    body = response.data.decode()
    assert f"<strong>{hook}</strong>" in body
    assert f'<dt>Original hook</dt><dd class="reasoning-text">{escape(original)}</dd>' in body
    assert evidence in body
    assert '<script>alert("hook")</script>' not in body


@pytest.mark.parametrize("original", [None, ""])
def test_detail_omits_original_hook_when_not_translated(client, make_app, original):
    app_id = make_app(company_hook="cloud cost optimization with FinOps", hook_original=original)
    response = client.get(f"/company/{app_id}")
    assert response.status_code == 200
    assert "<dt>Original hook</dt>" not in response.data.decode()


def test_detail_404_for_unknown_company(client):
    assert client.get("/company/9999").status_code == 404


def test_env_save_preserves_comments(client, isolated):
    """Regression: saving the workspace rewrote .env from parsed key=values,
    deleting every comment — and .env.example, which users copy, is mostly
    comments explaining each setting."""
    import dashboard.app as dashboard_app

    env_path = isolated / ".env"
    env_path.write_text("# keep me\nAI_MODEL=hy3\n\n# and me\nYOUR_NAME=Old\n")
    dashboard_app._save_env({"YOUR_NAME": "New"})

    content = env_path.read_text()
    assert "# keep me" in content
    assert "# and me" in content
    assert 'YOUR_NAME="New"' in content
    assert "AI_MODEL=hy3" in content


# ---------------------------------------------------------------------------
# Editing a prepared draft in place
# ---------------------------------------------------------------------------

def test_editing_a_draft_saves_subject_and_body(client, make_app):
    app_id = make_app(status="ready")
    response = client.post("/api/update-draft",
                           json={"app_id": app_id, "subject": "New subject",
                                 "body": "Rewritten by hand."},
                           headers=client.origin)
    data = response.get_json()
    assert data["ok"] and data["status"] == "ready"

    saved = db.get_application_by_id(app_id)
    assert saved["subject"] == "New subject"
    assert saved["body"] == "Rewritten by hand."


def test_an_edit_is_written_to_the_draft_cache_too(client, make_app, isolated):
    """The cache wins over the DB when a draft is loaded, so an edit that only
    reached the DB would be silently undone by the next run."""
    import cache_store
    app_id = make_app(status="ready", email="cache@acme.com")
    client.post("/api/update-draft",
                json={"app_id": app_id, "subject": "Edited", "body": "Edited body."},
                headers=client.origin)
    assert cache_store.load_draft("cache@acme.com")["subject"] == "Edited"


def test_editing_a_failed_draft_reports_the_status_it_moved_to(client, make_app):
    """The page needs this to know its status pill and Send button are stale."""
    app_id = make_app(status="failed")
    data = client.post("/api/update-draft",
                       json={"app_id": app_id, "subject": "Fixed", "body": "Fixed body."},
                       headers=client.origin).get_json()
    assert data["ok"]
    assert data["previous_status"] == "failed"
    assert data["status"] == "ready"
    assert db.get_application_by_id(app_id)["status"] == "ready"


def test_an_edit_cannot_blank_out_a_draft(client, make_app):
    app_id = make_app(status="ready")
    for payload in ({"subject": "", "body": "Body"}, {"subject": "S", "body": "   "}):
        data = client.post("/api/update-draft", json={"app_id": app_id, **payload},
                           headers=client.origin).get_json()
        assert data["ok"] is False
    assert db.get_application_by_id(app_id)["subject"] == "Subject"


def test_the_detail_page_offers_an_editor_for_an_editable_draft(client, make_app):
    app_id = make_app(status="ready")
    body = client.get(f"/company/{app_id}").data.decode()
    assert 'id="email-editor"' in body
    assert 'id="save-draft-btn"' in body
    # Still a real form POST, so editing survives JavaScript being unavailable.
    assert f'action="/company/{app_id}/edit"' in body
    # The preview mirrors the editor live, so it has to be able to say so.
    assert 'id="preview-dirty"' in body


def test_the_detail_page_offers_no_editor_for_a_sent_email(client, make_app):
    app_id = make_app(status="sent")
    body = client.get(f"/company/{app_id}").data.decode()
    assert 'id="email-editor"' not in body
    assert "can't be edited" in body


def test_the_form_post_fallback_saves_and_returns_to_the_company(client, make_app):
    """The no-JS path: a plain form submit, not the JSON endpoint."""
    app_id = make_app(status="ready")
    response = client.post(f"/company/{app_id}/edit",
                           data={"subject": "Via form", "body": "Saved without JS."},
                           headers=client.origin)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/company/{app_id}")
    assert db.get_application_by_id(app_id)["subject"] == "Via form"


def test_the_send_dialog_can_warn_about_unsaved_edits(client, make_app):
    """Sending reads the STORED draft, so the confirm dialog needs somewhere to
    say that unsaved edits would not go out."""
    app_id = make_app(status="ready")
    body = client.get(f"/company/{app_id}").data.decode()
    assert 'id="confirm-warn"' in body


# ---------------------------------------------------------------------------
# Timeline: failures explain themselves, superseded history folds away
# ---------------------------------------------------------------------------

def _scigility_history(app_id):
    """The shape of a real timeline: a console crash and five writer failures
    against a dead key / rate limit in September, then a clean run today."""
    import json as _json
    history = [
        ("research", "Scraping and analyzing https://scigility.com", None),
        ("research", "Research stage crashed",
         {"error": "'charmap' codec can't encode characters in position 102-109"}),
        ("research", "Scraping and analyzing https://scigility.com", None),
        ("write", "Writer agent failed",
         {"error": 'AI provider error 401: {"error":{"message":"Invalid api_key format"}}'}),
        ("write", "Writer agent failed",
         {"error": "AI provider error 429: The request rate exceeds the current model RPM limit"}),
        ("research", "Scraping and analyzing https://scigility.com", None),
        ("write", 'Draft ready: "Internship"', None),
        ("research", "Re-researched with the corrected prompt (website's own language).", None),
        ("write", 'Draft rebuilt: "Internship"', None),
    ]
    for stage, message, detail in history:
        db.log_event(app_id, stage, message, detail=detail)


def test_superseded_failures_fold_under_earlier_history(client, make_app):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    _scigility_history(app_id)
    page = client.get(f"/company/{app_id}").data.decode()

    assert "Earlier history" in page
    assert "3 failed attempts" in page
    # The folded list is <ol class="timeline timeline--earlier">; the current
    # attempt is the plain <ol class="timeline"> after it.
    earlier, current = page.split('<ol class="timeline">', 1)
    # The attempt that produced today's draft is shown in full…
    assert "Re-researched with the corrected prompt" in current
    assert "Draft rebuilt" in current
    # …and the September failures are folded above it, not mixed in.
    assert "Writer agent failed" not in current


def test_a_failure_says_why_in_plain_words(client, make_app):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    _scigility_history(app_id)
    page = client.get(f"/company/{app_id}").data.decode()
    assert "The AI provider rejected the API key." in page
    assert "The AI provider&#39;s rate limit was hit." in page or \
           "The AI provider's rate limit was hit." in page
    assert "Couldn&#39;t print a character" in page or "Couldn't print a character" in page


def test_a_clean_history_is_not_folded(client, make_app):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    db.log_event(app_id, "research", "Scraping and analyzing https://acme.com")
    db.log_event(app_id, "write", 'Draft ready: "Internship"')
    page = client.get(f"/company/{app_id}").data.decode()
    assert "Earlier history" not in page
    assert "Draft ready" in page


def test_a_failure_after_the_current_draft_stays_visible(client, make_app):
    """Only what the current draft superseded folds away. A newer attempt
    that failed is news, and must be in plain sight."""
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    db.log_event(app_id, "research", "Scraping and analyzing https://acme.com")
    db.log_event(app_id, "write", 'Draft ready: "Internship"')
    db.log_event(app_id, "research", "Scraping and analyzing https://acme.com")
    db.log_event(app_id, "research", "Research stage crashed", detail={"error": "boom"})
    page = client.get(f"/company/{app_id}").data.decode()
    assert "Earlier history" not in page
    assert "Research stage crashed" in page


def test_a_preparation_run_writes_a_live_readable_log(client, isolated, monkeypatch):
    """The Activity log was empty mid-run (block-buffered output to a file)
    and showed "�" for every dash (cp1252 output read back as UTF-8)."""
    import dashboard.app as dashboard_app
    companies = isolated / "companies.csv"
    companies.write_text("company_name,email\nAcme,a@acme.com\n")
    monkeypatch.setenv("COMPANIES_FILE_PATH", str(companies))
    monkeypatch.setattr(dashboard_app, "_setup_state", lambda: {"prep_ready": True})

    launched = {}

    class FakeProcess:
        def __init__(self, command, **kwargs):
            launched.update(kwargs)

        def poll(self):
            return 0

    monkeypatch.setattr(dashboard_app.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(dashboard_app, "run_process", None)
    client.post("/run", headers=client.origin)

    assert launched["env"]["PYTHONUNBUFFERED"] == "1"
    assert launched["env"]["PYTHONIOENCODING"] == "utf-8"


def test_the_run_controls_are_live(client):
    """The page kept showing "Preparing…" until a manual reload, even after
    the run had stopped — so a Stop looked like it did nothing."""
    page = client.get("/").data.decode()
    assert 'id="start-prep-btn"' in page and 'id="stop-prep-btn"' in page
    data = client.get("/api/overview").get_json()
    assert data["running"] is False and "stop_requested" in data
