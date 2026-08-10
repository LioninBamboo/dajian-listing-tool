"""Tests for source-to-live aspect reconciliation.

Color and Material are SINGLE_VALUE_ASPECTS on eBay, but GIGA ships combined
supplier values ("Black+Gold", "Natural Wood+Brown", "Beige,Light Brown").
Handing those back verbatim as the corrected aspect writes a value no buyer
facet can match: 33 of the 130 Color corrections queued on 2026-08-10 were of
that shape. The comparison was already component-aware; only the ``expected``
value carried the raw blob.
"""

from __future__ import annotations

import pytest

from src.utils.source_parameter_alignment import (
    find_source_parameter_mismatches,
    primary_component,
)


class TestPrimaryComponent:
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
    def _mismatch(self, field, attributes, aspects):
        rows = find_source_parameter_mismatches(
            source_attributes=attributes,
            source_title="Modern Accent Chair",
            source_description="A comfortable chair.",
            candidate_aspects=aspects,
        )
        return next((r for r in rows if r["field"] == field), None)

    def test_multi_colour_source_yields_a_single_writable_colour(self):
        row = self._mismatch("Color", {"Main Color": "Black+Gold"}, {"Color": ["White"]})
        assert row is not None
        assert row["expected"] == "Black"
        assert "+" not in row["expected"]

    def test_multi_material_source_yields_a_single_writable_material(self):
        row = self._mismatch(
            "Material", {"Main Material": "Wood+Metal"}, {"Material": ["Plastic"]}
        )
        assert row is not None
        assert row["expected"] == "Wood"

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
