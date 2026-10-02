"""Dashboard routes: CSRF, filters, edit protection, skip, overview, timeline,
the EN/FR switch — and that every route only ever sees the signed-in user's rows."""

import pytest
from markupsafe import escape

import cache_store
import runs


def test_index_renders(client):
    assert client.get("/").status_code == 200


def test_cross_origin_post_is_blocked(client, make_app, data):
    """Any site you merely visit must not be able to queue real sends."""
    app_id = make_app()
    response = client.post("/api/send-job", json={"app_ids": [app_id]},
                           headers={"Origin": "http://evil.example", "X-CSRF-Token": client.csrf})
    assert response.status_code == 403
    assert data.get_application_by_id(app_id)["status"] == "ready"


def test_post_without_csrf_token_is_blocked(client, make_app, data):
    app_id = make_app()
    response = client.post("/api/send-job", json={"app_ids": [app_id]},
                           headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    assert data.get_application_by_id(app_id)["status"] == "ready"


def test_post_without_origin_or_referer_is_blocked(client):
    assert client.post("/settings", data={"section": "sending"},
                       headers={"X-CSRF-Token": client.csrf}).status_code == 403


def test_same_origin_post_with_token_is_allowed(client, make_app, user_id):
    from user_config import UserConfig
    UserConfig(user_id).save_cv("cv.pdf", b"%PDF-1.4 test")
    app_id = make_app()
    response = client.post("/api/send-job", json={"app_ids": [app_id]}, headers=client.origin)
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_sending_needs_a_cv(client, make_app, data):
    app_id = make_app()
    data_ = client.post("/api/send-job", json={"app_ids": [app_id]}, headers=client.origin).get_json()
    assert data_["ok"] is False and "CV" in data_["message"]
    assert data.get_application_by_id(app_id)["status"] == "ready"


def test_get_requests_are_exempt_from_csrf(client):
    assert client.get("/api/overview", headers={"Origin": "http://evil.example"}).status_code == 200


def test_send_job_rejects_bad_ids(client):
    response = client.post("/api/send-job", json={"app_ids": ["abc"]}, headers=client.origin)
    assert response.status_code == 400


def test_cannot_edit_a_sent_email(client, make_app, data):
    app_id = make_app(status="sent")
    response = client.post("/api/update-draft", json={"app_id": app_id, "subject": "X", "body": "Y"},
                           headers=client.origin)
    assert response.get_json()["ok"] is False
    assert data.get_application_by_id(app_id)["subject"] == "Subject"


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


def test_overview_never_shows_another_users_rows(client, make_app, make_user):
    other = make_user("other@example.com")
    make_app(owner=other, company="Secret Co", email="secret@x.com")
    make_app(company="Mine", email="mine@x.com")
    data = client.get("/api/overview").get_json()
    assert "Secret Co" not in data["rows_html"]
    assert data["total_count"] == 1


def test_group_filter_selects_a_whole_stage(client, make_app):
    make_app(company="F", email="f@x.com", status="failed")
    make_app(company="B", email="b@x.com", status="bounced")
    make_app(company="R", email="r@x.com", status="ready")
    assert client.get("/api/overview?status=problems").get_json()["table_total"] == 2
    assert client.get("/api/overview?status=ready").get_json()["table_total"] == 1


def test_search_treats_wildcards_literally(client, make_app):
    make_app(company="Acme", email="a@x.com")
    make_app(company="Globex", email="g@x.com")
    assert client.get("/api/overview?q=%25").get_json()["table_total"] == 0
    assert client.get("/api/overview?q=acm").get_json()["table_total"] == 1


def test_bad_pagination_params_do_not_500(client):
    assert client.get("/api/applications?page=abc&limit=xyz").status_code == 200


def test_skip_and_unskip_round_trip(client, make_app, data):
    app_id = make_app(status="ready")
    assert client.post(f"/api/skip/{app_id}", json={}, headers=client.origin).get_json()["ok"]
    assert data.get_application_by_id(app_id)["status"] == "skipped"
    assert client.post(f"/api/skip/{app_id}", json={"unskip": True}, headers=client.origin).get_json()["ok"]
    assert data.get_application_by_id(app_id)["status"] == "ready"


def test_cannot_skip_a_sent_row(client, make_app, data):
    app_id = make_app(status="sent")
    assert client.post(f"/api/skip/{app_id}", json={}, headers=client.origin).get_json()["ok"] is False
    assert data.get_application_by_id(app_id)["status"] == "sent"


def test_detail_page_renders_preview_and_tabs(client, make_app):
    app_id = make_app(company="Preview Co", email="p@x.com")
    body = client.get(f"/company/{app_id}").data.decode()
    assert "detail-tab" in body
    assert "email-preview__headers" in body
    assert "Preview Co" in body


@pytest.mark.parametrize("original", [
    "optimisation des coûts cloud avec FinOps",
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


def test_another_users_company_is_a_404_everywhere(client, make_app, make_user):
    """No IDOR: another account's row id behaves like one that doesn't exist."""
    other = make_user("other@example.com")
    foreign = make_app(owner=other, company="Theirs", email="t@x.com")
    import db
    theirs = db.for_user(other)
    assert client.get(f"/company/{foreign}").status_code == 404
    assert client.post(f"/api/skip/{foreign}", json={}, headers=client.origin).status_code == 404
    assert client.post("/api/update-draft", json={"app_id": foreign, "subject": "X", "body": "Y"},
                       headers=client.origin).status_code == 404
    assert client.post(f"/api/regenerate/{foreign}", json={}, headers=client.origin).status_code in (400, 404)
    assert client.post(f"/company/{foreign}/edit", data={"subject": "X", "body": "Y"},
                       headers=client.origin).status_code == 404
    client.post("/api/favorite", json={"app_ids": [foreign]}, headers=client.origin)
    row = theirs.get_application_by_id(foreign)
    assert row["subject"] == "Subject" and row["status"] == "ready" and not row["favorite"]


def test_signed_out_visitors_see_the_landing_page(anon_client):
    page = anon_client.get("/")
    assert page.status_code == 200
    html = page.data.decode()
    assert "Ntern" in html and "/register" in html and 'class="appbar"' not in html
    assert anon_client.post("/", headers={"Origin": "http://localhost"}).status_code in (400, 403, 405)


def test_signed_out_visitors_are_sent_to_sign_in(anon_client):
    response = anon_client.get("/settings")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert anon_client.get("/api/overview").status_code == 401
    assert anon_client.get("/company/1").status_code == 302


def test_security_headers_are_set(client):
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self' 'nonce-" in csp
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# Editing a prepared draft in place
# ---------------------------------------------------------------------------

def test_editing_a_draft_saves_subject_and_body(client, make_app, data):
    app_id = make_app(status="ready")
    result = client.post("/api/update-draft",
                         json={"app_id": app_id, "subject": "New subject", "body": "Rewritten by hand."},
                         headers=client.origin).get_json()
    assert result["ok"] and result["status"] == "ready"
    saved = data.get_application_by_id(app_id)
    assert saved["subject"] == "New subject"
    assert saved["body"] == "Rewritten by hand."


def test_an_edit_is_written_to_the_draft_cache_too(client, make_app, user_id):
    """The cache wins over the DB when a draft is loaded, so an edit that only
    reached the DB would be silently undone by the next run."""
    app_id = make_app(status="ready", email="cache@acme.com")
    client.post("/api/update-draft", json={"app_id": app_id, "subject": "Edited", "body": "Edited body."},
                headers=client.origin)
    assert cache_store.load_draft(user_id, "cache@acme.com")["subject"] == "Edited"


def test_editing_a_failed_draft_reports_the_status_it_moved_to(client, make_app, data):
    app_id = make_app(status="failed")
    result = client.post("/api/update-draft", json={"app_id": app_id, "subject": "Fixed", "body": "Fixed body."},
                         headers=client.origin).get_json()
    assert result["ok"]
    assert result["previous_status"] == "failed"
    assert result["status"] == "ready"
    assert data.get_application_by_id(app_id)["status"] == "ready"


def test_an_edit_cannot_blank_out_a_draft(client, make_app, data):
    app_id = make_app(status="ready")
    for payload in ({"subject": "", "body": "Body"}, {"subject": "S", "body": "   "}):
        result = client.post("/api/update-draft", json={"app_id": app_id, **payload},
                             headers=client.origin).get_json()
        assert result["ok"] is False
    assert data.get_application_by_id(app_id)["subject"] == "Subject"


def test_the_detail_page_offers_an_editor_for_an_editable_draft(client, make_app):
    app_id = make_app(status="ready")
    body = client.get(f"/company/{app_id}").data.decode()
    assert 'id="email-editor"' in body
    assert 'id="save-draft-btn"' in body
    assert f'action="/company/{app_id}/edit"' in body
    assert 'id="preview-dirty"' in body
    assert 'name="csrf_token"' in body


def test_the_detail_page_offers_no_editor_for_a_sent_email(client, make_app):
    app_id = make_app(status="sent")
    body = client.get(f"/company/{app_id}").data.decode()
    assert 'id="email-editor"' not in body
    assert "can't be edited" in body


def test_the_form_post_fallback_saves_and_returns_to_the_company(client, make_app, data):
    app_id = make_app(status="ready")
    response = client.post(f"/company/{app_id}/edit",
                           data={"subject": "Via form", "body": "Saved without JS.", "csrf_token": client.csrf},
                           headers={"Origin": "http://localhost"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/company/{app_id}")
    assert data.get_application_by_id(app_id)["subject"] == "Via form"


def test_the_send_dialog_can_warn_about_unsaved_edits(client, make_app):
    app_id = make_app(status="ready")
    assert 'id="confirm-warn"' in client.get(f"/company/{app_id}").data.decode()


# ---------------------------------------------------------------------------
# Languages: the EN | FR switch
# ---------------------------------------------------------------------------

def _researched(make_app, data, **fields):
    import json
    app_id = make_app(company="Cyber SAS", email="rh@cyber.fr", status="ready", **fields)
    data.update_application(app_id, matched_extra_mentions=json.dumps(["offensive_security"]),
                            match_reasons="{}", hook_status="none offered", company_hook=None)
    return app_id


def test_the_language_switch_rewrites_the_draft_in_french(client, with_profile, make_app, data):
    app_id = _researched(make_app, data)
    result = client.post(f"/api/language/{app_id}", json={"language": "fr"}, headers=client.origin).get_json()
    assert result["ok"], result
    row = data.get_application_by_id(app_id)
    assert row["language"] == "fr"
    assert row["body"].startswith("Madame, Monsieur,")
    assert "Cordialement" in row["body"]
    assert row["subject"].startswith("Stage de fin d'études")


def test_the_language_switch_goes_back_to_english(client, with_profile, make_app, data):
    app_id = _researched(make_app, data)
    client.post(f"/api/language/{app_id}", json={"language": "fr"}, headers=client.origin)
    result = client.post(f"/api/language/{app_id}", json={"language": "en"}, headers=client.origin).get_json()
    assert result["ok"]
    row = data.get_application_by_id(app_id)
    assert row["language"] == "en"
    assert row["body"].startswith("Dear Cyber SAS Team,")


def test_the_language_switch_refuses_a_sent_email(client, with_profile, make_app, data):
    app_id = _researched(make_app, data)
    data.update_application(app_id, status="sent")
    assert client.post(f"/api/language/{app_id}", json={"language": "fr"},
                       headers=client.origin).get_json()["ok"] is False


def test_the_detail_page_shows_the_switch_when_both_languages_exist(client, with_profile, make_app, data):
    app_id = _researched(make_app, data)
    page = client.get(f"/company/{app_id}").data.decode()
    assert 'id="lang-switch"' in page and 'data-lang="fr"' in page


def test_rebuild_keeps_the_drafts_language(client, with_profile, make_app, data):
    app_id = _researched(make_app, data)
    client.post(f"/api/language/{app_id}", json={"language": "fr"}, headers=client.origin)
    assert client.post(f"/api/regenerate/{app_id}", json={}, headers=client.origin).get_json()["ok"]
    assert data.get_application_by_id(app_id)["language"] == "fr"


# ---------------------------------------------------------------------------
# Timeline: failures explain themselves, superseded history folds away
# ---------------------------------------------------------------------------

def _scigility_history(data, app_id):
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
        data.log_event(app_id, stage, message, detail=detail)


def test_superseded_failures_fold_under_earlier_history(client, make_app, data):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    _scigility_history(data, app_id)
    page = client.get(f"/company/{app_id}").data.decode()
    assert "Earlier history" in page
    assert "3 failed attempts" in page
    earlier, current = page.split('<ol class="timeline">', 1)
    assert "Re-researched with the corrected prompt" in current
    assert "Draft rebuilt" in current
    assert "Writer agent failed" not in current


def test_a_failure_says_why_in_plain_words(client, make_app, data):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    _scigility_history(data, app_id)
    page = client.get(f"/company/{app_id}").data.decode()
    assert "The AI provider rejected the API key." in page
    assert "rate limit was hit." in page
    assert "print a character" in page


def test_a_clean_history_is_not_folded(client, make_app, data):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    data.log_event(app_id, "research", "Scraping and analyzing https://acme.com")
    data.log_event(app_id, "write", 'Draft ready: "Internship"')
    page = client.get(f"/company/{app_id}").data.decode()
    assert "Earlier history" not in page
    assert "Draft ready" in page


def test_a_failure_after_the_current_draft_stays_visible(client, make_app, data):
    app_id = make_app(status="ready", body="Dear Team,\n\nBody.")
    data.log_event(app_id, "research", "Scraping and analyzing https://acme.com")
    data.log_event(app_id, "write", 'Draft ready: "Internship"')
    data.log_event(app_id, "research", "Scraping and analyzing https://acme.com")
    data.log_event(app_id, "research", "Research stage crashed", detail={"error": "boom"})
    page = client.get(f"/company/{app_id}").data.decode()
    assert "Earlier history" not in page
    assert "Research stage crashed" in page


# ---------------------------------------------------------------------------
# Preparation runs
# ---------------------------------------------------------------------------

def test_start_preparation_queues_a_run_for_this_user_only(client, monkeypatch, user_id):
    import dashboard.app as dashboard_app
    monkeypatch.setattr(dashboard_app, "_setup_state", lambda: {"prep_ready": True})
    response = client.post("/run", data={"batch_limit": "5", "csrf_token": client.csrf},
                           headers={"Origin": "http://localhost"})
    assert response.status_code == 302
    run = runs.active(user_id)
    assert run and run["status"] == "requested" and run["limit_n"] == 5
    # A second click can't start a parallel run.
    client.post("/run", data={"csrf_token": client.csrf}, headers={"Origin": "http://localhost"})
    assert runs.latest(user_id)["id"] == run["id"]


def test_stop_flags_the_active_run(client, user_id):
    runs.request_run(user_id)
    client.post("/stop", data={"csrf_token": client.csrf}, headers={"Origin": "http://localhost"})
    assert runs.active(user_id)["stop_requested"] == 1


def test_the_run_controls_are_live(client):
    page = client.get("/").data.decode()
    assert 'id="start-prep-btn"' in page and 'id="stop-prep-btn"' in page
    data = client.get("/api/overview").get_json()
    assert data["running"] is False and "stop_requested" in data
