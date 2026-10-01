"""One user's configuration: settings, encrypted secrets and CV file.

Settings keep the variable names the single-user app read from .env
(AI_PROVIDER, MAX_EMAILS_PER_DAY, …). That's deliberate: ai_client's
resolve_ai_settings() and model_router's build_router() already take an
`env` mapping, so handing them a user's mapping instead of os.environ gives
each user their own providers, keys and pool with no change to either.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select

import config
import database
import vault
from ai_client import PROVIDERS
from database import system_secrets, user_files, user_secrets, user_settings

# Non-secret settings a user can change, with their defaults.
SETTING_DEFAULTS = {
    "YOUR_NAME": "",
    "YOUR_TARGET_ROLE": "",
    "AI_PROVIDER": "groq",
    "AI_MODEL": "",
    "AI_FALLBACK_MODELS": "",
    "AI_TRANSLATION_MODEL": "",
    "AI_POOL_EXCLUDE": "",
    "MIN_DELAY_SECONDS": "45",
    "MAX_DELAY_SECONDS": "120",
    "MAX_EMAILS_PER_DAY": "20",
    "BOUNCE_CHECK_MINUTES": "30",
    "RESEARCH_WORKERS": "3",
    "WRITER_WORKERS": "2",
    "AI_MAX_RPM": "800",
    # How email goes out: oauth (Sign in with Google), app_password (Gmail +
    # app password) or smtp (any provider's SMTP server).
    "MAIL_METHOD": "",
    "GMAIL_ADDRESS": "",
    "SMTP_HOST": "",
    "SMTP_PORT": "",
    "SMTP_SECURITY": "ssl",       # ssl | starttls
    "SMTP_USERNAME": "",
    "IMAP_HOST": "",
}
for _pid in PROVIDERS:
    SETTING_DEFAULTS[f"{_pid.upper()}_BASE_URL"] = ""
    # The models ticked for this provider in Settings (comma-separated);
    # empty = the measured defaults (model_router.POOL).
    SETTING_DEFAULTS[f"AI_MODELS_{_pid.upper()}"] = ""

# Secrets: stored encrypted, never sent back to a browser.
SECRET_NAMES = {p["key_env"] for p in PROVIDERS.values()} | {"GMAIL_APP_PASSWORD", "GOOGLE_TOKEN"}

CV_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}


class ConfigError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserConfig:
    def __init__(self, user_id: int, role: str = "user"):
        self.user_id = int(user_id)
        self.role = role
        self._settings: dict | None = None
        self._secrets: dict = {}

    # -- settings ------------------------------------------------------------

    @property
    def settings(self) -> dict:
        if self._settings is None:
            with database.read() as conn:
                rows = conn.execute(select(user_settings.c.key, user_settings.c.value).where(
                    user_settings.c.user_id == self.user_id)).all()
            values = dict(SETTING_DEFAULTS)
            values.update({row.key: row.value or "" for row in rows})
            self._settings = values
        return self._settings

    def get(self, key: str, default: str = "") -> str:
        return (self.settings.get(key) or default or "").strip()

    def set_many(self, values: dict) -> None:
        unknown = set(values) - set(SETTING_DEFAULTS)
        if unknown:
            raise ConfigError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        with database.tx() as conn:
            for key, value in values.items():
                database.upsert(conn, user_settings, {"user_id": self.user_id, "key": key,
                                                      "value": str(value if value is not None else "")},
                                ["user_id", "key"])
        self._settings = None

    def int_setting(self, key: str, default: int) -> int:
        """An integer setting, never crashing on a stray value, and held under
        the platform ceilings for non-admin accounts."""
        try:
            value = int(self.get(key, str(default)))
        except ValueError:
            value = default
        if self.role != "admin":
            if key in ("RESEARCH_WORKERS", "WRITER_WORKERS"):
                value = min(value, config.USER_MAX_RESEARCH_WORKERS)
            elif key == "MAX_EMAILS_PER_DAY":
                value = min(value, config.USER_MAX_EMAILS_PER_DAY)
        return value

    # -- secrets -------------------------------------------------------------

    def secret(self, name: str) -> str:
        if name not in SECRET_NAMES:
            raise ConfigError(f"Unknown secret {name}")
        if name in self._secrets:
            return self._secrets[name]
        with database.read() as conn:
            row = conn.execute(select(user_secrets.c.ciphertext).where(
                user_secrets.c.user_id == self.user_id, user_secrets.c.name == name)).first()
        value = vault.decrypt(row.ciphertext, vault.user_scope(self.user_id, name)) if row else ""
        self._secrets[name] = value
        return value

    def has_secret(self, name: str) -> bool:
        with database.read() as conn:
            return conn.execute(select(user_secrets.c.name).where(
                user_secrets.c.user_id == self.user_id, user_secrets.c.name == name)).first() is not None

    def secret_version(self, name: str) -> str | None:
        """When a secret last changed (None if not set) — a cheap cache key."""
        with database.read() as conn:
            row = conn.execute(select(user_secrets.c.updated_at).where(
                user_secrets.c.user_id == self.user_id, user_secrets.c.name == name)).first()
        return row.updated_at if row else None

    def saved_secret_names(self) -> set:
        with database.read() as conn:
            return {row.name for row in conn.execute(select(user_secrets.c.name).where(
                user_secrets.c.user_id == self.user_id))}

    def set_secret(self, name: str, value: str) -> None:
        if name not in SECRET_NAMES:
            raise ConfigError(f"Unknown secret {name}")
        value = (value or "").strip()
        if not value:
            self.delete_secret(name)
            return
        ciphertext = vault.encrypt(value, vault.user_scope(self.user_id, name))
        with database.tx() as conn:
            database.upsert(conn, user_secrets, {"user_id": self.user_id, "name": name,
                                                 "ciphertext": ciphertext, "updated_at": _now()},
                            ["user_id", "name"])
        self._secrets[name] = value

    def delete_secret(self, name: str) -> None:
        with database.tx() as conn:
            conn.execute(delete(user_secrets).where(user_secrets.c.user_id == self.user_id,
                                                    user_secrets.c.name == name))
        self._secrets.pop(name, None)

    # -- the env-style mapping ai_client / model_router consume -----------------

    def ai_env(self) -> dict:
        env = {k: v for k, v in self.settings.items() if v}
        for preset in PROVIDERS.values():
            key = self.secret(preset["key_env"])
            if key:
                env[preset["key_env"]] = key
        return env

    # -- CV ------------------------------------------------------------------

    def save_cv(self, filename: str, content: bytes) -> dict:
        info = validate_cv(filename, content)
        with database.tx() as conn:
            conn.execute(delete(user_files).where(user_files.c.user_id == self.user_id,
                                                  user_files.c.kind == "cv"))
            conn.execute(user_files.insert().values(
                user_id=self.user_id, kind="cv", filename=info["filename"],
                content_type=info["content_type"], size=len(content),
                sha256=hashlib.sha256(content).hexdigest(), content=content, uploaded_at=_now()))
        return info

    def cv_info(self) -> dict | None:
        with database.read() as conn:
            row = conn.execute(select(user_files.c.filename, user_files.c.size,
                                      user_files.c.content_type, user_files.c.uploaded_at)
                               .where(user_files.c.user_id == self.user_id,
                                      user_files.c.kind == "cv")).first()
        if not row:
            return None
        return {"name": row.filename, "size_kb": round(row.size / 1024, 1),
                "content_type": row.content_type, "uploaded_at": row.uploaded_at}

    def cv(self) -> dict | None:
        """{"filename", "content_type", "content"} or None."""
        with database.read() as conn:
            row = conn.execute(select(user_files).where(user_files.c.user_id == self.user_id,
                                                        user_files.c.kind == "cv")).first()
        if not row:
            return None
        return {"filename": row.filename, "content_type": row.content_type,
                "content": bytes(row.content)}


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, default: str) -> str:
    """The name shown to a recruiter on the attachment. Path parts, control
    characters and anything outside a plain set are removed."""
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip(" ._")[:120]
    return base or default


def validate_cv(filename: str, content: bytes) -> dict:
    """Accept only real PDF / Word files, judged by their bytes, not by the
    name the browser claims."""
    if not content:
        raise ConfigError("The CV file is empty.")
    if len(content) > config.MAX_CV_BYTES:
        raise ConfigError(f"The CV is larger than {config.MAX_CV_BYTES // (1024 * 1024)} MB.")
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in (filename or "") else ""
    if suffix not in CV_TYPES:
        raise ConfigError("Upload your CV as a PDF or Word file (.pdf, .docx, .doc).")
    signatures = {".pdf": (b"%PDF-",), ".docx": (b"PK\x03\x04",), ".doc": (b"\xd0\xcf\x11\xe0",)}
    if not content.startswith(signatures[suffix]):
        raise ConfigError("That file isn't a real " + suffix.upper().lstrip(".") + " document.")
    return {"filename": safe_filename(filename, "CV" + suffix), "content_type": CV_TYPES[suffix]}


# ---------------------------------------------------------------------------
# Platform-level secrets (the Google OAuth client the whole app signs in with)
# ---------------------------------------------------------------------------

def get_system_secret(name: str) -> str:
    with database.read() as conn:
        row = conn.execute(select(system_secrets.c.ciphertext).where(
            system_secrets.c.name == name)).first()
    return vault.decrypt(row.ciphertext, vault.system_scope(name)) if row else ""


def set_system_secret(name: str, value: str) -> None:
    with database.tx() as conn:
        if not value:
            conn.execute(delete(system_secrets).where(system_secrets.c.name == name))
            return
        database.upsert(conn, system_secrets, {
            "name": name, "ciphertext": vault.encrypt(value, vault.system_scope(name)),
            "updated_at": _now()}, ["name"])
