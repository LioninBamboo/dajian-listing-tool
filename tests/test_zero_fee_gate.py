"""Zero-fee gate: example table, path patches, and unguarded-caller scan."""

from __future__ import annotations

import ast
import inspect
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from src.utils.zero_fee_gate import (
    REASON_BLACKLIST_UNAVAILABLE,
    REASON_CATEGORY_UNKNOWN,
    REASON_DISCOUNT_INVALID,
    REASON_FEE_NONZERO,
    REASON_HIGH_FEE_CATEGORY,
    REASON_NET_NEGATIVE,
    REASON_PREVIEW_UNAVAILABLE,
    SOURCE_INVENTORY,
    SOURCE_VERIFY_ADD,
    FeeGateRaw,
    FeeGateResult,
    ZeroFeeGateBlocked,
    assert_zero_fee_before_live,
    evaluate_fee_gate,
    publish_offer_if_zero_fee,
    wrapper_reason,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "zero_fee_gate"
SKU = "W1580P554752"
CAT_OK = "175758"
PATH_OK = "Home & Garden > Furniture > Coffee Tables"


def _load_fixture(name: str):
    import json

    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def _blacklist(tmp_path, ids=None, paths=None, applies=None, text=None, name="bl.json"):
    path = tmp_path / name
    if text is not None:
        path.write_text(text, encoding="utf-8")
        return str(path)
    payload = {"categoryIds": list(ids or ["00000"])}
    if paths:
        payload["categories"] = [{"id": cid, "path": p} for cid, p in paths]
    if applies is not None:
        payload["applies_to_stores"] = applies
    import json
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _inv(*fees):
    return {
        "feeSummaries": [{
            "fees": [
                {
                    "feeType": fee_type,
                    "amount": {"value": value, "currency": currency},
                    **({"promotionalDiscount": {"value": discount, "currency": currency}} if discount is not None else {}),
                }
                for fee_type, value, currency, discount in fees
            ]
        }]
    }


def _preview(payload, source=SOURCE_INVENTORY, http_status=200, ack=None, error=None):
    return lambda: FeeGateRaw(
        source=source,
        payload=payload,
        http_status=http_status,
        ack=ack,
        error=error,
    )


def _gate(tmp_path, *, category_id=CAT_OK, category_path=PATH_OK, preview=None, **kwargs):
    return evaluate_fee_gate(
        category_id=category_id,
        category_path=category_path,
        preview=preview or _preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=_blacklist(tmp_path),
        **kwargs,
    )


def test_row1_zero_insertion_passes_and_logs(tmp_path, caplog):
    sink = []
    with caplog.at_level(logging.INFO):
        result = assert_zero_fee_before_live(
            path_name="test.row1",
            sku=SKU,
            category_id=CAT_OK,
            category_path=PATH_OK,
            preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
            offer_id="offer-1",
            results_sink=sink,
            blacklist_path=_blacklist(tmp_path),
        )
    assert result.ok
    assert result.reason is None
    assert sink[0]["lines"][0]["amount"] == "0.00"
    assert SKU in caplog.text
    assert "INSERTION_FEE" in caplog.text


@pytest.mark.parametrize(
    "value",
    ["0.01", "0.35", "20.00"],
)
def test_rows_2_3_4_10_any_positive_blocks(tmp_path, value):
    result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", value, "USD", None))))
    assert result.ok is False
    assert result.reason == REASON_FEE_NONZERO


def test_sub_cent_gross_rounds_to_zero_cents(tmp_path):
    result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", "0.0000000001", "USD", None))))
    assert result.ok
    assert result.lines[0].amount == Decimal("0.0000000001")
    assert result.reason is None


def test_row5_non_insertion_line_blocks(tmp_path):
    result = _gate(
        tmp_path,
        preview=_preview(_inv(
            ("INSERTION_FEE", "0.00", "USD", None),
            ("SUBTITLE_FEE", "0.10", "USD", None),
        )),
    )
    assert result.reason == REASON_FEE_NONZERO


@pytest.mark.parametrize(
    "payload",
    [{}, {"feeSummaries": []}, None],
)
def test_row6_empty_payload_unavailable(tmp_path, payload):
    result = _gate(tmp_path, preview=_preview(payload))
    assert result.reason == REASON_PREVIEW_UNAVAILABLE


def test_row7_missing_insertion_line_unavailable(tmp_path):
    result = _gate(tmp_path, preview=_preview(_inv(("FINAL_VALUE_FEE", "0.00", "USD", None))))
    assert result.reason == REASON_PREVIEW_UNAVAILABLE


@pytest.mark.parametrize(
    "raw",
    [
        FeeGateRaw(source=SOURCE_INVENTORY, error=RuntimeError("boom")),
        FeeGateRaw(source=SOURCE_INVENTORY, error=TimeoutError("timeout")),
        FeeGateRaw(source=SOURCE_INVENTORY, payload=_inv(("INSERTION_FEE", "0.00", "USD", None)), http_status=500),
        FeeGateRaw(source=SOURCE_VERIFY_ADD, payload="<Ack>Failure</Ack>", http_status=200, ack="Failure"),
    ],
)
def test_row8_errors_unavailable(tmp_path, raw):
    result = _gate(tmp_path, preview=lambda: raw)
    assert result.reason == REASON_PREVIEW_UNAVAILABLE


@pytest.mark.parametrize(
    "value,currency",
    [("abc", "USD"), ("0.00", ""), ("0.00", "EUR")],
)
def test_row9_bad_amount_or_currency_unavailable(tmp_path, value, currency):
    result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", value, currency, None))))
    assert result.reason == REASON_PREVIEW_UNAVAILABLE


def test_row11_powersports_id_blocks(tmp_path):
    bl = _blacklist(tmp_path, ids=["66466"])
    result = evaluate_fee_gate(
        category_id="66466",
        category_path="eBay Motors > Powersports",
        preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=bl,
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY


def test_row12_boats_prefix_blocks(tmp_path):
    result = _gate(
        tmp_path,
        category_id="26429",
        category_path="eBay Motors > Boats > Power Boats",
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY


def test_row13_trailers_segment_blocks(tmp_path):
    result = _gate(
        tmp_path,
        category_id="63738",
        category_path="eBay Motors > Other Vehicles & Trailers > Trailers > Commercial Truck Trailers",
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY


def test_row14_no_substring_id_match(tmp_path):
    result = evaluate_fee_gate(
        category_id="142428",
        category_path=PATH_OK,
        preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=_blacklist(tmp_path, ids=["42428"]),
    )
    assert result.ok


def test_row15_reverse_substring_does_not_match(tmp_path):
    result = evaluate_fee_gate(
        category_id="42428",
        category_path=PATH_OK,
        preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=_blacklist(tmp_path, ids=["142428"]),
    )
    assert result.ok


def test_row16_trailer_hitches_does_not_match_trailers(tmp_path):
    result = _gate(
        tmp_path,
        category_id="33653",
        category_path="eBay Motors > Parts & Accessories > Towing & Hauling > Trailer Hitches",
    )
    assert result.ok


def test_row17_whole_segment_trailers_blocks(tmp_path):
    result = _gate(
        tmp_path,
        category_id="999",
        category_path="Home > Trailers > Utility",
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY


def test_row18_prefix_trap_boatsxyz_passes(tmp_path):
    result = _gate(
        tmp_path,
        category_id="1",
        category_path="eBay Motors > Boatsxyz",
    )
    assert result.ok


def test_row19_empty_category_unknown(tmp_path):
    result = _gate(tmp_path, category_id="", category_path="")
    assert result.reason == REASON_CATEGORY_UNKNOWN


@pytest.mark.parametrize(
    "text",
    [None, "{not-json", '{"categoryIds": [], "categories": []}'],
)
def test_row20_missing_invalid_empty_blacklist_unavailable(tmp_path, text):
    if text is None:
        path = str(tmp_path / "missing.json")
    else:
        path = _blacklist(tmp_path, text=text)
    result = evaluate_fee_gate(
        category_id=CAT_OK,
        category_path=PATH_OK,
        preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=path,
    )
    assert result.reason == REASON_BLACKLIST_UNAVAILABLE


def test_row21_applies_to_stores_exclude_is_not_unavailable(tmp_path, monkeypatch):
    from src.utils import publish_fee_preview

    class _Profile:
        brand_name = "GrovePop"

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: _Profile())
    publish_fee_preview._EMPTY_BLACKLIST_WARNED.clear()
    path = _blacklist(tmp_path, ids=["175758"], applies=["main", "aqua"])
    result = evaluate_fee_gate(
        category_id=CAT_OK,
        category_path=PATH_OK,
        preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=path,
    )
    assert result.ok
    assert result.reason is None


def test_row22_bypass_env_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW", "1")
    result = _gate(tmp_path, preview=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert result.reason == REASON_PREVIEW_UNAVAILABLE


def test_row23_promotional_discount_is_netted(tmp_path, caplog):
    trading = (
        "<VerifyAddFixedPriceItemResponse><Ack>Success</Ack><Fees>"
        "<Fee><Name>InsertionFee</Name><Fee currencyID=\"USD\">0.35</Fee>"
        "<PromotionalDiscount currencyID=\"USD\">0.35</PromotionalDiscount></Fee>"
        "</Fees></VerifyAddFixedPriceItemResponse>"
    )
    sink = []
    with caplog.at_level(logging.INFO):
        result = assert_zero_fee_before_live(
            path_name="test.row23",
            sku=SKU,
            category_id=CAT_OK,
            category_path=PATH_OK,
            preview=_preview(trading, source=SOURCE_VERIFY_ADD, ack="Success"),
            results_sink=sink,
            blacklist_path=_blacklist(tmp_path),
        )
    assert result.ok
    assert result.reason is None
    assert sink[0]["lines"][0]["amount"] == "0.35"
    assert sink[0]["lines"][0]["promotional_discount"] == "0.35"
    assert sink[0]["lines"][0]["net"] == "0.00"
    assert "gross=0.35" in caplog.text
    assert "discount=0.35" in caplog.text
    assert "net=0.00" in caplog.text


def test_real_ebay_insertion_and_listing_promo_covered_passes(tmp_path, caplog):
    payload = _load_fixture("ebay_insertion_listing_promo_covered.json")
    sink = []
    with caplog.at_level(logging.INFO):
        result = assert_zero_fee_before_live(
            path_name="test.ebay_real_covered",
            sku=SKU,
            category_id=CAT_OK,
            category_path=PATH_OK,
            preview=_preview(payload),
            results_sink=sink,
            blacklist_path=_blacklist(tmp_path),
        )
    assert result.ok
    assert [line["fee_type"] for line in sink[0]["lines"]] == [
        "InsertionFee",
        "ListingFee",
        "SubtitleFee",
    ]
    assert {line["net"] for line in sink[0]["lines"]} == {"0.00"}
    assert "gross=0.10" in caplog.text
    assert "discount=0.10" in caplog.text
    assert "net=0.00" in caplog.text


def test_promo_missing_means_discount_zero_so_gross_must_be_zero(tmp_path):
    result = _gate(
        tmp_path,
        preview=_preview(_load_fixture("ebay_insertion_promo_missing.json")),
    )
    assert result.ok is False
    assert result.reason == REASON_FEE_NONZERO
    assert result.lines[0].promotional_discount is None


def test_promo_null_means_discount_zero(tmp_path):
    payload = {
        "feeSummaries": [{
            "fees": [{
                "feeType": "InsertionFee",
                "amount": {"value": "0.00", "currency": "USD"},
                "promotionalDiscount": None,
            }]
        }]
    }
    result = _gate(tmp_path, preview=_preview(payload))
    assert result.ok
    payload["feeSummaries"][0]["fees"][0]["amount"]["value"] = "0.10"
    result = _gate(tmp_path, preview=_preview(payload))
    assert result.reason == REASON_FEE_NONZERO


def test_zero_gross_absent_discount_passes(tmp_path):
    result = _gate(tmp_path, preview=_preview(_inv(("InsertionFee", "0", "USD", None))))
    assert result.ok


@pytest.mark.parametrize(
    "name,reason",
    [
        ("ebay_discount_currency_mismatch.json", REASON_DISCOUNT_INVALID),
        ("ebay_discount_non_usd.json", REASON_DISCOUNT_INVALID),
        ("ebay_discount_garbage.json", REASON_DISCOUNT_INVALID),
        ("ebay_discount_exceeds_amount.json", REASON_NET_NEGATIVE),
        ("ebay_net_one_cent.json", REASON_FEE_NONZERO),
        ("ebay_unknown_feetype_nonzero_net.json", REASON_FEE_NONZERO),
        ("ebay_mixed_lines_one_nonzero.json", REASON_FEE_NONZERO),
    ],
)
def test_net_gate_boundary_fixtures_block(tmp_path, name, reason):
    result = _gate(tmp_path, preview=_preview(_load_fixture(name)))
    assert result.ok is False
    assert result.reason == reason


@pytest.mark.parametrize(
    "promo",
    [
        {"value": "0.10"},
        {"currency": "USD"},
        {"value": "", "currency": "USD"},
        {"value": "0.10", "currency": ""},
        {"value": None, "currency": "USD"},
        "0.10",
        0.10,
        ["USD", "0.10"],
    ],
)
def test_discount_garbage_or_missing_currency_blocks(tmp_path, promo):
    payload = {
        "feeSummaries": [{
            "fees": [{
                "feeType": "InsertionFee",
                "amount": {"value": "0.10", "currency": "USD"},
                "promotionalDiscount": promo,
            }]
        }]
    }
    result = _gate(tmp_path, preview=_preview(payload))
    assert result.reason == REASON_DISCOUNT_INVALID


def test_trading_verify_add_promo_covered_passes(tmp_path, caplog):
    xml = _load_fixture("trading_verify_add_promo_covered.xml")
    sink = []
    with caplog.at_level(logging.INFO):
        result = assert_zero_fee_before_live(
            path_name="test.trading_aqua_motors",
            sku=SKU,
            category_id=CAT_OK,
            category_path=PATH_OK,
            preview=_preview(xml, source=SOURCE_VERIFY_ADD, ack="Success"),
            results_sink=sink,
            blacklist_path=_blacklist(tmp_path),
        )
    assert result.ok
    assert [line["fee_type"] for line in sink[0]["lines"]] == ["InsertionFee", "ListingFee"]
    assert {line["net"] for line in sink[0]["lines"]} == {"0.00"}
    assert "gross=0.10" in caplog.text
    assert "discount=0.10" in caplog.text


def test_feetype_whole_value_no_substring(tmp_path):
    result = _gate(
        tmp_path,
        preview=_preview(_inv(
            ("FooInsertionFee", "0.00", "USD", None),
            ("InsertionFeeExtra", "0.00", "USD", None),
        )),
    )
    assert result.reason == REASON_PREVIEW_UNAVAILABLE
    result = _gate(
        tmp_path,
        preview=_preview(_inv(
            ("InsertionFee", "0.00", "USD", None),
            ("FooInsertionFee", "0.05", "USD", None),
        )),
    )
    assert result.reason == REASON_FEE_NONZERO


def test_row24_category_id_strip_both_sides(tmp_path):
    result = evaluate_fee_gate(
        category_id="66466",
        category_path=PATH_OK,
        preview=_preview(_inv(("INSERTION_FEE", "0.00", "USD", None))),
        lookup_attempted=True,
        blacklist_path=_blacklist(tmp_path, ids=[" 66466 "]),
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY


def test_multiple_nonzero_lines_all_logged(tmp_path):
    result = _gate(
        tmp_path,
        preview=_preview(_inv(
            ("INSERTION_FEE", "0.10", "USD", None),
            ("SUBTITLE_FEE", "0.20", "USD", None),
        )),
    )
    assert result.reason == REASON_FEE_NONZERO
    assert [line.fee_type for line in result.lines] == ["INSERTION_FEE", "SUBTITLE_FEE"]


def test_category_block_skips_preview(tmp_path):
    called = []
    result = evaluate_fee_gate(
        category_id="1",
        category_path="eBay Motors > Trailers",
        preview=lambda: called.append("preview") or _inv(("INSERTION_FEE", "9.00", "USD", None)),
        lookup_attempted=True,
        blacklist_path=_blacklist(tmp_path),
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY
    assert called == []


def test_unknown_fee_type_still_blocks_if_positive(tmp_path):
    result = _gate(
        tmp_path,
        preview=_preview(_inv(
            ("INSERTION_FEE", "0.00", "USD", None),
            ("WEIRD_NEW_FEE", "0.05", "USD", None),
        )),
    )
    assert result.reason == REASON_FEE_NONZERO


def test_negative_amount_unavailable(tmp_path):
    result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", "-0.05", "USD", None))))
    assert result.reason == REASON_PREVIEW_UNAVAILABLE


def test_numeric_and_string_zero_pass(tmp_path):
    for raw in (0, 0.00, "0", "0.00"):
        result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", raw, "USD", None))))
        assert result.ok, raw


def test_scientific_notation_sub_cent_rounds_to_zero(tmp_path):
    result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", "1e-8", "USD", None))))
    assert result.ok


def test_slash_and_gt_and_whitespace_normalize(tmp_path):
    result = _gate(
        tmp_path,
        category_id="x",
        category_path="eBay   Motors /  Boats /  Dinghies",
    )
    assert result.reason == REASON_HIGH_FEE_CATEGORY


def test_wrapper_maps_nonzero_to_legacy_name(tmp_path):
    result = _gate(tmp_path, preview=_preview(_inv(("INSERTION_FEE", "0.01", "USD", None))))
    assert wrapper_reason(result) == "high_insertion_fee"


def test_wrapper_keeps_discount_reasons_clear(tmp_path):
    mismatch = _gate(tmp_path, preview=_preview(_load_fixture("ebay_discount_currency_mismatch.json")))
    assert wrapper_reason(mismatch) == REASON_DISCOUNT_INVALID
    negative = _gate(tmp_path, preview=_preview(_load_fixture("ebay_discount_exceeds_amount.json")))
    assert wrapper_reason(negative) == REASON_NET_NEGATIVE


def test_publish_offer_helper_blocks_write(tmp_path):
    published = []

    class Client:
        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.01", "USD", None))

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "L"}

        def delete_offer(self, offer_id):
            return True

    with pytest.raises(ZeroFeeGateBlocked):
        publish_offer_if_zero_fee(
            ebay_client=Client(),
            offer_id="o1",
            path_name="test.helper",
            sku=SKU,
            category_id=CAT_OK,
            category_path=PATH_OK,
            blacklist_path=_blacklist(tmp_path),
        )
    assert published == []


def test_publish_offer_helper_calls_gate_before_write(tmp_path, monkeypatch):
    order = []

    class Client:
        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.00", "USD", None))

        def publish_offer(self, offer_id):
            order.append("write")
            return {"listingId": "L"}

    real = assert_zero_fee_before_live

    def _spy(**kwargs):
        order.append(("gate", kwargs["sku"], kwargs["path_name"], kwargs["category_id"]))
        return real(**kwargs)

    monkeypatch.setattr("src.utils.zero_fee_gate.assert_zero_fee_before_live", _spy)
    publish_offer_if_zero_fee(
        ebay_client=Client(),
        offer_id="o1",
        path_name="test.helper",
        sku=SKU,
        category_id=CAT_OK,
        category_path=PATH_OK,
        blacklist_path=_blacklist(tmp_path),
    )
    assert order[0][0] == "gate"
    assert order[0][1] == SKU
    assert order[-1] == "write"


def _coffee_product():
    return {
        "sku": "W3204P565677",
        "title": "Coffee Table",
        "description": "Coffee table with storage shelf. Assembly Required Yes.",
        "price": 50.0,
        "suggested_price": 99.0,
        "shipping": 0,
        "images": ["https://example.com/a.jpg"],
        "attributes": {
            "Assembled Length (in.)": "40",
            "Assembled Width (in.)": "20",
            "Assembled Height (in.)": "18",
        },
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 40},
        "optimization": {
            "title": "Coffee Table",
            "description": (
                "<div><h3>KEY FEATURES</h3><ul>"
                "<li>Storage shelf keeps living room essentials organized.</li>"
                "<li>Sturdy frame supports everyday household use.</li>"
                "<li>Compact profile fits apartments and family rooms.</li>"
                "</ul></div>"
            ),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Coffee Table"],
                "Assembly Required": ["Yes"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        },
    }


class _FakeOAuth:
    def __init__(self, environment):
        self.environment = environment

    def is_authorized(self):
        return True

    def get_application_token(self):
        return "app-token"

    def get_valid_token(self):
        return "token"


class _FakeCategoryMatcher:
    def __init__(self, oauth, **kwargs):
        self._category_id = kwargs.get("category_id", "38204")
        self._category_name = kwargs.get("category_name", "Coffee Tables")

    def canonicalize_category(self, title_context, category_id, category_name, description=None):
        return category_id, category_name

    def get_category_and_aspects(self, title, aspects, description):
        return self._category_id, self._category_name, dict(aspects)

    def is_category_plausible_for_text(self, title, category_id, category_name):
        return True

    def _get_category_aspects(self, category_id):
        return [], []


def _stub_batch(monkeypatch, client_factory):
    import batch_publish

    monkeypatch.setattr(batch_publish, "enrich_product_dimensions", lambda product: None)
    monkeypatch.setattr(batch_publish, "validate_product", lambda product: [])
    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", lambda product, market_p: 99.0)
    monkeypatch.setattr(batch_publish, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "_is_sellable_leaf_category", lambda oauth, category_id: True)
    monkeypatch.setattr(
        batch_publish,
        "run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": [],
            "warnings": [],
            "source_fingerprint": "source",
            "candidate_fingerprint": "candidate",
            "ruleset_version": "listing-qc-v2",
            "fact_sheet_version": 4,
        },
    )
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *a, **k: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(batch_publish, "update_product_status", lambda *a, **k: None)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", client_factory)
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _FakeOAuth)
    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", lambda oauth: _FakeCategoryMatcher(oauth))
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", lambda oauth: object())


def test_batch_publish_inventory_block_does_not_write(monkeypatch, tmp_path):
    import batch_publish

    published = []
    deleted = []

    class Client:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {}

        def create_offer(self, *a, **k):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "UNPUBLISHED"}

        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.01", "USD", None))

        def delete_offer(self, offer_id):
            deleted.append(offer_id)

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "L"}

    _stub_batch(monkeypatch, Client)
    result = batch_publish.publish_single_product(_coffee_product(), dry_run=False)
    assert result["status"] == "error"
    assert result.get("blocked_fee") is True
    assert published == []
    assert deleted == ["offer-1"]


def test_batch_publish_inventory_promo_covered_net_zero_publishes(monkeypatch, tmp_path):
    import batch_publish

    published = []
    deleted = []

    class Client:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {}

        def create_offer(self, *a, **k):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "UNPUBLISHED"}

        def get_listing_fees(self, offer_id):
            return _load_fixture("ebay_insertion_listing_promo_covered.json")

        def get_inventory_item(self, sku):
            return {"product": {}}

        def delete_offer(self, offer_id):
            deleted.append(offer_id)

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "L1"}

    monkeypatch.setattr(
        "batch_publish.verify_publish_readback",
        lambda *a, **k: {"status": "ok", "passed": True, "issues": [], "transport_failures": []},
    )
    monkeypatch.setattr("batch_publish.build_publish_readback_expectation", lambda **k: {})
    _stub_batch(monkeypatch, Client)
    result = batch_publish.publish_single_product(_coffee_product(), dry_run=False)
    assert result["status"] == "success"
    assert published == ["offer-1"]
    assert deleted == []


