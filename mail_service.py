"""
Mail service — sends one application for one user, and never raises.

Resolves the user's sending method, credentials and CV, then hands a built
message to mailer.py. The result is structured, so the sender worker never
has to understand SMTP codes or Google errors.
"""

from dataclasses import dataclass

import mailer
from mailer import AuthenticationError, PermanentSendError, TransientSendError
from user_config import UserConfig

PROVIDER_LABELS = {"oauth": "Gmail (Google sign-in)", "app_password": "Gmail (app password)",
                   "smtp": "SMTP"}


@dataclass
class SendResult:
    success: bool
    retryable: bool = False
    error_code: str = ""
    message: str = ""
    message_id: str = ""
    provider: str = ""


def sending_method(cfg: UserConfig) -> str:
    """The method in use: the one chosen in Settings, else whatever is set up."""
    import google_auth_helper
    chosen = cfg.get("MAIL_METHOD")
    if chosen in PROVIDER_LABELS:
        return chosen
    if google_auth_helper.token_exists(cfg):
        return "oauth"
    if cfg.get("SMTP_HOST") and cfg.has_secret("GMAIL_APP_PASSWORD"):
        return "smtp"
    if cfg.get("GMAIL_ADDRESS") and cfg.has_secret("GMAIL_APP_PASSWORD"):
        return "app_password"
    return ""


def smtp_settings(cfg: UserConfig, method: str) -> dict:
    if method == "app_password":
        host, port, security = mailer.GMAIL_SMTP
        return {"host": host, "port": port, "security": security,
                "username": cfg.get("GMAIL_ADDRESS"),
                "password": cfg.secret("GMAIL_APP_PASSWORD").replace(" ", "")}
    port = cfg.get("SMTP_PORT")
    return {"host": cfg.get("SMTP_HOST"), "port": int(port) if port.isdigit() else 465,
            "security": cfg.get("SMTP_SECURITY", "ssl"),
            "username": cfg.get("SMTP_USERNAME") or cfg.get("GMAIL_ADDRESS"),
            "password": cfg.secret("GMAIL_APP_PASSWORD")}


def display_name(cfg: UserConfig) -> str:
    import profiles
    facts = (profiles.load(cfg.user_id) or {}).get("facts") or {}
    return (facts.get("full_name") or cfg.get("YOUR_NAME") or "").strip()


def from_address(cfg: UserConfig, method: str) -> str:
    import google_auth_helper
    if method == "oauth":
        return google_auth_helper.get_authorized_email(cfg) or cfg.get("GMAIL_ADDRESS")
    return cfg.get("GMAIL_ADDRESS") or cfg.get("SMTP_USERNAME")


def send(cfg: UserConfig, app: dict) -> SendResult:
    """Send one email for an application row. Never raises."""
    if not app.get("subject") or not app.get("body"):
        return SendResult(False, error_code="no_draft",
                          message="No email draft (missing subject or body).")
    cv = cfg.cv()
    if not cv:
        return SendResult(False, error_code="no_cv", message="Upload your CV in Settings before sending.")
    method = sending_method(cfg)
    if not method:
        return SendResult(False, error_code="auth_failed",
                          message="No email account connected — connect one in Settings.")
    provider = PROVIDER_LABELS[method]

    try:
        sender = from_address(cfg, method)
        if not sender:
            raise AuthenticationError("No sender address — reconnect your email account in Settings.")
        msg = mailer.build_message(from_address=sender, display_name=display_name(cfg),
                                   to_email=app["email"], subject=app["subject"], body=app["body"],
                                   attachment=cv)
        if method == "oauth":
            import google_auth_helper
            credentials = google_auth_helper.get_credentials(cfg)
            if credentials is None:
                raise AuthenticationError("Google sign-in expired or was revoked — sign in with "
                                          "Google again in Settings.")
            message_id = mailer.send_via_gmail_api(msg, credentials)
        else:
            settings = smtp_settings(cfg, method)
            if not (settings["host"] and settings["username"] and settings["password"]):
                raise AuthenticationError("Email login incomplete — finish it in Settings.")
            import safe_http
            try:
                safe_http.check_host(settings["host"], settings["port"])
            except safe_http.BlockedURL as exc:
                raise AuthenticationError(str(exc)) from exc
            message_id = mailer.send_via_smtp(msg, **settings)
        return SendResult(True, message=f"Sent to {app['email']}", message_id=message_id or "",
                          provider=provider)
    except AuthenticationError as e:
        return SendResult(False, error_code="auth_failed", message=str(e), provider=provider)
    except PermanentSendError as e:
        return SendResult(False, error_code="permanent", message=str(e), provider=provider)
    except TransientSendError as e:
        return SendResult(False, retryable=True, error_code="transient", message=str(e), provider=provider)
    except Exception as e:
        return SendResult(False, retryable=True, error_code="unknown", message=str(e)[:300], provider=provider)
