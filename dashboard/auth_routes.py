"""Sign in, sign up, sign out, password change."""

from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import accounts
from dashboard import security

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("index"))
    email = ""
    if request.method == "POST":
        email = accounts.normalize_email(request.form.get("email", ""))[:320]
        password = request.form.get("password", "")[:accounts.MAX_PASSWORD_LENGTH + 1]
        ip = security.client_ip()
        # Per IP and per account: slows both spraying many accounts from one
        # address and hammering one account from many.
        if security.rate_limited(f"login-ip:{ip}", 20, 600) or \
                security.rate_limited(f"login-acct:{email}", 10, 600):
            flash("Too many sign-in attempts. Wait a few minutes and try again.", "error")
            return render_template("auth/login.html", email=email), 429
        result = accounts.authenticate(email, password)
        if not result.ok:
            accounts.audit("login_failed", detail={"email": email}, ip=ip)
            flash(result.reason, "error")
            return render_template("auth/login.html", email=email), 401
        token, _ = accounts.create_session(result.user["id"], ip, request.headers.get("User-Agent", ""))
        accounts.audit("login", actor=result.user["id"], ip=ip)
        response = redirect(security.safe_next(request.args.get("next")))
        security.set_session_cookie(response, token)
        return response
    return render_template("auth/login.html", email=email)


@bp.route("/register", methods=["GET", "POST"])
def register():
    mode = accounts.signup_mode()
    if g.get("user"):
        return redirect(url_for("index"))
    if mode == "closed":
        return render_template("auth/register.html", closed=True, form={})
    form = {"email": "", "full_name": ""}
    if request.method == "POST":
        form = {"email": accounts.normalize_email(request.form.get("email", ""))[:320],
                "full_name": (request.form.get("full_name") or "").strip()[:120]}
        password = request.form.get("password", "")
        if security.rate_limited(f"register-ip:{security.client_ip()}", 5, 3600):
            flash("Too many sign-ups from this network. Try again later.", "error")
            return render_template("auth/register.html", form=form), 429
        if password != request.form.get("password_confirm", ""):
            flash("The two passwords don't match.", "error")
            return render_template("auth/register.html", form=form), 400
        if not form["full_name"]:
            flash("Enter your name.", "error")
            return render_template("auth/register.html", form=form), 400
        status = "active" if mode == "open" else "pending"
        try:
            user_id = accounts.create_user(form["email"], password, full_name=form["full_name"],
                                           status=status)
        except accounts.AccountError as exc:
            # An existing address gets the same outcome page as a new one, so
            # sign-up can't be used to discover who has an account.
            if "already exists" in str(exc):
                accounts.audit("register_duplicate", detail={"email": form["email"]},
                               ip=security.client_ip())
                return render_template("auth/register_done.html", pending=(mode != "open"))
            flash(str(exc), "error")
            return render_template("auth/register.html", form=form), 400
        accounts.audit("register", target=user_id, ip=security.client_ip(),
                       detail={"status": status})
        return render_template("auth/register_done.html", pending=(status == "pending"))
    return render_template("auth/register.html", form=form)


@bp.post("/logout")
def logout():
    if g.get("auth_session"):
        accounts.end_session(g.auth_session["id"])
        accounts.audit("logout", actor=g.user["id"], ip=security.client_ip())
    response = redirect(url_for("auth.login"))
    security.clear_session_cookie(response)
    return response


@bp.route("/account/password", methods=["GET", "POST"])
@security.login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        if new != request.form.get("new_password_confirm", ""):
            flash("The two new passwords don't match.", "error")
            return render_template("auth/change_password.html"), 400
        check = accounts.authenticate(g.user["email"], current)
        if not check.ok:
            flash("Your current password is incorrect.", "error")
            return render_template("auth/change_password.html"), 400
        if new == current:
            flash("Choose a password different from the current one.", "error")
            return render_template("auth/change_password.html"), 400
        try:
            accounts.set_password(g.user["id"], new, keep_session=g.auth_session["id"])
        except accounts.AccountError as exc:
            flash(str(exc), "error")
            return render_template("auth/change_password.html"), 400
        accounts.audit("password_changed", actor=g.user["id"], ip=security.client_ip())
        flash("Password changed. Every other device was signed out.", "success")
        return redirect(url_for("index"))
    return render_template("auth/change_password.html")


@bp.post("/account/sessions/end-others")
@security.login_required
def end_other_sessions():
    from sqlalchemy import delete
    import database
    with database.tx() as conn:
        conn.execute(delete(database.auth_sessions).where(
            database.auth_sessions.c.user_id == g.user["id"],
            database.auth_sessions.c.id != g.auth_session["id"]))
    flash("Signed out on every other device.", "success")
    return redirect(url_for("settings_page") + "#s-account")
