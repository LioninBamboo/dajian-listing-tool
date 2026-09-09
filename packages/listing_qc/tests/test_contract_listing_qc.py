"""Contract tests for the shared listing_qc package."""

from listing_qc import (
    SCHEDULED_SOURCE_ASPECT_AUTOFIX_FIX_KEYS,
    SCHEDULED_SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES,
    category_allows_assembly_auto_yes,
    fix_attempt_succeeded,
    fix_key_matches_issue,
    residual_issues_for_item,
    store_email_label,
)


def test_whitelist_excludes_category_id():
    assert "categoryId" not in SCHEDULED_SOURCE_ASPECT_AUTOFIX_FIX_KEYS
    assert "category_mismatch" not in SCHEDULED_SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES


def test_store_email_label_prefers_injected_brand():
    assert store_email_label("GrovePop") == "GrovePop"
    assert store_email_label(brand_getter=lambda: "AquaRides") == "AquaRides"
    assert store_email_label() == "eBay/GIGA"


def test_residual_drops_covered_types_after_success():
    item = {
        "issues": [
            {"type": "assembly_package_conflict", "severity": "CRITICAL"},
            {"type": "semantic_feature", "severity": "HIGH"},
        ],
        "selected_fix_keys": ["Assembly Required"],
        "fixes_applied": ["Fixed Assembly Required: ['Yes']"],
    }
    residual = residual_issues_for_item(item)
    assert [i["type"] for i in residual] == ["semantic_feature"]


def test_residual_keeps_all_when_error():
    item = {
        "issues": [{"type": "assembly_package_conflict", "severity": "CRITICAL"}],
        "selected_fix_keys": ["Assembly Required"],
        "fixes_applied": ["ERROR: boom"],
    }
    assert residual_issues_for_item(item)
    assert not fix_attempt_succeeded(item["fixes_applied"])


def test_assembly_key_matches_package_conflict():
    assert fix_key_matches_issue("Assembly Required", "assembly_package_conflict")


def test_category_policy_skips_gabion_allows_chairs():
    assert category_allows_assembly_auto_yes("54235")
    assert category_allows_assembly_auto_yes(79684)
    assert not category_allows_assembly_auto_yes("20518")
    assert not category_allows_assembly_auto_yes(None)