def test_batch_publish_inventory_gate_runs_before_write(monkeypatch, tmp_path):
    import batch_publish

    order = []

    class Client:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {}

        def create_offer(self, *a, **k):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "UNPUBLISHED"}

        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.00", "USD", None))

        def get_inventory_item(self, sku):
            return {"product": {}}

        def publish_offer(self, offer_id):
            order.append("write")
            return {"listingId": "L1"}

    def _spy(**kwargs):
        order.append(("gate", kwargs["sku"], kwargs["path_name"], kwargs["category_id"]))
        return FeeGateResult(True, None, (), kwargs["category_id"], kwargs["category_path"])

    monkeypatch.setattr("batch_publish.assert_zero_fee_before_live", _spy)
    monkeypatch.setattr(
        "batch_publish.verify_publish_readback",
        lambda *a, **k: {"status": "ok", "passed": True, "issues": [], "transport_failures": []},
    )
    monkeypatch.setattr("batch_publish.build_publish_readback_expectation", lambda **k: {})
    _stub_batch(monkeypatch, Client)
    result = batch_publish.publish_single_product(_coffee_product(), dry_run=False)
    assert order[0][0] == "gate"
    assert order[0][1] == "W3204P565677"
    assert order[0][2] == "batch_publish.inventory"
    assert order[-1] == "write"
    assert result["status"] == "success"


