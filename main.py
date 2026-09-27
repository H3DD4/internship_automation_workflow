"""
Main orchestrator — PREPARATION ONLY (research + write). Run with:  python main.py
It never sends: review drafts and send from the dashboard (python dashboard/app.py),
which owns pacing, the daily cap, and anti-spam measures via sender_worker.py.

Optional flags:
  --limit N              Only prepare the first N not-yet-handled companies (good for testing)
  --all                  Include every row from the file, not just ones still needing work
  --companies PATH        Path to a companies file (.xlsx or .csv), default companies.xlsx
  --research-workers N   Concurrent scrape+research workers (default from .env, 3)
  --writer-workers N     Concurrent email-drafting workers (default from .env, 2)

ARCHITECTURE (see pipeline.py for the full explanation):
  Scraping + AI research + AI email drafting run concurrently in background
  worker threads (a research pool feeding a writer pool), so a long company
  list is prepared far faster than doing each company fully in sequence.

  Every finished research result and drafted email is cached to disk
  (cache/ folder) and the database the instant it's ready, so re-running the
  script reuses that work instantly instead of redoing it.
"""

import os
import sys
import json
import argparse
import socket
import threading
import webbrowser
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from ai_client import CompatibleAIClient

import db
from utils import is_valid_email, extract_company_name, derive_website_from_email, \
    _company_name_from_domain
from pipeline import Pipeline, needs_preparation

from ai_client import resolve_ai_settings
SETUP_URL = "http://127.0.0.1:5050"


def _dashboard_setup_needed(companies_arg: str = None) -> bool:
    """Return whether main.py should hand setup back to the dashboard.

    main.py only PREPARES emails (research + write) — it never sends, so
    Gmail credentials and the CV (both send-time-only concerns, checked by
    the dashboard/sender worker instead) are deliberately not required here.
    An AI key, applicant profile, and a companies file are what preparation
    actually needs.
    """
    load_dotenv(override=True)
    ai = resolve_ai_settings()
    if not (ai["api_key"] and ai["base_url"] and ai["model"]):
        return True
    if not all(os.getenv(key) for key in ("YOUR_NAME", "YOUR_TARGET_ROLE")):
        return True

    companies_value = companies_arg or os.getenv("COMPANIES_FILE_PATH") or "companies.xlsx"
    companies_path = Path(companies_value)
    if not companies_path.is_absolute():
        companies_path = Path(__file__).parent / companies_path
    return not companies_path.is_file()


def _open_dashboard_setup():
    """Start the local setup UI when main.py is launched before configuration."""
    try:
        with socket.create_connection(("127.0.0.1", 5050), timeout=0.3):
            print(f"Setup is required. Open {SETUP_URL} to upload your CV, company list, and credentials.")
            webbrowser.open(SETUP_URL)
            return
    except OSError:
        pass

    from dashboard.app import app
    print(f"Setup is required. Opening the dashboard at {SETUP_URL}...")
    threading.Timer(1.0, lambda: webbrowser.open(SETUP_URL)).start()
    app.run(host="127.0.0.1", port=5050, debug=False)


def load_config():
    """Config for PREPARATION only (research + write). Gmail credentials and
    the CV are send-time concerns owned by mail_service.py/sender_worker.py,
    which read GMAIL_* / CV_FILE_PATH from .env directly — not required here,
    so an AI-key-only setup (no Gmail connected yet) can still prepare drafts."""
    load_dotenv(override=True)

    ai = resolve_ai_settings()
    missing = [k for k in ("YOUR_NAME", "YOUR_TARGET_ROLE") if not os.getenv(k)]
    if not ai["api_key"]:
        missing.append(f"an API key for {ai['label']}")
    if not ai["base_url"]:
        missing.append("the provider base URL")
    if not ai["model"]:
        missing.append("AI_MODEL")
    if missing:
        print(f"ERROR: missing required .env values: {', '.join(missing)}")
        print("Copy .env.example to .env and fill it in first.")
        sys.exit(1)

    spec_path = Path(__file__).parent / "specializations.json"
    with open(spec_path) as f:
        specializations = json.load(f)

    return {
        "ai_api_key": ai["api_key"],
        "ai_base_url": ai["base_url"],
        "ai_model": ai["model"],
        "applicant_name": os.getenv("YOUR_NAME"),
        "target_role": os.getenv("YOUR_TARGET_ROLE"),
        "research_workers": int(os.getenv("RESEARCH_WORKERS", 3)),
        "writer_workers": int(os.getenv("WRITER_WORKERS", 2)),
        "spec": specializations,
    }


