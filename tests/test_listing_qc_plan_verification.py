from src.services.listing_qc_plan_verification import verify_publish_dry_run


def dry(sku, *, status="dry_run", images=2, dimensions=True, compatibility=None):
    return {
        "sku": sku,
        "status": status,
        "images": images,
        "dimensions": {
            "Item Length": ["1 in"],
            "Item Width": ["1 in"],
            "Item Height": ["1 in"],
        } if dimensions else {},
        "compatibility_issues": compatibility or [],
    }


def test_dry_run_verification_requires_exact_set_and_structural_fields():
    result = verify_publish_dry_run(["A", "B"], [dry("A"), dry("B")])
    assert result["verified"] is True
    assert result["allowlist_minus_results"] == []
    assert result["results_minus_allowlist"] == []


def test_dry_run_verification_reports_mismatch_and_bad_rows():
    result = verify_publish_dry_run(
        ["A", "B"],
        [dry("A"), dry("C", status="error", images=1, dimensions=False, compatibility=["bad"])],
    )
    assert result["verified"] is False
    assert result["allowlist_minus_results"] == ["B"]
    assert result["results_minus_allowlist"] == ["C"]
    assert {item["reason"] for item in result["structural_failures"]} == {
        "status_not_dry_run",
        "missing_dimensions",
        "images_below_minimum",
        "compatibility_issues",
    }