def test_batch_publish_motors_block_does_not_write(monkeypatch, tmp_path):
    import batch_publish
    published = []

    class Client:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def build_motors_add_fixed_price_xml(self, *a, **k):
            return "<AddFixedPriceItemRequest/>"

        def verify_add_fixed_price_item_motors(self, xml):
            return FeeGateRaw(
                source=SOURCE_VERIFY_ADD,
                payload=(
                    "<r><Ack>Success</Ack><Fees><Fee><Name>InsertionFee</Name>"
                    "<Fee currencyID=\"USD\">0.35</Fee></Fee></Fees></r>"
                ),
                http_status=200,
                ack="Success",
            )

        def add_fixed_price_item_motors(self, *a, **k):
            published.append("motors")
            return {"itemId": "X"}

    monkeypatch.setattr("batch_publish.uses_motors_trading_channel", lambda *a, **k: True)
    _stub_batch(monkeypatch, Client)
    result = batch_publish.publish_single_product(_coffee_product(), dry_run=False)
    assert result["status"] != "success"
    assert published == []


def test_batch_publish_motors_promo_covered_net_zero_publishes(monkeypatch):
    import batch_publish

    published = []

    class Client:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def build_motors_add_fixed_price_xml(self, *a, **k):
            return "<AddFixedPriceItemRequest/>"

        def verify_add_fixed_price_item_motors(self, xml):
            return FeeGateRaw(
                source=SOURCE_VERIFY_ADD,
                payload=_load_fixture("trading_verify_add_promo_covered.xml"),
                http_status=200,
                ack="Success",
            )

        def add_fixed_price_item_motors(self, *a, **k):
            published.append("motors")
            return {"itemId": "1887", "ack": "Success"}

    monkeypatch.setattr("batch_publish.uses_motors_trading_channel", lambda *a, **k: True)
    _stub_batch(monkeypatch, Client)
    result = batch_publish.publish_single_product(_coffee_product(), dry_run=False)
    assert result["status"] == "success"
    assert published == ["motors"]