def load_companies(path: str) -> pd.DataFrame:
    """
    Loads a companies/contacts file and normalizes it into the standard shape:
    company_name, email, website, contact_name.

    Supports two schemas:
      1. Simple:  company_name, email, [website]
      2. Contact-list style (e.g. exported "PFE contacts" sheets):
         email, name, attributes  — where `attributes` is a JSON string like
         '{"Company": "Rtone"}' and `name` is the contact person's name
         (which may or may not actually BE a person's name — see utils.py).

    Also: drops rows with missing/invalid emails, de-duplicates by email
    (keeping the first occurrence), and never crashes on a single bad row.
    """
    path = Path(path)
    if not path.exists():
        print(f"ERROR: companies file not found at '{path}'")
        sys.exit(1)

    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    df.columns = [c.strip().lower() for c in df.columns]

    if "email" not in df.columns:
        print(f"ERROR: companies file must have at least an 'email' column. Found: {list(df.columns)}")
        sys.exit(1)

    total_before = len(df)

    # Normalize (strip + lowercase) BEFORE validating/deduping — otherwise
    # "Jane@Acme.com" and " jane@acme.com" are treated as two different
    # mailboxes and both get an email, instead of being recognized as the
    # same recipient and deduplicated to one.
    df["email"] = df["email"].astype(str).str.strip().str.lower()

    # Drop rows with missing/invalid emails first — everything else depends on it.
    df = df[df["email"].apply(is_valid_email)].copy()
    invalid_email_count = total_before - len(df)

    # De-duplicate by email (keep first occurrence).
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["email"], keep="first")
    duplicate_count = before_dedup - len(df)

    # --- Normalize into the standard columns ---
    if "company_name" in df.columns:
        # Schema 1: already has company_name directly.
        df["company_name"] = df["company_name"].fillna("").astype(str).str.strip()
        missing_company = df["company_name"] == ""
        if missing_company.any():
            df.loc[missing_company, "company_name"] = df.loc[missing_company, "email"].apply(
                _company_name_from_domain
            )
    elif "attributes" in df.columns:
        # Schema 2: derive company_name from the attributes JSON.
        df["company_name"] = df.apply(
            lambda r: extract_company_name(r.get("attributes"), r["email"]), axis=1
        )
    else:
        # No company info at all — best-effort guess from email domain.
        df["company_name"] = df["email"].apply(_company_name_from_domain)

    if "website" in df.columns:
        df["website"] = df["website"].fillna("").astype(str).str.strip()
        missing_website = df["website"] == ""
        if missing_website.any():
            df.loc[missing_website, "website"] = df.loc[missing_website, "email"].apply(
                derive_website_from_email
            )
    else:
        df["website"] = df["email"].apply(derive_website_from_email)

    if "name" in df.columns:
        df["contact_name"] = df["name"]
    elif "contact_name" in df.columns:
        pass
    else:
        df["contact_name"] = ""

    df["company_name"] = df["company_name"].astype(str).str.strip()
    df["website"] = df["website"].fillna("").astype(str).str.strip()

    print(f"Loaded {total_before} rows -> {len(df)} usable "
          f"({invalid_email_count} invalid/missing email, {duplicate_count} duplicate email).")

    return df[["company_name", "email", "website", "contact_name"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Prepare at most N companies that still need work (skips finished rows)")
    parser.add_argument("--all", action="store_true",
                        help="Include every row from the file (default: skip already-prepared)")
    parser.add_argument("--companies", type=str, default=None,
                        help="Path to companies file (.xlsx or .csv)")
    parser.add_argument("--research-workers", type=int, default=None,
                        help="Override RESEARCH_WORKERS from .env")
    parser.add_argument("--writer-workers", type=int, default=None,
                        help="Override WRITER_WORKERS from .env")
    args = parser.parse_args()

    if _dashboard_setup_needed(args.companies):
        _open_dashboard_setup()
        return

    db.init_db()
    cfg = load_config()
    client = CompatibleAIClient(cfg["ai_api_key"], cfg["ai_base_url"])

    companies_path = args.companies or os.getenv("COMPANIES_FILE_PATH", "companies.xlsx")
    df = load_companies(companies_path)

    all_rows = list(df[["company_name", "email", "website", "contact_name"]].itertuples(
        index=False, name=None
    ))

    if args.all:
        candidate_rows = all_rows
    else:
        candidate_rows = []
        skipped_done = 0
        for row in all_rows:
            existing = db.get_application_by_email(row[1])
            if not needs_preparation(existing):
                skipped_done += 1
                continue
            candidate_rows.append(row)
        if skipped_done:
            print(f"Skipping {skipped_done} already-prepared or sent row(s) from the file.")

    if args.limit:
        rows = candidate_rows[: args.limit]
    else:
        rows = candidate_rows

    pending_in_db = db.count_needing_preparation()
    print(f"Loaded {len(all_rows)} companies from {companies_path}")
    print(f"Preparing {len(rows)} this run ({pending_in_db} still need work in the database).")
    print("Preparation only — review and send from the dashboard.\n")

    research_workers = args.research_workers or cfg["research_workers"]
    writer_workers = args.writer_workers or cfg["writer_workers"]
    print(f"Preparation engine: {research_workers} research worker(s), "
          f"{writer_workers} writer worker(s).\n")

    pipeline = Pipeline(
        client, cfg, cfg["ai_model"],
        research_workers=research_workers,
        writer_workers=writer_workers,
    )
    results = pipeline.run(rows)

    print("\n--- Run summary ---")
    for status, count in results.items():
        print(f"  {status}: {count}")

    total_accounted = sum(results.values())
    if total_accounted != len(rows):
        # Sanity check: every company must land in exactly one bucket above.
        # If this ever fires, something was lost/double-counted — surface it
        # loudly rather than silently reporting a clean-looking summary.
        print(f"\n  WARNING: {len(rows)} companies loaded but only {total_accounted} "
              f"accounted for in the summary above — please check applications.db.")

    print("\nOpen the dashboard to review and send: python dashboard/app.py")


if __name__ == "__main__":
    main()
