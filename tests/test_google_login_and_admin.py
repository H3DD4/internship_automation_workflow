"""The administrator (username + password from the environment) and
"Continue with Google" for everyone else."""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

import accounts
from user_config import UserConfig

ADMIN_USER, ADMIN_PASS = "Admin$$$", "Admin$$$$$$"
LOCAL = "http://127.0.0.1:5050"
CLIENT = {"web": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "shh",
                  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                  "token_uri": "https://oauth2.googleapis.com/token"}}
SEND = "https://www.googleapis.com/auth/gmail.send"


# ---------------------------------------------------------------------------
# The administrator
# ---------------------------------------------------------------------------

def test_the_admin_is_created_from_the_environment(isolated):
    uid = accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    user = accounts.get_user(uid)
    assert user["username"] == ADMIN_USER and user["role"] == "admin" and user["status"] == "active"
    assert user["email"].endswith("@admin.invalid")
    assert accounts.authenticate(ADMIN_USER, ADMIN_PASS).ok


def test_usernames_are_case_sensitive(isolated):
    accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    assert not accounts.authenticate("admin$$$", ADMIN_PASS).ok


def test_changing_the_environment_password_takes_effect(isolated):
    uid = accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    token, _ = accounts.create_session(uid)
    accounts.ensure_admin(ADMIN_USER, "a-completely-new-admin-password")
    assert accounts.authenticate(ADMIN_USER, "a-completely-new-admin-password").ok
    assert not accounts.authenticate(ADMIN_USER, ADMIN_PASS).ok
    assert accounts.load_session(token) is None             # old sessions ended


def test_ensure_admin_repairs_a_demoted_or_suspended_admin(isolated):
    uid = accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    accounts.update_user(uid, role="user", status="suspended")
    accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    user = accounts.get_user(uid)
    assert user["role"] == "admin" and user["status"] == "active"


def _signin(client, identifier, password):
    client.get("/login")
    with client.session_transaction() as session:
        token = session["anon_csrf"]
    return client.post("/login", data={"csrf_token": token, "email": identifier, "password": password},
                       headers={"Origin": "http://localhost"})


def test_the_admin_signs_in_with_the_username_and_lands_on_the_admin_panel(anon_client):
    accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    response = _signin(anon_client, ADMIN_USER, ADMIN_PASS)
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/")
    assert anon_client.get("/admin/").status_code == 200


def test_a_regular_user_never_reaches_the_admin_panel(client):
    assert client.get("/admin/").status_code == 404


