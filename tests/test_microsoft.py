"""Microsoft accounts: Outlook, Hotmail and university Microsoft 365 — sign
in, connect, send through Graph (small and large CVs), read bounces — and the
address check that tells a student which way to connect. Microsoft itself is
faked: no network."""

import base64
import json
import time

import pytest

import accounts
import mail_providers
import microsoft_auth
from user_config import UserConfig

LOCAL = "http://127.0.0.1:5050"


def _id_token(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload = status, payload
        self.content = content or (json.dumps(payload).encode() if payload is not None else b"")
        self.text = self.content.decode("utf-8", "ignore")

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.content or b"{}")


@pytest.fixture
def ms(monkeypatch, isolated):
    """A fake Microsoft: token endpoint, Graph /me, messages, attachments."""
    microsoft_auth.save_client("11111111-2222-3333-4444-555555555555", "client-secret-value")
    state = {"upn": "jdoe@etu.univ.example", "mail": "jane.doe@univ.example", "scope":
             "openid email profile offline_access User.Read Mail.Send Mail.Read",
             "posts": [], "puts": [], "inbox": []}

    def post(url, **kw):
        state["posts"].append((url, kw))
        if url.endswith("/token"):
            return FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                                      "scope": state["scope"],
                                      "id_token": _id_token(oid="oid-1", tid="tid-1")})
        if url.endswith("/me/messages"):
            return FakeResponse(201, {"id": "msg-1", "internetMessageId": "<abc@univ.example>"})
        if url.endswith("/attachments"):
            return FakeResponse(201, {"id": "att"})
        if url.endswith("/createUploadSession"):
            return FakeResponse(201, {"uploadUrl": "https://upload.example/session"})
        if url.endswith("/send"):
            return FakeResponse(202, None, b"")
        raise AssertionError(url)

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/me"):
            return FakeResponse(200, {"id": "oid-1", "displayName": "Jane Doe", "mail": state["mail"],
                                      "userPrincipalName": state["upn"]})
        if url.endswith("/mailFolders/inbox/messages"):
            return FakeResponse(200, {"value": [{"id": i, "subject": s, "from": {"emailAddress": {"address": f}}}
                                                for i, s, f, _ in state["inbox"]]})
        if url.endswith("/$value"):
            mid = url.split("/messages/")[1].split("/")[0]
            return FakeResponse(200, None, next(raw for i, _, _, raw in state["inbox"] if i == mid))
        raise AssertionError(url)

    def put(url, data=None, headers=None, timeout=None):
        state["puts"].append(headers["Content-Range"])
        return FakeResponse(200, {})

    monkeypatch.setattr(microsoft_auth.requests, "post", post)
    monkeypatch.setattr(microsoft_auth.requests, "get", get)
    monkeypatch.setattr(microsoft_auth.requests, "put", put)
    return state


def _round_trip(client, start_path, base=LOCAL):
    from urllib.parse import parse_qs, urlparse
    start = client.get(start_path, base_url=base)
    assert start.status_code == 302, start.headers
    query = parse_qs(urlparse(start.headers["Location"]).query)
    assert "Mail.Send" in query["scope"][0] and query["code_challenge_method"][0] == "S256"
    return client.get(f"/oauth/microsoft/callback?state={query['state'][0]}&code=c", base_url=base)


