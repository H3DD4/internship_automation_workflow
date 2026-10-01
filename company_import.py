"""Reading a user's company list (CSV or Excel), with a report they can act on.

The format:
  * a header row;
  * an `email` column (required) — one recipient per row;
  * optional `company_name`, `website`, `contact_name`;
  * also understood: common English/French names for those columns
    ("Company", "Entreprise", "Site web", "Nom"...), and contact-export
    sheets with an `attributes` JSON column like {"Company": "Rtone"}.

Nothing is saved until the whole file has been read: the preview says how
many rows are usable, how many are new, and exactly which rows are wrong and
why, so the user can fix the file instead of guessing.
"""

from __future__ import annotations

import io
import re
import unicodedata

import config
from utils import (_company_name_from_domain, derive_website_from_email, extract_company_name,
                   is_valid_email)

REQUIRED = "email"
COLUMN_ALIASES = {
    "email": {"email", "e-mail", "mail", "email address", "e-mail address", "adresse email",
              "adresse e-mail", "courriel", "adresse mail"},
    "company_name": {"company_name", "company", "company name", "organisation", "organization",
                     "entreprise", "societe", "société", "nom de l'entreprise", "raison sociale"},
    "website": {"website", "site", "site web", "url", "web", "site internet", "domain", "domaine"},
    "contact_name": {"contact_name", "contact", "contact name", "name", "nom", "nom du contact",
                     "full name", "prenom", "prénom", "first name"},
    "attributes": {"attributes"},
}
MAX_ERRORS_SHOWN = 25
_URL_RE = re.compile(r"^(https?://)?[a-z0-9.-]+\.[a-z]{2,}(/.*)?$", re.IGNORECASE)


class ImportFailure(ValueError):
    pass


def _fold(name: str) -> str:
    name = unicodedata.normalize("NFKC", str(name)).strip().lower().replace("_", " ")
    return re.sub(r"\s+", " ", name)


def _map_columns(columns: list) -> dict:
    """{original_column: standard_name} for the columns we understand."""
    mapping, taken = {}, set()
    for column in columns:
        folded = _fold(column)
        for standard, aliases in COLUMN_ALIASES.items():
            if standard in taken:
                continue
            if folded in {_fold(a) for a in aliases}:
                mapping[column] = standard
                taken.add(standard)
                break
    return mapping


def read_table(filename: str, content: bytes):
    import pandas as pd
    if not content:
        raise ImportFailure("The file is empty.")
    if len(content) > config.MAX_COMPANIES_UPLOAD_BYTES:
        raise ImportFailure(f"The file is larger than {config.MAX_COMPANIES_UPLOAD_BYTES // (1024 * 1024)} MB.")
    name = (filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xlsm")):
            if not content.startswith(b"PK"):
                raise ImportFailure("That file isn't a real Excel workbook.")
            frame = pd.read_excel(io.BytesIO(content), dtype=str, engine="openpyxl")
        elif name.endswith(".csv") or name.endswith(".txt"):
            text = None
            for encoding in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = content.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            # The separator is judged from the header line, among real
            # separators only: letting pandas guess split a one-column file
            # ("email") on the letter "m".
            header = text.splitlines()[0] if text else ""
            counts = {sep: header.count(sep) for sep in (",", ";", "\t", "|")}
            separator = max(counts, key=counts.get) if any(counts.values()) else ","
            frame = pd.read_csv(io.StringIO(text), dtype=str, sep=separator, keep_default_na=False)
        else:
            raise ImportFailure("Upload a .csv or .xlsx file.")
    except ImportFailure:
        raise
    except Exception as exc:
        raise ImportFailure(f"The file couldn't be read as a table ({str(exc)[:120]}).") from exc
    if len(frame) > config.MAX_COMPANIES_PER_USER:
        raise ImportFailure(f"The file has {len(frame)} rows; the limit is {config.MAX_COMPANIES_PER_USER}.")
    return frame


def parse(filename: str, content: bytes, existing_emails: set | None = None) -> dict:
    """Validate and normalise. Returns a report:
      rows          usable rows (dicts: email, company_name, website, contact_name)
      new_rows      the usable rows not already in the user's list
      total, invalid, duplicates, already_listed, errors [{row, email, reason}],
      columns       {standard_name: column found in the file}"""
    frame = read_table(filename, content)
    mapping = _map_columns(list(frame.columns))
    if REQUIRED not in mapping.values():
        found = ", ".join(str(c) for c in frame.columns) or "none"
        raise ImportFailure(f"No email column found. The file needs a header row with a column "
                            f"named 'email'. Columns found: {found}.")
    frame = frame.rename(columns=mapping)[[c for c in mapping.values()]]
    frame = frame.fillna("")

    existing = existing_emails or set()
    rows, errors, seen = [], [], set()
    invalid = duplicates = already = 0
    for index, record in enumerate(frame.to_dict("records"), start=2):  # row 1 is the header
        email = str(record.get("email", "")).strip().lower()
        if not email:
            invalid += 1
            if len(errors) < MAX_ERRORS_SHOWN:
                errors.append({"row": index, "email": "", "reason": "no email address"})
            continue
        if not is_valid_email(email):
            invalid += 1
            if len(errors) < MAX_ERRORS_SHOWN:
                errors.append({"row": index, "email": email[:80], "reason": "not a valid email address"})
            continue
        if email in seen:
            duplicates += 1
            continue
        seen.add(email)

        company = str(record.get("company_name", "")).strip()
        if not company and record.get("attributes"):
            company = extract_company_name(str(record["attributes"]), email)
        company = company or _company_name_from_domain(email)

        website = str(record.get("website", "")).strip()
        if website and not _URL_RE.match(website):
            if len(errors) < MAX_ERRORS_SHOWN:
                errors.append({"row": index, "email": email, "reason": f"website '{website[:60]}' "
                               "doesn't look like a web address — it will be guessed from the email"})
            website = ""
        website = website or derive_website_from_email(email)
        if website and not website.lower().startswith(("http://", "https://")):
            website = "https://" + website

        rows.append({"email": email, "company_name": company[:200], "website": website[:300],
                     "contact_name": str(record.get("contact_name", "")).strip()[:120]})
        if email in existing:
            already += 1

    return {
        "rows": rows,
        "new_rows": [r for r in rows if r["email"] not in existing],
        "total": len(frame),
        "invalid": invalid,
        "duplicates": duplicates,
        "already_listed": already,
        "errors": errors,
        "columns": {std: orig for orig, std in mapping.items()},
    }


TEMPLATE_CSV = ("company_name,email,website,contact_name\n"
                "Example Security Labs,hr@example-security.com,https://example-security.com,Jane Doe\n"
                "Acme Cloud Systems,careers@acmecloud.io,https://acmecloud.io,\n"
                "Nimbus Data,jobs@nimbusdata.co,,\n")
