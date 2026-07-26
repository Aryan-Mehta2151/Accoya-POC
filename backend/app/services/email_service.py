"""SMTP transport for password resets and approved outreach delivery."""

from __future__ import annotations

import smtplib
import socket
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from app.config import Settings, get_settings


class EmailDeliveryFailure(RuntimeError):
    """The SMTP relay definitively did not accept the message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EmailDeliveryUnknown(RuntimeError):
    """SMTP may have accepted the message before the connection failed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def smtp_is_configured(settings: Settings | None = None) -> bool:
    """Return whether the settings can authenticate to an SMTP relay."""

    configured = settings or get_settings()
    return bool(
        configured.smtp_host.strip()
        and configured.smtp_port > 0
        and configured.smtp_email.strip()
        and configured.smtp_password.strip()
        and configured.smtp_timeout_seconds > 0
    )


def send_outreach_email(
    *,
    sender_email: str,
    recipient_email: str,
    subject: str,
    body: str,
    message_id: str,
    settings: Settings | None = None,
) -> None:
    """Deliver one plain-text outreach message or raise a typed outcome."""

    configured = settings or get_settings()
    if not smtp_is_configured(configured):
        raise EmailDeliveryFailure("smtp_not_configured")
    if sender_email.strip().casefold() != configured.smtp_email.strip().casefold():
        raise EmailDeliveryFailure("smtp_sender_configuration_changed")

    message = EmailMessage()
    try:
        message["From"] = sender_email.strip()
        message["To"] = recipient_email.strip()
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = message_id
        message.set_content(body)
    except (TypeError, ValueError) as exc:
        raise EmailDeliveryFailure("invalid_email_message") from exc

    _deliver_message(
        message,
        sender_email=sender_email.strip(),
        recipient_email=recipient_email.strip(),
        settings=configured,
    )


def _deliver_message(
    message: EmailMessage,
    *,
    sender_email: str,
    recipient_email: str,
    settings: Settings,
) -> None:
    """Perform the SMTP conversation with conservative ambiguity handling."""

    server: smtplib.SMTP | None = None
    accepted = False
    try:
        try:
            server = smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(settings.smtp_email, settings.smtp_password)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailDeliveryFailure("smtp_authentication_failed") from exc
        except (
            smtplib.SMTPConnectError,
            smtplib.SMTPHeloError,
            smtplib.SMTPNotSupportedError,
        ) as exc:
            raise EmailDeliveryFailure("smtp_connection_rejected") from exc
        except (socket.timeout, TimeoutError, OSError) as exc:
            raise EmailDeliveryFailure("smtp_connection_failed") from exc
        except smtplib.SMTPException as exc:
            raise EmailDeliveryFailure("smtp_setup_failed") from exc

        try:
            refused = server.send_message(
                message,
                from_addr=sender_email,
                to_addrs=[recipient_email],
            )
            if refused:
                raise EmailDeliveryFailure("smtp_recipient_refused")
            accepted = True
        except EmailDeliveryFailure:
            raise
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
        ) as exc:
            raise EmailDeliveryFailure("smtp_message_rejected") from exc
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, OSError) as exc:
            raise EmailDeliveryUnknown("smtp_submission_interrupted") from exc
        except smtplib.SMTPResponseException as exc:
            raise EmailDeliveryFailure("smtp_message_rejected") from exc
        except smtplib.SMTPException as exc:
            raise EmailDeliveryUnknown("smtp_submission_uncertain") from exc

        # Once DATA was accepted, a QUIT failure must not turn success into an
        # automatic retry candidate.
        try:
            server.quit()
        except Exception:
            pass
    finally:
        if server is not None and not accepted:
            try:
                server.close()
            except Exception:
                pass


def send_password_reset_email(recipient_email: str, reset_link: str) -> bool:
    """Send a password-reset email while preserving the legacy bool contract."""

    settings = get_settings()
    if not smtp_is_configured(settings):
        return False

    message = EmailMessage()
    try:
        message["From"] = settings.smtp_email.strip()
        message["To"] = recipient_email.strip()
        message["Subject"] = "Reset Your Password - Accoya"
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid(domain=settings.smtp_email.split("@")[-1])
        message.set_content(
            "We received a request to reset your Accoya password. Use this "
            f"link within {settings.password_reset_token_expire_minutes} minutes:\n"
            f"{reset_link}"
        )
        message.add_alternative(
            f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto;">
                        <h2 style="color: #2c3e50;">Password Reset Request</h2>
                        <p>Hi there,</p>
                        <p>We received a request to reset your password. Click the link below to create a new password:</p>
                        <p style="margin: 30px 0;">
                            <a href="{reset_link}" style="display: inline-block; padding: 12px 30px; background-color: #3498db; color: white; text-decoration: none; border-radius: 5px;">
                                Reset Password
                            </a>
                        </p>
                        <p style="color: #7f8c8d; font-size: 14px;">Or copy and paste this link in your browser:</p>
                        <p style="color: #3498db; word-break: break-all; font-size: 12px;">{reset_link}</p>
                        <p style="color: #7f8c8d; font-size: 14px;">This link expires in {settings.password_reset_token_expire_minutes} minutes.</p>
                        <p style="color: #7f8c8d; font-size: 14px;">If you didn't request this, please ignore this email.</p>
                        <hr style="border: none; border-top: 1px solid #ecf0f1; margin: 20px 0;">
                        <p style="color: #95a5a6; font-size: 12px;">© 2026 Accoya. All rights reserved.</p>
                    </div>
                </body>
            </html>
            """,
            subtype="html",
        )
        _deliver_message(
            message,
            sender_email=settings.smtp_email.strip(),
            recipient_email=recipient_email.strip(),
            settings=settings,
        )
        return True
    except Exception:
        return False
