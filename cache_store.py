"""
Lightweight per-company JSON cache (disk-backed).

Each company gets its OWN small JSON file, keyed by a hash of its email
address — not one big shared JSON blob. This is what makes the pipeline
resumable and crash-safe, and it's the single biggest time-saver on a
re-run (e.g. after yesterday's daily send cap was hit):

  - Stage 1 (research) writes cache/research/<hash>.json the moment it's done.
  - Stage 2 (writer)   writes cache/drafts/<hash>.json the moment it's done.
  - On the next run, any file that's already there is loaded instantly
    instead of re-scraping the website or re-calling the AI.

Why one file per company instead of one shared file:
  - No locking needed between worker threads — each one only ever touches
    its own company's file, so there's no contention and no risk of one
    company's write corrupting another's data.
  - Writes are atomic (write to a temp file, then os.replace onto the real
    path), so a crash mid-write can never leave a half-written/corrupt file
    behind. A corrupt/unreadable file is simply treated as a cache miss —
    that company is just re-processed, it never crashes the pipeline.
"""

import json
import hashlib
import os
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
RESEARCH_DIR = CACHE_DIR / "research"
DRAFTS_DIR = CACHE_DIR / "drafts"


def _key(email: str) -> str:
    return hashlib.sha1(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)  # atomic on POSIX and Windows


def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Corrupt or partially-written file (e.g. process killed mid-write
        # before the atomic replace happened) — treat as a plain cache miss.
        return None


def save_research(email: str, context: dict) -> None:
    _atomic_write(RESEARCH_DIR / f"{_key(email)}.json", context)


def load_research(email: str):
    return _load(RESEARCH_DIR / f"{_key(email)}.json")


def save_draft(email: str, content: dict) -> None:
    _atomic_write(DRAFTS_DIR / f"{_key(email)}.json", content)


def load_draft(email: str):
    return _load(DRAFTS_DIR / f"{_key(email)}.json")
