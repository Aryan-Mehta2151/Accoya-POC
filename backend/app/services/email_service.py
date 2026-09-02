"""SMTP transport for password resets and approved outreach delivery."""

from __future__ import annotations

import smtplib
import socket
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html import escape

from app.config import Settings, get_settings
from app.email_signature import (
    DEFAULT_EMAIL_SIGNATURES,
    DEFAULT_US_EMAIL_SIGNATURE,
    signature_html_for,
)


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
    """Deliver one outreach message with plain text and styled HTML."""

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
        message.add_alternative(
            _render_outreach_html(body),
            subtype="html",
        )
    except (TypeError, ValueError) as exc:
        raise EmailDeliveryFailure("invalid_email_message") from exc

    _deliver_message(
        message,
        sender_email=sender_email.strip(),
        recipient_email=recipient_email.strip(),
        settings=configured,
    )


def _render_outreach_html(body: str) -> str:
    """Render a safe HTML version of the approved outreach content.

    The stored body remains the canonical plain text; this only affects how the
    message is displayed by HTML-capable clients. Known market signatures are
    replaced by their structured HTML signature blocks.
    """

    normalized = body.replace("\r\n", "\n")

    signature_html = ""
    message_text = normalized
    index, marker = max(
        (
            (normalized.rfind(signature.strip()), signature.strip())
            for signature in DEFAULT_EMAIL_SIGNATURES
        ),
        key=lambda match: match[0],
    )
    if index != -1:
        structured = signature_html_for(marker)
        if structured is not None:
            signature_html = structured
            message_text = normalized[:index]

    text_lines = [line.rstrip() for line in message_text.split("\n")]
    while text_lines and not text_lines[-1]:
        text_lines.pop()

    blocks: list[list[str]] = []
    current_block: list[str] = []
    for line in text_lines:
        if line:
            current_block.append(line)
            continue
        if current_block:
            blocks.append(current_block)
            current_block = []
    if current_block:
        blocks.append(current_block)

    body_html_parts = []
    for paragraph in blocks:
        paragraph_html = "<br>".join(escape(line) for line in paragraph)
        body_html_parts.append(
            f"<p style=\"margin:0 0 16px;\">{paragraph_html}</p>"
        )

    body_html = "".join(body_html_parts)
    return (
        "<html><body style=\"margin:0;padding:0;background:#ffffff;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        "style=\"width:100%;border-collapse:collapse;\"><tr><td "
        "style=\"padding:0 20px;font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.65;color:#1f2937;\">"
        f"{body_html}{signature_html}"
        "</td></tr></table></body></html>"
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
            smtp_username = (
                settings.smtp_username.strip()
                if settings.smtp_username and settings.smtp_username.strip()
                else settings.smtp_email.strip()
            )
            server.login(smtp_username, settings.smtp_password)
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


def send_access_request_review_email(
    *,
    approver_email: str,
    requester_email: str,
    requester_name: str | None,
    approve_link: str,
    reject_link: str,
) -> bool:
    """Notify the approver about a pending sign-in request."""

    settings = get_settings()
    if not smtp_is_configured(settings):
        return False

    display_name = requester_name.strip() if requester_name else "Not provided"
    message = EmailMessage()
    try:
        message["From"] = settings.smtp_email.strip()
        message["To"] = approver_email.strip()
        message["Subject"] = "Accoya Access Request Pending Approval"
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid(domain=settings.smtp_email.split("@")[-1])
        message.set_content(
            "A user requested access to Accoya Outreach.\n\n"
            f"Email: {requester_email}\n"
            f"Name: {display_name}\n\n"
            f"Approve: {approve_link}\n"
            f"Reject: {reject_link}\n\n"
            "These links are single-use and expire automatically."
        )
        message.add_alternative(
            f"""
            <html>
                <body style=\"font-family: Arial, sans-serif; line-height: 1.6; color: #1f2937;\">
                    <div style=\"max-width: 620px; margin: 0 auto;\">
                        <h2 style=\"margin-bottom: 8px;\">New Access Request</h2>
                        <p style=\"margin-top: 0; color: #4b5563;\">A user requested access to Accoya Outreach.</p>
                        <p><strong>Email:</strong> {requester_email}</p>
                        <p><strong>Name:</strong> {display_name}</p>
                        <div style=\"margin-top: 24px;\">
                            <a href=\"{approve_link}\" style=\"display: inline-block; margin-right: 12px; padding: 10px 18px; border-radius: 6px; text-decoration: none; background: #065f46; color: white;\">Approve</a>
                            <a href=\"{reject_link}\" style=\"display: inline-block; padding: 10px 18px; border-radius: 6px; text-decoration: none; background: #991b1b; color: white;\">Reject</a>
                        </div>
                        <p style=\"margin-top: 20px; color: #6b7280; font-size: 13px;\">The decision links are single-use and expire automatically.</p>
                    </div>
                </body>
            </html>
            """,
            subtype="html",
        )
        _deliver_message(
            message,
            sender_email=settings.smtp_email.strip(),
            recipient_email=approver_email.strip(),
            settings=settings,
        )
        return True
    except Exception:
        return False


def send_access_request_decision_email(
    *,
    recipient_email: str,
    approved: bool,
    reset_link: str | None = None,
) -> bool:
    """Notify the requester that access was approved or rejected."""

    settings = get_settings()
    if not smtp_is_configured(settings):
        return False

    message = EmailMessage()
    try:
        message["From"] = settings.smtp_email.strip()
        message["To"] = recipient_email.strip()
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid(domain=settings.smtp_email.split("@")[-1])

        if approved:
            message["Subject"] = "Accoya Access Request Approved"
            text_body = (
                "Great news - your access request for Accoya Outreach has been approved.\n\n"
                "Set your account password using the secure link below:\n"
                f"{reset_link or ''}\n\n"
                "For security, this link expires automatically. "
                "If it expires, use Forgot Password on the sign-in page."
            )
            html_body = (
                "<html><body style=\"font-family: Arial, sans-serif; color: #1f2937; background: #f8fafc;\">"
                "<div style=\"max-width: 620px; margin: 24px auto; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 28px;\">"
                "<p style=\"margin:0; color:#0f766e; letter-spacing:0.06em; text-transform:uppercase; font-size:12px;\">Accoya Outreach</p>"
                "<h2 style=\"margin:10px 0 8px;\">Your access request was approved</h2>"
                "<p style=\"margin:0 0 18px; color:#4b5563;\">You're almost done. Set your password to activate sign-in.</p>"
                f"<p style=\"margin:22px 0;\"><a href=\"{reset_link or ''}\" style=\"display:inline-block; padding:12px 20px; border-radius:8px; text-decoration:none; background:#065f46; color:#ffffff; font-weight:600;\">Set Password</a></p>"
                f"<p style=\"font-size:13px; color:#6b7280; word-break:break-all;\">If the button does not work, copy this link:<br>{reset_link or ''}</p>"
                "<p style=\"font-size:13px; color:#6b7280; margin-top:18px;\">For security, this link expires automatically. If needed, use Forgot Password on the sign-in page.</p>"
                "</div></body></html>"
            )
        else:
            message["Subject"] = "Accoya Access Request Update"
            text_body = (
                "Your access request for Accoya Outreach was reviewed but not approved at this time.\n\n"
                "If you think this is a mistake, please contact your administrator."
            )
            html_body = (
                "<html><body style=\"font-family: Arial, sans-serif; color: #1f2937; background: #f8fafc;\">"
                "<div style=\"max-width: 620px; margin: 24px auto; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 28px;\">"
                "<p style=\"margin:0; color:#991b1b; letter-spacing:0.06em; text-transform:uppercase; font-size:12px;\">Accoya Outreach</p>"
                "<h2 style=\"margin:10px 0 8px;\">Access request update</h2>"
                "<p style=\"margin:0; color:#4b5563;\">Your request was reviewed but was not approved at this time. "
                "If you think this is a mistake, please contact your administrator.</p>"
                "</div></body></html>"
            )

        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        _deliver_message(
            message,
            sender_email=settings.smtp_email.strip(),
            recipient_email=recipient_email.strip(),
            settings=settings,
        )
        return True
    except Exception:
        return False
