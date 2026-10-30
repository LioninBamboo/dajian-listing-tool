"""Tests for source-to-live aspect reconciliation.

Material must exactly equal the GIGA source string (Sergey 2026-10-23). Color
still uses primary-component / subset rules and is out of scope for that change.
"""

from __future__ import annotations

import pytest

from src.utils.publish_autofix import source_combined_aspect_keys
from src.utils.source_parameter_alignment import (
    find_source_parameter_mismatches,
    material_exactly_matches_source,
    primary_component,
    resolve_material_write,
)


class TestPrimaryComponent:
    """primary_component remains for Color; Material expected no longer uses it."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Black+Gold", "Black"),
            ("Natural Wood+Brown", "Natural Wood"),
            ("Beige,Light Brown", "Beige"),
            ("Teak+ Beige", "Teak"),
            ("Walnut/Black", "Walnut"),
            ("Wood and Metal", "Wood"),
            ("Wood & Metal", "Wood"),
        ],
    )
    def test_multi_value_reduces_to_the_leading_component(self, raw, expected):
        assert primary_component(raw) == expected

    @pytest.mark.parametrize("raw", ["Corduroy", "Camel", "Solid Wood"])
    def test_single_value_is_untouched(self, raw):
        assert primary_component(raw) == raw

    def test_casing_is_preserved(self):
        assert primary_component("MDF+Pine") == "MDF"

    @pytest.mark.parametrize("raw", ["", None, "   ", "+", ","])
    def test_empty_and_degenerate_input(self, raw):
        assert primary_component(raw) in ("", "+", ",")


class TestMismatchExpectedValueIsWritable:
    def _mismatch(self, field, attributes, aspects, **kwargs):
        rows = find_source_parameter_mismatches(
            source_attributes=attributes,
            source_title="Modern Accent Chair",
            source_description="A comfortable chair.",
            candidate_aspects=aspects,
            **kwargs,
        )
        return next((r for r in rows if r["field"] == field), None)

    def test_multi_colour_source_yields_a_single_writable_colour(self):
        row = self._mismatch("Color", {"Main Color": "Black+Gold"}, {"Color": ["White"]})
        assert row is not None
        assert row["expected"] == "Black"
        assert "+" not in row["expected"]

    def test_multi_material_source_yields_exact_giga_string(self):
        row = self._mismatch(
            "Material",
            {"Main Material": "Wood+Metal"},
            {"Material": ["Plastic"]},
            aspect_metadata_available=True,
            material_aspect=None,
        )
        assert row is not None
        assert row["expected"] == ["Wood+Metal"]
        assert not row.get("needs_human")

    def test_detail_still_shows_the_full_source_value(self):
        """The operator must still see what the supplier actually said."""
        row = self._mismatch("Color", {"Main Color": "Black+Gold"}, {"Color": ["White"]})
        assert "Black+Gold" in row["detail"]

    def test_single_value_source_is_unchanged(self):
        row = self._mismatch("Color", {"Main Color": "Camel"}, {"Color": ["Beige"]})
        assert row is not None
        assert row["expected"] == "Camel"

    def test_matching_aspect_reports_no_mismatch(self):
        assert self._mismatch("Color", {"Main Color": "Camel"}, {"Color": ["Camel"]}) is None

    def test_component_subset_still_counts_as_supported(self):
        """Comparison stays component-aware — live listing both parts is fine."""
        row = self._mismatch(
            "Color", {"Main Color": "Black+Gold"}, {"Color": ["Black", "Gold"]}
        )
        assert row is None


class TestColorPrimaryStillFaithful:
    """Color keeps the 2026-08-10 primary-component pass; Material does not."""

    def _m(self, attributes, aspects):
        rows = find_source_parameter_mismatches(
            source_attributes=attributes,
            source_title="Chair",
            source_description="A chair.",
            candidate_aspects=aspects,
        )
        return {r["field"] for r in rows}

    def test_live_holds_primary_material_is_now_a_mismatch(self):
        assert "Material" in self._m(
            {"Main Material": "Acacia Wood,Polyester"}, {"Material": ["Acacia Wood"]}
        )

    def test_live_holds_primary_color_no_mismatch(self):
        assert "Color" not in self._m(
            {"Main Color": "Black+Gold"}, {"Color": ["Black"]}
        )

    def test_genuine_wrong_material_still_flagged(self):
        assert "Material" in self._m(
            {"Main Material": "Wood,Metal"}, {"Material": ["Plastic"]}
        )

    def test_genuine_wrong_color_still_flagged(self):
        assert "Color" in self._m({"Main Color": "Black"}, {"Color": ["White"]})

    def test_missing_live_material_still_flagged(self):
        assert "Material" in self._m({"Main Material": "Wood,Metal"}, {})

    def test_non_primary_component_alone_is_a_real_mismatch(self):
        assert "Color" in self._m({"Main Color": "Black+Gold"}, {"Color": ["Gold"]})


class TestMaterialExactMatch:
    def test_separator_spacing_normalisation_passes(self):
        assert material_exactly_matches_source("Metal & Wood", ["Metal&Wood"])
        assert material_exactly_matches_source(
            "Particle Board+MDF", ["Particle Board + MDF"]
        )

    def test_primary_alone_does_not_pass(self):
        assert not material_exactly_matches_source("Fabric+Metal", ["Fabric"])

    def test_family_alias_does_not_pass(self):
        assert not material_exactly_matches_source("PU,Steel", ["Polyurethane (PU)"])

    def test_token_set_equality_for_multi_write(self):
        assert material_exactly_matches_source("Fabric+Metal", ["Fabric", "Metal"])

    def test_superset_does_not_pass(self):
        assert not material_exactly_matches_source(
            "Steel", ["Cast Iron", "Steel"]
        )

    def test_empty_source_or_live(self):
        assert not material_exactly_matches_source("", ["Steel"])
        assert not material_exactly_matches_source("Steel", [])


class TestResolveMaterialWrite:
    def test_custom_aspect_writes_exact(self):
        result = resolve_material_write(
            "Fabric+Metal", aspect_meta=None, metadata_available=True
        )
        assert result["status"] == "write"
        assert result["expected"] == ["Fabric+Metal"]

    def test_custom_aspect_over_65_needs_review(self):
        result = resolve_material_write(
            "Y" * 70, aspect_meta=None, metadata_available=True
        )
        assert result["status"] == "needs_review"
        assert result["reason"] == "value_too_long"

    def test_selection_only_single_not_allowed_is_review(self):
        result = resolve_material_write(
            "Steel/Plastic",
            aspect_meta={
                "mode": "SELECTION_ONLY",
                "cardinality": "SINGLE",
                "allowed_values": ["Steel", "Plastic"],
            },
            metadata_available=True,
        )
        assert result["status"] == "needs_review"
        assert result["reason"] == "selection_only_value_not_allowed"

    def test_selection_only_multi_splits_all_tokens(self):
        result = resolve_material_write(
            "Fabric+Metal",
            aspect_meta={
                "mode": "SELECTION_ONLY",
                "cardinality": "MULTI",
                "allowed_values": ["Fabric", "Metal", "Wood"],
            },
            metadata_available=True,
        )
        assert result["status"] == "write"
        assert result["expected"] == ["Fabric", "Metal"]

    def test_metadata_unavailable_fail_closed(self):
        result = resolve_material_write(
            "Wood+Metal", aspect_meta=None, metadata_available=False
        )
        assert result["status"] == "needs_review"
        assert result["reason"] == "aspect_metadata_unavailable"

    def test_value_too_long_fail_closed(self):
        long_value = "A" * 66
        result = resolve_material_write(
            long_value,
            aspect_meta={"mode": "FREE_TEXT", "cardinality": "SINGLE", "allowed_values": []},
            metadata_available=True,
        )
        assert result["status"] == "needs_review"
        assert result["reason"] == "value_too_long"


def _material_row(attributes, aspects, **kwargs):
    rows = find_source_parameter_mismatches(
        source_attributes=attributes,
        source_title="Test SKU",
        source_description="",
        candidate_aspects=aspects,
        **kwargs,
    )
    return next((r for r in rows if r["field"] == "Material"), None)


class TestRealSkuMaterialTable:
    """Every row from fabric_metal_spec real-SKU table."""

    def test_row1_w3916_custom_fabric_metal(self):
        row = _material_row(
            {"Main Material": "Fabric+Metal"},
            {"Material": ["Linen"]},
            category_id="54235",
            material_aspect=None,
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Fabric+Metal"]
        assert not material_exactly_matches_source("Fabric+Metal", ["Fabric"])
        assert not material_exactly_matches_source("Fabric+Metal", ["Linen"])

    def test_row2_glass_mdf_melamine_subset_does_not_pass(self):
        row = _material_row(
            {"Main Material": "Glass,MDF,Melamine"},
            {"Material": ["Glass"]},
            category_id="20689",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "SINGLE",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Glass,MDF,Melamine"]
        assert row.get("legacy_partial_match") is True
        assert not material_exactly_matches_source(
            "Glass,MDF,Melamine", ["Glass"]
        )

    def test_row3_mixed_separators_ceramic_solid_wood(self):
        row = _material_row(
            {"Main Material": "Ceramic,Solid Wood+MDF"},
            {"Material": ["Ceramic"]},
            category_id="20487",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "SINGLE",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Ceramic,Solid Wood+MDF"]
        assert row["expected"] != ["Ceramic", "Solid Wood"]

    def test_row4_metal_and_wood_ampersand_preserved(self):
        row = _material_row(
            {"Main Material": "Metal & Wood"},
            {"Material": ["Metal"]},
            category_id="176937",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "SINGLE",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Metal & Wood"]
        preserved = source_combined_aspect_keys({"Material": ["Metal & Wood"]})
        assert "Material" in preserved

    def test_row5_metal_and_wood_spacing_pass(self):
        assert (
            _material_row(
                {"Main Material": "Metal & Wood"},
                {"Material": ["Metal&Wood"]},
                category_id="176937",
                aspect_metadata_available=True,
                material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
            )
            is None
        )

    def test_row6_particle_board_spacing_pass(self):
        assert (
            _material_row(
                {"Main Material": "Particle Board+MDF"},
                {"Material": ["Particle Board + MDF"]},
                category_id="20740",
                aspect_metadata_available=True,
                material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
            )
            is None
        )

    def test_row7_abs_steel_suffix(self):
        row = _material_row(
            {"Main Material": "ABS+Steel(Q235)"},
            {"Material": ["ABS+Steel"]},
            category_id="63700",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "SINGLE",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["ABS+Steel(Q235)"]
        assert not material_exactly_matches_source(
            "ABS+Steel(Q235)", ["ABS+Steel"]
        )
        assert not material_exactly_matches_source("ABS+Steel(Q235)", ["ABS"])

    def test_row8_pu_steel_alias_does_not_pass(self):
        row = _material_row(
            {"Main Material": "PU,Steel"},
            {"Material": ["Polyurethane (PU)"]},
            category_id="173950",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "SINGLE",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["PU,Steel"]

    def test_row9_steel_exact_pass_superset_fails(self):
        assert (
            _material_row(
                {"Main Material": "Steel"},
                {"Material": ["Steel"]},
                category_id="20761",
                aspect_metadata_available=True,
                material_aspect={
                    "mode": "FREE_TEXT",
                    "cardinality": "MULTI",
                    "allowed_values": ["Steel"],
                },
            )
            is None
        )
        assert not material_exactly_matches_source(
            "Steel", ["Cast Iron", "Steel"]
        )

    def test_row10_wood_fabric_exact_pass_and_sanitizer_preserves(self):
        assert (
            _material_row(
                {"Main Material": "Wood+Fabric"},
                {"Material": ["Wood+Fabric"]},
                category_id="20490",
                aspect_metadata_available=True,
                material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
            )
            is None
        )
        assert "Material" in source_combined_aspect_keys(
            {"Material": ["Wood+Fabric"]}
        )

    def test_row11_selection_only_single_slash_needs_review(self):
        row = _material_row(
            {"Main Material": "Steel/Plastic"},
            {"Material": ["Steel"]},
            category_id="SYNTHETIC",
            material_aspect={
                "mode": "SELECTION_ONLY",
                "cardinality": "SINGLE",
                "allowed_values": ["Steel", "Plastic"],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row.get("needs_human") is True
        assert row.get("issue_type") == "source_aspect_needs_review"
        assert row.get("reason") == "selection_only_value_not_allowed"
        assert row.get("expected") is None

    def test_row12_selection_only_multi_writes_all_tokens(self):
        row = _material_row(
            {"Main Material": "Fabric+Metal"},
            {"Material": ["Linen"]},
            category_id="SYNTHETIC",
            material_aspect={
                "mode": "SELECTION_ONLY",
                "cardinality": "MULTI",
                "allowed_values": ["Fabric", "Metal", "Linen"],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Fabric", "Metal"]
        assert row["expected"] != ["Fabric"]

    def test_row13_taxonomy_unavailable_needs_review(self):
        row = _material_row(
            {"Main Material": "Wood+Metal"},
            {"Material": ["Wood"]},
            category_id="SYNTHETIC",
            material_aspect=None,
            aspect_metadata_available=False,
        )
        assert row is not None
        assert row.get("needs_human") is True
        assert row.get("reason") == "aspect_metadata_unavailable"
        assert row.get("expected") is None


class TestMaterialEdgeCasesAdded:
    """Three extra edge cases beyond the real-SKU table."""

    def test_edge_trailing_separator_wood_plus_no_crash_writes_exact(self):
        """Trailing ``+`` must not crash; wrong live still writes the exact GIGA string."""
        row = _material_row(
            {"Main Material": "Wood+"},
            {"Material": ["Plastic"]},
            material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Wood+"]
        assert resolve_material_write(
            "Wood+", aspect_meta=None, metadata_available=True
        )["expected"] == ["Wood+"]

    def test_edge_duplicate_tokens_steel_steel_vs_steel_fails(self):
        """SYNTHETIC: ``Steel+Steel`` vs live ``[Steel]`` must FAIL (B2 — single live)."""
        assert not material_exactly_matches_source("Steel+Steel", ["Steel"])
        row = _material_row(
            {"Main Material": "Steel+Steel"},
            {"Material": ["Steel"]},
            material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Steel+Steel"]

    def test_edge_case_only_difference_passes(self):
        """Case-only difference is not a mismatch (compare is case-insensitive)."""
        assert (
            _material_row(
                {"Main Material": "fabric+metal"},
                {"Material": ["Fabric+Metal"]},
                material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
                aspect_metadata_available=True,
            )
            is None
        )


class TestMaterialNullSafety:
    def test_missing_giga_material_no_issue(self):
        assert (
            _material_row({}, {"Material": ["Steel"]}, aspect_metadata_available=True)
            is None
        )

    def test_empty_giga_material_no_issue(self):
        assert (
            _material_row(
                {"Main Material": "  "},
                {"Material": ["Steel"]},
                aspect_metadata_available=True,
            )
            is None
        )

    def test_list_source_uses_all_elements(self):
        """SYNTHETIC / B4: list ``['Fabric','Metal']`` must keep both elements."""
        row = _material_row(
            {"Main Material": ["Fabric", "Metal"]},
            {"Material": ["Linen"]},
            material_aspect=None,
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Fabric+Metal"]
        assert "Fabric" in row["source_value"]
        assert "Metal" in row["source_value"]

    def test_non_string_source_does_not_crash(self):
        row = _material_row(
            {"Main Material": 12345},
            {"Material": ["Steel"]},
            material_aspect={"mode": "FREE_TEXT", "cardinality": "SINGLE"},
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["12345"]


class TestMaterialReviewFixesB2B3B4:
    """Targeted regressions from PR #28 review (B2/B3/B4 + empty aspects)."""

    def test_b2_single_live_reordered_composite_does_not_pass(self):
        assert not material_exactly_matches_source("Metal & Wood", ["Wood/Metal"])
        assert not material_exactly_matches_source(
            "Glass,MDF,Melamine", ["Melamine+MDF+Glass"]
        )

    def test_b2_multi_value_split_write_still_passes(self):
        assert material_exactly_matches_source("Fabric+Metal", ["Fabric", "Metal"])
        assert material_exactly_matches_source("Metal & Wood", ["Metal", "Wood"])

    def test_b3_custom_aspect_over_65_needs_review(self):
        """SYNTHETIC: 70-char material → needs_review, never hard-truncate."""
        long_value = "X" * 70
        result = resolve_material_write(
            long_value, aspect_meta=None, metadata_available=True
        )
        assert result["status"] == "needs_review"
        assert result["reason"] == "value_too_long"
        row = _material_row(
            {"Main Material": long_value},
            {"Material": ["Steel"]},
            material_aspect=None,
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row.get("needs_human") is True
        assert row.get("reason") == "value_too_long"
        assert row.get("expected") is None

    def test_b4_list_source_selection_only_multi_writes_both(self):
        row = _material_row(
            {"Main Material": ["Fabric", "Metal"]},
            {"Material": ["Linen"]},
            material_aspect={
                "mode": "SELECTION_ONLY",
                "cardinality": "MULTI",
                "allowed_values": ["Fabric", "Metal", "Wood"],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Fabric", "Metal"]


class TestReviewRealSkuCases:
    def test_w3916_fabric_plus_metal(self):
        row = _material_row(
            {"Main Material": "Fabric+Metal"},
            {"Material": ["Linen"]},
            category_id="54235",
            material_aspect=None,
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Fabric+Metal"]

    def test_n752_glass_mdf_melamine(self):
        row = _material_row(
            {"Main Material": "Glass,MDF,Melamine"},
            {"Material": ["Glass"]},
            category_id="20689",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "MULTI",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Glass,MDF,Melamine"]
        assert row.get("legacy_partial_match") is True

    def test_w882_metal_and_wood(self):
        row = _material_row(
            {"Main Material": "Metal & Wood"},
            {"Material": ["Metal"]},
            category_id="176937",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "MULTI",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Metal & Wood"]

    def test_w465_pu_steel(self):
        row = _material_row(
            {"Main Material": "PU,Steel"},
            {"Material": ["Polyurethane (PU)"]},
            category_id="173950",
            material_aspect={
                "mode": "FREE_TEXT",
                "cardinality": "SINGLE",
                "allowed_values": [],
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["PU,Steel"]


class TestAddedEdgeCasesRound2:
    """Three additional edge cases beyond the review checklist."""

    def test_edge_list_source_skips_blank_elements(self):
        row = _material_row(
            {"Main Material": ["Fabric", "  ", "Metal"]},
            {"Material": ["Linen"]},
            material_aspect=None,
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row["expected"] == ["Fabric+Metal"]

    def test_edge_exact_pass_does_not_churn_when_already_correct(self):
        assert (
            _material_row(
                {"Main Material": "Fabric+Metal"},
                {"Material": ["Fabric+Metal"]},
                material_aspect=None,
                aspect_metadata_available=True,
            )
            is None
        )

    def test_edge_selection_only_partial_tokens_needs_review_no_subset_write(self):
        row = _material_row(
            {"Main Material": "Fabric+Metal+Wood"},
            {"Material": ["Fabric"]},
            material_aspect={
                "mode": "SELECTION_ONLY",
                "cardinality": "MULTI",
                "allowed_values": ["Fabric", "Metal"],  # Wood missing
            },
            aspect_metadata_available=True,
        )
        assert row is not None
        assert row.get("needs_human") is True
        assert row.get("expected") is None
        assert row.get("reason") == "selection_only_value_not_allowed"
