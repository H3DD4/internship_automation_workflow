"""Per-company research and draft cache, kept per user in the database.

It used to be one JSON file per company under cache/. On a server with
several processes (or an ephemeral disk) files don't survive or aren't
shared, so the same records now live in the cache_entries table — same keys
(a hash of the recipient address), same contents, same role: a re-run reuses
finished research and drafts instead of redoing them.
"""

import hashlib

import db


def _key(email: str) -> str:
    return hashlib.sha1(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def save_research(user_id: int, email: str, context: dict) -> None:
    db.for_user(user_id).cache_put("research", _key(email), context)


def load_research(user_id: int, email: str):
    return db.for_user(user_id).cache_get("research", _key(email))


def save_draft(user_id: int, email: str, content: dict) -> None:
    db.for_user(user_id).cache_put("draft", _key(email), content)


def load_draft(user_id: int, email: str):
    return db.for_user(user_id).cache_get("draft", _key(email))
