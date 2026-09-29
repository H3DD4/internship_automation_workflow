"""Companies-file loading, greeting rules, and send error classification."""

import smtplib
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


def _parse(text: str, existing=None):
    import company_import
    return company_import.parse("companies.csv", text.encode("utf-8"), existing)


def test_duplicate_emails_differing_in_case_are_merged():
    """Regression: dedup ran before normalization, so one mailbox could
    receive two applications."""
    report = _parse("company_name,email,website\n"
                    "Acme,Jobs@Acme.com,https://acme.com\n"
                    "Acme Again, jobs@acme.com ,https://acme.com\n"
                    "Other,hello@other.com,https://other.com\n")
    assert sorted(r["email"] for r in report["rows"]) == ["hello@other.com", "jobs@acme.com"]
    assert report["duplicates"] == 1


def test_invalid_emails_are_dropped_and_reported_by_row():
    report = _parse("company_name,email\nGood,ok@x.com\nBad,not-an-email\nEmpty,\n")
    assert [r["email"] for r in report["rows"]] == ["ok@x.com"]
    assert report["invalid"] == 2
    assert {e["row"] for e in report["errors"]} == {3, 4}


def test_company_name_derived_from_domain_when_missing():
    report = _parse("email\nhr@redpoint-security.com\n")
    assert report["rows"][0]["company_name"] == "Redpoint Security"
    assert report["rows"][0]["website"] == "https://redpoint-security.com"


def test_french_column_names_and_semicolons_are_understood():
    report = _parse("Entreprise;Adresse email;Site web;Nom\nSociété X;rh@x.fr;x.fr;Jeanne\n")
    row = report["rows"][0]
    assert row == {"email": "rh@x.fr", "company_name": "Société X", "website": "https://x.fr",
                   "contact_name": "Jeanne"}


def test_a_file_without_an_email_column_explains_itself():
    import company_import
    with pytest.raises(company_import.ImportFailure, match="email"):
        _parse("company,phone\nAcme,123\n")


def test_rows_already_in_the_list_are_not_new():
    report = _parse("email\na@x.com\nb@x.com\n", existing={"a@x.com"})
    assert report["already_listed"] == 1
    assert [r["email"] for r in report["new_rows"]] == ["b@x.com"]


def test_a_fake_excel_file_is_refused():
    import company_import
    with pytest.raises(company_import.ImportFailure):
        company_import.parse("companies.xlsx", b"not a zip at all")


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


def test_mail_service_reports_missing_draft(user_id):
    from user_config import UserConfig
    result = mail_service.send(UserConfig(user_id), {"email": "x@test.com", "subject": "", "body": ""})
    assert result.success is False
    assert result.error_code == "no_draft"


def test_mail_service_needs_a_cv(user_id):
    from user_config import UserConfig
    result = mail_service.send(UserConfig(user_id), {"email": "x@test.com", "subject": "S", "body": "B"})
    assert result.success is False and result.error_code == "no_cv"


def test_mail_service_sends_with_the_users_app_password(user_id):
    from user_config import UserConfig
    cfg = UserConfig(user_id)
    cfg.save_cv("My CV.pdf", b"%PDF-1.4 cv")
    cfg.set_many({"MAIL_METHOD": "app_password", "GMAIL_ADDRESS": "me@gmail.com", "YOUR_NAME": "Me Myself"})
    cfg.set_secret("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    with patch("mailer.send_via_smtp", return_value="<id@mail>") as send:
        result = mail_service.send(UserConfig(user_id), {"email": "to@x.com", "subject": "S", "body": "B"})
    assert result.success is True
    assert result.message_id == "<id@mail>"
    msg = send.call_args.args[0]
    assert send.call_args.kwargs["password"] == "abcdefghijklmnop"
    assert msg["From"] == "Me Myself <me@gmail.com>"
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "My CV.pdf"
    assert attachment.get_content() == b"%PDF-1.4 cv"


def test_mail_service_refuses_a_private_smtp_server_in_production(user_id, monkeypatch):
    from user_config import UserConfig
    monkeypatch.setenv("APP_ENV", "production")
    cfg = UserConfig(user_id)
    cfg.save_cv("cv.pdf", b"%PDF-1.4 cv")
    cfg.set_many({"MAIL_METHOD": "smtp", "GMAIL_ADDRESS": "me@corp.com", "SMTP_HOST": "127.0.0.1",
                  "SMTP_PORT": "25"})
    cfg.set_secret("GMAIL_APP_PASSWORD", "pw")
    result = mail_service.send(UserConfig(user_id), {"email": "to@x.com", "subject": "S", "body": "B"})
    assert result.success is False and result.error_code == "auth_failed"
    assert "private" in result.message
