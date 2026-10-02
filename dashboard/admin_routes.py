"""Administration: accounts, platform settings, audit trail.

There is one administrator: the account named by ADMIN_USERNAME /
ADMIN_PASSWORD in the environment, which signs in with its username. It
manages accounts, not their contents: it never sees another user's API
keys, mailbox credentials, drafts or CV — only counts. Every action is
written to the audit log, and the administrator can't suspend or delete
itself.
"""

from __future__ import annotations

import secrets
import string

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import case, func, select

import accounts
import database
from dashboard import security

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))
        if accounts.password_problem(candidate) is None:
            return candidate


def _usage_by_user() -> dict:
    a = database.applications
    with database.read() as conn:
        rows = conn.execute(select(
            a.c.user_id, func.count().label("total"),
            func.sum(case((a.c.status == "ready", 1), else_=0)).label("ready"),
            func.sum(case((a.c.status == "sent", 1), else_=0)).label("sent"),
            func.sum(case((a.c.status == "bounced", 1), else_=0)).label("bounced"),
        ).group_by(a.c.user_id)).all()
        listed = dict(conn.execute(select(database.company_list.c.user_id, func.count())
                                   .group_by(database.company_list.c.user_id)).all())
        active_runs = {r.user_id for r in conn.execute(select(database.prep_runs.c.user_id).where(
            database.prep_runs.c.status.in_(["requested", "running"])))}
    usage = {r.user_id: {"total": r.total, "ready": r.ready or 0, "sent": r.sent or 0,
                         "bounced": r.bounced or 0} for r in rows}
    for uid, count in listed.items():
        usage.setdefault(uid, {"total": 0, "ready": 0, "sent": 0, "bounced": 0})["listed"] = count
    for uid in active_runs:
        usage.setdefault(uid, {"total": 0, "ready": 0, "sent": 0, "bounced": 0})["running"] = True
    return usage


def _target(user_id: int) -> dict:
    user = accounts.get_user(user_id)
    if not user:
        abort(404)
    return user


def _guard_self(user: dict, action: str):
    if user["id"] == g.user["id"]:
        flash(f"You can't {action} your own account.", "error")
        return redirect(url_for("admin.users"))
    return None


def _guard_last_admin(user: dict):
    if user["role"] == "admin" and user["status"] == "active" and accounts.count_admins() <= 1:
        flash("That's the last active administrator — promote someone else first.", "error")
        return redirect(url_for("admin.users"))
    return None


def _audit(action: str, target: int | None = None, **detail):
    accounts.audit(action, actor=g.user["id"], target=target, detail=detail or None,
                   ip=security.client_ip())


@bp.get("/")
@security.admin_required
def users():
    all_users = accounts.list_users()
    status_filter = request.args.get("status", "")
    if status_filter in accounts.STATUSES:
        shown = [u for u in all_users if u["status"] == status_filter]
    else:
        shown = all_users
    return render_template("admin/users.html", users=shown, all_count=len(all_users),
                           pending_count=sum(1 for u in all_users if u["status"] == "pending"),
                           status_filter=status_filter, usage=_usage_by_user(),
                           signup_mode=accounts.signup_mode(), now_iso=accounts._now(),
                           sessions={u["id"]: accounts.count_sessions(u["id"]) for u in shown})


@bp.post("/users")
@security.admin_required
def create_user():
    email = accounts.normalize_email(request.form.get("email", ""))
    full_name = (request.form.get("full_name") or "").strip()
    role = "user"
    password = _temp_password()
    try:
        user_id = accounts.create_user(email, password, full_name=full_name, role=role,
                                       status="active", must_change_password=True,
                                       approved_by=g.user["id"])
    except accounts.AccountError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.users"))
    _audit("admin_create_user", user_id, email=email, role=role)
    # Shown once, on this page only — never stored in plain text, never in a
    # cookie. The user must replace it at first sign-in.
    return render_template("admin/credentials.html", user=accounts.get_user(user_id),
                           password=password, created=True)


@bp.post("/users/<int:user_id>/approve")
@security.admin_required
def approve(user_id):
    user = _target(user_id)
    accounts.update_user(user_id, status="active", approved_at=accounts._now(), approved_by=g.user["id"])
    _audit("admin_approve", user_id)
    flash(f"{user['email']} can now sign in.", "success")
    return redirect(url_for("admin.users", status=request.args.get("status", "")))


@bp.post("/users/<int:user_id>/suspend")
@security.admin_required
def suspend(user_id):
    user = _target(user_id)
    blocked = _guard_self(user, "suspend") or _guard_last_admin(user)
    if blocked:
        return blocked
    accounts.update_user(user_id, status="suspended")
    ended = accounts.end_all_sessions(user_id)
    _audit("admin_suspend", user_id, sessions_ended=ended)
    flash(f"{user['email']} is suspended and was signed out everywhere. Their data is kept.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/activate")
