from src.clients.real_ebay_client import _sanitize_inventory_identifiers


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
