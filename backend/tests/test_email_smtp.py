"""Offline tests for the shared SMTP transport."""

from __future__ import annotations

import smtplib
import socket
import unittest
from email.message import EmailMessage
from unittest.mock import patch

from app.config import Settings
from app.services import email_service


class FakeSMTP:
    """Small SMTP double that records the conversation and injects failures."""

    def __init__(self) -> None:
        self.ehlo_count = 0
        self.starttls_context = None
        self.login_args: tuple[str, str] | None = None
        self.send_args: tuple[EmailMessage, str, list[str]] | None = None
        self.quit_called = False
        self.close_called = False
        self.login_error: Exception | None = None
        self.send_error: Exception | None = None
        self.quit_error: Exception | None = None
        self.refused: dict[str, object] = {}

    def ehlo(self) -> None:
        self.ehlo_count += 1

    def starttls(self, *, context: object) -> None:
        self.starttls_context = context

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)
        if self.login_error is not None:
            raise self.login_error

    def send_message(
        self,
        message: EmailMessage,
        *,
        from_addr: str,
        to_addrs: list[str],
    ) -> dict[str, object]:
        self.send_args = (message, from_addr, to_addrs)
        if self.send_error is not None:
            raise self.send_error
        return self.refused

    def quit(self) -> None:
        self.quit_called = True
        if self.quit_error is not None:
            raise self.quit_error

    def close(self) -> None:
        self.close_called = True


class EmailSmtpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_username=None,
            smtp_password="offline-secret",
            smtp_timeout_seconds=12,
        )

    def _send_with(
        self,
        server: FakeSMTP,
        *,
        settings: Settings | None = None,
    ) -> None:
        with patch.object(
            email_service.smtplib,
            "SMTP",
            return_value=server,
        ) as smtp_factory:
            email_service.send_outreach_email(
                sender_email="sender@example.com",
                recipient_email="architect@example.com",
                subject="Accoya technical review",
                body=(
                    "Hello Team,\n\n"
                    "Thank you for reviewing the proposal.\n\n"
                    + email_service.DEFAULT_US_EMAIL_SIGNATURE
                ),
                message_id="<stable-job-id@accoya-outreach.local>",
                settings=settings or self.settings,
            )
        smtp_factory.assert_called_once_with(
            "smtp.example.com",
            587,
            timeout=12,
        )

    def test_distinct_login_username_does_not_change_sender(self) -> None:
        settings = self.settings.model_copy(
            update={"smtp_username": "  relay-user@example.net  "}
        )
        server = FakeSMTP()

        self._send_with(server, settings=settings)

        self.assertEqual(
            server.login_args,
            ("relay-user@example.net", "offline-secret"),
        )
        message, sender, recipients = server.send_args
        self.assertEqual(sender, "sender@example.com")
        self.assertEqual(recipients, ["architect@example.com"])
        self.assertEqual(message["From"], "sender@example.com")

    def test_blank_login_username_falls_back_to_sender(self) -> None:
        settings = self.settings.model_copy(update={"smtp_username": "  \t "})
        server = FakeSMTP()

        self._send_with(server, settings=settings)

        self.assertEqual(
            server.login_args,
            ("sender@example.com", "offline-secret"),
        )

    def test_outreach_uses_tls_login_exact_envelope_and_headers(self) -> None:
        server = FakeSMTP()
        self._send_with(server)

        self.assertEqual(server.ehlo_count, 2)
        self.assertIsNotNone(server.starttls_context)
        self.assertEqual(
            server.login_args,
            ("sender@example.com", "offline-secret"),
        )
        self.assertTrue(server.quit_called)
        self.assertFalse(server.close_called)
        message, sender, recipients = server.send_args
        self.assertEqual(sender, "sender@example.com")
        self.assertEqual(recipients, ["architect@example.com"])
        self.assertEqual(message["From"], "sender@example.com")
        self.assertEqual(message["To"], "architect@example.com")
        self.assertEqual(message["Subject"], "Accoya technical review")
        self.assertEqual(
            message["Message-ID"],
            "<stable-job-id@accoya-outreach.local>",
        )
        self.assertIsNotNone(message["Date"])
        self.assertEqual(
            message.get_body(preferencelist=("plain",)).get_content().rstrip("\n"),
            (
                "Hello Team,\n\n"
                "Thank you for reviewing the proposal.\n\n"
                + email_service.DEFAULT_US_EMAIL_SIGNATURE
            ),
        )
        html_part = message.get_body(preferencelist=("html",))
        self.assertIsNotNone(html_part)
        html_body = html_part.get_content()
        self.assertIn("Doug Gillikin", html_body)
        self.assertIn("Specification Manager (Associate AIA)", html_body)
        self.assertIn("Kingsport, TN 37660-5147", html_body)
        self.assertIn("border-left:4px solid #0f766e", html_body)
        self.assertIn("width:44px;height:2px;background:#5f6b66", html_body)
        self.assertIn("Thank you for reviewing the proposal.", html_body)
        self.assertIn('padding:0 20px', html_body)
        self.assertNotIn('max-width:600px', html_body)

    def test_unsigned_non_us_body_keeps_its_closing_as_normal_content(self) -> None:
        html_body = email_service._render_outreach_html(
            "Beste team,\n\nDank voor uw tijd.\n\nMet vriendelijke groet,"
        )

        self.assertIn(
            '<p style="margin:0 0 16px;">Met vriendelijke groet,</p>',
            html_body,
        )
        self.assertNotIn("border-left:4px solid", html_body)
        self.assertNotIn("width:44px;height:2px", html_body)
        self.assertIn('padding:0 20px', html_body)
        self.assertNotIn('max-width:600px', html_body)

    def test_authentication_and_connection_failures_are_definite(self) -> None:
        auth_server = FakeSMTP()
        auth_server.login_error = smtplib.SMTPAuthenticationError(
            535,
            b"authentication failed",
        )
        with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
            self._send_with(auth_server)
        self.assertEqual(raised.exception.code, "smtp_authentication_failed")
        self.assertTrue(auth_server.close_called)

        with patch.object(
            email_service.smtplib,
            "SMTP",
            side_effect=socket.timeout(),
        ):
            with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
                email_service.send_outreach_email(
                    sender_email="sender@example.com",
                    recipient_email="architect@example.com",
                    subject="Subject",
                    body="Body",
                    message_id="<connection-test@accoya-outreach.local>",
                    settings=self.settings,
                )
        self.assertEqual(raised.exception.code, "smtp_connection_failed")

    def test_explicit_submission_rejection_is_definite(self) -> None:
        server = FakeSMTP()
        server.send_error = smtplib.SMTPDataError(550, b"rejected")
        with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
            self._send_with(server)
        self.assertEqual(raised.exception.code, "smtp_message_rejected")
        self.assertTrue(server.close_called)

    def test_disconnect_or_timeout_during_submission_is_unknown(self) -> None:
        for error in (
            smtplib.SMTPServerDisconnected("connection lost"),
            socket.timeout(),
        ):
            with self.subTest(error=type(error).__name__):
                server = FakeSMTP()
                server.send_error = error
                with self.assertRaises(
                    email_service.EmailDeliveryUnknown
                ) as raised:
                    self._send_with(server)
                self.assertEqual(
                    raised.exception.code,
                    "smtp_submission_interrupted",
                )
                self.assertTrue(server.close_called)

    def test_quit_failure_after_data_acceptance_still_succeeds(self) -> None:
        server = FakeSMTP()
        server.quit_error = smtplib.SMTPServerDisconnected("quit failed")
        self._send_with(server)
        self.assertTrue(server.quit_called)
        self.assertFalse(server.close_called)

    def test_invalid_message_headers_fail_before_connecting(self) -> None:
        with patch.object(email_service.smtplib, "SMTP") as smtp_factory:
            with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
                email_service.send_outreach_email(
                    sender_email="sender@example.com",
                    recipient_email="architect@example.com",
                    subject="Injected\r\nBcc: target@example.com",
                    body="Body",
                    message_id="<header-test@accoya-outreach.local>",
                    settings=self.settings,
                )
        self.assertEqual(raised.exception.code, "invalid_email_message")
        smtp_factory.assert_not_called()

    def test_password_reset_wrapper_preserves_legacy_boolean_contract(self) -> None:
        observed: dict[str, object] = {}

        def deliver(
            message: EmailMessage,
            *,
            sender_email: str,
            recipient_email: str,
            settings: Settings,
        ) -> None:
            observed.update(
                message=message,
                sender_email=sender_email,
                recipient_email=recipient_email,
                settings=settings,
            )

        with (
            patch.object(email_service, "get_settings", return_value=self.settings),
            patch.object(email_service, "_deliver_message", side_effect=deliver),
        ):
            sent = email_service.send_password_reset_email(
                "reviewer@example.com",
                "https://app.example.com/reset?token=offline",
            )
        self.assertTrue(sent)
        self.assertEqual(observed["sender_email"], "sender@example.com")
        self.assertEqual(observed["recipient_email"], "reviewer@example.com")
        self.assertEqual(
            observed["message"]["Subject"],
            "Reset Your Password - Accoya",
        )

        with (
            patch.object(email_service, "get_settings", return_value=self.settings),
            patch.object(
                email_service,
                "_deliver_message",
                side_effect=email_service.EmailDeliveryUnknown(
                    "smtp_submission_uncertain"
                ),
            ),
        ):
            self.assertFalse(
                email_service.send_password_reset_email(
                    "reviewer@example.com",
                    "https://app.example.com/reset?token=offline",
                )
            )

    def test_access_review_uses_configured_sender_and_approver_recipient(self) -> None:
        observed: dict[str, object] = {}

        def deliver(
            message: EmailMessage,
            *,
            sender_email: str,
            recipient_email: str,
            settings: Settings,
        ) -> None:
            observed.update(
                message=message,
                sender_email=sender_email,
                recipient_email=recipient_email,
                settings=settings,
            )

        with (
            patch.object(email_service, "get_settings", return_value=self.settings),
            patch.object(email_service, "_deliver_message", side_effect=deliver),
        ):
            sent = email_service.send_access_request_review_email(
                approver_email="approver@example.com",
                requester_email="requester@example.com",
                requester_name="Requesting User",
                approve_link="https://app.example.com/access/approve?token=offline",
                reject_link="https://app.example.com/access/reject?token=offline",
            )

        self.assertTrue(sent)
        self.assertEqual(observed["sender_email"], "sender@example.com")
        self.assertEqual(observed["recipient_email"], "approver@example.com")
        self.assertEqual(observed["message"]["From"], "sender@example.com")
        self.assertEqual(observed["message"]["To"], "approver@example.com")

    def test_access_decisions_use_configured_sender_and_requester_recipient(
        self,
    ) -> None:
        for approved in (True, False):
            with self.subTest(approved=approved):
                observed: dict[str, object] = {}

                def deliver(
                    message: EmailMessage,
                    *,
                    sender_email: str,
                    recipient_email: str,
                    settings: Settings,
                ) -> None:
                    observed.update(
                        message=message,
                        sender_email=sender_email,
                        recipient_email=recipient_email,
                        settings=settings,
                    )

                with (
                    patch.object(
                        email_service,
                        "get_settings",
                        return_value=self.settings,
                    ),
                    patch.object(
                        email_service,
                        "_deliver_message",
                        side_effect=deliver,
                    ),
                ):
                    sent = email_service.send_access_request_decision_email(
                        recipient_email="requester@example.com",
                        approved=approved,
                        reset_link=(
                            "https://app.example.com/reset?token=offline"
                            if approved
                            else None
                        ),
                    )

                self.assertTrue(sent)
                self.assertEqual(observed["sender_email"], "sender@example.com")
                self.assertEqual(
                    observed["recipient_email"],
                    "requester@example.com",
                )
                self.assertEqual(
                    observed["message"]["From"],
                    "sender@example.com",
                )
                self.assertEqual(
                    observed["message"]["To"],
                    "requester@example.com",
                )


if __name__ == "__main__":
    unittest.main()