@pytest.fixture(autouse=True)
def _public_base(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", LOCAL)


def test_continue_with_microsoft_creates_the_account_and_connects_the_mailbox(ms, app_module):
    c = app_module.app.test_client()
    response = _round_trip(c, "/auth/microsoft")
    assert response.status_code == 302
    user = accounts.get_user_by_email("jdoe@etu.univ.example")       # the verified sign-in name
    assert user and user["status"] == "active" and user["full_name"] == "Jane Doe"
    cfg = UserConfig(user["id"])
    assert cfg.get("MAIL_METHOD") == "microsoft"
    assert microsoft_auth.sender_address(cfg) == "jane.doe@univ.example"
    assert c.get("/settings", base_url=LOCAL).status_code == 200


def test_a_guest_identity_is_refused(ms, app_module):
    ms["upn"] = "jdoe_gmail.com#EXT#@tenant.onmicrosoft.com"
    _round_trip(app_module.app.test_client(), "/auth/microsoft")
    assert accounts.get_user_by_email("jdoe_gmail.com#ext#@tenant.onmicrosoft.com") is None


def test_the_mail_attribute_is_never_used_as_identity(ms, app_module, make_user):
    """A tenant admin can set "mail" to anyone's address; only the verified
    userPrincipalName identifies the account."""
    victim = make_user("victim@example.com")
    ms["mail"] = "victim@example.com"
    _round_trip(app_module.app.test_client(), "/auth/microsoft")
    assert not UserConfig(victim).has_secret("MICROSOFT_TOKEN")


def test_the_admin_cannot_sign_in_with_microsoft(ms, app_module):
    uid = accounts.ensure_admin("Admin$$$", "Admin$$$$$$-long-enough")
    ms["upn"] = accounts.get_user(uid)["email"]
    c = app_module.app.test_client()
    _round_trip(c, "/auth/microsoft")
    assert c.get("/admin/", base_url=LOCAL).status_code != 200


def test_a_signed_in_student_connects_their_university_mailbox(ms, app_module, login, make_user, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost")
    uid = make_user("student@example.com")
    c = login(uid)
    response = _round_trip(c, "/oauth/microsoft/start", base="http://localhost")
    assert response.status_code == 302 and "#s-gmail" in response.headers["Location"]
    assert UserConfig(uid).get("MAIL_METHOD") == "microsoft"


def test_a_university_that_blocks_user_consent_gets_a_clear_message(ms, app_module, login, make_user, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost")
    uid = make_user("student@example.com")
    c = login(uid)
    c.get("/oauth/microsoft/start")
    c.get("/oauth/microsoft/callback?error=access_denied&error_description=AADSTS65001%3A+admin+approval")
    page = c.get("/settings").data.decode()
    assert "IT administrators approve" in page and "adminconsent" in page


def test_a_forged_callback_is_refused(ms, app_module, login, make_user, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost")
    uid = make_user("student@example.com")
    c = login(uid)
    c.get("/oauth/microsoft/start")
    c.get("/oauth/microsoft/callback?state=forged&code=c")
    assert not UserConfig(uid).has_secret("MICROSOFT_TOKEN")


def _connected(make_user, ms):
    uid = make_user("student@example.com")
    cfg = UserConfig(uid)
    microsoft_auth.save_token(cfg, {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3000,
                                    "scope": ms["scope"]},
                              {"email": "jdoe@etu.univ.example", "sender": "jane.doe@univ.example", "oid": "o", "tid": "t"})
    cfg.set_many({"MAIL_METHOD": "microsoft"})
    return cfg


def test_sending_with_a_small_cv_attaches_it_directly(ms, make_user):
    cfg = _connected(make_user, ms)
    message_id = microsoft_auth.send(cfg, to_email="jobs@acme.com", subject="S", body="B",
                                     attachment={"filename": "cv.pdf", "content_type": "application/pdf",
                                                 "content": b"%PDF small"})
    assert message_id == "<abc@univ.example>"
    urls = [u for u, _ in ms["posts"]]
    assert any(u.endswith("/attachments") for u in urls) and urls[-1].endswith("/send")


def test_a_large_cv_goes_through_an_upload_session(ms, make_user):
    cfg = _connected(make_user, ms)
    big = b"x" * (4 * 1024 * 1024 + 10)
    microsoft_auth.send(cfg, to_email="jobs@acme.com", subject="S", body="B",
                        attachment={"filename": "cv.pdf", "content_type": "application/pdf", "content": big})
    assert len(ms["puts"]) == 2 and ms["puts"][-1].endswith(f"/{len(big)}")


def test_the_mail_service_sends_through_microsoft(ms, make_user, data, monkeypatch):
    import mail_service
    cfg = _connected(make_user, ms)
    monkeypatch.setattr(cfg, "cv", lambda: {"filename": "cv.pdf", "content_type": "application/pdf", "content": b"pdf"})
    result = mail_service.send(cfg, {"email": "jobs@acme.com", "subject": "S", "body": "B"})
    assert result.success and result.provider == "Outlook / Microsoft 365"


def test_an_expired_token_is_refreshed(ms, make_user):
    cfg = _connected(make_user, ms)
    stored = json.loads(cfg.secret("MICROSOFT_TOKEN"))
    stored["expires_at"] = time.time() - 10
    cfg.set_secret("MICROSOFT_TOKEN", json.dumps(stored))
    assert microsoft_auth.access_token(cfg)["expires_at"] > time.time()


def test_bounces_are_read_from_the_outlook_inbox(ms, make_user, monkeypatch):
    import bounce_checker
    import db
    cfg = _connected(make_user, ms)
    d = db.for_user(cfg.user_id)
    app_id = d.get_or_create_application("Acme", "jobs@acme.com", "")
    d.update_application(app_id, status="sent", subject="S", body="B")
    ndr = (b"From: Microsoft Outlook <MicrosoftExchange329e71ec88ae@univ.example>\r\n"
           b"Subject: Undeliverable: S\r\nX-Failed-Recipients: jobs@acme.com\r\n\r\n"
           b"Your message to jobs@acme.com couldn't be delivered.\r\n")
    ms["inbox"] = [("m1", "Undeliverable: S", "MicrosoftExchange329e71ec88ae@univ.example", ndr)]
    assert bounce_checker.credentials_available(cfg)
    assert bounce_checker.check_bounces(cfg, 3) == 1
    assert d.get_application_by_id(app_id)["status"] == "bounced"


@pytest.mark.parametrize("email, mx, txt, kind", [
    ("a@gmail.com", [], "", "google"),
    ("a@hotmail.fr", [], "", "microsoft"),
    ("a@epfl.ch", ["epfl-ch.mail.protection.outlook.com"], "", "microsoft"),
    ("a@uni.example", ["aspmx.l.google.com"], "", "google"),
    ("a@uni.example", ["mx1.proofpoint.example"], "v=spf1 include:spf.protection.outlook.com -all", "microsoft"),
    ("a@yahoo.fr", [], "", "smtp"),
    ("a@uni.example", ["mx.uni.example"], "v=spf1 mx -all", "smtp"),
])
def test_which_service_hosts_an_address(email, mx, txt, kind):
    result = mail_providers.detect(email, mx_lookup=lambda d: mx, txt_lookup=lambda d: txt)
    assert result["kind"] == kind


def test_the_school_server_gets_prefilled_smtp_settings():
    result = mail_providers.detect("a@uni.example", mx_lookup=lambda d: ["mx.uni.example"], txt_lookup=lambda d: "")
    assert result["smtp"]["host"] == "smtp.uni.example" and result["smtp"]["guess"]


def test_the_detect_api_says_what_is_available(client, ms, monkeypatch):
    monkeypatch.setattr(mail_providers, "_mx_hosts", lambda d: ["x.mail.protection.outlook.com"])
    data = client.get("/api/mail/detect?email=a@school.example").get_json()
    assert data["ok"] and data["kind"] == "microsoft" and data["available"]["microsoft"]


def test_the_login_page_offers_microsoft_when_enabled(ms, anon_client):
    page = anon_client.get("/login").data.decode()
    assert "/auth/microsoft" in page and "Continue with Microsoft" in page


def test_no_microsoft_button_when_not_set_up(anon_client):
    assert "/auth/microsoft" not in anon_client.get("/login").data.decode()


def test_the_admin_saves_the_microsoft_app_encrypted(admin_client):
    import database
    from sqlalchemy import select
    admin_client.post("/admin/platform", headers=admin_client.origin, data={
        "csrf_token": admin_client.csrf, "action": "microsoft_client",
        "ms_client_id": "11111111-2222-3333-4444-555555555555", "ms_client_secret": "super-secret-value-123"})
    assert microsoft_auth.is_configured()
    with database.read() as conn:
        stored = " ".join(r[0] for r in conn.execute(select(database.system_secrets.c.ciphertext)))
    assert "super-secret-value-123" not in stored
