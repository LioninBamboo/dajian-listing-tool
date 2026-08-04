from src.clients.real_ebay_client import _sanitize_inventory_identifiers
import batch_publish


def test_identifier_sanitizer_drops_upc_when_it_is_the_sku():
    aspects = {
        "Brand": ["AquaVerve"],
        "MPN": ["Does Not Apply"],
        "UPC": ["N707S185531G"],
    }

    brand, mpn, identifiers = _sanitize_inventory_identifiers(
        "N707S185531G",
        aspects,
        {},
    )

    assert brand == "AquaVerve"
    assert mpn == "Does Not Apply"
    assert identifiers == {}
    assert "UPC" not in aspects


def test_identifier_sanitizer_backfills_mpn_for_unbranded_items():
    aspects = {
        "Brand": ["Unbranded"],
    }

    brand, mpn, identifiers = _sanitize_inventory_identifiers(
        "W1117S00330",
        aspects,
        {},
    )

    assert brand == "Unbranded"
    assert mpn == "Does Not Apply"
    assert identifiers == {}
    assert aspects["MPN"] == ["Does Not Apply"]


def test_identifier_sanitizer_preserves_official_unavailable_upc_text():
    aspects = {
        "Brand": ["AquaVerve"],
        "UPC": ["Does Not Apply"],
    }

    brand, mpn, identifiers = _sanitize_inventory_identifiers(
        "W1578P515534",
        aspects,
        {},
    )

    assert brand == "AquaVerve"
    assert mpn == ""
    assert identifiers == {"upc": ["Does not apply"]}
    assert aspects["UPC"] == ["Does not apply"]


def test_missing_upc_publish_error_adds_official_unavailable_marker():
    aspects = {"Brand": ["AquaVerve"]}

    fixed = batch_publish._try_fix_missing_product_identifier(
        "The UPC field is missing. Please add UPC to the listing and try again.",
        aspects,
    )

    assert fixed is True
    assert aspects["UPC"] == ["Does not apply"]
