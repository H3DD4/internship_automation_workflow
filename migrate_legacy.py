"""Import the single-user installation into one account of the multi-user app.

Everything the old version kept is carried over, untouched, into the account
of the given email:

    applications.db   every application, event, send job and item — same
                      ids, so /company/<id> links still work
    cache/            every research and draft cache file
    specializations.json (+ specializations_fr.json) as the account's own
                      hand-written wording, exactly as it was
    .env              settings (pacing, workers, AI provider and model) and,
                      encrypted, the AI keys and Gmail app password
    token.json        the Google sign-in, encrypted
    client_secret_*   the Google OAuth client, as the platform's (encrypted)
    the CV and the companies file referenced by .env

The source files are only read, never modified or moved. The import refuses
to run twice into an account that already has applications.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import func, select

import accounts
import company_import
import database
import profiles
import user_config
from ai_client import PROVIDERS

_PLACEHOLDERS = {
    "your_groq_api_key_here", "your_opencode_api_key_here", "your_openrouter_api_key_here",
    "your_mistral_api_key_here", "your_gemini_api_key_here", "you@gmail.com", "your full name",
    "xxxx xxxx xxxx xxxx", "xxxxxxxxxxxxxxxx", "./my_cv.pdf", "my_cv.pdf", "companies.xlsx",
    "./companies.xlsx",
}
LEGACY_TABLES = ("applications", "events", "send_jobs", "send_job_items")


class MigrationError(RuntimeError):
    pass


def _real(value) -> str:
    value = (value or "").strip().strip("\"'")
    return "" if value.lower() in _PLACEHOLDERS or value.replace(" ", "").lower() in _PLACEHOLDERS else value


def _resolve(root: Path, raw: str) -> Path | None:
    raw = _real(raw)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else (root / path)


def _log(report: list, message: str) -> None:
    report.append(message)
    print("  " + message)


def run(*, email: str, root: Path, password: str | None = None, role: str = "user",
        log=print) -> dict:
    root = Path(root)
    database.init_schema()
    report: list = []
    env = {k: (v or "") for k, v in dotenv_values(root / ".env").items()} if (root / ".env").exists() else {}
    sqlite_path = root / "applications.db"
    if not sqlite_path.exists():
        raise MigrationError(f"{sqlite_path} not found.")

    # --- the account ------------------------------------------------------------
    # A regular account (the platform administrator is a separate, username
    # account). Without a chosen password it gets an unguessable one and
    # signs in with Google — no temporary password to deal with.
    email = accounts.normalize_email(email)
    user = accounts.get_user_by_email(email)
    if user is None:
        uid = accounts.create_user(email, password or secrets.token_urlsafe(32),
                                   full_name=_real(env.get("YOUR_NAME")), role=role, status="active")
        _log(report, f"created {role} account {email} (id {uid})")
    else:
        uid = user["id"]
        if user["status"] != "active":
            accounts.update_user(uid, status="active")
        _log(report, f"using existing account {email} (id {uid})")

    with database.read() as conn:
        already = conn.execute(select(func.count()).select_from(database.applications)
                               .where(database.applications.c.user_id == uid)).scalar_one()
    if already:
        raise MigrationError(f"{email} already has {already} applications — the import already ran. "
                             "Nothing was changed.")

    cfg = user_config.UserConfig(uid, role)

    # --- settings and secrets from .env ---------------------------------------
    settings = {key: _real(env.get(key)) for key in user_config.SETTING_DEFAULTS
                if _real(env.get(key)) and key != "MAIL_METHOD"}
    token_path = root / "token.json"
    if token_path.exists():
        settings["MAIL_METHOD"] = "oauth"
    elif _real(env.get("GMAIL_APP_PASSWORD")):
        settings["MAIL_METHOD"] = "app_password"
    cfg.set_many(settings)
    _log(report, f"settings: {', '.join(sorted(settings)) or 'none'}")

    saved_secrets = []
    for preset in PROVIDERS.values():
        value = _real(env.get(preset["key_env"]))
        if value:
            cfg.set_secret(preset["key_env"], value)
            saved_secrets.append(preset["key_env"])
    legacy_key = _real(env.get("AI_API_KEY"))
    provider = (settings.get("AI_PROVIDER") or "groq").lower()
    if legacy_key and provider in PROVIDERS and PROVIDERS[provider]["key_env"] not in saved_secrets:
        cfg.set_secret(PROVIDERS[provider]["key_env"], legacy_key)
        saved_secrets.append(PROVIDERS[provider]["key_env"])
    if _real(env.get("GMAIL_APP_PASSWORD")):
        cfg.set_secret("GMAIL_APP_PASSWORD", _real(env.get("GMAIL_APP_PASSWORD")))
        saved_secrets.append("GMAIL_APP_PASSWORD")
    if token_path.exists():
        token = json.loads(token_path.read_text(encoding="utf-8"))
        cfg.set_secret("GOOGLE_TOKEN", json.dumps(token))
        saved_secrets.append("GOOGLE_TOKEN")
    _log(report, f"encrypted secrets: {', '.join(saved_secrets) or 'none'}")

    client_files = [root / "credentials.json", *sorted(root.glob("client_secret_*.json"))]
    client_file = next((p for p in client_files if p.exists()), None)
    if client_file:
        import google_auth_helper
        raw = client_file.read_bytes()
        ok, _ = google_auth_helper.validate_client_secret(raw)
        if ok and not google_auth_helper.oauth_is_configured():
            google_auth_helper.save_client_secret(raw)
            _log(report, f"Google OAuth client from {client_file.name} saved as the platform's (encrypted)")

    # --- CV -----------------------------------------------------------------
    cv_text = None
    cv_path = _resolve(root, env.get("CV_FILE_PATH", ""))
    if cv_path and cv_path.is_file():
        content = cv_path.read_bytes()
        cfg.save_cv(cv_path.name, content)
        try:
            cv_text = profiles.extract_cv_text(cv_path.name, content)
        except profiles.ProfileError:
            cv_text = None
        _log(report, f"CV {cv_path.name} ({len(content) // 1024} KB)")
    else:
        _log(report, "no CV file found (CV_FILE_PATH)")

    # --- wording: the hand-written spec, as it was ---------------------------
    spec_en = json.loads((root / "specializations.json").read_text(encoding="utf-8"))
    fr_path = root / "specializations_fr.json"
    spec_fr = json.loads(fr_path.read_text(encoding="utf-8")) if fr_path.exists() else None
    profiles.save(uid, mode="custom", template_id="custom", language_mode="auto",
                  spec_en=spec_en, spec_fr=spec_fr, cv_text=cv_text)
    _log(report, f"wording: specializations.json{' + specializations_fr.json' if spec_fr else ''} (hand-written)")

    # --- the companies list ------------------------------------------------------
    companies_path = _resolve(root, env.get("COMPANIES_FILE_PATH", ""))
    listed = 0
    if companies_path and companies_path.is_file():
        parsed = company_import.parse(companies_path.name, companies_path.read_bytes())
        import db as db_module
        listed = db_module.for_user(uid).add_companies(parsed["rows"])
        _log(report, f"companies list {companies_path.name}: {listed} rows "
                     f"({parsed['invalid']} invalid, {parsed['duplicates']} duplicate)")
    else:
        _log(report, "no companies file found (COMPANIES_FILE_PATH)")

    # --- the SQLite history, ids preserved -------------------------------------
    source = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    counts = {}
    try:
        with database.tx() as conn:
            for name in LEGACY_TABLES:
                target = database.metadata.tables[name]
                target_cols = {c.name for c in target.columns}
                rows = [dict(r) for r in source.execute(f"SELECT * FROM {name} ORDER BY id")]
                batch = []
                for row in rows:
                    record = {k: v for k, v in row.items() if k in target_cols}
                    if name in ("applications", "send_jobs"):
                        record["user_id"] = uid
                    if name == "applications":
                        record["favorite"] = int(record.get("favorite") or 0)
                    batch.append(record)
                for start in range(0, len(batch), 500):
                    conn.execute(target.insert(), batch[start:start + 500])
                counts[name] = len(batch)
            meta_rows = source.execute("SELECT key, value FROM meta").fetchall()
            for row in meta_rows:
                database.upsert(conn, database.user_meta,
                                {"user_id": uid, "key": row["key"], "value": row["value"]}, ["user_id", "key"])
            counts["meta"] = len(meta_rows)
            database.reset_sequences(conn)
    finally:
        source.close()
    _log(report, "history: " + ", ".join(f"{v} {k}" for k, v in counts.items()))

    # --- cache files ------------------------------------------------------------
    cached = {"research": 0, "draft": 0}
    now = datetime.now(timezone.utc).isoformat()
    with database.tx() as conn:
        for kind, folder in (("research", "research"), ("draft", "drafts")):
            directory = root / "cache" / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                database.upsert(conn, database.cache_entries, {
                    "user_id": uid, "kind": kind, "key": path.stem,
                    "data": json.dumps(data, ensure_ascii=False), "updated_at": now,
                }, ["user_id", "kind", "key"])
                cached[kind] += 1
    _log(report, f"cache: {cached['research']} research, {cached['draft']} drafts")

    # --- verification: every row arrived --------------------------------------
    check = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        for name in LEGACY_TABLES:
            expected = check.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            with database.read() as conn:
                table = database.metadata.tables[name]
                ids = [r[0] for r in check.execute(f"SELECT id FROM {name}")]
                got = conn.execute(select(func.count()).select_from(table)
                                   .where(table.c.id.in_(ids) if ids else table.c.id == -1)).scalar_one()
            if got != expected:
                raise MigrationError(f"{name}: expected {expected} rows, found {got}.")
    finally:
        check.close()
    _log(report, "verified: every legacy row is present with its original id")
    accounts.audit("legacy_import", actor=uid, target=uid, detail={**counts, **cached, "listed": listed})
    return {"user_id": uid, "counts": counts,
            "cache": cached, "listed": listed, "report": report}
