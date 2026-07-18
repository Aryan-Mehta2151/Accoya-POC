"""Catalog-backed product-routing examples from the approved brief."""

from __future__ import annotations

import unittest

from agent.normalization import normalize_lead
from agent.routing import get_routing_hints


class ProductRoutingTests(unittest.TestCase):
    def assert_primary_route(
        self,
        text: str,
        expected_family: str,
        expected_application: str,
        *,
        field: str = "Signal",
    ) -> None:
        lead = normalize_lead({"id": "route-case", field: text})
        hints = get_routing_hints(lead)
        self.assertTrue(hints, msg=f"No routing hint for {text!r}")
        self.assertEqual(hints[0].product_family, expected_family)
        self.assertEqual(hints[0].application, expected_application)
        self.assertEqual(hints[0].source_field, field.casefold())
        self.assertIn(hints[0].source_trigger.casefold(), text.casefold())

    def test_thermory_walkway_routes_to_accoya_wood_decking(self):
        self.assert_primary_route(
            "Thermory is named for the elevated walkway.",
            "accoya_wood",
            "standard_decking",
        )

    def test_trex_porch_routes_to_accoya_wood_decking(self):
        self.assert_primary_route(
            "Trex decking is being considered for the porch.",
            "accoya_wood",
            "standard_decking",
        )

    def test_ipe_boardwalk_routes_to_accoya_wood_decking(self):
        self.assert_primary_route(
            "Ipe decking option for a public boardwalk.",
            "accoya_wood",
            "standard_decking",
        )

    def test_grey_deck_routes_to_color_grey_before_generic_decking(self):
        self.assert_primary_route(
            "The design calls for grey decking around the pool deck.",
            "accoya_color_grey",
            "color_grey_decking",
        )

    def test_exterior_mdf_fascia_routes_to_tricoya(self):
        self.assert_primary_route(
            "Exterior MDF fascia and soffit components are specified.",
            "tricoya_panels",
            "exterior_mdf_panels",
        )

    def test_rainscreen_facade_routes_to_accoya_wood_siding(self):
        self.assert_primary_route(
            "Timber rainscreen façade for the west elevation.",
            "accoya_wood",
            "standard_siding",
        )

    def test_sash_window_routes_to_accoya_wood_windows(self):
        self.assert_primary_route(
            "Replacement sash windows are under design review.",
            "accoya_wood",
            "general_wooden_windows",
        )

    def test_garage_door_routes_to_accoya_wood_doors(self):
        self.assert_primary_route(
            "Custom garage door package for the residence.",
            "accoya_wood",
            "exterior_wooden_doors",
        )

    def test_unrelated_project_returns_no_hint(self):
        lead = normalize_lead(
            {
                "id": "route-unrelated",
                "Project": "Interior lighting controls",
                "Summary": "Upgrade fixtures and occupancy sensors in offices.",
            }
        )
        self.assertEqual(get_routing_hints(lead), [])

    def test_procurement_window_is_not_a_wooden_window_hint(self):
        lead = normalize_lead(
            {
                "id": "route-window-metaphor",
                "Summary": "The procurement window closes next week.",
            }
        )
        self.assertEqual(get_routing_hints(lead), [])


if __name__ == "__main__":
    unittest.main()
