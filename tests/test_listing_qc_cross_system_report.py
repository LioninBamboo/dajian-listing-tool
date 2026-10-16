from src.services.listing_qc_cross_system_report import (
    build_cross_system_report,
    build_reconciliation_row,
    extract_ebay_quantity,
    extract_giga_inventory_snapshot,
)


def product(*, sku="SKU-1", status="PUBLISHED", stock=99, listing_id="L1"):
    return {
        "sku": sku,
        "status": status,
        "stock": stock,
        "listing_id": listing_id,
        "title": "Test product",
        "description": "Source description",
        "attributes": {"Material": "MDF"},
        "specs": {"Length": "10 in"},
        "url": "https://www.gigab2b.com/index.php?route=product/product&product_id=1",
        "images": ["a", "b"],
        "videos": [],
        "optimization": {"source_facts": {"materials": ["mdf"]}},
    }


def giga(qty, *, arrival=None):
    return {
        "sku": "SKU-1",
        "buyerInventoryInfo": {
            "totalBuyerAvailableInventory": qty,
            "totalFutureInventory": 0 if arrival is None else 5,
        },
        "sellerInventoryInfo": {"nextArrivalInventory": arrival},
    }


def ebay(qty):
    return {"availability": {"shipToLocationAvailability": {"quantity": qty}}}


def test_extract_inventory_quantities_from_known_api_shapes():
    assert extract_giga_inventory_snapshot(giga(7))["quantity"] == 7
    assert extract_giga_inventory_snapshot(giga(0, arrival={"arrivalDate": "2026-08-10"}))["arrival_present"] is True
    assert extract_ebay_quantity(ebay(0)) == 0


def test_zero_buyer_allocation_falls_back_to_seller_available_quantity():
    snapshot = extract_giga_inventory_snapshot({
        "buyerInventoryInfo": {"totalBuyerAvailableInventory": 0},
        "sellerInventoryInfo": {"sellerAvailableInventory": 12},
    })
    assert snapshot["quantity"] == 12
    assert snapshot["quantity_source"] == "sellerInventoryInfo.sellerAvailableInventory"


def test_giga_in_stock_and_ebay_zero_is_a_hold_not_an_end():
    row = build_reconciliation_row(product(), giga_inventory=giga(8), ebay_inventory=ebay(0))
    assert row["reconciliation"] == "giga_in_stock_ebay_zero"
    assert row["decision"] == "hold_and_reconcile"
    assert "do_not_end_listing" in row["risk_flags"]


def test_giga_oos_with_arrival_is_preserved_for_manual_review():
    row = build_reconciliation_row(
        product(),
        giga_inventory=giga(0, arrival={"arrivalDate": "2026-08-10"}),
        ebay_inventory=ebay(0),
    )
    assert row["giga"]["status"] == "out_of_stock"
    assert row["giga"]["arrival_present"] is True
    assert row["decision"] == "preserve_and_manual_review"


def test_missing_live_quantity_is_not_treated_as_zero():
    row = build_reconciliation_row(product(), giga_inventory=None, ebay_inventory=None)
    assert row["ebay"]["status"] == "unknown"
    assert row["reconciliation"] == "unknown_evidence"
    assert row["decision"] == "collect_evidence"


def test_local_stock_fallback_cannot_trigger_a_live_mismatch():
    row = build_reconciliation_row(product(stock=99), giga_inventory=None, ebay_inventory=ebay(0))
    assert row["giga"]["local_snapshot_status"] == "in_stock"
    assert row["giga"]["status"] == "unknown"
    assert row["reconciliation"] == "unknown_evidence"
    assert row["decision"] == "collect_evidence"


def test_not_published_rows_do_not_create_false_live_mismatch():
    row = build_reconciliation_row(
        product(status="READY", listing_id=None, stock=4),
        giga_inventory=giga(4),
        ebay_inventory=None,
    )
    assert row["ebay"]["status"] == "not_published"
    assert row["reconciliation"] == "not_published"


def test_report_summarizes_reconciliation_and_dedupes_scope_by_input():
    report = build_cross_system_report(
        [product(sku="A"), product(sku="B"), product(sku="C", status="READY", listing_id=None)],
        giga_inventory_by_sku={"A": giga(3)},
        ebay_inventory_by_sku={"A": ebay(0)},
    )
    assert report["summary"]["total"] == 3
    assert report["summary"]["giga_in_stock_ebay_zero"] == 1
    assert report["summary"]["not_published"] == 1  # C is not published in this fixture