def test_batch_publish_motors_gate_before_write(monkeypatch):
    import batch_publish

    order = []

    class Client:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def build_motors_add_fixed_price_xml(self, *a, **k):
            return "<AddFixedPriceItemRequest/>"

        def verify_add_fixed_price_item_motors(self, xml):
            return FeeGateRaw(source=SOURCE_VERIFY_ADD, payload="<Ack>Success</Ack>", http_status=200)

        def add_fixed_price_item_motors(self, *a, **k):
            order.append("write")
            return {"itemId": "1887", "ack": "Success"}

    def _spy(**kwargs):
        order.append(("gate", kwargs["path_name"], kwargs["sku"]))
        return FeeGateResult(True, None, (), kwargs["category_id"], kwargs["category_path"])

    monkeypatch.setattr("batch_publish.assert_zero_fee_before_live", _spy)
    monkeypatch.setattr("batch_publish.uses_motors_trading_channel", lambda *a, **k: True)
    _stub_batch(monkeypatch, Client)
    result = batch_publish.publish_single_product(_coffee_product(), dry_run=False)
    assert order[0][0] == "gate"
    assert order[0][1] == "batch_publish.motors_trading"
    assert order[-1] == "write"
    assert result["status"] == "success"


def test_ebay_publisher_do_publish_blocks_and_remap_retry(monkeypatch, tmp_path):
    from src.services.ebay_publisher import EbayPublisher, PublishStatus

    published = []

    class Client:
        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.01", "USD", None))

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "L"}

        def delete_offer(self, offer_id):
            return True

    pub = object.__new__(EbayPublisher)
    pub.ebay_client = Client()
    pub.logger = logging.getLogger("test")
    pub._publish_product = {"sku": SKU}
    result = pub._do_publish("offer-1", CAT_OK, "Coffee Tables", {})
    assert result.status == PublishStatus.ERROR
    assert published == []

    order = []

    def _pass(**kwargs):
        order.append(("gate", kwargs["path_name"], kwargs["sku"]))
        return FeeGateResult(True, None, (), kwargs["category_id"], kwargs["category_path"])

    class PassClient:
        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.00", "USD", None))

        def publish_offer(self, offer_id):
            order.append("write")
            return {"listingId": "L9"}

    monkeypatch.setattr("src.utils.zero_fee_gate.assert_zero_fee_before_live", _pass)
    pub.ebay_client = PassClient()
    result = pub._do_publish("offer-2", CAT_OK, "Coffee Tables", {})
    assert order[0][0] == "gate"
    assert order[0][1] == "ebay_publisher._do_publish"
    assert order[0][2] == SKU
    assert order[-1] == "write"
    assert result.status == PublishStatus.SUCCESS


