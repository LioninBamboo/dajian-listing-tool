from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.ebay_category_matcher import EbayCategoryMatcher


class _DummyOauth:
    def get_application_token(self):
        raise AssertionError("network call should not be needed for fallback tests")


def test_ping_pong_table_fallback_category_avoids_dining_set_match():
    matcher = EbayCategoryMatcher(_DummyOauth())

    category_id, category_name = matcher._fallback_category(
        "4.5ft Foldable Table Tennis Table Set with Net and 2 Paddles"
    )

    assert category_id == "97075"
    assert category_name == "Tables"


def test_ping_pong_table_category_plausibility_accepts_table_tennis_category():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "4.5ft Foldable Table Tennis Table Set with Net and 2 Paddles"

    assert matcher.is_category_plausible_for_text(title, "97075", "Tables") is True
    assert matcher.is_category_plausible_for_text(title, "107578", "Dining Sets") is False


def test_coffee_table_set_category_plausibility_rejects_dining_set_match():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "2-Piece Round Nesting Coffee Table Set Black Metal Modern Indoor Outdoor"

    assert matcher.is_category_plausible_for_text(title, "38204", "Coffee Tables") is True
    assert matcher.is_category_plausible_for_text(title, "107578", "Dining Sets") is False


def test_coffee_table_with_patio_keywords_is_not_canonicalized_to_patio_table():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = (
        "2-Piece Round Nesting Coffee Table Set, Modern Black Metal Side & End Tables, "
        "Space-Saving Accent Table Set for Living Room, Bedroom, Balcony, Patio, Indoor & Outdoor Use"
    )

    assert matcher.canonicalize_category(title, "38204", "Coffee Tables") == ("38204", "Coffee Tables")


def test_sofa_side_table_nightstand_not_remapped_to_sofas_or_beds():
    """2026-07-13 live incident: 'Storage Bedside Table and Sofa Side Table'
    was remapped 38199 -> 175758 (Bed Frames) by a substring 'storage bed'
    match, and console tables were remapped to 38208 by the bare sofa word."""
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Solid Wood Nightstand with Two Drawers and Pull-out Panel Storage Bedside Table and Sofa Side Table"

    category_id, _ = matcher.canonicalize_category(title, "38199", "Nightstands")

    assert category_id in (None, "38199")


def test_console_sofa_table_behind_couch_not_remapped_to_sofas():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "60 Inch Narrow Console Table with Built-in Power Outlet, Farmhouse Sofa Table Behind Couch, Entryway"

    category_id, _ = matcher.canonicalize_category(title, "38204", "Tables")

    assert category_id != "38208"


def test_real_sofa_still_remapped_to_sofas():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "71 Inch 3 Seater Sofa Corduroy Fabric Deep Seat Couch Comfy Loveseat"

    category_id, category_name = matcher.canonicalize_category(title, "38204", "Tables")

    assert category_id == "38208"
    assert category_name == "Sofas, Armchairs & Couches"


def test_sofa_with_storage_ottoman_stays_on_sofa_leaf():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Velvet Sofa Couch with Storage Ottoman"

    category_id, _ = matcher.canonicalize_category(
        title, "38208", "Sofas, Armchairs & Couches"
    )
    assert category_id == "38208"


def test_accent_chair_description_ottoman_does_not_remap():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Upholstered Accent Chair"
    description = "Includes a matching storage ottoman for extra seating and hidden storage."

    category_id, _ = matcher.canonicalize_category(
        title, "38208", "Sofas, Armchairs & Couches", description
    )
    assert category_id == "38208"


def test_indoor_rocking_chair_category_uses_rockers_gliders_leaf():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Upholstered Nursery Rocking Chair Glider for Living Room Reading"

    assert matcher._fallback_category(title)[0] == "66690"
    assert matcher.canonicalize_category(title, "20877", "Rocking Chairs") == (
        "66690",
        "Rockers, Gliders",
    )


def test_outdoor_porch_rocker_canonicalizes_to_outdoor_chairs():
    """GrovePop W465P255396: porch/outdoor rockers were stuck in indoor 66690
    because keyword 'rocking chair' beat outdoor-chair remap (2026-09-29)."""
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = (
        "Wooden Porch Rocker Chair, Fir Log Outdoor Rocking Chair, "
        "Patio Rockers for Outside, Light Gray"
    )

    assert matcher._fallback_category(title) == ("79682", "Patio Chairs")
    assert matcher.canonicalize_category(title, "66690", "Rockers, Gliders") == (
        "79682",
        "Patio Chairs",
    )
    assert matcher.canonicalize_category(title, "20877", "Rocking Chairs") == (
        "79682",
        "Patio Chairs",
    )
    # plausible() canonicalizes first, so a stale 66690 input becomes 79682 and passes.
    assert matcher.is_category_plausible_for_text(title, "79682", "Patio Chairs") is True


def test_legacy_laundry_hamper_category_uses_current_leaf():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Water Hyacinth Woven Laundry Hamper with Lid Natural Light Brown 18x18x23"

    assert matcher._fallback_category(title)[0] == "43517"
    assert matcher.canonicalize_category(title, "43527", "Laundry Hampers") == (
        "43517",
        "Laundry Carts & Hampers",
    )


