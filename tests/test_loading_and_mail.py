"""Companies-file loading, greeting rules, and send error classification."""

import smtplib
from pathlib import Path
from unittest.mock import patch

import pytest

import mail_service
from mailer import (
    AuthenticationError,
    PermanentSendError,
    TransientSendError,
    _classify_oauth_error,
    _classify_smtp_error,
)
from utils import build_greeting, derive_website_from_email, is_valid_email


def test_duplicate_emails_differing_in_case_are_merged(tmp_path):
    """Regression: dedup ran before normalization, so one mailbox could
    receive two applications."""
    from main import load_companies

    csv_path = tmp_path / "companies.csv"
    csv_path.write_text(
        "company_name,email,website\n"
        "Acme,Jobs@Acme.com,https://acme.com\n"
        "Acme Again, jobs@acme.com ,https://acme.com\n"
        "Other,hello@other.com,https://other.com\n"
    )
    with patch("builtins.print"):
        df = load_companies(str(csv_path))

    assert len(df) == 2
    assert sorted(df["email"]) == ["hello@other.com", "jobs@acme.com"]


def test_invalid_emails_are_dropped(tmp_path):
    from main import load_companies

    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("company_name,email\nGood,ok@x.com\nBad,not-an-email\nEmpty,\n")
    with patch("builtins.print"):
        df = load_companies(str(csv_path))
    assert list(df["email"]) == ["ok@x.com"]


def test_company_name_derived_from_domain_when_missing(tmp_path):
    from main import load_companies

    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("email\nhr@redpoint-security.com\n")
    with patch("builtins.print"):
        df = load_companies(str(csv_path))
    assert df.iloc[0]["company_name"] == "Redpoint Security"


def test_personal_domains_are_not_scraped_as_company_sites():
    assert derive_website_from_email("someone@gmail.com") == ""
    assert derive_website_from_email("hr@acme.io") == "https://acme.io"


@pytest.mark.parametrize("contact,company,expected", [
    ("Charly Dupont", "Rtone", "Dear Charly,"),
    ("recruiting", "Rtone", "Dear Rtone Team,"),
    ("", "Rtone", "Dear Rtone Team,"),
    ("Rtone", "Rtone", "Dear Rtone Team,"),
    ("hr@rtone.fr", "Rtone", "Dear Rtone Team,"),
])
def test_greeting_rules(contact, company, expected):
    assert build_greeting(contact, company) == expected


def test_is_valid_email_handles_non_strings():
    assert is_valid_email(None) is False
    assert is_valid_email(12345) is False
    assert is_valid_email("a@b.co") is True


def test_smtp_auth_error_is_not_mistaken_for_transient():
    """SMTPAuthenticationError subclasses SMTPResponseException; if the
    generic branch catches it first, every company is marked retry_later
    instead of surfacing a credentials problem."""
    error = smtplib.SMTPAuthenticationError(535, b"bad creds")
    assert isinstance(_classify_smtp_error(error), AuthenticationError)


def test_smtp_recipient_rejection_is_permanent():
    error = smtplib.SMTPResponseException(550, b"no such user")
    assert isinstance(_classify_smtp_error(error), PermanentSendError)


def test_smtp_server_busy_is_transient():
    error = smtplib.SMTPResponseException(421, b"try later")
    assert isinstance(_classify_smtp_error(error), TransientSendError)


class FakeHttpError(Exception):
    """Mimics googleapiclient.errors.HttpError's shape."""
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


@pytest.mark.parametrize("status,expected", [
    (401, AuthenticationError),
    (403, AuthenticationError),
    (400, PermanentSendError),
    (429, TransientSendError),
    (503, TransientSendError),
])
def test_oauth_errors_are_classified_by_status_not_message(status, expected):
    """Regression: any message containing "token"/"credentials" was treated
    as an account-wide auth failure, halting the whole job over what was
    often just one bad recipient."""
    import googleapiclient.errors as gerrors

    # _classify_oauth_error imports HttpError at call time, so patching the
    # module attribute makes it recognise our stand-in.
    with patch.object(gerrors, "HttpError", FakeHttpError):
        assert isinstance(_classify_oauth_error(FakeHttpError(status)), expected)


def test_unknown_oauth_error_defaults_to_transient():
    assert isinstance(_classify_oauth_error(RuntimeError("something odd")), TransientSendError)


def test_mail_service_reports_missing_draft():
    result = mail_service.send({"email": "x@test.com", "subject": "", "body": ""})
    assert result.success is False
    assert result.error_code == "no_draft"


def test_mail_service_passes_message_id_through(tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("CV_FILE_PATH", str(cv))
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app pass")

    with patch("mail_service.load_dotenv"), \
         patch("mail_service.send_email", return_value="<id@mail>") as mock_send:
        result = mail_service.send({"email": "to@x.com", "subject": "S", "body": "B"})

    assert result.success is True
    assert result.message_id == "<id@mail>"
    assert mock_send.called
