"""
Which mail service hosts an address — so a student types their email and
Settings shows the one way to connect that will actually work.

University addresses rarely say who runs them: "jdoe@etu.univ-lyon1.fr" may
be Microsoft 365, Google Workspace or the university's own server. The
domain's MX records tell: Microsoft 365 receives mail at
*.mail.protection.outlook.com, Google at *.google.com / googlemail.com.
Everything else gets SMTP, pre-filled for the providers students commonly
use. Only DNS is queried — no connection is made to the domain itself.
"""

from __future__ import annotations

import re

GOOGLE = "google"
MICROSOFT = "microsoft"
SMTP = "smtp"

# Consumer domains, known without a DNS lookup.
_DOMAINS = {
    GOOGLE: {"gmail.com", "googlemail.com"},
    MICROSOFT: {"outlook.com", "hotmail.com", "hotmail.fr", "hotmail.co.uk", "live.com", "live.fr",
                "msn.com", "outlook.fr", "hotmail.de", "hotmail.it", "hotmail.es", "outlook.de"},
}

# SMTP / IMAP for common non-Google, non-Microsoft providers. Most of these
# need an app password when two-step verification is on.
SMTP_PRESETS = {
    "yahoo": {"label": "Yahoo Mail", "match": ("yahoo.", "ymail.com", "rocketmail.com"),
              "mx": ("yahoodns.net",), "smtp": ("smtp.mail.yahoo.com", 465, "ssl"), "imap": "imap.mail.yahoo.com",
              "app_password": "https://login.yahoo.com/account/security"},
    "icloud": {"label": "iCloud Mail", "match": ("icloud.com", "me.com", "mac.com"),
               "mx": ("icloud.com",), "smtp": ("smtp.mail.me.com", 587, "starttls"), "imap": "imap.mail.me.com",
               "app_password": "https://account.apple.com/account/manage"},
    "zoho": {"label": "Zoho Mail", "match": ("zoho.com", "zohomail.com", "zoho.eu"),
             "mx": ("zoho.com", "zoho.eu"), "smtp": ("smtp.zoho.eu", 465, "ssl"), "imap": "imap.zoho.eu",
             "app_password": "https://accounts.zoho.eu/home#security/app_password"},
    "gmx": {"label": "GMX", "match": ("gmx.", "gmx.net"), "mx": ("gmx.net",),
            "smtp": ("mail.gmx.net", 465, "ssl"), "imap": "imap.gmx.net", "app_password": ""},
    "webde": {"label": "WEB.DE", "match": ("web.de",), "mx": ("web.de",),
              "smtp": ("smtp.web.de", 587, "starttls"), "imap": "imap.web.de", "app_password": ""},
    "infomaniak": {"label": "Infomaniak", "match": ("ik.me", "etik.com"), "mx": ("infomaniak.ch",),
                   "smtp": ("mail.infomaniak.com", 465, "ssl"), "imap": "mail.infomaniak.com", "app_password": ""},
    "ovh": {"label": "OVHcloud", "match": (), "mx": ("ovh.net",),
            "smtp": ("ssl0.ovh.net", 465, "ssl"), "imap": "ssl0.ovh.net", "app_password": ""},
    "orange": {"label": "Orange", "match": ("orange.fr", "wanadoo.fr"), "mx": ("orange.fr",),
               "smtp": ("smtp.orange.fr", 465, "ssl"), "imap": "imap.orange.fr", "app_password": ""},
    "laposte": {"label": "La Poste", "match": ("laposte.net",), "mx": ("laposte.net",),
                "smtp": ("smtp.laposte.net", 465, "ssl"), "imap": "imap.laposte.net", "app_password": ""},
    "free": {"label": "Free", "match": ("free.fr",), "mx": ("free.fr",),
             "smtp": ("smtp.free.fr", 465, "ssl"), "imap": "imap.free.fr", "app_password": ""},
    "bluewin": {"label": "Swisscom (bluewin)", "match": ("bluewin.ch",), "mx": ("bluewin.ch",),
                "smtp": ("smtpauths.bluewin.ch", 465, "ssl"), "imap": "imaps.bluewin.ch", "app_password": ""},
    "fastmail": {"label": "Fastmail", "match": ("fastmail.com", "fastmail.fm"), "mx": ("messagingengine.com",),
                 "smtp": ("smtp.fastmail.com", 465, "ssl"), "imap": "imap.fastmail.com",
                 "app_password": "https://app.fastmail.com/settings/security/apps"},
}

_EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def _mx_hosts(domain: str) -> list:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        return sorted((str(r.exchange).rstrip(".").lower() for r in answers))
    except Exception:
        return []


def _txt_records(domain: str) -> str:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "TXT", lifetime=4)
        return " ".join(b"".join(r.strings).decode("utf-8", "ignore") for r in answers).lower()
    except Exception:
        return ""


def detect(email: str, mx_lookup=None, txt_lookup=None) -> dict:
    """{"kind": google|microsoft|smtp, "label", "domain", "smtp": {...} | None,
    "imap_host", "app_password_url", "school": bool}."""
    mx_lookup = mx_lookup or _mx_hosts
    txt_lookup = txt_lookup or _txt_records
    match = _EMAIL_RE.match((email or "").strip())
    if not match:
        return {"kind": "", "label": "", "domain": "", "smtp": None, "imap_host": "", "app_password_url": ""}
    domain = match.group(1).lower()
    result = {"domain": domain, "smtp": None, "imap_host": "", "app_password_url": "",
              "school": bool(re.search(r"(^|\.)(edu|ac\.[a-z]{2})$|univ|etu\.|student|campus|school|ecole|hes|epfl|ethz",
                                       domain))}
    for kind, domains in _DOMAINS.items():
        if domain in domains:
            return {**result, "kind": kind, "label": "Gmail" if kind == GOOGLE else "Outlook.com / Hotmail"}
    for preset in SMTP_PRESETS.values():
        if any(domain == m or domain.startswith(m) or domain.endswith("." + m.strip(".")) for m in preset["match"]):
            return _smtp(result, preset)
    mx = mx_lookup(domain)
    joined = " ".join(mx)
    if "protection.outlook.com" in joined or "outlook.com" in joined:
        return {**result, "kind": MICROSOFT, "label": "Microsoft 365"}
    if "google.com" in joined or "googlemail.com" in joined:
        return {**result, "kind": GOOGLE, "label": "Google Workspace"}
    for preset in SMTP_PRESETS.values():
        if any(any(host.endswith(m) for m in preset["mx"]) for host in mx):
            return _smtp(result, preset)
    # A security gateway (Proofpoint, Mimecast…) in front of the mailboxes
    # hides the host in MX; the domain's SPF record still names who sends.
    txt = txt_lookup(domain)
    if "spf.protection.outlook.com" in txt:
        return {**result, "kind": MICROSOFT, "label": "Microsoft 365"}
    if "_spf.google.com" in txt:
        return {**result, "kind": GOOGLE, "label": "Google Workspace"}
    # The university's own server: the common names, to be confirmed.
    return {**result, "kind": SMTP, "label": "Your organisation's mail server",
            "smtp": {"host": f"smtp.{domain}", "port": 587, "security": "starttls", "guess": True},
            "imap_host": f"imap.{domain}"}


def _smtp(result: dict, preset: dict) -> dict:
    host, port, security = preset["smtp"]
    return {**result, "kind": SMTP, "label": preset["label"],
            "smtp": {"host": host, "port": port, "security": security, "guess": False},
            "imap_host": preset["imap"], "app_password_url": preset["app_password"]}