def test_bell_tent_with_cooler_weather_copy_stays_in_tents():
    """2026-07-27: the rebuilt description said "cozy even in cooler weather".
    A bare \bcooler\b match set has_cooler and wanted to move 4 bell tents from
    Tents (179010) to Ice Chests & Coolers (79691). category_mismatch carries a
    categoryId fix key, so a --fix run would have re-categorised them on live."""
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Luxury 4 Season Bell Tent 13.1FT with Stove Jack Waterproof Glamping Yurt"
    description = (
        "Whether spring, autumn, or winter, you can stay warm and enjoy a cozy "
        "atmosphere even in cooler weather. Outstanding ventilation with mesh windows."
    )

    assert matcher.canonicalize_category(title, "179010", "Tents", description) == (
        "179010",
        "Tents",
    )


def test_comparative_cooler_phrases_do_not_imply_a_cooler_product():
    matcher = EbayCategoryMatcher(_DummyOauth())
    for phrase in (
        "ideal for cooler months on the patio",
        "keeps the room comfortable in cooler temperatures",
        "breathable mesh promotes cooler air circulation",
        "great for cooler evenings outdoors",
    ):
        assert matcher.canonicalize_category(
            "Outdoor Patio Sofa Set with Cushions", "139849", "Patio & Garden Furniture Sets", phrase
        ) != ("79691", "Ice Chests & Coolers"), phrase


def test_real_cooler_products_are_still_detected():
    matcher = EbayCategoryMatcher(_DummyOauth())
    for title in (
        "52QT Rotomolded Hard Cooler with Wheels and Bottle Opener",
        "Portable Cooler for Camping and Tailgating, 45 Can Capacity",
        "Insulated Cooler Bag Holds 30 Cans, Leakproof Liner",
    ):
        assert matcher.canonicalize_category(title, "20518", "Other") == (
            "79691",
            "Ice Chests & Coolers",
        ), title


def test_indoor_bench_mentioning_a_balcony_is_not_an_outdoor_daybed():
    """2026-07-27 live 误伤:两条软包储物凳被改判到 Outdoor Daybeds(138996)。
    描述明写 For Living Room, Entryway, Dormitory, Bedroom,只因末尾一句
    "or a leisure bench on the balcony" 触发 has_outdoor_context,叠加标题里的
    "Bench Daybed" 就成立。一个顺带提到的摆放场所不该压过四个室内房间。"""
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = (
        '65.75" Wide Modern Upholstered Storage Bench With Double Lids, '
        "Napped fabric Foot Stool With Rolled Armrest, Bench Daybed With Rubberwood Legs"
    )
    description = (
        "For Living Room, Entryway, Dormitory, Bedroom. Use it as a temporary seat "
        "in the study, or a leisure bench on the balcony."
    )

    category_id, _ = matcher.canonicalize_category(title, "38204", "Benches", description)

    assert category_id != "138996"
    assert category_id == "262980"  # Benches


def test_genuine_outdoor_daybeds_still_classified():
    matcher = EbayCategoryMatcher(_DummyOauth())
    for title, description in (
        ("Patio Rattan Daybed with Canopy and Cushions", "Weather resistant wicker for your garden."),
        ("Outdoor Daybed Round Rattan Sun Lounger", "Poolside lounging all summer."),
        ("Rattan Daybed with Canopy", "Perfect for the patio and poolside."),
    ):
        category_id, _ = matcher.canonicalize_category(title, "38204", "Benches", description)
        assert category_id == "138996", title


MOTORS_TRAILER_TREE = {
    "categoryTreeId": "100",
    "categoryTreeVersion": "1",
    "rootCategoryNode": {
        "category": {"categoryId": "6000", "categoryName": "eBay Motors"},
        "childCategoryTreeNodes": [
            {
                "category": {
                    "categoryId": "66468",
                    "categoryName": "Other Vehicles & Trailers",
                },
                "childCategoryTreeNodes": [
                    {
                        "category": {"categoryId": "6038", "categoryName": "Trailers"},
                    }
                ],
            }
        ],
    },
}


def _install_tree_cache(monkeypatch, tmp_path, trees=None):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    for tree_id, payload in (trees or {"100": MOTORS_TRAILER_TREE}).items():
        category_tree_cache.write_category_tree_cache(tree_id, payload, cache_dir)
    return cache_dir


@pytest.mark.real_category_tree
def test_get_category_path_builds_from_cached_tree(monkeypatch, tmp_path):
    http_calls = []

    def _forbid_http(*args, **kwargs):
        http_calls.append((args, kwargs))
        raise AssertionError("publish path must not fetch taxonomy trees")

    _install_tree_cache(monkeypatch, tmp_path)
    monkeypatch.setattr("src.services.ebay_category_matcher.requests.get", _forbid_http)
    matcher = EbayCategoryMatcher(_DummyOauth())
    path = matcher.get_category_path(
        "6038",
        category_name="Trailers",
        title="Bathroom Vanity with Sink",
    )
    assert path == "eBay Motors > Other Vehicles & Trailers > Trailers"
    assert http_calls == []