def test_the_admin_is_seeded_on_first_request(app_module, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(app_module, "_started", False)
    app_module.app.test_client().get("/login")
    assert accounts.get_user_by_username(ADMIN_USER)["role"] == "admin"


# ---------------------------------------------------------------------------
# Continue with Google
# ---------------------------------------------------------------------------

@pytest.fixture
def google(isolated, monkeypatch):
    """Google configured, and Google's answers faked: the token exchange and
    the verification of the signed ID token."""
    from user_config import set_system_secret
    import google_auth_helper
    import dashboard.app as app_module
    set_system_secret(google_auth_helper.CLIENT_SECRET_NAME, json.dumps(CLIENT))
    monkeypatch.setenv("PUBLIC_BASE_URL", LOCAL)
    identity = {"email": "student@gmail.com", "email_verified": True, "name": "Student One"}
    state = {"scopes": [SEND, "openid"], "refresh": "rt"}

    from google_auth_oauthlib.flow import Flow

    def fetch_token(self, **kwargs):
        self.oauth2session.token = {"access_token": "at", "refresh_token": state["refresh"],
                                    "token_type": "Bearer", "expires_in": 3600,
                                    "expires_at": time.time() + 3600, "id_token": "signed.jwt.value",
                                    "scope": state["scopes"]}
        return self.oauth2session.token
    monkeypatch.setattr(Flow, "fetch_token", fetch_token)

    def verify(creds):
        if not identity.get("email_verified"):
            raise ValueError("unverified")
        return dict(identity)
    monkeypatch.setattr(app_module, "verify_google_identity", verify)
    return {"identity": identity, "state": state}


def _google_round_trip(test_client):
    start = test_client.get("/auth/google", base_url=LOCAL)
    assert start.status_code == 302, start.data
    query = parse_qs(urlparse(start.headers["Location"]).query)
    assert "openid" in query["scope"][0] and SEND in query["scope"][0]
    return test_client.get(f"/oauth/callback?state={query['state'][0]}&code=c", base_url=LOCAL)


def test_the_sign_in_page_offers_google(google, anon_client):
    page = anon_client.get("/login").data.decode()
    assert "Continue with Google" in page and "/auth/google" in page
    assert "Sign up with Google" in anon_client.get("/register").data.decode()


def test_no_google_button_when_google_is_not_set_up(anon_client):
    assert "Continue with Google" not in anon_client.get("/login").data.decode()


def test_an_existing_user_signs_in_with_google_and_sending_is_connected(google, app_module, make_user):
    uid = make_user("student@gmail.com")
    accounts.update_user(uid, must_change_password=1)          # the old temporary-password case
    c = app_module.app.test_client()
    response = _google_round_trip(c)
    assert response.status_code == 302 and response.headers["Location"] in ("/", f"{LOCAL}/")
    assert c.get("/settings", base_url=LOCAL).status_code == 200      # signed in, no password page
    assert accounts.get_user(uid)["must_change_password"] == 0
    cfg = UserConfig(uid)
    assert cfg.get("MAIL_METHOD") == "oauth" and cfg.get("GMAIL_ADDRESS") == "student@gmail.com"
    assert json.loads(cfg.secret("GOOGLE_TOKEN"))["refresh_token"] == "rt"


def test_a_new_person_signing_up_with_google_lands_inside_the_app(google, app_module):
    # Approval mode (the default here): Google has verified the address, so
    # the account is created, signed in and connected in one step.
    c = app_module.app.test_client()
    response = _google_round_trip(c)
    assert response.status_code == 302 and response.headers["Location"] in ("/", f"{LOCAL}/")
    user = accounts.get_user_by_email("student@gmail.com")
    assert user["status"] == "active" and user["role"] == "user" and user["full_name"] == "Student One"
    home = c.get("/", base_url=LOCAL).data.decode()
    assert "Welcome to Ntern" in home and "Getting started" in home
    assert UserConfig(user["id"]).get("GMAIL_ADDRESS") == "student@gmail.com"


def test_open_sign_up_with_google_signs_in_at_once(google, app_module):
    accounts.set_system_setting("signup_mode", "open")
    c = app_module.app.test_client()
    assert _google_round_trip(c).status_code == 302
    assert c.get("/settings", base_url=LOCAL).status_code == 200


def test_closed_sign_up_creates_nothing(google, app_module):
    accounts.set_system_setting("signup_mode", "closed")
    _google_round_trip(app_module.app.test_client())
    assert accounts.get_user_by_email("student@gmail.com") is None


def test_the_admin_cannot_sign_in_with_google(google, app_module):
    uid = accounts.ensure_admin(ADMIN_USER, ADMIN_PASS)
    google["identity"]["email"] = accounts.get_user(uid)["email"]
    c = app_module.app.test_client()
    _google_round_trip(c)
    assert c.get("/admin/", base_url=LOCAL).status_code == 302   # not signed in


def test_an_unverified_google_email_is_refused(google, app_module, make_user):
    make_user("student@gmail.com")
    google["identity"]["email_verified"] = False
    c = app_module.app.test_client()
    _google_round_trip(c)
    assert c.get("/settings", base_url=LOCAL).status_code == 302


def test_a_suspended_user_cannot_sign_in_with_google(google, app_module, make_user):
    uid = make_user("student@gmail.com")
    accounts.update_user(uid, status="suspended")
    c = app_module.app.test_client()
    _google_round_trip(c)
    assert c.get("/settings", base_url=LOCAL).status_code == 302


def test_a_returning_sign_in_keeps_the_stored_refresh_token(google, app_module, make_user):
    uid = make_user("student@gmail.com")
    _google_round_trip(app_module.app.test_client())
    google["state"]["refresh"] = None                             # Google omits it on repeat sign-ins
    _google_round_trip(app_module.app.test_client())
    assert json.loads(UserConfig(uid).secret("GOOGLE_TOKEN"))["refresh_token"] == "rt"


def test_signing_in_without_the_send_permission_still_signs_in(google, app_module, make_user):
    uid = make_user("student@gmail.com")
    google["state"]["scopes"] = ["openid"]
    c = app_module.app.test_client()
    _google_round_trip(c)
    assert c.get("/settings", base_url=LOCAL).status_code == 200
    assert UserConfig(uid).secret("GOOGLE_TOKEN") == ""


def test_a_forged_callback_is_refused(google, app_module, make_user):
    make_user("student@gmail.com")
    c = app_module.app.test_client()
    c.get("/auth/google", base_url=LOCAL)
    c.get("/oauth/callback?state=forged&code=c", base_url=LOCAL)
    assert c.get("/settings", base_url=LOCAL).status_code == 302


def test_google_sign_in_starts_on_the_public_address(google, app_module):
    response = app_module.app.test_client().get("/auth/google", base_url="http://localhost:5050")
    assert response.headers["Location"].startswith(f"{LOCAL}/auth/google")


def test_auto_accept_lets_new_and_waiting_accounts_in(admin_client, anon_client, make_user):
    import accounts
    waiting = make_user("wait@example.com", status="pending")
    page = admin_client.get("/admin/").data.decode()
    assert "Auto-accept new accounts" in page
    reply = admin_client.post("/admin/auto-accept", headers=admin_client.origin,
                              data={"csrf_token": admin_client.csrf, "auto_accept": "on"})
    assert reply.status_code == 302
    assert accounts.signup_mode() == "open"
    assert accounts.get_user(waiting)["status"] == "active"
    admin_client.post("/admin/auto-accept", headers=admin_client.origin,
                      data={"csrf_token": admin_client.csrf, "auto_accept": "off"})
    assert accounts.signup_mode() == "approval"


def test_only_an_admin_can_flip_auto_accept(client):
    import accounts
    client.post("/admin/auto-accept", headers=client.origin, data={"csrf_token": client.csrf, "auto_accept": "on"})
    assert accounts.signup_mode() != "open"


def test_the_admin_sets_the_daily_limit_for_everyone(admin_client, make_user):
    import accounts
    from user_config import UserConfig
    uid = make_user("sender@example.com")
    UserConfig(uid).set_many({"MAX_EMAILS_PER_DAY": "80"})
    admin_client.post("/admin/platform", headers=admin_client.origin,
                      data={"csrf_token": admin_client.csrf, "action": "daily_limit", "daily_limit": "25"})
    assert accounts.platform_daily_limit() == 25
    assert UserConfig(uid).int_setting("MAX_EMAILS_PER_DAY", 20) == 25          # held under the limit
    UserConfig(uid).set_many({"MAX_EMAILS_PER_DAY": "10"})
    assert UserConfig(uid).int_setting("MAX_EMAILS_PER_DAY", 20) == 10          # a lower own choice stays
    admin_client.post("/admin/platform", headers=admin_client.origin,
                      data={"csrf_token": admin_client.csrf, "action": "daily_limit", "daily_limit": "40",
                            "apply_all": "on"})
    assert UserConfig(uid).int_setting("MAX_EMAILS_PER_DAY", 20) == 40          # set for everyone


def test_the_admin_can_give_one_account_its_own_limit(admin_client, make_user):
    import accounts
    from user_config import UserConfig
    uid = make_user("trusted@example.com")
    admin_client.post(f"/admin/users/{uid}/daily-limit", headers=admin_client.origin,
                      data={"csrf_token": admin_client.csrf, "daily_limit": "150"})
    assert accounts.daily_limit_for(uid) == 150 and UserConfig(uid).int_setting("MAX_EMAILS_PER_DAY", 20) == 150
    admin_client.post(f"/admin/users/{uid}/daily-limit", headers=admin_client.origin,
                      data={"csrf_token": admin_client.csrf, "daily_limit": ""})
    assert accounts.daily_limit_for(uid) == accounts.platform_daily_limit()


def test_users_cannot_change_the_daily_limit(client):
    import accounts
    before = accounts.platform_daily_limit()
    client.post("/admin/platform", headers=client.origin,
                data={"csrf_token": client.csrf, "action": "daily_limit", "daily_limit": "499"})
    assert accounts.platform_daily_limit() == before
