"""Offline tests for the shared Microsoft Graph mail transport."""

from __future__ import annotations

import unittest
from email.message import EmailMessage
from unittest.mock import patch

import httpx

from app.config import Settings
from app.email_signature import DEFAULT_NL_EMAIL_SIGNATURE
from app.services import email_service


class EmailGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            microsoft_client_id="client-id",
            microsoft_tenant_id="tenant-id",
            microsoft_client_secret="offline-secret",
            microsoft_sender_email="sender@example.com",
            microsoft_graph_timeout_seconds=12,
        )

    def _send_with(
        self,
        *,
        token_status: int = 200,
        token_body: dict[str, object] | None = None,
        send_status: int = 202,
        send_error: Exception | None = None,
        settings: Settings | None = None,
    ) -> list[tuple[str, dict[str, object]]]:
        calls: list[tuple[str, dict[str, object]]] = []
        token_payload = (
            token_body if token_body is not None else {"access_token": "offline-token"}
        )

        def post(url: str, **kwargs: object) -> httpx.Response:
            calls.append((url, kwargs))
            if url.startswith("https://login.microsoftonline.com/"):
                return httpx.Response(token_status, json=token_payload)
            if send_error is not None:
                raise send_error
            return httpx.Response(send_status)

        with patch.object(email_service.httpx, "post", side_effect=post):
            email_service.send_outreach_email(
                sender_email="sender@example.com",
                recipient_email="architect@example.com",
                subject="Accoya technical review",
                body=(
                    "Hello Team\n\n"
                    "Thank you for reviewing the proposal.\n\n"
                    + email_service.DEFAULT_US_EMAIL_SIGNATURE
                ),
                message_id="<stable-job-id@accoya-outreach.local>",
                settings=settings or self.settings,
            )
        return calls

    def test_outreach_uses_graph_token_and_json_sendmail(self) -> None:
        calls = self._send_with()

        self.assertEqual(len(calls), 2)
        token_url, token_kwargs = calls[0]
        self.assertEqual(
            token_url,
            "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token",
        )
        self.assertEqual(token_kwargs["timeout"], 12)
        self.assertEqual(
            token_kwargs["data"],
            {
                "client_id": "client-id",
                "client_secret": "offline-secret",
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
        )

        send_url, send_kwargs = calls[1]
        self.assertEqual(
            send_url,
            "https://graph.microsoft.com/v1.0/users/sender%40example.com/sendMail",
        )
        self.assertEqual(
            send_kwargs["headers"],
            {
                "Authorization": "Bearer offline-token",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(send_kwargs["timeout"], 12)
        payload = send_kwargs["json"]
        self.assertEqual(
            payload["message"]["subject"],
            "Accoya technical review",
        )
        self.assertEqual(
            payload["message"]["toRecipients"],
            [{"emailAddress": {"address": "architect@example.com"}}],
        )
        self.assertTrue(payload["saveToSentItems"])
        self.assertEqual(
            payload["message"]["body"]["contentType"],
            "HTML",
        )
        self.assertEqual(
            payload["message"]["internetMessageHeaders"],
            [
                {
                    "name": "x-accoya-message-id",
                    "value": "<stable-job-id@accoya-outreach.local>",
                },
            ],
        )
        html_body = payload["message"]["body"]["content"]
        self.assertIn("ARTURO LUGO", html_body)
        self.assertIn("NORTH AMERICA ARCHITECTURE SEGMENT MANAGER", html_body)
        self.assertIn("Kingsport, TN 37660-5147", html_body)
        self.assertIn("border-left:4px solid #0f766e", html_body)
        self.assertIn("width:44px;height:2px;background:#5f6b66", html_body)
        self.assertIn("Thank you for reviewing the proposal.", html_body)
        self.assertIn('padding:0 20px', html_body)
        self.assertNotIn('max-width:600px', html_body)

    def test_nl_signature_uses_the_structured_html_signature(self) -> None:
        html_body = email_service._render_outreach_html(
            "Beste team,\n\nDank voor uw tijd.\n\n"
            + DEFAULT_NL_EMAIL_SIGNATURE
        )

        self.assertIn("LAURA KEILY", html_body)
        self.assertIn("HEAD OF MARKETING", html_body)
        self.assertIn("4th Floor, 3 Moorgate Place, London, EC2R 6EA", html_body)
        self.assertIn("border-left:4px solid #0f766e", html_body)

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

    def test_authentication_failures_are_definite(self) -> None:
        with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
            self._send_with(token_status=401)
        self.assertEqual(
            raised.exception.code,
            "microsoft_graph_authentication_failed",
        )

        with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
            self._send_with(send_status=403)
        self.assertEqual(
            raised.exception.code,
            "microsoft_graph_authentication_failed",
        )

    def test_token_network_failures_are_definite_before_submission(self) -> None:
        with patch.object(
            email_service.httpx,
            "post",
            side_effect=httpx.ConnectError("network unavailable"),
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
        self.assertEqual(
            raised.exception.code,
            "microsoft_graph_auth_request_failed",
        )

    def test_invalid_token_response_is_definite(self) -> None:
        with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
            self._send_with(token_body={})
        self.assertEqual(
            raised.exception.code,
            "microsoft_graph_auth_response_invalid",
        )

    def test_explicit_submission_rejection_is_definite(self) -> None:
        with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
            self._send_with(send_status=400)
        self.assertEqual(raised.exception.code, "microsoft_graph_message_rejected")

    def test_disconnect_or_timeout_during_submission_is_unknown(self) -> None:
        for error in (
            httpx.ConnectError("connection lost"),
            httpx.TimeoutException("timeout"),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(email_service.EmailDeliveryUnknown) as raised:
                    self._send_with(send_error=error)
                self.assertEqual(
                    raised.exception.code,
                    "microsoft_graph_submission_interrupted",
                )

    def test_transient_submission_response_is_unknown(self) -> None:
        with self.assertRaises(email_service.EmailDeliveryUnknown) as raised:
            self._send_with(send_status=503)
        self.assertEqual(
            raised.exception.code,
            "microsoft_graph_submission_uncertain",
        )

    def test_invalid_message_headers_fail_before_calling_graph(self) -> None:
        with patch.object(email_service.httpx, "post") as post:
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
        post.assert_not_called()

    def test_unconfigured_graph_fails_before_calling_graph(self) -> None:
        settings = self.settings.model_copy(update={"microsoft_client_secret": ""})
        with patch.object(email_service.httpx, "post") as post:
            with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
                email_service.send_outreach_email(
                    sender_email="sender@example.com",
                    recipient_email="architect@example.com",
                    subject="Subject",
                    body="Body",
                    message_id="<not-configured@accoya-outreach.local>",
                    settings=settings,
                )
        self.assertEqual(raised.exception.code, "microsoft_graph_not_configured")
        post.assert_not_called()

    def test_sender_configuration_mismatch_fails_before_calling_graph(self) -> None:
        with patch.object(email_service.httpx, "post") as post:
            with self.assertRaises(email_service.EmailDeliveryFailure) as raised:
                email_service.send_outreach_email(
                    sender_email="other@example.com",
                    recipient_email="architect@example.com",
                    subject="Subject",
                    body="Body",
                    message_id="<sender-test@accoya-outreach.local>",
                    settings=self.settings,
                )
        self.assertEqual(
            raised.exception.code,
            "microsoft_sender_configuration_changed",
        )
        post.assert_not_called()

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
                    "microsoft_graph_submission_uncertain"
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
                self.assertEqual(observed["message"]["From"], "sender@example.com")
                self.assertEqual(observed["message"]["To"], "requester@example.com")


if __name__ == "__main__":
    unittest.main()