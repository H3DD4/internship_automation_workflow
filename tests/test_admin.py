"""Administration: managing accounts, with safety rails and an audit trail."""

import accounts
import db


def _post(c, path, **fields):
    return c.post(path, data={"csrf_token": c.csrf, **fields}, headers={"Origin": "http://localhost"})


def test_the_admin_area_is_invisible_to_regular_users(client):
    assert client.get("/admin/").status_code == 404
    assert client.get("/admin/platform").status_code == 404
    assert client.get("/admin/audit").status_code == 404
    assert "/admin/" not in client.get("/").data.decode()


def test_admin_sees_every_account_with_usage_counts(admin_client, make_user, make_app):
    uid = make_user("student@example.com", name="Student One")
    make_app(owner=uid, company="X", email="x@x.com", status="sent")
    page = admin_client.get("/admin/").data.decode()
    assert "student@example.com" in page and "Student One" in page
    assert "1 sent" in page


def test_admin_never_sees_a_users_secrets_or_drafts(admin_client, make_user, make_app):
    from user_config import UserConfig
    uid = make_user("student@example.com")
    UserConfig(uid).set_secret("GROQ_API_KEY", "student-secret-key")
    make_app(owner=uid, company="X", email="x@x.com", body="Private draft body")
    page = admin_client.get("/admin/").data.decode()
    assert "student-secret-key" not in page and "Private draft body" not in page


def test_approving_a_pending_account(admin_client, make_user):
    uid = make_user("waiting@example.com", status="pending")
    assert "Accounts waiting" in admin_client.get("/admin/").data.decode() or \
        "waiting for your approval" in admin_client.get("/admin/").data.decode()
    _post(admin_client, f"/admin/users/{uid}/approve")
    assert accounts.get_user(uid)["status"] == "active"


def test_creating_a_user_shows_the_temporary_password_once(admin_client):
    response = _post(admin_client, "/admin/users", email="new@example.com", full_name="New", role="user")
    page = response.data.decode()
    user = accounts.get_user_by_email("new@example.com")
    assert user["must_change_password"] == 1 and user["status"] == "active"
    password = page.split('credential-box__secret">', 1)[1].split("<", 1)[0]
    assert accounts.authenticate("new@example.com", password).ok
    assert password not in user["password_hash"]


def test_suspending_signs_the_user_out_everywhere(admin_client, make_user, login):
    uid = make_user("student@example.com")
    student = login(uid)
    assert student.get("/").status_code == 200
    _post(admin_client, f"/admin/users/{uid}/suspend")
    assert accounts.get_user(uid)["status"] == "suspended"
    assert student.get("/").status_code == 302
    _post(admin_client, f"/admin/users/{uid}/activate")
    assert accounts.get_user(uid)["status"] == "active"


def test_an_admin_cannot_lock_themself_out(admin_client):
    me = admin_client.user_id
    _post(admin_client, f"/admin/users/{me}/suspend")
    _post(admin_client, f"/admin/users/{me}/role", role="user")
    _post(admin_client, f"/admin/users/{me}/delete", confirm_email="admin@example.com")
    user = accounts.get_user(me)
    assert user and user["role"] == "admin" and user["status"] == "active"


def test_there_is_no_way_to_promote_someone_to_admin(admin_client, make_user):
    uid = make_user("student@example.com")
    assert _post(admin_client, f"/admin/users/{uid}/role", role="admin").status_code == 404
    assert "Make administrator" not in admin_client.get("/admin/").data.decode()
    assert accounts.get_user(uid)["role"] == "user"


def test_accounts_created_by_the_admin_are_regular_users(admin_client):
    _post(admin_client, "/admin/users", email="new@example.com", full_name="New", role="admin")
    assert accounts.get_user_by_email("new@example.com")["role"] == "user"


def test_resetting_a_password_forces_a_change(admin_client, make_user):
    uid = make_user("student@example.com")
    page = _post(admin_client, f"/admin/users/{uid}/reset-password").data.decode()
    password = page.split('credential-box__secret">', 1)[1].split("<", 1)[0]
    assert accounts.authenticate("student@example.com", password).ok
    assert accounts.get_user(uid)["must_change_password"] == 1


def test_deleting_needs_the_email_typed_and_removes_everything(admin_client, make_user, make_app):
    from user_config import UserConfig
    uid = make_user("student@example.com")
    app_id = make_app(owner=uid, company="X", email="x@x.com")
    db.for_user(uid).log_event(app_id, "research", "hello")
    UserConfig(uid).set_secret("GROQ_API_KEY", "k")
    db.for_user(uid).add_companies([{"email": "a@a.com", "company_name": "A", "website": "", "contact_name": ""}])

    _post(admin_client, f"/admin/users/{uid}/delete", confirm_email="wrong@example.com")
    assert accounts.get_user(uid) is not None

    _post(admin_client, f"/admin/users/{uid}/delete", confirm_email="student@example.com")
    assert accounts.get_user(uid) is None
    import database
    from sqlalchemy import func, select
    with database.read() as conn:
        for table in (database.applications, database.events, database.user_secrets, database.company_list):
            assert conn.execute(select(func.count()).select_from(table)).scalar_one() == 0, table.name


def test_signup_mode_is_set_by_the_admin(admin_client):
    _post(admin_client, "/admin/platform", action="signup", signup_mode="closed")
    assert accounts.signup_mode() == "closed"


def test_admin_actions_are_audited(admin_client, make_user):
    uid = make_user("student@example.com")
    _post(admin_client, f"/admin/users/{uid}/suspend")
    actions = [e["action"] for e in accounts.recent_audit()]
    assert "admin_suspend" in actions
    assert "admin_suspend" in admin_client.get("/admin/audit").data.decode()
