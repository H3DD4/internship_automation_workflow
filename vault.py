"""Encryption at rest for secrets the app must be able to use again later:
AI provider keys, Gmail app passwords, Google OAuth tokens.

These can't be hashed like a login password — sending an email needs the
actual credential — so they are encrypted with Fernet (AES-128-CBC +
HMAC-SHA256, authenticated). The key lives only in the server environment
(ENCRYPTION_KEYS), never in the database, so a stolen database dump alone
reveals nothing.

ENCRYPTION_KEYS is a comma-separated list: the first key encrypts, all of
them decrypt. To rotate, put a new key first, run `python manage.py
rotate-keys`, then drop the old one.

Each ciphertext is bound to what it is (user id + secret name) through a
prefix inside the encrypted payload, so a row copied onto another user or
another secret name fails to decrypt instead of silently working.
"""

from __future__ import annotations

import os
import secrets as _secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

import config


class VaultError(RuntimeError):
    pass


_fernet: MultiFernet | None = None
_fernet_source: str | None = None


def _keys_value() -> str:
    return config.get("ENCRYPTION_KEYS")


def ensure_dev_keys() -> None:
    """Outside production, create SECRET_KEY and ENCRYPTION_KEYS on first run
    and keep them in .env, so a local install works with zero setup. In
    production they must be provided — generating one silently there would
    lock every user out of their saved secrets on the next deploy."""
    missing = {name for name in ("SECRET_KEY", "ENCRYPTION_KEYS") if not config.get(name)}
    if not missing:
        return
    if config.is_production():
        raise RuntimeError(f"Set {', '.join(sorted(missing))} in the environment "
                           "(python manage.py generate-keys prints fresh ones).")
    generated = {}
    if "SECRET_KEY" in missing:
        generated["SECRET_KEY"] = _secrets.token_urlsafe(48)
    if "ENCRYPTION_KEYS" in missing:
        generated["ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
    lines = ["", "# ---- Generated on first run: keep these, and keep them secret ----"]
    lines += [f'{name}="{value}"' for name, value in generated.items()]
    with open(config.ENV_PATH, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.environ.update(generated)


def _cipher() -> MultiFernet:
    global _fernet, _fernet_source
    value = _keys_value()
    if _fernet is not None and _fernet_source == value:
        return _fernet
    keys = [k.strip() for k in value.split(",") if k.strip()]
    if not keys:
        raise VaultError("ENCRYPTION_KEYS is not set.")
    try:
        _fernet = MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise VaultError("ENCRYPTION_KEYS holds an invalid key.") from exc
    _fernet_source = value
    return _fernet


def _context(scope: str) -> bytes:
    return f"{scope}\x00".encode()


def encrypt(plaintext: str, scope: str) -> str:
    return _cipher().encrypt(_context(scope) + plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, scope: str) -> str:
    try:
        raw = _cipher().decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise VaultError("A stored secret could not be decrypted (wrong key or tampered).") from exc
    prefix = _context(scope)
    if not raw.startswith(prefix):
        raise VaultError("A stored secret belongs to a different owner or name.")
    return raw[len(prefix):].decode("utf-8")


def rotate(token: str) -> str:
    """Re-encrypt under the newest key."""
    return _cipher().rotate(token.encode("ascii")).decode("ascii")


def user_scope(user_id: int, name: str) -> str:
    return f"user:{int(user_id)}:{name}"


def system_scope(name: str) -> str:
    return f"system:{name}"
