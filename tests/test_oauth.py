"""Connecting Gmail with Google OAuth: client upload, redirect, callback."""

import base64
import io
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

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


def _id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.fixture
def oauth_client(client, isolated, monkeypatch):
    import google_auth_helper
    monkeypatch.setattr(google_auth_helper, "ROOT_DIR", isolated)
    monkeypatch.setattr(google_auth_helper, "TOKEN_PATH", isolated / "token.json")
    # No network: the Gmail profile lookup fails, so the id_token is used.
    monkeypatch.setattr(google_auth_helper, "_build_gmail_service",
                        lambda credentials=None: (_ for _ in ()).throw(RuntimeError("offline")))
    return client


def _upload(client, data):
    return client.post("/oauth/client-secret", headers=client.origin,
                       data={"client_secret": (io.BytesIO(data), "client.json")},
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


def test_rejects_a_file_that_is_not_an_oauth_client(oauth_client, isolated):
    response = _upload(oauth_client, b'{"type": "service_account"}')
    assert response.status_code == 302
    assert not (isolated / "credentials.json").exists()


def test_full_connect_flow(oauth_client, isolated, monkeypatch):
    assert _upload(oauth_client, json.dumps(CLIENT_JSON).encode()).status_code == 302
    assert (isolated / "credentials.json").exists()

    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    # Opened as "localhost": bounced to 127.0.0.1 first so the cookie and the
    # redirect URI registered on Google's side both match.
    first = oauth_client.get("/oauth/start", base_url="http://localhost:5050")
    assert first.headers["Location"] == "http://127.0.0.1:5050/oauth/start"

    start = oauth_client.get("/oauth/start", base_url="http://127.0.0.1:5050")
    query = parse_qs(urlparse(start.headers["Location"]).query)
    assert query["redirect_uri"] == ["http://127.0.0.1:5050/oauth/callback"]
    assert query["login_hint"] == ["me@gmail.com"]
    assert query["code_challenge_method"] == ["S256"]
    assert "include_granted_scopes" not in query

    _fake_token(monkeypatch, [SEND, READ, "openid"])
    oauth_client.get(f"/oauth/callback?state={query['state'][0]}&code=the-code",
                     base_url="http://127.0.0.1:5050")

    token = json.loads((isolated / "token.json").read_text())
    assert token["email"] == "me@gmail.com"
    assert 'GMAIL_ADDRESS="me@gmail.com"' in (isolated / ".env").read_text()


def test_missing_send_permission_is_refused(oauth_client, isolated, monkeypatch):
    _upload(oauth_client, json.dumps(CLIENT_JSON).encode())
    start = oauth_client.get("/oauth/start", base_url="http://127.0.0.1:5050")
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]

    _fake_token(monkeypatch, [READ, "openid"])
    page = oauth_client.get(f"/oauth/callback?state={state}&code=the-code",
                            base_url="http://127.0.0.1:5050", follow_redirects=True)
    assert not (isolated / "token.json").exists()
    assert "left unticked" in page.data.decode()


def test_state_mismatch_is_rejected(oauth_client, isolated):
    _upload(oauth_client, json.dumps(CLIENT_JSON).encode())
    oauth_client.get("/oauth/start", base_url="http://127.0.0.1:5050")
    oauth_client.get("/oauth/callback?state=forged&code=x", base_url="http://127.0.0.1:5050")
    assert not (isolated / "token.json").exists()


def test_disconnect_button_is_not_nested_in_the_settings_form(oauth_client):
    """A <form> inside the settings <form> is dropped by browsers, so the
    Disconnect button used to submit the whole settings form instead."""
    html = oauth_client.get("/settings").data.decode()
    setup_form = html[html.index('class="settings-main"'):]
    setup_form = setup_form[:setup_form.index("</form>")]
    assert "<form" not in setup_form
    assert 'id="oauth-client-form"' in html


def test_refreshing_the_token_keeps_the_stored_email(isolated, monkeypatch):
    import google_auth_helper
    monkeypatch.setattr(google_auth_helper, "TOKEN_PATH", isolated / "token.json")
    (isolated / "token.json").write_text(json.dumps({"token": "old", "email": "me@gmail.com"}))

    class Creds:
        def to_json(self):
            return json.dumps({"token": "new"})

    google_auth_helper._save_credentials(Creds())
    assert json.loads((isolated / "token.json").read_text()) == {"token": "new", "email": "me@gmail.com"}
