"""
Mail service — clean wrapper around mailer.py.

Returns structured results instead of raising raw exceptions.
This decouples the send logic from Gmail-specific exception handling,
so the dashboard/sender worker don't need to understand SMTP error codes.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from mailer import send_email, PermanentSendError, TransientSendError, AuthenticationError


@dataclass
class SendResult:
    success: bool
    retryable: bool = False
    error_code: str = ""
    message: str = ""
    message_id: str = ""
    provider: str = ""


def send(app: dict) -> SendResult:
    """Send one email for an application dict (from db.get_application_by_id).

    Returns a SendResult — never raises an exception to the caller.
    """
    load_dotenv(override=True)
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    cv_path = os.getenv("CV_FILE_PATH", "")

    if not app.get("subject") or not app.get("body"):
        return SendResult(
            success=False, retryable=False,
            error_code="no_draft", message="No email draft (missing subject or body)."
        )

    if not cv_path:
        return SendResult(
            success=False, retryable=False,
            error_code="no_cv", message="CV_FILE_PATH not set in .env."
        )

    # Detect which provider will be used
    provider = "smtp"
    try:
        from google_auth_helper import token_exists, get_credentials
        if token_exists() and get_credentials() is not None:
            provider = "gmail_api"
    except ImportError:
        pass

    try:
        send_email(
            gmail_address, gmail_password,
            app["email"], app["subject"], app["body"], cv_path
        )
        return SendResult(
            success=True,
            message=f"Sent to {app['email']}",
            provider=provider
        )
    except AuthenticationError as e:
        return SendResult(
            success=False, retryable=False,
            error_code="auth_failed", message=str(e), provider=provider
        )
    except PermanentSendError as e:
        return SendResult(
            success=False, retryable=False,
            error_code="permanent", message=str(e), provider=provider
        )
    except TransientSendError as e:
        return SendResult(
            success=False, retryable=True,
            error_code="transient", message=str(e), provider=provider
        )
    except Exception as e:
        return SendResult(
            success=False, retryable=True,
            error_code="unknown", message=str(e), provider=provider
        )
