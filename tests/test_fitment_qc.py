"""Tests for src/services/fitment_qc.py — title fitment parsing + QC."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.services.fitment_qc import (
    build_compatible_products,
    check_fitment_qc,
    classify_fitment,
    parse_title_fitment,
    resolve_apply_fitment,
)
from src.utils.store_profile import StoreProfile, reset_store_profile_cache


@pytest.fixture(autouse=True)
def _reset_profile_cache():
    reset_store_profile_cache()
    yield
    reset_store_profile_cache()


# ── parsing ──────────────────────────────────────────────────────────────
def test_year_range_make_model():
    fits = parse_title_fitment('6" Running Boards For 2019-2026 Chevy Silverado 1500 Crew Cab')
    assert len(fits) == 1
    f = fits[0]
    assert f.make == "chevrolet"
    assert f.model == "silverado 1500"
    assert (f.year_from, f.year_to) == (2019, 2026)


def test_fit_keyword_no_dash_range():
    fits = parse_title_fitment("Running Boards Side Steps Fit Toyota Tacoma 2005-2023 (Crew Cab)")
    assert fits and fits[0].make == "toyota" and fits[0].model == "tacoma"
    assert (fits[0].year_from, fits[0].year_to) == (2005, 2023)


def test_toyota_sienna_title_parses_model():
    fits = parse_title_fitment(
        "Class 3 Hitch Compatible with 2004-2020 Toyota Sienna All Styles"
    )
    assert fits and fits[0].make == "toyota" and fits[0].model == "sienna"
    assert (fits[0].year_from, fits[0].year_to) == (2004, 2020)


def test_toyota_highlander_title_parses_model():
    fits = parse_title_fitment(
        "Rear Trailer Hitch Compatible For Toyota Highlander 2008-2013"
    )
    assert fits and fits[0].make == "toyota" and fits[0].model == "highlander"
    assert (fits[0].year_from, fits[0].year_to) == (2008, 2013)


def test_make_without_year():
    fits = parse_title_fitment("Off-Road Tailgate Flagpole Mount Kit Fit For New Land Rover Defender")
    assert fits and fits[0].make == "land rover" and fits[0].model == "defender"
    assert fits[0].year_from is None


def test_multiple_vehicles():
    fits = parse_title_fitment(
        "Door and Hard Top Storage Cart for 2007-2026 Jeep Wrangler JK JL, Jeep Gladiator")
    makes = {f.make for f in fits}
    models = {f.model for f in fits}
    assert makes == {"jeep"}
    assert "wrangler jk" in models and "gladiator" in models


def test_universal_returns_empty():
    for title in [
        "21PCS Metric Jumbo Service Wrench Set, CR-V Forged Steel",
        "10,000 lbs RV Scissor Jacks, 24 in Stabilizer Leveling, Set of 2",
        "13 Pcs Paintless Dent Repair Rods Kit, Stainless Steel",
        "8IN DRILL PRESS, 110V/60HZ 400W MOTOR",
        "Roof Rack Cargo Basket 50 x 36 Universal Rooftop Luggage",
    ]:
        assert parse_title_fitment(title) == [], title
        assert classify_fitment(title) == "universal", title


def test_rv_year_not_a_vehicle():
    # "RV" + a capacity number must NOT be read as a car+year
    assert parse_title_fitment("5000 lbs RV Scissor Jacks, 24 in, Set of 2") == []


# ── T2 parsing table (one test per row) ──────────────────────────────────
W3611P339821_TITLE = (
    "4PCS Running Boards For 2024 2025 Toyota Tacoma Double Cab/Crew Cab Side Steps"
)
W3611P339821_STORED_2005_2025 = [
    {
        "compatibilityProperties": [
            {"name": "Year", "value": str(year)},
            {"name": "Make", "value": "Toyota"},
            {"name": "Model", "value": "Tacoma"},
        ]
    }
    for year in range(2005, 2026)
]


def test_t2_f1_real_sku_w3611p339821_title_years_not_live_range():
    title = W3611P339821_TITLE
    fits = parse_title_fitment(title)
    assert fits and fits[0].make == "toyota"
    assert fits[0].model == "tacoma"
    assert (fits[0].year_from, fits[0].year_to) == (2024, 2025)
    issues = check_fitment_qc(title, live_compat=W3611P339821_STORED_2005_2025)
    assert any(i.code == "fitment_mismatch" for i in issues)
    assert not any(i.code == "missing_fitment" for i in issues)
    products, skip = resolve_apply_fitment(
        title,
        source_title=title,
        source_description="",
        stored_compatible_products=W3611P339821_STORED_2005_2025,
    )
    assert products == []
    assert skip == "skipped_stored_mismatch"


def test_synthetic_silverado_title_years_not_live_range():
    title = "Running Boards For 2024–2025 Chevy Silverado 1500"
    fits = parse_title_fitment(title)
    assert fits and fits[0].make == "chevrolet"
    assert (fits[0].year_from, fits[0].year_to) == (2024, 2025)
    live = [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": str(year)},
                {"name": "Make", "value": "Chevrolet"},
                {"name": "Model", "value": "Silverado 1500"},
            ]
        }
        for year in range(2005, 2026)
    ]
    issues = check_fitment_qc(title, live_compat=live)
    assert any(i.code == "fitment_mismatch" for i in issues)
    products, skip = resolve_apply_fitment(title, source_title="", source_description="")
    assert products == []
    assert skip == "skipped_no_source_fitment"


def test_t2_f2_chevy_silverado_not_gmc():
    fits = parse_title_fitment("Running Boards For 2019-2026 Chevy Silverado 1500")
    assert len(fits) == 1
    assert fits[0].make == "chevrolet"
    assert fits[0].model == "silverado 1500"
    assert (fits[0].year_from, fits[0].year_to) == (2019, 2026)
    assert all(f.make != "gmc" for f in fits)


def test_t2_f3_universal_trailer_hitch_pin_synthetic():
    title = "Universal Trailer Hitch Pin Fits Most Trucks"
    assert parse_title_fitment(title) == []
    assert classify_fitment(title) == "universal"


def test_t2_f4_ram_1500_not_dodge_unless_named_synthetic():
    fits = parse_title_fitment("Tool Box for Ram 1500 2019-2024")
    assert fits
    assert any(f.make == "ram" and (f.model or "").endswith("1500") for f in fits)
    assert (fits[0].year_from, fits[0].year_to) == (2019, 2024)
    assert all(f.make != "dodge" for f in fits)


def test_t2_f5_weight_2000_is_not_a_year_synthetic():
    title = "48 Inch Tool Chest 2000 lbs Capacity"
    assert parse_title_fitment(title) == []
    assert classify_fitment(title) == "universal"


def test_t2_f6_ram_inside_rampage_is_not_a_make_synthetic():
    title = "Rampage Bike Rack"
    assert parse_title_fitment(title) == []
    assert classify_fitment(title) == "universal"


def test_t2_f7_jeep_wrangler_jl_no_invented_year_synthetic():
    fits = parse_title_fitment("Floor Mats for Jeep Wrangler JL")
    assert fits and fits[0].make == "jeep"
    assert fits[0].model and "wrangler" in fits[0].model
    assert fits[0].year_from is None and fits[0].year_to is None


def test_t2_f8_tacoma_double_cab_synthetic():
    fits = parse_title_fitment("Fits 2005-2023 Toyota Tacoma Double Cab")
    assert fits and fits[0].make == "toyota"
    assert fits[0].model in {"tacoma", "tacoma double cab"}
    assert (fits[0].year_from, fits[0].year_to) == (2005, 2023)


def test_t2_f9_f150_aliases_collapse_to_one_entry_synthetic():
    fits = parse_title_fitment("Ford F-150 / F150 2015–2020 Bed Rail")
    ford = [f for f in fits if f.make == "ford"]
    assert len(ford) == 1
    assert ford[0].model == "f-150"
    assert (ford[0].year_from, ford[0].year_to) == (2015, 2020)


def test_t2_f10_real_sku_w46559181_sizes_are_not_years():
    title = "Grinder Stand for 150mm 180mm 230mm Grinders"
    assert parse_title_fitment(title) == []
    assert classify_fitment(title) == "universal"


# ── extra SYNTHETIC edge cases ───────────────────────────────────────────
def test_synthetic_en_dash_year_range_keeps_make():
    fits = parse_title_fitment("Nerf Bars For 2018–2022 Toyota Tundra")
    assert fits and fits[0].make == "toyota" and fits[0].model == "tundra"
    assert (fits[0].year_from, fits[0].year_to) == (2018, 2022)


def test_synthetic_voltage_and_size_do_not_become_fitment():
    title = "12V 18in Shop Fan for Garage"
    assert parse_title_fitment(title) == []


def test_synthetic_dodge_only_when_word_appears():
    fits = parse_title_fitment("Running Boards for 2019-2024 Dodge Ram 1500")
    assert any(f.make == "dodge" for f in fits)


# ── QC ───────────────────────────────────────────────────────────────────
def test_missing_fitment_flagged():
    issues = check_fitment_qc("Running Boards For 2019-2026 Chevy Silverado 1500", live_compat=[])
    assert any(i.code == "missing_fitment" and i.severity == "HIGH" for i in issues)


def test_universal_no_missing_issue():
    issues = check_fitment_qc("21PCS Metric Jumbo Service Wrench Set", live_compat=[])
    assert not any(i.code == "missing_fitment" for i in issues)


def test_present_fitment_no_missing_issue():
    live = [{"compatibilityProperties": [
        {"name": "Year", "value": "2020"}, {"name": "Make", "value": "Chevrolet"},
        {"name": "Model", "value": "Silverado 1500"}]}]
    issues = check_fitment_qc("Running Boards For 2019-2026 Chevy Silverado 1500", live_compat=live)
    assert not any(i.code == "missing_fitment" for i in issues)


def test_mismatch_flagged():
    live = [{"compatibilityProperties": [
        {"name": "Make", "value": "Ford"}, {"name": "Model", "value": "F-150"}]}]
    issues = check_fitment_qc("Running Boards For 2019-2026 Chevy Silverado 1500", live_compat=live)
    assert any(i.code == "fitment_mismatch" for i in issues)


def test_f150_title_matches_live_f_150_no_mismatch():
    """Title uses F150; live catalog uses F-150 — must not flag mismatch."""
    title = "Boards SuperCrew Cab Compatible for 2015-2026 F150 & 2017-2020"
    live = [{"compatibilityProperties": [
        {"name": "Year", "value": "2018"},
        {"name": "Make", "value": "Ford"},
        {"name": "Model", "value": "F-150"},
    ]}]
    issues = check_fitment_qc(title, live_compat=live)
    assert not any(i.code == "fitment_mismatch" for i in issues)


def test_f_150_title_matches_live_f150_token():
    title = "Side Steps Compatible for 2015-2026 Ford F-150 SuperCrew"
    live = [{"compatibilityProperties": [
        {"name": "Make", "value": "Ford"},
        {"name": "Model", "value": "F150"},
    ]}]
    issues = check_fitment_qc(title, live_compat=live)
    assert not any(i.code == "fitment_mismatch" for i in issues)


def test_defender_series_title_matches_live_defender_110():
    title = "Mirror Cover Fit For New Land Rover Defender 90/110/130 Wing Mirror Cap"
    live = [{"compatibilityProperties": [
        {"name": "Make", "value": "Land Rover"},
        {"name": "Model", "value": "Defender 110"},
    ]}]
    issues = check_fitment_qc(title, live_compat=live)
    assert not any(i.code == "fitment_mismatch" for i in issues)


SIERRA_SILVERADO_TITLE = (
    '6" Running Boards For 2019-2026 Chevy Silverado 1500 Crew Cab, '
    "2020-2026 Silverado/Sierra 2500HD 3500HD Crew Cab, Two Stairs Design "
    "Nerf Bars Step Bars Hexagonal style"
)


def test_sierra_token_parses_as_gmc():
    fits = parse_title_fitment(SIERRA_SILVERADO_TITLE)
    assert {f.make for f in fits} >= {"chevrolet", "gmc"}
    assert any(f.make == "gmc" and f.model and "sierra" in f.model for f in fits)


def test_sierra_in_title_does_not_flag_gmc_fitment_as_mismatch():
    live = [{"compatibilityProperties": [
        {"name": "Year", "value": "2021"},
        {"name": "Make", "value": "GMC"},
        {"name": "Model", "value": "Sierra 2500HD"},
    ]}]
    issues = check_fitment_qc(SIERRA_SILVERADO_TITLE, live_compat=live)
    assert not any(i.code == "fitment_mismatch" for i in issues)


# ── build / C3 / C4 ──────────────────────────────────────────────────────
def test_build_expands_year_range():
    cps = build_compatible_products("Running Boards For 2019-2021 Chevy Silverado 1500")
    years = sorted(
        p["value"] for e in cps for p in e["compatibilityProperties"] if p["name"] == "Year")
    assert years == ["2019", "2020", "2021"]
    makes = {p["value"] for e in cps for p in e["compatibilityProperties"] if p["name"] == "Make"}
    assert makes == {"Chevrolet"}


def test_build_universal_empty():
    assert build_compatible_products("21PCS Metric Jumbo Service Wrench Set") == []


def test_c3_apply_xml_has_no_itemspecifics_or_product_listing_details():
    from scripts.fitment_audit import build_apply_xml

    xml = build_apply_xml(
        "188000000001",
        build_compatible_products("Running Boards For 2019-2021 Chevy Silverado 1500"),
    )
    assert "<ItemCompatibilityList>" in xml
    assert "<Compatibility>" in xml
    assert "<ItemSpecifics>" not in xml
    assert "<ProductListingDetails>" not in xml
    assert "<Description>" not in xml
    assert "<ReplaceAll>" not in xml


def test_c4_skips_stored_list_that_mismatches_giga_title():
    stored = [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": "2020"},
                {"name": "Make", "value": "Chevrolet"},
                {"name": "Model", "value": "Silverado 1500"},
            ]
        }
    ]
    products, skip = resolve_apply_fitment(
        "Running Boards For 2024-2025 Chevy Silverado 1500",
        source_title="",
        source_description="",
        stored_compatible_products=stored,
    )
    assert products == []
    assert skip == "skipped_stored_mismatch"


def test_c4_pushes_stored_when_it_matches_giga_title():
    stored = [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": str(year)},
                {"name": "Make", "value": "Chevrolet"},
                {"name": "Model", "value": "Silverado 1500"},
            ]
        }
        for year in (2024, 2025)
    ]
    products, skip = resolve_apply_fitment(
        "Running Boards For 2024-2025 Chevy Silverado 1500",
        source_title="",
        source_description="",
        stored_compatible_products=stored,
    )
    assert skip == ""
    assert products == stored


def test_c4_skips_when_giga_source_lacks_same_ymm():
    products, skip = resolve_apply_fitment(
        "Running Boards For 2019-2026 Chevy Silverado 1500",
        source_title="",
        source_description="Steel running board, no vehicle named.",
    )
    assert products == []
    assert skip == "skipped_no_source_fitment"


def test_c4_applies_when_giga_source_has_same_ymm():
    products, skip = resolve_apply_fitment(
        "Running Boards For 2019-2026 Chevy Silverado 1500",
        source_title="",
        source_description="Fits 2019-2026 Chevy Silverado 1500 crew cab.",
    )
    assert skip == ""
    years = {p["value"] for e in products for p in e["compatibilityProperties"] if p["name"] == "Year"}
    assert "2019" in years and "2026" in years


def test_fitment_process_exit_code():
    from scripts.fitment_audit import _fitment_process_exit_code

    assert _fitment_process_exit_code(apply=True, apply_fail=0) == 0
    assert _fitment_process_exit_code(apply=True, apply_fail=2) == 2
    assert _fitment_process_exit_code(apply=False, apply_fail=0) == 0
    assert _fitment_process_exit_code(apply=True, apply_fail=0) == 0
    assert _fitment_process_exit_code(apply=False, apply_fail=0, read_fail=24) == 1
    assert _fitment_process_exit_code(apply=True, apply_fail=0, read_fail=1) == 1


def test_fitment_audit_refuses_main_and_gp_without_client_calls(monkeypatch):
    from scripts import fitment_audit

    built = []

    def _boom(*_args, **_kwargs):
        built.append(True)
        raise AssertionError("furniture profile must not call eBay")

    monkeypatch.setattr(fitment_audit, "get_live_compat", _boom)
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: (_ for _ in ()).throw(AssertionError("no oauth")))

    monkeypatch.setattr(
        "src.utils.store_profile.get_store_profile",
        lambda: StoreProfile(),
    )
    assert fitment_audit.main([]) == 2
    assert built == []

    monkeypatch.setattr(
        "src.utils.store_profile.get_store_profile",
        lambda: StoreProfile(
            store_kind="furniture",
            brand_name="GrovePop",
            listing_channel="inventory",
            ebay_site_id="0",
            scheduler_profile="ops",
        ),
    )
    assert fitment_audit.main([]) == 2
    assert built == []


def test_fitment_audit_apply_skips_without_source_and_does_not_post(monkeypatch, tmp_path):
    from scripts import fitment_audit

    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE collected_products ("
        "sku TEXT, listing_id TEXT, title TEXT, description TEXT, "
        "optimization TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?,?,?,?,?,?)",
        (
            "W3611P339821",
            "188000000002",
            "Running Boards For 2019-2026 Chevy Silverado 1500",
            "Steel board. No vehicle in the GIGA copy.",
            json.dumps({}),
            "PUBLISHED",
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "src.utils.store_profile.get_store_profile",
        lambda: StoreProfile(
            store_kind="auto",
            brand_name="AquaRides",
            listing_channel="trading",
            ebay_site_id="100",
            qc_profile="motors",
            scheduler_profile="ops",
        ),
    )

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    posted = []

    def _get_live(_tok, _item_id, *, site_id, session=None):
        return []

    def _push(*args, **kwargs):
        posted.append((args, kwargs))
        return True, "", "<xml/>"

    monkeypatch.setattr(fitment_audit, "get_live_compat", _get_live)
    monkeypatch.setattr(fitment_audit, "push_compat", _push)
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit, "time", type("T", (), {"sleep": staticmethod(lambda *_: None)})())

    rc = fitment_audit.main(["--apply", "--db", str(db), "--sku", "W3611P339821"])
    assert rc == 0
    assert posted == []


def test_s1_space_separated_years_parse_as_range():
    fits = parse_title_fitment(W3611P339821_TITLE)
    assert fits and (fits[0].year_from, fits[0].year_to) == (2024, 2025)


def test_s1_plus_year_runs_through_current_year():
    from src.services.vehicle_compatibility import CURRENT_YEAR

    fits = parse_title_fitment("Running Boards For 2021+ Toyota Tacoma")
    assert fits and fits[0].make == "toyota" and fits[0].model == "tacoma"
    assert (fits[0].year_from, fits[0].year_to) == (2021, CURRENT_YEAR)


def test_s2_ram_jack_is_not_the_ram_make():
    for title in (
        "Hydraulic Long Ram Jack",
        "Ram Jack 8 Ton Heavy Duty",
        "Hydraulic Ram Press",
    ):
        assert parse_title_fitment(title) == [], title
        assert classify_fitment(title) == "universal", title


def test_real_sku_w2801p540956_yearless_defender_is_not_applied():
    title = "Off-Road Tailgate Flagpole Mount Kit Fit For New Land Rover Defender 90/110"
    fits = parse_title_fitment(title)
    assert fits and fits[0].make == "land rover" and fits[0].model == "defender"
    assert fits[0].year_from is None
    products, skip = resolve_apply_fitment(
        title,
        source_title=title,
        source_description=title,
    )
    assert products == []
    assert skip == "skipped_no_source_fitment"
    built = build_compatible_products(title)
    assert built == []


def _aqua_auto_profile() -> StoreProfile:
    return StoreProfile(
        store_kind="auto",
        brand_name="AquaRides",
        listing_channel="trading",
        ebay_site_id="100",
        qc_profile="motors",
        scheduler_profile="ops",
    )


def _published_rows(tmp_path, rows):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE collected_products ("
        "sku TEXT, listing_id TEXT, title TEXT, description TEXT, "
        "optimization TEXT, status TEXT)"
    )
    for sku, listing_id, title, description, optimization in rows:
        conn.execute(
            "INSERT INTO collected_products VALUES (?,?,?,?,?,?)",
            (
                sku,
                listing_id,
                title,
                description,
                json.dumps(optimization or {}),
                "PUBLISHED",
            ),
        )
    conn.commit()
    conn.close()
    return db


def _published_db(tmp_path, sku, listing_id, title, description="", optimization=None):
    return _published_rows(
        tmp_path, [(sku, listing_id, title, description, optimization)]
    )


def test_real_sku_w3611p339821_apply_does_not_push_stored_tacoma_2005_2025(
    monkeypatch, tmp_path
):
    from scripts import fitment_audit

    db = _published_db(
        tmp_path,
        "W3611P339821",
        "188000000821",
        W3611P339821_TITLE,
        W3611P339821_TITLE,
        {
            "motorsCompatibility": {
                "compatibleProducts": W3611P339821_STORED_2005_2025,
            }
        },
    )
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_auto_profile)

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    posted = []
    monkeypatch.setattr(fitment_audit, "get_live_compat", lambda *a, **k: [])
    monkeypatch.setattr(
        fitment_audit,
        "push_compat",
        lambda *a, **k: posted.append((a, k)) or (True, "", "<xml/>"),
    )
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit, "time", type("T", (), {"sleep": staticmethod(lambda *_: None)})())

    rc = fitment_audit.main(["--apply", "--db", str(db), "--sku", "W3611P339821"])
    assert rc == 0
    assert posted == []
    reports = list(tmp_path.glob("fitment_audit_*.json"))
    assert reports
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["applied"] == 0
    assert any(row.get("skip_reason") == "skipped_stored_mismatch" for row in payload["findings"])


def test_real_sku_w2801p540956_apply_does_not_write_yearless_defender(
    monkeypatch, tmp_path
):
    from scripts import fitment_audit

    title = "Off-Road Tailgate Flagpole Mount Kit Fit For New Land Rover Defender 90/110"
    db = _published_db(tmp_path, "W2801P540956", "188000000956", title, title, {})
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_auto_profile)

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    posted = []
    monkeypatch.setattr(fitment_audit, "get_live_compat", lambda *a, **k: [])
    monkeypatch.setattr(
        fitment_audit,
        "push_compat",
        lambda *a, **k: posted.append((a, k)) or (True, "", "<xml/>"),
    )
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit, "time", type("T", (), {"sleep": staticmethod(lambda *_: None)})())

    rc = fitment_audit.main(["--apply", "--db", str(db), "--sku", "W2801P540956"])
    assert rc == 0
    assert posted == []
    reports = list(tmp_path.glob("fitment_audit_*.json"))
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["applied"] == 0
    assert any(row.get("skip_reason") == "skipped_no_source_fitment" for row in payload["findings"])


def test_getitem_failure_is_unknown_and_apply_does_not_write(monkeypatch, tmp_path):
    from scripts import fitment_audit

    db = _published_db(
        tmp_path,
        "SYNTHETIC-GETITEM-FAIL",
        "188000000099",
        "Running Boards For 2019-2026 Chevy Silverado 1500",
        "Fits 2019-2026 Chevy Silverado 1500.",
        {},
    )
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_auto_profile)

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    class FailResp:
        status_code = 200
        text = (
            "<GetItemResponse>"
            "<Ack>Failure</Ack>"
            "<Errors><LongMessage>Auth token is hard expired.</LongMessage>"
            "<SeverityCode>Error</SeverityCode></Errors>"
            "</GetItemResponse>"
        )

    posted = []

    def _no_push(*args, **kwargs):
        posted.append((args, kwargs))
        return True, "", "<xml/>"

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    monkeypatch.setattr("requests.post", lambda *a, **k: FailResp())
    monkeypatch.setattr(fitment_audit, "push_compat", _no_push)
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit, "time", type("T", (), {"sleep": staticmethod(lambda *_: None)})())

    with pytest.raises(fitment_audit.LiveCompatReadError):
        fitment_audit.get_live_compat("token", "188000000099", site_id="100", session=None)

    rc = fitment_audit.main(["--apply", "--db", str(db), "--sku", "SYNTHETIC-GETITEM-FAIL"])
    assert rc != 0
    assert posted == []
    reports = list(tmp_path.glob("fitment_audit_*.json"))
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["applied"] == 0
    assert payload["read_failed"] == 1
    assert payload["findings"]
    row = payload["findings"][0]
    assert row["status"] == "unknown"
    assert row["applied"] is False
    assert row["issues"] == []
    assert "missing_fitment" not in json.dumps(row)


def test_n1_all_24_getitem_failures_are_not_a_pass(monkeypatch, tmp_path):
    from scripts import fitment_audit

    title = "Running Boards For 2019-2026 Chevy Silverado 1500"
    rows = [
        (f"SKU-{i:02d}", f"1880001{i:04d}", title, "Fits 2019-2026 Chevy Silverado 1500.", {})
        for i in range(24)
    ]
    db = _published_rows(tmp_path, rows)
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_auto_profile)

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    posted = []
    emails = []

    def _fail_read(*_args, **_kwargs):
        raise fitment_audit.LiveCompatReadError("Invalid IAF token")

    def _no_push(*args, **kwargs):
        posted.append((args, kwargs))
        return True, "", "<xml/>"

    def _send_email(subject, html, send_smtp=False, **kwargs):
        emails.append({"subject": subject, "html": html, "send_smtp": send_smtp})
        return True

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    monkeypatch.setattr(fitment_audit, "get_live_compat", _fail_read)
    monkeypatch.setattr(fitment_audit, "push_compat", _no_push)
    monkeypatch.setattr("src.utils.email_sender.send_email", _send_email)
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit, "time", type("T", (), {"sleep": staticmethod(lambda *_: None)})())

    rc = fitment_audit.main(["--apply", "--email", "--db", str(db)])
    assert rc != 0
    assert posted == []
    assert emails
    subject = emails[0]["subject"]
    html = emails[0]["html"]
    assert "通过" not in subject
    assert "全部通过" not in html
    assert "读取失败 24" in subject
    assert "live 读取失败 24" in html
    assert emails[0]["send_smtp"] is True
    reports = list(tmp_path.glob("fitment_audit_*.json"))
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["read_failed"] == 24
    assert payload["applied"] == 0
    assert all(row["status"] == "unknown" for row in payload["findings"])


W3611P511712_TITLE = "Side Steps Running Boards For Toyota Tacoma Double Cab"
W3611P339830_TITLE = "Nerf Bars For Jeep Wrangler JL"


def test_n2_yearless_title_does_not_push_stored_fitment():
    stored_tacoma = [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": str(year)},
                {"name": "Make", "value": "Toyota"},
                {"name": "Model", "value": "Tacoma"},
            ]
        }
        for year in range(2005, 2026)
    ]
    stored_wrangler = [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": "2018"},
                {"name": "Make", "value": "Jeep"},
                {"name": "Model", "value": "Wrangler"},
            ]
        }
    ]
    for sku, title, stored in (
        ("W3611P511712", W3611P511712_TITLE, stored_tacoma),
        ("W3611P339830", W3611P339830_TITLE, stored_wrangler),
    ):
        fits = parse_title_fitment(title)
        assert fits and fits[0].year_from is None, sku
        products, skip = resolve_apply_fitment(
            title,
            source_title=title,
            source_description=title,
            stored_compatible_products=stored,
        )
        assert products == [], sku
        assert skip == "skipped_title_no_year", sku


def test_n2_yearless_stored_apply_reports_skip_and_does_not_write(monkeypatch, tmp_path):
    from scripts import fitment_audit

    stored = [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": str(year)},
                {"name": "Make", "value": "Toyota"},
                {"name": "Model", "value": "Tacoma"},
            ]
        }
        for year in range(2005, 2026)
    ]
    db = _published_rows(
        tmp_path,
        [
            (
                "W3611P511712",
                "188000005117",
                W3611P511712_TITLE,
                W3611P511712_TITLE,
                {"motorsCompatibility": {"compatibleProducts": stored}},
            ),
            (
                "W3611P339830",
                "188000003398",
                W3611P339830_TITLE,
                W3611P339830_TITLE,
                {"motorsCompatibility": {"compatibleProducts": stored}},
            ),
        ],
    )
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_auto_profile)

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    posted = []
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    monkeypatch.setattr(fitment_audit, "get_live_compat", lambda *a, **k: [])
    monkeypatch.setattr(
        fitment_audit,
        "push_compat",
        lambda *a, **k: posted.append((a, k)) or (True, "", "<xml/>"),
    )
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit, "time", type("T", (), {"sleep": staticmethod(lambda *_: None)})())

    rc = fitment_audit.main(["--apply", "--db", str(db)])
    assert rc == 0
    assert posted == []
    reports = list(tmp_path.glob("fitment_audit_*.json"))
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["applied"] == 0
    assert payload["skipped_title_no_year"] == 2
    reasons = {row["sku"]: row.get("skip_reason") for row in payload["findings"]}
    assert reasons["W3611P511712"] == "skipped_title_no_year"
    assert reasons["W3611P339830"] == "skipped_title_no_year"