def test_tools_publish_product_both_functions_gated(monkeypatch, tmp_path):
    import tools.publish_product as pp

    published = []

    class Client:
        def oauth(self):
            return self

        def is_authorized(self):
            return True

        def create_or_replace_inventory_item(self, *a, **k):
            return {}

        def create_offer(self, *a, **k):
            return {"offerId": "o1", "status": "NEW"}

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "L"}

        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.01", "USD", None))

        def delete_offer(self, offer_id):
            return True

    client = Client()
    client.oauth = client
    monkeypatch.setattr(pp, "create_real_ebay_client", lambda *a, **k: client)
    assert pp.publish_product_complete() is False
    assert published == []
    assert pp.publish_custom("SKU1", "Title", 1.0) is False
    assert published == []

    order = []

    def _pass(**kwargs):
        order.append(kwargs["path_name"])
        return FeeGateResult(True, None, (), kwargs["category_id"], kwargs["category_path"])

    monkeypatch.setattr("src.utils.zero_fee_gate.assert_zero_fee_before_live", _pass)
    client.get_listing_fees = lambda offer_id: _inv(("INSERTION_FEE", "0.00", "USD", None))
    assert pp.publish_custom("SKU1", "Title", 1.0) is True
    assert "tools.publish_custom" in order
    assert published == ["o1"]


