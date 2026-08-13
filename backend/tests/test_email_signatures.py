"""Offline tests for US signature selection and plain-text rendering."""

from __future__ import annotations

import unittest

from app.email_content import email_content_hash, render_outreach_body
from app.email_signature import (
    DEFAULT_US_EMAIL_SIGNATURE,
    US_STATE_CODES,
    default_signature_for_state,
    is_us_opportunity_state,
)


class EmailSignaturePolicyTests(unittest.TestCase):
    def test_all_fifty_states_and_dc_receive_the_default(self) -> None:
        self.assertEqual(len(US_STATE_CODES), 51)
        for code in US_STATE_CODES:
            with self.subTest(code=code):
                self.assertTrue(is_us_opportunity_state(code))
                self.assertEqual(
                    default_signature_for_state(code),
                    DEFAULT_US_EMAIL_SIGNATURE,
                )

        for name in ("California", "Tennessee", "District of Columbia"):
            with self.subTest(name=name):
                self.assertTrue(is_us_opportunity_state(name))

    def test_non_us_unknown_and_territory_values_do_not_receive_default(self) -> None:
        for value in (None, "", "NL", "Netherlands", "PR", "GU", "XX"):
            with self.subTest(value=value):
                self.assertFalse(is_us_opportunity_state(value))
                self.assertIsNone(default_signature_for_state(value))

    def test_renderer_and_hash_use_the_complete_plain_text_message(self) -> None:
        self.assertEqual(render_outreach_body(" Body \n", None), " Body \n")
        self.assertEqual(
            render_outreach_body(" Body \n", " Signature \n"),
            "Body\n\nSignature",
        )
        without_signature = email_content_hash(
            "to@example.com", "Subject", "Body", None
        )
        with_signature = email_content_hash(
            "to@example.com", "Subject", "Body", "Signature"
        )
        self.assertNotEqual(without_signature, with_signature)


if __name__ == "__main__":
    unittest.main()