@pytest.mark.real_category_tree
def test_get_category_path_stale_cache_returns_empty(monkeypatch, tmp_path):
    import time

    from src.utils import category_tree_cache

    cache_dir = _install_tree_cache(monkeypatch, tmp_path)
    cache_file = cache_dir / "category_tree_100.json"
    stale = time.time() - (8 * 24 * 60 * 60)
    os.utime(cache_file, (stale, stale))
    category_tree_cache.reset_category_tree_cache()

    matcher = EbayCategoryMatcher(_DummyOauth())
    assert matcher.get_category_path("6038") == ""
    assert "6038" not in matcher._path_cache


def test_refresh_writes_get_category_tree_not_subtree(tmp_path):
    from src.utils.category_tree_cache import fetch_category_tree, refresh_category_tree_cache

    class _Session:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)

            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "categoryTreeId": "0",
                        "rootCategoryNode": {
                            "category": {"categoryId": "0", "categoryName": "Root"}
                        },
                    }

            return _Resp()

    session = _Session()
    payload = fetch_category_tree(
        "0",
        session=session,
        token="app-token",
        api_base="https://api.ebay.com",
    )
    assert session.urls == ["https://api.ebay.com/commerce/taxonomy/v1/category_tree/0"]
    assert "subtree" not in session.urls[0]

    fetched = []

    def _fetch(tree_id):
        fetched.append(tree_id)
        return payload

    written = refresh_category_tree_cache(("0", "100"), fetch=_fetch, cache_dir=tmp_path)
    assert fetched == ["0", "100"]
    assert [path.name for path in written] == ["category_tree_0.json", "category_tree_100.json"]
    snapshot = json.loads(written[0].read_text(encoding="utf-8"))
    assert snapshot["source_host"] == "https://api.ebay.com"


def test_refresh_script_uses_production_when_env_is_production(monkeypatch, tmp_path):
    import scripts.refresh_category_tree_cache as refresh

    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    constructed = []

    class _Oauth:
        def __init__(self, environment="SANDBOX"):
            constructed.append(environment)
            self.api_base = "https://api.ebay.com"

        def get_application_token(self):
            return "app-token"

    class _Session:
        def get(self, url, headers=None, timeout=None):
            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "categoryTreeId": "0",
                        "rootCategoryNode": {
                            "category": {"categoryId": "0", "categoryName": "Root"},
                        },
                    }

            return _Resp()

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _Oauth)
    monkeypatch.setattr("requests.Session", lambda: _Session())

    assert refresh.refresh_oauth_environment() == "PRODUCTION"
    rc = refresh.main(["--cache-dir", str(tmp_path), "--trees", "0"])
    assert rc == 0
    assert constructed == ["PRODUCTION"]
    snapshot = json.loads((tmp_path / "category_tree_0.json").read_text(encoding="utf-8"))
    assert snapshot["source_host"] == "https://api.ebay.com"


def test_refresh_script_refuses_sandbox_host_without_flag(monkeypatch, tmp_path):
    import scripts.refresh_category_tree_cache as refresh
    from src.utils.category_tree_cache import require_production_api_base

    monkeypatch.setenv("EBAY_ENVIRONMENT", "SANDBOX")

    class _Oauth:
        def __init__(self, environment="SANDBOX"):
            self.environment = environment
            self.api_base = "https://api.sandbox.ebay.com"

        def get_application_token(self):
            raise AssertionError("must not fetch taxonomy after refusing sandbox host")

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _Oauth)

    try:
        require_production_api_base("https://api.sandbox.ebay.com")
        raise AssertionError("sandbox host should be refused")
    except ValueError as exc:
        assert "non-production" in str(exc)

    rc = refresh.main(["--cache-dir", str(tmp_path), "--trees", "0"])
    assert rc == 2
    assert list(tmp_path.glob("category_tree_*.json")) == []


@pytest.mark.real_category_tree
def test_loader_rejects_sandbox_snapshot(monkeypatch, tmp_path):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    category_tree_cache.write_category_tree_cache(
        "100",
        MOTORS_TRAILER_TREE,
        cache_dir,
        source_host="https://api.sandbox.ebay.com",
        allow_sandbox=True,
    )

    matcher = EbayCategoryMatcher(_DummyOauth())
    assert matcher.get_category_path("6038") == ""
    assert "6038" not in matcher._path_cache


@pytest.mark.real_category_tree
def test_get_category_path_missing_cache_returns_empty(monkeypatch, tmp_path):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)

    http_calls = []
    monkeypatch.setattr(
        "src.services.ebay_category_matcher.requests.get",
        lambda *args, **kwargs: http_calls.append((args, kwargs)) or (_ for _ in ()).throw(AssertionError("no HTTP")),
    )
    matcher = EbayCategoryMatcher(_DummyOauth())
    path = matcher.get_category_path("6038", category_name="Trailers", title="Hollywood vanity")
    assert path == ""
    assert "6038" not in matcher._path_cache
    assert http_calls == []
