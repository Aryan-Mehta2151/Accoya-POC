"""Tests for immutable public contracts and the versioned prompt/catalog."""

from __future__ import annotations

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
    CTAType,
    EmailDraftComponents,
    EvidenceReference,
    GenerationResult,
    GenerationStatus,
    ProductSelection,
    SelectionStatus,
    ValidationStatus,
    assemble_email_draft,
)
from agent.prompts import SYSTEM_PROMPT


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
    def test_structured_components_assemble_into_two_or_three_paragraphs(self):
        components = EmailDraftComponents(
            subject="A subject",
            opening_paragraph=" Opening paragraph. ",
            value_paragraph="Value paragraph.",
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            cta_type=CTAType.SAMPLE,
            cta_text="Would reviewing a physical sample help with the current material evaluation?",
        )
        self.assertEqual(
            assemble_email_draft(components).body,
            "Opening paragraph.\n\nValue paragraph.",
        )
        with_closing = components.model_copy(
            update={"closing_paragraph": "Closing paragraph."}
        )
        self.assertEqual(
            assemble_email_draft(with_closing).body.count("\n\n"),
            2,
        )

    def test_product_fields_are_null_only_for_explicit_low_confidence(self):
        low = ProductSelection(
            confidence=0.59,
            selection_status=SelectionStatus.LOW_CONFIDENCE,
            missing_information=["No exterior application signal"],
        )
        self.assertIsNone(low.selected_product_family)
        self.assertIsNone(low.selected_application)

        with self.assertRaises(ValidationError):
            ProductSelection(
                selected_product_family="accoya_wood",
                selected_application="standard_decking",
                confidence=0.59,
                selection_status=SelectionStatus.LOW_CONFIDENCE,
            )
        with self.assertRaises(ValidationError):
            ProductSelection(confidence=0.9)

    def test_selected_product_requires_exact_source_trigger(self):
        with self.assertRaisesRegex(ValidationError, "exact source"):
            ProductSelection(
                selected_product_family="accoya_wood",
                selected_application="standard_decking",
                confidence=0.9,
            )

        selected = ProductSelection(
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            exact_source_trigger=EvidenceReference(
                source_type="lead",
                source_id="lead-1",
                source_field="signal",
                quote="Thermory walkway",
            ),
            cta_type=CTAType.SAMPLE,
            confidence=0.9,
        )
        self.assertEqual(selected.exact_source_trigger.quote, "Thermory walkway")

    def test_non_generated_result_cannot_expose_invalid_draft(self):
        with self.assertRaisesRegex(ValidationError, "must not expose"):
            GenerationResult(
                status=GenerationStatus.VALIDATION_FAILED,
                lead_id="lead-1",
                original_lead={"id": "lead-1"},
                subject="Hidden",
                body="Hidden",
                prompt_version="test",
                validation_status=ValidationStatus.INVALID,
            )


class PromptTests(unittest.TestCase):
    def test_system_prompt_contains_all_eleven_sections_and_versions(self):
        headings = (
            "1. ROLE AND OBJECTIVE",
            "2. SOURCE-PRECEDENCE RULES",
            "3. STATIC ACCOYA CATALOG",
            "4. PRODUCT-SELECTION RULES",
            "5. AUDIENCE-POSITIONING RULES",
            "6. EMAIL STRUCTURE AND LENGTH",
            "7. CLAIM ALLOWLIST AND PROHIBITED LANGUAGE",
            "8. GROUNDING REQUIREMENTS",
            "9. CTA-SELECTION RULES",
            "10. STRUCTURED OUTPUT SCHEMA",
            "11. FAILURE AND UNCERTAINTY BEHAVIOR",
        )
        positions = [SYSTEM_PROMPT.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Prompt version:", SYSTEM_PROMPT)
        self.assertIn("Catalog version: 1.0.0", SYSTEM_PROMPT)
        for family in ("Accoya Wood", "Accoya Color Grey", "Tricoya Panels"):
            self.assertIn(family, SYSTEM_PROMPT)
        for field in (
            "opening_paragraph",
            "value_paragraph",
            "closing_paragraph",
        ):
            self.assertIn(field, SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
