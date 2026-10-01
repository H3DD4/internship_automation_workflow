"""Sign-up, sign-in, sessions and passwords."""

import accounts

PASSWORD = "correct-horse-battery-9"


def _form(client, path, **fields):
    """POST a pre-login form the way a browser does: same origin + the
    anonymous CSRF token from the page."""
    client.get(path)
    with client.session_transaction() as session:
        token = session.get("anon_csrf")
    return client.post(path, data={"csrf_token": token, **fields}, headers={"Origin": "http://localhost"})


def test_passwords_are_hashed_with_argon2(user_id):
    stored = accounts.get_user(user_id)["password_hash"]
    assert stored.startswith("$argon2id$") and PASSWORD not in stored


def test_weak_passwords_are_refused(isolated):
    for weak in ("short", "password123", "aaaaaaaaaaaa", "maria.lopez-2027" if False else "1234567890"):
        assert accounts.password_problem(weak) is not None
    assert accounts.password_problem("correct horse battery staple") is None
    assert accounts.password_problem("jean.dupont99!", "jean.dupont@x.fr") is not None


def test_sign_in_and_out(anon_client, make_user):
    make_user("me@example.com")
    response = _form(anon_client, "/login", email="Me@Example.com", password=PASSWORD)
    assert response.status_code == 302
    assert anon_client.get("/settings").status_code == 200
    csrf = anon_client.get("/settings").data.decode().split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    anon_client.post("/logout", data={"csrf_token": csrf}, headers={"Origin": "http://localhost"})
    assert anon_client.get("/settings").status_code == 302


def test_a_wrong_password_and_an_unknown_account_look_the_same(anon_client, make_user):
    make_user("me@example.com")
    wrong = _form(anon_client, "/login", email="me@example.com", password="nope-nope-nope")
    unknown = _form(anon_client, "/login", email="ghost@example.com", password="nope-nope-nope")
    assert wrong.status_code == unknown.status_code == 401
    assert accounts.GENERIC_LOGIN_ERROR in wrong.data.decode()
    assert accounts.GENERIC_LOGIN_ERROR in unknown.data.decode()


def test_login_form_needs_its_csrf_token(anon_client, make_user):
    make_user("me@example.com")
    response = anon_client.post("/login", data={"email": "me@example.com", "password": PASSWORD},
                                headers={"Origin": "http://localhost"})
    assert response.status_code == 403


def test_repeated_failures_lock_the_account(make_user):
    uid = make_user("me@example.com")
    for _ in range(accounts.LOCK_AFTER_FAILURES):
        assert not accounts.authenticate("me@example.com", "wrong-password!").ok
    # Even the right password is refused while locked.
    result = accounts.authenticate("me@example.com", PASSWORD)
    assert not result.ok and "Too many" in result.reason
    assert accounts.get_user(uid)["locked_until"]


def test_the_session_cookie_is_http_only_and_same_site(anon_client, make_user):
    make_user("me@example.com")
    response = _form(anon_client, "/login", email="me@example.com", password=PASSWORD)
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_only_a_hash_of_the_session_token_is_stored(anon_client, make_user):
    import database
    from sqlalchemy import select
    make_user("me@example.com")
    response = _form(anon_client, "/login", email="me@example.com", password=PASSWORD)
    token = response.headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
    with database.read() as conn:
        ids = [r.id for r in conn.execute(select(database.auth_sessions.c.id))]
    assert token not in ids and len(ids) == 1


def test_next_cannot_redirect_off_site(anon_client, make_user):
    make_user("me@example.com")
    anon_client.get("/login?next=https://evil.example")
    with anon_client.session_transaction() as session:
        token = session["anon_csrf"]
    response = anon_client.post("/login?next=//evil.example/x",
                                data={"csrf_token": token, "email": "me@example.com", "password": PASSWORD},
                                headers={"Origin": "http://localhost"})
    assert response.headers["Location"] in ("/", "http://localhost/")


def test_registration_waits_for_approval_by_default(anon_client):
    response = _form(anon_client, "/register", email="new@example.com", full_name="New Person",
                     password=PASSWORD, password_confirm=PASSWORD)
    assert "administrator approves" in response.data.decode()
    user = accounts.get_user_by_email("new@example.com")
    assert user["status"] == "pending" and user["role"] == "user"
    assert not accounts.authenticate("new@example.com", PASSWORD).ok


def test_open_registration_activates_immediately(anon_client):
    accounts.set_system_setting("signup_mode", "open")
    _form(anon_client, "/register", email="new@example.com", full_name="New Person",
          password=PASSWORD, password_confirm=PASSWORD)
    assert accounts.authenticate("new@example.com", PASSWORD).ok


def test_closed_registration_creates_nothing(anon_client):
    accounts.set_system_setting("signup_mode", "closed")
    _form(anon_client, "/register", email="new@example.com", full_name="New",
          password=PASSWORD, password_confirm=PASSWORD)
    assert accounts.get_user_by_email("new@example.com") is None


def test_registering_an_existing_address_reveals_nothing(anon_client, make_user):
    make_user("me@example.com")
    page = _form(anon_client, "/register", email="me@example.com", full_name="Me",
                 password=PASSWORD, password_confirm=PASSWORD).data.decode()
    assert "already" not in page.lower()


def test_changing_the_password_ends_other_sessions(client, user_id):
    other_token, _ = accounts.create_session(user_id)
    response = client.post("/account/password", headers={"Origin": "http://localhost"},
                           data={"csrf_token": client.csrf, "current_password": PASSWORD,
                                 "new_password": "a-brand-new-passphrase", "new_password_confirm": "a-brand-new-passphrase"})
    assert response.status_code == 302
    assert accounts.load_session(other_token) is None          # other device signed out
    assert client.get("/settings").status_code == 200                  # this one kept
    assert accounts.authenticate("user@example.com", "a-brand-new-passphrase").ok


def test_a_temporary_password_must_be_replaced_first(login, make_user):
    uid = make_user("temp@example.com")
    accounts.update_user(uid, must_change_password=1)
    c = login(uid)
    response = c.get("/")
    assert response.status_code == 302 and "/account/password" in response.headers["Location"]
    assert c.get("/api/overview").status_code == 403


def test_a_suspended_account_loses_its_session_at_once(client, user_id):
    accounts.update_user(user_id, status="suspended")
    assert client.get("/settings").status_code == 302


def test_expired_sessions_are_rejected(client, user_id):
    import database
    from sqlalchemy import update
    with database.tx() as conn:
        conn.execute(update(database.auth_sessions).values(expires_at="2000-01-01T00:00:00+00:00"))
    assert client.get("/settings").status_code == 302