def test_variation_publisher_block_does_not_publish(tmp_path, monkeypatch):
    from src.services.variation_publisher import publish_variation_group
    _VARIANTS = [
        {"sku": "EB-9-a", "value": "Style 1", "price": 16.99, "image_urls": ["a.jpg"]},
        {"sku": "EB-9-b", "value": "Style 2", "price": 21.99, "image_urls": ["b.jpg"]},
    ]
    _COMMON = {"Brand": ["Unbranded"], "Type": ["Plush"]}

    class _FakeClient:
        def __init__(self):
            self.items = []
            self.offers = []
            self.groups = []
            self.published = []

        def create_or_replace_inventory_item(self, sku, product):
            self.items.append((sku, product))
            return {"sku": sku}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            self.offers.append((sku, price, category_id))
            return {"offerId": f"off-{sku}"}

        def create_or_replace_inventory_item_group(self, group_key, group):
            self.groups.append((group_key, group))
            return {"status": "ok"}

        def publish_by_inventory_item_group(self, group_key, marketplace_id=None):
            self.published.append(group_key)
            return {"listingId": "LISTING123"}

        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.00", "USD", None))

    c = _FakeClient()
    c.get_listing_fees = lambda offer_id: _inv(("INSERTION_FEE", "0.25", "USD", None))
    out = publish_variation_group(
        c, group_key="EB-9", title="Series", description="d",
        image_urls=["a.jpg"], common_aspects=_COMMON, varies_by_aspect="Style",
        variants=_VARIANTS, category_id="261068", dry_run=False,
    )
    assert out["status"] == "error"
    assert c.published == []


def test_sync_relist_block_does_not_write(monkeypatch, tmp_path):
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    db = tmp_path / "e.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE collected_products (sku TEXT, listing_id TEXT, status TEXT)")
    conn.execute("INSERT INTO collected_products VALUES ('S1','OLD','PUBLISHED')")
    conn.commit()
    conn.close()

    calls = []

    class Trading:
        def call(self, name, body, site_id=None):
            calls.append(name)
            if name == "VerifyRelistItem":
                return (
                    "<r><Ack>Success</Ack><Fees><Fee><Name>InsertionFee</Name>"
                    "<Fee currencyID=\"USD\">0.35</Fee></Fee></Fees></r>"
                )
            raise AssertionError("RelistFixedPriceItem must not run")

    svc = InventorySyncService(db_path=str(db))
    svc.logger = logging.getLogger("test")
    monkeypatch.setattr(svc, "_trading_client", lambda: (Trading(), "100"))
    ok = svc._relist_fixed_price_item(
        "S1",
        "OLD",
        2,
        "100",
        snapshot={"category_id": CAT_OK, "category_name": "Coffee Tables"},
    )
    assert ok is False
    assert calls == ["VerifyRelistItem"]
    conn = sqlite3.connect(db)
    action = conn.execute("SELECT action FROM inventory_sync_log WHERE sku='S1'").fetchone()[0]
    conn.close()
    assert action == "relist_blocked_fee"


