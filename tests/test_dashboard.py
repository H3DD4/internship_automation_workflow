"""Dashboard routes: CSRF guard, filters, edit protection, skip, overview."""

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
