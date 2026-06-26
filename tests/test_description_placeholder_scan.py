import json

from scripts.scan_active_description_placeholders import (
    extract_source_measurements,
    find_description_placeholders,
    is_placeholder_value,
    load_skus_from_report,
    merge_sku_filters,
    should_use_empty_filter,
)


def test_find_description_placeholders_matches_exact_dimension_and_weight_rows():
    html = (
        "<table>"
        "<tr><td>Overall Dimensions (L×W×H)</td><td>Not specified</td></tr>"
        "<tr><td>Overall Weight</td><td>Not specified - see package details</td></tr>"
        "</table>"
    )

    matches = find_description_placeholders(html)

    assert matches["dimensions"] == [{"label": "Overall Dimensions (L×W×H)", "value": "Not specified"}]
    assert matches["weight"] == [{"label": "Overall Weight", "value": "Not specified - see package details"}]


def test_find_description_placeholders_ignores_non_placeholder_measurements():
    html = (
        "<table>"
        "<tr><td>Overall Dimensions (L×W×H)</td><td>80.0 × 26.0 × 44.0 inches</td></tr>"
        "<tr><td>Item Weight</td><td>52 lbs</td></tr>"
        "</table>"
    )

    matches = find_description_placeholders(html)

    assert matches == {"dimensions": [], "weight": []}


def test_is_placeholder_value_accepts_common_variants():
    assert is_placeholder_value("Not specified")
    assert is_placeholder_value("NOT AVAILABLE")
    assert is_placeholder_value("See Description")
    assert is_placeholder_value("Not specified - see package details")


def test_extract_source_measurements_merges_specs_and_attributes():
    attrs_raw = '{"Assembled Length (in.)": "80", "Assembled Width (in.)": "26"}'
    specs_raw = '{"Assembled Height (in.)": "44", "Product Weight (lbs.)": "52"}'

    measurements = extract_source_measurements(attrs_raw, specs_raw)

    assert measurements["length"] == 80.0
    assert measurements["width"] == 26.0
    assert measurements["height"] == 44.0
    assert measurements["weight"] == 52.0


def test_load_skus_from_report_preserves_unique_order(tmp_path):
    report_path = tmp_path / "scan.json"
    report_path.write_text(
        json.dumps(
            {
                "matches": [
                    {"sku": "A"},
                    {"sku": "B"},
                    {"sku": "A"},
                    {"sku": ""},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_skus_from_report(str(report_path)) == ["A", "B"]


def test_merge_sku_filters_deduplicates_across_sources():
    assert merge_sku_filters(["A", "B"], ["B", "C"], []) == ["A", "B", "C"]


def test_should_use_empty_filter_only_when_report_supplies_no_skus():
    assert should_use_empty_filter("logs/report.json", []) is True
    assert should_use_empty_filter(None, []) is False
    assert should_use_empty_filter("logs/report.json", ["A"]) is False