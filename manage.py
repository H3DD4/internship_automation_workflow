"""Operator commands.

    python manage.py generate-keys                 fresh SECRET_KEY + ENCRYPTION_KEYS
    python manage.py init-db                       create every table
    python manage.py ensure-admin                  create/update the admin from ADMIN_USERNAME/ADMIN_PASSWORD
    python manage.py create-admin --email a@b.c    an extra administrator (password prompted)
    python manage.py set-role --email a@b.c --role user|admin
    python manage.py set-password --email a@b.c    reset anyone's password (prompted)
    python manage.py import-legacy --email a@b.c   move a single-user install into an account
    python manage.py verify-golden --email a@b.c --baseline golden.json
                                                   prove drafts are byte-identical to before
    python manage.py rotate-keys                   re-encrypt secrets under the first key
    python manage.py prepare --email a@b.c [--limit N]
                                                   run preparation in this terminal
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

import config


def _password(args, prompt: str = "Password") -> str | None:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(f"{prompt}: ")
    if first != getpass.getpass(f"{prompt} (again): "):
        sys.exit("The two passwords don't match.")
    return first


def cmd_generate_keys(_args):
    import secrets
    from cryptography.fernet import Fernet
    print(f'SECRET_KEY="{secrets.token_urlsafe(48)}"')
    print(f'ENCRYPTION_KEYS="{Fernet.generate_key().decode()}"')


def _boot():
    import database
    import vault
    vault.ensure_dev_keys()
    database.init_schema()


def cmd_init_db(_args):
    _boot()
    print(f"Schema ready on {config.database_url().split('@')[-1]}")


def cmd_create_admin(args):
    _boot()
    import accounts
    password = _password(args)
    try:
        uid = accounts.create_user(args.email, password, full_name=args.name or "", role="admin", status="active")
    except accounts.AccountError as exc:
        sys.exit(str(exc))
    accounts.audit("cli_create_admin", target=uid)
    print(f"Administrator {accounts.normalize_email(args.email)} created (id {uid}).")


def cmd_ensure_admin(_args):
    _boot()
    import accounts
    creds = config.admin_credentials()
    if not creds:
        print("ADMIN_USERNAME / ADMIN_PASSWORD not set — no administrator account managed.")
        return
    uid = accounts.ensure_admin(*creds)
    print(f"Administrator '{creds[0]}' ready (id {uid}).")


def cmd_set_role(args):
    _boot()
    import accounts
    user = accounts.get_user_by_email(args.email)
    if not user:
        sys.exit("No such account.")
    accounts.update_user(user["id"], role=args.role, must_change_password=0)
    accounts.end_all_sessions(user["id"])
    accounts.audit("cli_set_role", target=user["id"], detail={"role": args.role})
    print(f"{user['email']} is now '{args.role}'.")


def cmd_set_password(args):
    _boot()
    import accounts
    user = accounts.get_user_by_email(args.email)
    if not user:
        sys.exit("No such account.")
    try:
        accounts.set_password(user["id"], _password(args, "New password"))
    except accounts.AccountError as exc:
        sys.exit(str(exc))
    accounts.update_user(user["id"], status="active", failed_logins=0, locked_until=None)
    accounts.audit("cli_set_password", target=user["id"])
    print(f"Password set for {user['email']}; every session of that account was ended.")


def cmd_import_legacy(args):
    _boot()
    import migrate_legacy
    password = _password(args, "Password for this account") if args.set_password else None
    try:
        result = migrate_legacy.run(email=args.email, root=Path(args.root), password=password)
    except migrate_legacy.MigrationError as exc:
        sys.exit(f"Import stopped: {exc}")
    print("\nImport complete.")
    if not password:
        print(f"  Sign in with Google as {args.email} (or set a password: manage.py set-password).")


def cmd_verify_golden(args):
    _boot()
    import accounts
    import drafting
    import db
    import pipeline
    user = accounts.get_user_by_email(args.email)
    if not user:
        sys.exit("No such account.")
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    dcfg = drafting.load_config(user["id"])
    apps = db.for_user(user["id"]).get_applications_by_email()
    same, different, missing = 0, [], []
    for email, expected in baseline["composed"].items():
        app = apps.get(email)
        if not app:
            missing.append(email)
            continue
        research = pipeline._load_research(user["id"], email, app)
        draft = drafting.compose_for(dcfg, app, research, lang="en")
        if draft["subject"] == expected["subject"] and draft["body"] == expected["body"]:
            same += 1
        else:
            different.append(email)
    stored_same = sum(1 for email, row in baseline["stored"].items()
                      if apps.get(email) and apps[email]["subject"] == row["subject"]
                      and apps[email]["body"] == row["body"] and apps[email]["status"] == row["status"])
    print(f"Composed with the new code: {same}/{len(baseline['composed'])} byte-identical"
          f"{', ' + str(len(different)) + ' different' if different else ''}"
          f"{', ' + str(len(missing)) + ' missing' if missing else ''}.")
    print(f"Stored drafts carried over: {stored_same}/{len(baseline['stored'])} identical (subject, body, status).")
    for email in different[:10]:
        print("  differs:", email)
    if different or missing or stored_same != len(baseline["stored"]):
        sys.exit(1)
    print("GOLDEN CHECK PASSED")


def cmd_rotate_keys(_args):
    _boot()
    import database
    import vault
    from sqlalchemy import select, update
    count = 0
    with database.tx() as conn:
        for table, key_cols in ((database.user_secrets, ("user_id", "name")),
                                (database.system_secrets, ("name",))):
            for row in conn.execute(select(table)).all():
                data = dict(row._mapping)
                where = [table.c[c] == data[c] for c in key_cols]
                conn.execute(update(table).where(*where).values(ciphertext=vault.rotate(data["ciphertext"])))
                count += 1
    print(f"Re-encrypted {count} secret(s) under the first key in ENCRYPTION_KEYS.")


def cmd_prepare(args):
    _boot()
    import accounts
    import logsink
    import worker
    user = accounts.get_user_by_email(args.email)
    if not user:
        sys.exit("No such account.")
    logsink.install()
    worker.execute_prep_run(user["id"], user["role"], limit=args.limit, include_all=args.all)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-keys").set_defaults(func=cmd_generate_keys)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    p = sub.add_parser("create-admin")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=cmd_create_admin)
    sub.add_parser("ensure-admin").set_defaults(func=cmd_ensure_admin)
    p = sub.add_parser("set-role")
    p.add_argument("--email", required=True)
    p.add_argument("--role", required=True, choices=["user", "admin"])
    p.set_defaults(func=cmd_set_role)
    p = sub.add_parser("set-password")
    p.add_argument("--email", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=cmd_set_password)
    p = sub.add_parser("import-legacy")
    p.add_argument("--email", required=True)
    p.add_argument("--root", default=str(Path(__file__).parent))
    p.add_argument("--set-password", action="store_true", help="choose the password now instead of a temporary one")
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=cmd_import_legacy)
    p = sub.add_parser("verify-golden")
    p.add_argument("--email", required=True)
    p.add_argument("--baseline", required=True)
    p.set_defaults(func=cmd_verify_golden)
    sub.add_parser("rotate-keys").set_defaults(func=cmd_rotate_keys)
    p = sub.add_parser("prepare")
    p.add_argument("--email", required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_prepare)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