@security.admin_required
def activate(user_id):
    user = _target(user_id)
    accounts.update_user(user_id, status="active", failed_logins=0, locked_until=None)
    _audit("admin_activate", user_id)
    flash(f"{user['email']} is active again.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/reset-password")
@security.admin_required
def reset_password(user_id):
    user = _target(user_id)
    if user["id"] == g.user["id"]:
        flash("Change your own password from your account menu.", "error")
        return redirect(url_for("admin.users"))
    password = _temp_password()
    accounts.set_password(user_id, password, must_change=True)
    _audit("admin_reset_password", user_id)
    return render_template("admin/credentials.html", user=user, password=password, created=False)


@bp.post("/users/<int:user_id>/sign-out")
@security.admin_required
def sign_out(user_id):
    user = _target(user_id)
    ended = accounts.end_all_sessions(user_id)
    _audit("admin_sign_out", user_id, sessions_ended=ended)
    flash(f"Signed {user['email']} out of {ended} session(s).", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/unlock")
@security.admin_required
def unlock(user_id):
    user = _target(user_id)
    accounts.update_user(user_id, failed_logins=0, locked_until=None)
    _audit("admin_unlock", user_id)
    flash(f"{user['email']} can try signing in again.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/delete")
@security.admin_required
def delete(user_id):
    user = _target(user_id)
    blocked = _guard_self(user, "delete") or _guard_last_admin(user)
    if blocked:
        return blocked
    if accounts.normalize_email(request.form.get("confirm_email", "")) != user["email"]:
        flash("Type the account's email address exactly to confirm deletion.", "error")
        return redirect(url_for("admin.users"))
    accounts.delete_user(user_id)
    _audit("admin_delete_user", user_id, email=user["email"])
    flash(f"{user['email']} and all of their data were deleted.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/platform", methods=["GET", "POST"])
@security.admin_required
def platform():
    import google_auth_helper
    if request.method == "POST":
        action = request.form.get("action")
        if action == "signup":
            mode = request.form.get("signup_mode", "")
            if mode in accounts.SIGNUP_MODES:
                accounts.set_system_setting("signup_mode", mode)
                _audit("admin_signup_mode", mode=mode)
                flash("Sign-up setting saved.", "success")
        elif action == "google_scope":
            value = "on" if request.form.get("google_inbox_read") == "on" else "off"
            accounts.set_system_setting("google_inbox_read", value)
            _audit("admin_google_scope", inbox_read=value)
            flash("Google permissions saved. They apply to the next sign-in with Google.", "success")
        elif action == "google_client":
            upload = request.files.get("client_secret")
            raw = upload.read(64 * 1024) if upload and upload.filename else b""
            ok, detail = google_auth_helper.validate_client_secret(raw) if raw else (False, "Choose the JSON file first.")
            if ok:
                google_auth_helper.save_client_secret(raw)
                _audit("admin_google_client")
                flash("Google OAuth client saved (encrypted). Users can now sign in with Google.", "success")
            else:
                flash(detail, "error")
        elif action == "microsoft_client":
            import microsoft_auth
            try:
                microsoft_auth.save_client(request.form.get("ms_client_id", ""), request.form.get("ms_client_secret", ""))
                _audit("admin_microsoft_client")
                flash("Microsoft app saved (encrypted). Students can now use their Microsoft accounts.", "success")
            except microsoft_auth.MicrosoftError as exc:
                flash(str(exc), "error")
        elif action == "microsoft_client_remove":
            import microsoft_auth
            microsoft_auth.remove_client()
            _audit("admin_microsoft_client_removed")
            flash("Microsoft app removed.", "success")
        elif action == "microsoft_scope":
            value = "on" if request.form.get("microsoft_inbox_read") == "on" else "off"
            accounts.set_system_setting("microsoft_inbox_read", value)
            _audit("admin_microsoft_scope", inbox_read=value)
            flash("Microsoft permissions saved. They apply to the next connection.", "success")
        elif action == "google_client_remove":
            from user_config import set_system_secret
            set_system_secret(google_auth_helper.CLIENT_SECRET_NAME, "")
            _audit("admin_google_client_removed")
            flash("Google OAuth client removed.", "success")
        return redirect(url_for("admin.platform"))
    from dashboard.app import oauth_redirect_uri
    from dashboard.microsoft_routes import redirect_uri as ms_redirect_uri
    import microsoft_auth
    return render_template("admin/platform.html", signup_mode=accounts.signup_mode(),
                           ms_configured=microsoft_auth.is_configured(),
                           ms_inbox_read=microsoft_auth.inbox_read_enabled(),
                           ms_redirect_uri=ms_redirect_uri(),
                           google_configured=google_auth_helper.oauth_is_configured(),
                           google_inbox_read=google_auth_helper.inbox_read_scope_enabled(),
                           redirect_uri=oauth_redirect_uri())


@bp.get("/audit")
@security.admin_required
def audit():
    entries = accounts.recent_audit(300)
    emails = {u["id"]: u["email"] for u in accounts.list_users()}
    return render_template("admin/audit.html", entries=entries, emails=emails)
