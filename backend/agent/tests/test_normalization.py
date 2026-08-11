"""Tests for deterministic lead adaptation and inference."""

from __future__ import annotations

from datetime import date
import unittest

from agent.normalization import (
    EARLYBID_NATURAL_ID_PREFIX,
    determine_audience,
    determine_stage,
    normalize_lead,
)


class NormalizeLeadTests(unittest.TestCase):
    def test_requires_stable_id_and_uses_first_nonblank_supported_id(self):
        lead = normalize_lead(
            {"lead_id": " ", "id": "feed-42", "external_id": "external-9"}
        )
        self.assertEqual(lead.lead_id, "feed-42")

        with self.assertRaisesRegex(ValueError, "stable lead identifier"):
            normalize_lead({"Project": "No ID"})

    def test_rejects_dashboard_display_rank(self):
        for display_id in ("Lead #4", "lead no. 12", "LEAD NUMBER 7"):
            with self.subTest(display_id=display_id), self.assertRaisesRegex(
                ValueError, "display rank"
            ):
                normalize_lead(
                    {"id": display_id}, identity_scope="reseller/client"
                )

    def test_derives_deterministic_id_from_canonical_natural_key(self):
        first = normalize_lead(
            {
                "Project": " Caf\u00e9\u00a0Pavilion ",
                "Location": " Portland, Oregon ",
            },
            identity_scope=" AMPED/Accoya ",
        )
        equivalent = normalize_lead(
            {
                "Project": "CAFE\u0301   PAVILION",
                "Location": "portland",
                "State": "or",
            },
            identity_scope="amped/accoya",
        )

        self.assertEqual(first.lead_id, equivalent.lead_id)
        self.assertTrue(first.lead_id.startswith(EARLYBID_NATURAL_ID_PREFIX))
        digest = first.lead_id.removeprefix(EARLYBID_NATURAL_ID_PREFIX)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_natural_id_ignores_mutable_fields_but_is_scoped(self):
        natural_key = {
            "Project": "Riverside Walkway",
            "Location": "Sacramento, California",
        }
        initial = normalize_lead(
            {
                **natural_key,
                "Score": "72",
                "URL": "https://example.test/old",
                "Next Step": "Call the architect",
                "Contacts": "Pat One, pat@example.test",
            },
            identity_scope="reseller/client-a",
        )
        refreshed = normalize_lead(
            {
                **natural_key,
                "Score": "99",
                "URL": "https://example.test/new",
                "Next Step": "Send samples",
                "Contacts": "A different contact",
            },
            identity_scope="reseller/client-a",
        )
        other_feed = normalize_lead(
            natural_key,
            identity_scope="reseller/client-b",
        )

        self.assertEqual(initial.lead_id, refreshed.lead_id)
        self.assertNotEqual(initial.lead_id, other_feed.lead_id)

    def test_explicit_id_precedes_natural_identity(self):
        lead = normalize_lead(
            {"lead_id": "provided-42"},
            identity_scope="reseller/client",
        )

        self.assertEqual(lead.lead_id, "provided-42")

    def test_natural_identity_requires_nonblank_scope_project_and_location(self):
        cases = (
            ({"Project": "Library", "Location": "Boston, MA"}, " ", "identity_scope"),
            ({"Location": "Boston, MA"}, "reseller/client", "Project"),
            ({"Project": "Library"}, "reseller/client", "Location"),
        )
        for record, scope, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                normalize_lead(record, identity_scope=scope)

    def test_natural_identity_does_not_mutate_or_augment_source_mapping(self):
        source = {
            "Project": "Garden Room",
            "Location": "Seattle, WA",
            "Tags": ["Exterior", "Wood"],
        }
        expected = {
            "Project": "Garden Room",
            "Location": "Seattle, WA",
            "Tags": ["Exterior", "Wood"],
        }

        lead = normalize_lead(source, identity_scope="reseller/client")

        self.assertEqual(source, expected)
        self.assertEqual(lead.source_values, expected)
        self.assertNotIn("lead_id", lead.source_values)
        self.assertNotIn("external_id", lead.source_values)

    def test_normalizes_display_aliases_contacts_date_tags_location_and_mentions(self):
        lead = normalize_lead(
            {
                "id": "export-1",
                "Section": "  Planning  ",
                "Project": " Riverside Walkway ",
                "Location": "Portland, Oregon",
                "State": "",
                "Signal": "Thermory, or similar decking is being considered.",
                "Intelligence": "Architect is reviewing the walkway concept.",
                "Score": "1,234.5",
                "Timing": "Proposed for early design",
                "Next Step": "Offer a specification discussion",
                "Awarded To": "N/A",
                "Priority Reasons": "Exterior timber opportunity",
                "Summary": "Planning-stage pedestrian deck.",
                "Contacts": (
                    "Taylor Smith - Project Architect, Smith Design LLC, "
                    "TAYLOR@EXAMPLE.COM; 503-555-0199"
                ),
                "Meeting Date": "Jul 17, 2026",
                "Tags": "#Decking, Planning; decking | Public Realm",
                "URL": " https://example.test/lead/1 ",
            }
        )

        self.assertEqual(lead.project, "Riverside Walkway")
        self.assertEqual(lead.city, "Portland")
        self.assertEqual(lead.state, "OR")
        self.assertEqual(lead.score, 1234.5)
        self.assertEqual(lead.meeting_date, date(2026, 7, 17))
        self.assertEqual(lead.meeting_date_raw, "Jul 17, 2026")
        self.assertEqual(lead.tags, ["Decking", "Planning", "Public Realm"])
        self.assertEqual(lead.contacts[0].email, "taylor@example.com")
        self.assertEqual(lead.project_stage.value, "planning")
        self.assertEqual(lead.audience.value, "architect_specifier")
        self.assertTrue(
            any(ref.quote == "Thermory, or similar" for ref in lead.material_mentions)
        )
        self.assertTrue(
            any(ref.quote.casefold() == "thermory" for ref in lead.competitor_mentions)
        )

    def test_preserves_deep_copy_and_does_not_mutate_input(self):
        source = {
            "external_id": "deep-copy-1",
            "Project": "Garden room",
            "Contacts": [
                {
                    "name": "Casey Lee",
                    "email": "CASEY@EXAMPLE.COM",
                    "meta": {"source": "feed"},
                }
            ],
            "Tags": ["One", "Two"],
        }
        expected = {
            **source,
            "Contacts": [dict(source["Contacts"][0])],
            "Tags": list(source["Tags"]),
        }
        expected["Contacts"][0]["meta"] = {"source": "feed"}

        lead = normalize_lead(source)

        self.assertEqual(source, expected)
        self.assertEqual(lead.source_values, expected)
        source["Contacts"][0]["meta"]["source"] = "changed"
        source["Tags"].append("Three")
        self.assertEqual(lead.source_values["Contacts"][0]["meta"]["source"], "feed")
        self.assertEqual(lead.source_values["Tags"], ["One", "Two"])

    def test_normalizes_blanks_invalid_values_and_contact_email_fallback(self):
        lead = normalize_lead(
            {
                "lead_id": "blank-1",
                "Project": " n/a ",
                "Score": "not-a-score",
                "Meeting Date": "not-a-date",
                "Contacts": "Morgan Doe - Builder",
                "Contact Email": "MORGAN@EXAMPLE.COM",
                "Tags": "[]",
            }
        )

        self.assertIsNone(lead.project)
        self.assertIsNone(lead.score)
        self.assertIsNone(lead.meeting_date)
        self.assertEqual(lead.tags, [])
        self.assertEqual(lead.contacts[0].email, "morgan@example.com")

    def test_preserves_netherlands_state_codes_for_language_routing(self):
        lead_from_code = normalize_lead(
            {
                "lead_id": "nl-1",
                "Project": "Canal Renovation",
                "Location": "Amsterdam, NL",
                "State": "NL",
            }
        )
        lead_from_name = normalize_lead(
            {
                "lead_id": "nl-2",
                "Project": "Canal Renovation",
                "Location": "Amsterdam, Netherlands",
                "State": "Netherlands",
            }
        )

        self.assertEqual(lead_from_code.state, "NL")
        self.assertEqual(lead_from_name.state, "NL")


class InferenceTests(unittest.TestCase):
    def test_stage_categories(self):
        cases = {
            "planning": {"timing": "Concept design is planned"},
            "specification": {"next_step": "Review CSI specification language"},
            "procurement": {"signal": "Request for quote and supplier selection"},
            "unknown": {"summary": "Exterior opportunity"},
        }
        for expected, values in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(determine_stage(values).value, expected)

    def test_audience_categories(self):
        cases = {
            "architect_specifier": {"contacts": "Alex, Project Architect"},
            "contractor_builder": {"contacts": "Sam, General Contractor"},
            "distributor_supplier": {"contacts": "Jamie, Purchasing Manager"},
            "owner_developer": {"contacts": "Jordan, Property Developer"},
            "manufacturer_fabricator": {"contacts": "Robin, Window Fabricator"},
            "facilities_property": {"contacts": "Avery, Facilities Manager"},
            "unknown": {"contacts": "Pat Example"},
        }
        for expected, values in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(determine_audience(values).value, expected)


if __name__ == "__main__":
    unittest.main()
