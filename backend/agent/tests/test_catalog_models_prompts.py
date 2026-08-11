"""Tests for immutable public contracts and the versioned prompt/catalog."""

from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from agent.catalog import (
    ACCOYA_CATALOG,
    APPLICATION_IDS,
    CATALOG_VERSION,
    PRODUCT_FAMILY_IDS,
    application_belongs_to_family,
    get_application,
    get_family,
)
from agent.models import (
    EmailDraft,
    GenerationResult,
    GenerationStatus,
    NormalizedLead,
    ProductSelection,
    SelectionStatus,
)
from agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_compose_prompt


class CatalogTests(unittest.TestCase):
    def test_catalog_has_exact_version_and_three_canonical_families(self):
        self.assertEqual(CATALOG_VERSION, "1.0.0")
        self.assertEqual(ACCOYA_CATALOG.version, "1.0.0")
        self.assertEqual(
            PRODUCT_FAMILY_IDS,
            ("accoya_wood", "accoya_color_grey", "tricoya_panels"),
        )
        self.assertEqual(len(APPLICATION_IDS), len(set(APPLICATION_IDS)))

    def test_catalog_contains_every_requested_application(self):
        expected_display_names = {
            "Accoya Wood": {
                "Standard decking",
                "Pool decking",
                "Rooftop decking",
                "Deck stairs",
                "Elevated walkways",
                "Boardwalks and pedestrian decking",
                "Standard siding",
                "Charred or Shou Sugi Ban siding",
                "Rainscreen siding",
                "Brise Soleil",
                "Louvres and shade screens",
                "General wooden windows",
                "Bay windows",
                "Sash windows",
                "Casement windows",
                "Window shutters",
                "Exterior wooden doors",
                "Front and entrance doors",
                "Garage doors",
                "Bifold doors",
                "French doors",
                "Bridges",
                "Boardwalks",
                "Garden rooms",
                "Conservatories",
                "Gates",
                "Outdoor furniture",
                "Planters",
                "Fencing and railings",
                "Structures and sculptures",
                "Freshwater and specialist exterior applications",
            },
            "Accoya Color Grey": {
                "Color Grey decking",
                "Grey pool decking",
                "Color Grey siding and cladding",
            },
            "Tricoya Panels": {
                "Exterior MDF panels",
                "Exterior doors and door components",
                "Siding and façades",
                "Exterior paneling",
                "Mouldings and trim",
                "Fascias and soffits",
                "Signage",
                "Routed or CNC-cut exterior components",
                "Painted large-format exterior panels",
            },
        }
        actual = {
            family.display_name: {app.display_name for app in family.applications}
            for family in ACCOYA_CATALOG.families
        }
        self.assertEqual(actual, expected_display_names)

    def test_color_grey_and_tricoya_invariants_are_explicit(self):
        color_grey = get_family("Accoya Color Grey")
        tricoya = get_family("Tricoya")
        self.assertIsNotNone(color_grey)
        self.assertIn("pre-greyed", color_grey.selection_rule.casefold())
        self.assertIsNotNone(tricoya)
        self.assertFalse(tricoya.is_solid_lumber)
        self.assertIn("panel", tricoya.material_form.casefold())
        self.assertIn("not solid lumber", " ".join(ACCOYA_CATALOG.invariants).casefold())

    def test_aliases_resolve_but_membership_stays_with_canonical_family(self):
        self.assertEqual(get_application("garage door").id, "garage_doors")
        self.assertTrue(
            application_belongs_to_family("Accoya Wood", "garage door")
        )
        self.assertFalse(
            application_belongs_to_family("Tricoya", "standard decking")
        )
        self.assertIsNone(get_family("made-up-product"))
        self.assertIsNone(get_application("made-up-application"))


class ContractTests(unittest.TestCase):
    def test_email_draft_requires_nonblank_content_and_catalog_metadata(self):
        long_subject = "S" * 5000
        draft = EmailDraft(
            subject=long_subject,
            body="Opening paragraph.\n\nValue paragraph.",
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
        )
        self.assertEqual(draft.subject, long_subject)
        with self.assertRaises(ValidationError):
            EmailDraft.model_validate(
                {**draft.model_dump(), "subject": ""}
            )

    def test_product_fields_are_null_only_for_explicit_low_confidence(self):
        low = ProductSelection(
            confidence=0.59,
            selection_status=SelectionStatus.LOW_CONFIDENCE,
        )
        self.assertIsNone(low.selected_product_family)
        self.assertIsNone(low.selected_application)

        with self.assertRaises(ValidationError):
            ProductSelection(
                selected_product_family="accoya_wood",
                selected_application="standard_decking",
                confidence=0.59,
                selection_status=SelectionStatus.LOW_CONFIDENCE,
                retrieval_query="decking",
            )
        with self.assertRaises(ValidationError):
            ProductSelection(confidence=0.9)

    def test_selected_product_requires_model_retrieval_query(self):
        with self.assertRaisesRegex(ValidationError, "retrieval query"):
            ProductSelection(
                selected_product_family="accoya_wood",
                selected_application="standard_decking",
                confidence=0.9,
            )
        selected = ProductSelection(
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            confidence=0.9,
            retrieval_query="Accoya decking",
        )
        self.assertEqual(selected.retrieval_query, "Accoya decking")

    def test_non_generated_result_cannot_expose_a_draft(self):
        with self.assertRaisesRegex(ValidationError, "cannot expose"):
            GenerationResult(
                status=GenerationStatus.PROVIDER_ERROR,
                lead_id="lead-1",
                original_lead={"id": "lead-1"},
                subject="Hidden",
                body="Hidden",
                prompt_version="test",
            )


class PromptTests(unittest.TestCase):
    def test_system_prompt_is_concise_catalog_and_schema_prompt(self):
        self.assertEqual(PROMPT_VERSION, "accoya-email-v2.2.0")
        self.assertIn("CATALOG VERSION: 1.0.0", SYSTEM_PROMPT)
        for family in ("Accoya Wood", "Accoya Color Grey", "Tricoya Panels"):
            self.assertIn(family, SYSTEM_PROMPT)
        for field in ("retrieval_query", "email_number", "subject", "body"):
            self.assertIn(field, SYSTEM_PROMPT)
        for removed in ("lead_evidence_used", "repair_once", "approved_strategy"):
            self.assertNotIn(removed, SYSTEM_PROMPT)

    def test_compose_prompt_adds_dutch_requirement_for_nl_state(self):
        lead = NormalizedLead(
            lead_id="lead-nl",
            state="NL",
            source_values={"state": "NL"},
        )
        selection = ProductSelection(
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            confidence=0.95,
            retrieval_query="Accoya decking",
        )

        prompt = build_compose_prompt(lead, selection, chunks=[])
        payload = json.loads(prompt)

        self.assertIn(
            "Write the subject and body in Dutch.",
            payload["requirements"],
        )

    def test_compose_prompt_keeps_default_requirements_for_non_nl_state(self):
        lead = NormalizedLead(
            lead_id="lead-us",
            state="OR",
            source_values={"state": "OR"},
        )
        selection = ProductSelection(
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            confidence=0.95,
            retrieval_query="Accoya decking",
        )

        prompt = build_compose_prompt(lead, selection, chunks=[])
        payload = json.loads(prompt)

        self.assertNotIn(
            "Write the subject and body in Dutch.",
            payload["requirements"],
        )


if __name__ == "__main__":
    unittest.main()