def test_sync_relist_promo_covered_net_zero_writes(monkeypatch, tmp_path):
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    db = tmp_path / "e.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE collected_products (sku TEXT, listing_id TEXT, status TEXT)")
    conn.execute("INSERT INTO collected_products VALUES ('S1','OLD','PUBLISHED')")
    conn.commit()
    conn.close()
    calls = []

    class Trading:
        def call(self, name, body, site_id=None):
            calls.append(name)
            if name == "VerifyRelistItem":
                return (
                    "<r><Ack>Success</Ack><Fees><Fee><Name>InsertionFee</Name>"
                    "<Fee currencyID=\"USD\">0.10</Fee>"
                    "<PromotionalDiscount currencyID=\"USD\">0.10</PromotionalDiscount>"
                    "</Fee></Fees></r>"
                )
            return "<r><Ack>Success</Ack><ItemID>189063211766</ItemID></r>"

    svc = InventorySyncService(db_path=str(db))
    svc.logger = logging.getLogger("test")
    monkeypatch.setattr(svc, "_trading_client", lambda: (Trading(), "100"))
    monkeypatch.setattr(svc, "_persist_listing_id", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_verify_trading_quantity", lambda *a, **k: True)
    monkeypatch.setattr(svc, "_record_sold_through_restock", lambda *a, **k: None)
    ok = svc._relist_fixed_price_item(
        "S1", "OLD", 2, "100",
        snapshot={"category_id": CAT_OK, "category_name": "Coffee Tables"},
    )
    assert ok is True
    assert calls == ["VerifyRelistItem", "RelistFixedPriceItem"]


def test_sync_relist_gate_before_write(monkeypatch, tmp_path):
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    db = tmp_path / "e.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE collected_products (sku TEXT, listing_id TEXT, status TEXT)")
    conn.execute("INSERT INTO collected_products VALUES ('S1','OLD','PUBLISHED')")
    conn.commit()
    conn.close()
    order = []

    class Trading:
        def call(self, name, body, site_id=None):
            order.append(name)
            if name == "VerifyRelistItem":
                return (
                    "<r><Ack>Success</Ack><Fees><Fee><Name>InsertionFee</Name>"
                    "<Fee currencyID=\"USD\">0.00</Fee></Fee></Fees></r>"
                )
            return "<r><Ack>Success</Ack><ItemID>189063211766</ItemID></r>"

    def _pass(**kwargs):
        order.append(("gate", kwargs["path_name"], kwargs["sku"]))
        return FeeGateResult(True, None, (), kwargs["category_id"], kwargs["category_path"])

    monkeypatch.setattr(
        "src.utils.zero_fee_gate.assert_zero_fee_before_live",
        _pass,
    )
    svc = InventorySyncService(db_path=str(db))
    svc.logger = logging.getLogger("test")
    monkeypatch.setattr(svc, "_trading_client", lambda: (Trading(), "100"))
    monkeypatch.setattr(svc, "_persist_listing_id", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_verify_trading_quantity", lambda *a, **k: True)
    monkeypatch.setattr(svc, "_record_sold_through_restock", lambda *a, **k: None)
    ok = svc._relist_fixed_price_item(
        "S1", "OLD", 2, "100",
        snapshot={"category_id": CAT_OK, "category_name": "Coffee Tables"},
    )
    assert ok is True
    assert order[0][0] == "gate"
    assert order[0][1] == "sync_service.relist_fixed_price_item"
    assert "RelistFixedPriceItem" in order
    assert order.index(("gate", "sync_service.relist_fixed_price_item", "S1")) < order.index("RelistFixedPriceItem")


def test_sales_health_ghost_oos_uses_gated_relist():
    import scripts.sales_health_check as shc
    source = inspect.getsource(shc.SalesHealthChecker._check_quantity_integrity)
    assert "update_ebay_quantity" in source
    relist_src = inspect.getsource(
        __import__("src.plugins.inventory_sync.sync_service", fromlist=["InventorySyncService"])
        .InventorySyncService._relist_fixed_price_item
    )
    assert "assert_zero_fee_before_live" in relist_src


def test_app_publish_paths_gated(monkeypatch):
    import app as app_mod

    published = []

    class OAuth:
        def is_authorized(self):
            return True

    class Client:
        def create_or_replace_inventory_item(self, *a, **k):
            return {}

        def create_or_replace_product_compatibility(self, *a, **k):
            return {}

        def delete_product_compatibility(self, *a, **k):
            return {}

        def create_offer(self, *a, **k):
            return {"offerId": "o1"}

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "L"}

        def get_listing_fees(self, offer_id):
            return _inv(("INSERTION_FEE", "0.01", "USD", None))

        def delete_offer(self, offer_id):
            return True

    monkeypatch.setattr(app_mod, "get_oauth_service", lambda: OAuth())
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", lambda *a, **k: Client())
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", lambda *a, **k: object())
    from src.utils.store_profile import StoreProfile
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: StoreProfile())
    monkeypatch.setattr("src.utils.approved_category.blocks_automated_category_change", lambda *a, **k: False)
    monkeypatch.setattr("src.utils.approved_category.is_valid_category_id", lambda *a, **k: True)
    monkeypatch.setattr("src.utils.category_decision.legacy_remap_candidate", lambda *a, **k: (CAT_OK, "publish_candidate"))
    monkeypatch.setattr("src.utils.category_decision.gate_publish_category_choice", lambda *a, **k: (CAT_OK, "ok", None))
    monkeypatch.setattr("src.utils.category_decision.prepublish_category_check", lambda *a, **k: type("V", (), {"ok": True})())
    monkeypatch.setattr(app_mod, "is_sellable_leaf_category", lambda *a, **k: True)
    monkeypatch.setattr(
        "src.services.vehicle_compatibility.analyze_ebay_motors_compatibility",
        lambda **k: type("C", (), {"compatible_products": [], "mode": "not_applicable"})(),
    )
    monkeypatch.setattr("src.services.vehicle_compatibility.apply_compatibility_aspects", lambda *a, **k: {"Brand": ["X"]})
    monkeypatch.setattr("src.services.vehicle_compatibility.serialize_compatibility_analysis", lambda *a, **k: {})
    product = {
        "sku": SKU,
        "title": "Coffee Table",
        "description": "desc",
        "suggested_price": 10,
        "images": ["https://example.com/a.jpg"],
        "optimization": {"title": "Coffee Table", "description": "desc", "categoryId": CAT_OK, "categoryName": "Coffee Tables", "aspects": {"Brand": ["X"]}},
    }
    result = app_mod.publish_to_ebay(product)
    assert result["status"] == "error"
    assert published == []

    order = []

    def _pass(**kwargs):
        order.append(kwargs["path_name"])
        return FeeGateResult(True, None, (), kwargs["category_id"], kwargs["category_path"])

    monkeypatch.setattr("src.utils.zero_fee_gate.assert_zero_fee_before_live", _pass)
    result = app_mod.publish_to_ebay(product)
    assert "app.publish_to_ebay" in order
    assert published == ["o1"]
    assert result["status"] == "success"


