"""Sign in with Google: the platform's OAuth client (set by an admin), each
user's own token (encrypted), the redirect and the callback."""

import base64
import io
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

import google_auth_helper
from user_config import UserConfig, get_system_secret

CLIENT_JSON = {
    "web": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "shh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://127.0.0.1:5050/oauth/callback"],
    }
}
SEND = "https://www.googleapis.com/auth/gmail.send"
READ = "https://www.googleapis.com/auth/gmail.readonly"
LOCAL = "http://127.0.0.1:5050"


def _id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _upload(admin_client, data):
    return admin_client.post("/admin/platform", headers={"Origin": "http://localhost"},
                             data={"csrf_token": admin_client.csrf, "action": "google_client",
                                   "client_secret": (io.BytesIO(data), "client.json")},
                             content_type="multipart/form-data")


def _fake_token(monkeypatch, scopes, email="me@gmail.com"):
    from google_auth_oauthlib.flow import Flow

    def fetch_token(self, **kwargs):
        assert kwargs.get("code") == "the-code"
        assert self.code_verifier, "the PKCE verifier from /oauth/start must be reused"
        self.oauth2session.token = {
            "access_token": "at", "refresh_token": "rt", "token_type": "Bearer",
            "expires_in": 3600, "expires_at": time.time() + 3600, "id_token": _id_token(email), "scope": scopes,
        }
        return self.oauth2session.token
    monkeypatch.setattr(Flow, "fetch_token", fetch_token)


@pytest.fixture
def configured(admin_client):
    assert _upload(admin_client, json.dumps(CLIENT_JSON).encode()).status_code == 302
    assert google_auth_helper.oauth_is_configured()
    return admin_client


def _local(test_client):
    """The same signed-in client, addressed as 127.0.0.1 (cookies follow)."""
    from dashboard import security
    cookie = test_client.get_cookie(security.COOKIE_NAME, domain="localhost")
    test_client.set_cookie(security.COOKIE_NAME, cookie.value, domain="127.0.0.1")
    return test_client


def test_only_an_admin_can_set_the_google_client(client):
    response = client.post("/admin/platform", headers={"Origin": "http://localhost"},
                           data={"csrf_token": client.csrf, "action": "google_client",
                                 "client_secret": (io.BytesIO(json.dumps(CLIENT_JSON).encode()), "c.json")},
                           content_type="multipart/form-data")
    assert response.status_code == 404
    assert not google_auth_helper.oauth_is_configured()


def test_rejects_a_file_that_is_not_an_oauth_client(admin_client):
    _upload(admin_client, b'{"type": "service_account"}')
    assert not google_auth_helper.oauth_is_configured()


def test_the_client_is_stored_encrypted(configured):
    import database
    from sqlalchemy import select
    with database.read() as conn:
        stored = conn.execute(select(database.system_secrets.c.ciphertext)).scalar_one()
    assert "shh" not in stored and "client_id" not in stored
    assert json.loads(get_system_secret(google_auth_helper.CLIENT_SECRET_NAME))["web"]["client_secret"] == "shh"


def test_full_connect_flow(configured, client, user_id, monkeypatch):
    UserConfig(user_id).set_many({"GMAIL_ADDRESS": "me@gmail.com"})
    # Opened as "localhost": bounced to 127.0.0.1 first so the cookie and the
    # redirect URI registered on Google's side both match.
    first = client.get("/oauth/start", base_url="http://localhost:5050")
    assert first.headers["Location"] == f"{LOCAL}/oauth/start"
    _local(client)
    start = client.get("/oauth/start", base_url=LOCAL)
    query = parse_qs(urlparse(start.headers["Location"]).query)
    assert query["redirect_uri"] == [f"{LOCAL}/oauth/callback"]
    assert query["login_hint"] == ["me@gmail.com"]
    assert query["code_challenge_method"] == ["S256"]

    _fake_token(monkeypatch, [SEND, READ, "openid"])
    client.get(f"/oauth/callback?state={query['state'][0]}&code=the-code", base_url=LOCAL)

    cfg = UserConfig(user_id)
    assert google_auth_helper.get_authorized_email(cfg) == "me@gmail.com"
    assert cfg.get("MAIL_METHOD") == "oauth"
    import database
    from sqlalchemy import select
    with database.read() as conn:
        raw = conn.execute(select(database.user_secrets.c.ciphertext).where(
            database.user_secrets.c.name == "GOOGLE_TOKEN")).scalar_one()
    assert "rt" not in raw.split("gAAAA")[0] and "refresh_token" not in raw


def test_missing_send_permission_is_refused(configured, client, user_id, monkeypatch):
    _local(client)
    start = client.get("/oauth/start", base_url=LOCAL)
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
    _fake_token(monkeypatch, [READ, "openid"])
    page = client.get(f"/oauth/callback?state={state}&code=the-code", base_url=LOCAL, follow_redirects=True)
    assert not google_auth_helper.token_exists(UserConfig(user_id))
    assert "left unticked" in page.data.decode()


def test_state_mismatch_is_rejected(configured, client, user_id):
    _local(client)
    client.get("/oauth/start", base_url=LOCAL)
    client.get("/oauth/callback?state=forged&code=x", base_url=LOCAL)
    assert not google_auth_helper.token_exists(UserConfig(user_id))


def test_a_callback_cannot_attach_a_token_to_another_account(configured, client, login, make_user, monkeypatch):
    """The state is bound to the user who started the sign-in."""
    _local(client)
    start = client.get("/oauth/start", base_url=LOCAL)
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
    victim = make_user("victim@example.com")
    victim_client = _local(login(victim))
    _fake_token(monkeypatch, [SEND, "openid"])
    victim_client.get(f"/oauth/callback?state={state}&code=the-code", base_url=LOCAL)
    assert not google_auth_helper.token_exists(UserConfig(victim))


def test_disconnect_removes_the_token(client, user_id):
    cfg = UserConfig(user_id)
    cfg.set_secret("GOOGLE_TOKEN", json.dumps({"token": "x", "email": "me@gmail.com"}))
    client.post("/oauth/disconnect", data={"csrf_token": client.csrf}, headers={"Origin": "http://localhost"})
    assert not google_auth_helper.token_exists(UserConfig(user_id))


def test_the_disconnect_form_is_not_nested_in_another_form(configured, client, user_id):
    UserConfig(user_id).set_secret("GOOGLE_TOKEN", json.dumps({"token": "x", "email": "me@gmail.com"}))
    html = client.get("/settings").data.decode()
    assert 'id="oauth-disconnect-form"' in html
    mail_form = html[html.index('id="mail-form"'):]
    mail_form = mail_form[:mail_form.index("</form>")]
    assert "<form" not in mail_form


def test_refreshing_the_token_keeps_the_stored_email(user_id):
    cfg = UserConfig(user_id)
    cfg.set_secret("GOOGLE_TOKEN", json.dumps({"token": "old", "email": "me@gmail.com"}))

    class Creds:
        def to_json(self):
            return json.dumps({"token": "new"})
    google_auth_helper.save_credentials(cfg, Creds())
    assert json.loads(UserConfig(user_id).secret("GOOGLE_TOKEN")) == {"token": "new", "email": "me@gmail.com"}
