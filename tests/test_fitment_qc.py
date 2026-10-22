"""Tests for src/services/fitment_qc.py — title fitment parsing + QC."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.fitment_qc import (  # noqa: E402
    parse_title_fitment,
    classify_fitment,
    check_fitment_qc,
    build_compatible_products,
)


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


# ── build ────────────────────────────────────────────────────────────────
def test_build_expands_year_range():
    cps = build_compatible_products("Running Boards For 2019-2021 Chevy Silverado 1500")
    years = sorted(
        p["value"] for e in cps for p in e["compatibilityProperties"] if p["name"] == "Year")
    assert years == ["2019", "2020", "2021"]
    makes = {p["value"] for e in cps for p in e["compatibilityProperties"] if p["name"] == "Make"}
    assert makes == {"Chevrolet"}


def test_build_universal_empty():
    assert build_compatible_products("21PCS Metric Jumbo Service Wrench Set") == []


def test_fitment_process_exit_code():
    from scripts.fitment_audit import _fitment_process_exit_code

    assert _fitment_process_exit_code(apply=True, apply_fail=0) == 0
    assert _fitment_process_exit_code(apply=True, apply_fail=2) == 2
    assert _fitment_process_exit_code(apply=False, apply_fail=0) == 0
    # Residual HIGH/mismatch must not force non-zero by itself
    assert _fitment_process_exit_code(apply=True, apply_fail=0) == 0