def test_server_publish_uses_helper():
    import server
    source = inspect.getsource(server.publish_product)
    assert "publish_offer_if_zero_fee" in source
    assert "server.publish_product" in source
    assert "publish_offer(" not in source.replace("publish_offer_if_zero_fee", "")


def test_app_auto_category_uses_helper():
    import app as app_mod
    source = inspect.getsource(app_mod.publish_with_auto_category)
    assert "publish_offer_if_zero_fee" in source
    assert "app.publish_with_auto_category" in source


def test_cro_relist_uses_gated_batch_publish():
    from src.services.cro_relist_lifecycle import _default_publish
    source = inspect.getsource(_default_publish)
    assert "publish_single_product" in source


def test_batch_all_fee_unavailable_does_not_exit_0(monkeypatch, tmp_path):
    import batch_publish

    monkeypatch.setattr(batch_publish, "LOG_DIR", tmp_path)
    monkeypatch.setattr(batch_publish, "get_ready_products", lambda *a, **k: [{"sku": "S1", "title": "T", "suggested_price": 1}])
    monkeypatch.setattr(
        batch_publish,
        "publish_single_product",
        lambda *a, **k: {"status": "error", "message": "fee_preview_unavailable", "blocked_fee": True},
    )
    monkeypatch.setattr("sys.argv", ["batch_publish.py"])
    assert batch_publish.main() == 2


def test_allow_publish_without_fee_preview_is_gone():
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".example", ".txt"}:
            continue
        if any(part in {"archive", ".git", ".venv"} for part in path.parts):
            continue
        if path.name.startswith("test_") and "zero_fee_gate" not in path.name and "publish_aspect" not in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW" in text or "allow_publish_without_fee_preview" in text:
            if path.name in {"test_zero_fee_gate.py", "test_publish_aspect_guards.py"}:
                continue
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []


def test_category_write_unresolved_path_blocks_even_with_blacklist(tmp_path):
    from src.utils.publish_fee_preview import guard_category_write_high_fee

    bl = _blacklist(tmp_path, ids=["__sentinel__"])
    blocked = guard_category_write_high_fee(
        category_id="85040",
        index={},
        blacklist_path=bl,
    )
    assert blocked and "UNVERIFIED" in blocked


_N_A_FUNCS = {
    ("src/clients/real_ebay_client.py", "publish_offer"),
    ("src/clients/real_ebay_client.py", "add_fixed_price_item_motors"),
    ("src/clients/ebay_client.py", "publish_offer"),
    ("src/services/ebay_publisher.py", "_publish_offer"),
    ("src/services/qc_experience_replay.py", "_publish_offer"),
    ("src/services/live_listing_writes.py", "_publish_offer_checked"),
    ("src/services/semantic_rewrite.py", "apply_rewrite"),
    ("src/services/semantic_rewrite.py", "_restore_from_backup"),
    ("scripts/repair_broken_listings.py", "repair_one"),
    ("scripts/reanalyze_and_revise_auto.py", "main"),
    ("scripts/reanalyze_and_revise_garden.py", "main"),
    ("scripts/trading_content_qc.py", "update_inventory_description"),
    ("scripts/semantic_rewrite_rollback.py", "rollback_sku"),
    ("scripts/audit_fix_active_listings.py", "fix_listing_on_ebay"),
    ("server.py", "publish_offer"),
    ("src/services/qc_experience_replay.py", "replay_partial_write_fixture"),
}


def test_no_new_unguarded_create_or_relist_callers():
    unguarded = []
    for path in list(ROOT.glob("*.py")) + list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")) + list((ROOT / "tools").rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if any(part in {"archive", "tests"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            key = (rel, node.name)
            if key in _N_A_FUNCS:
                continue
            if node.name in {"publish_offer_if_zero_fee"}:
                continue
            text = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
            has_write = False
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
                if name in {"publish_offer", "add_fixed_price_item_motors", "publish_by_inventory_item_group"}:
                    has_write = True
                for arg in list(child.args) + [kw.value for kw in child.keywords]:
                    if isinstance(arg, ast.Constant) and arg.value in {"RelistFixedPriceItem", "AddFixedPriceItem"}:
                        has_write = True
            if not has_write:
                continue
            if "publish_offer_if_zero_fee" in text or "assert_zero_fee_before_live" in text:
                continue
            if rel.startswith("src/clients/") or rel.endswith("motors_trading.py"):
                continue
            if rel.endswith("ebay_trading_client.py"):
                continue
            unguarded.append(f"{rel}:{node.name}")
    assert unguarded == [], unguarded
