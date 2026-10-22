"""Unit tests for the manifest-driven claim strip (spec table P1–P11, N1–N8)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.utils import claim_strip as cs


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = ROOT / "tests" / "fixtures" / "claims_manifest_fixture.csv"
BEFORE_JSON = ROOT / "tests" / "fixtures" / "claim_strip" / "main_before_subset.json"


def load_before(sku: str) -> tuple[dict, dict]:
    payload = json.loads(BEFORE_JSON.read_text(encoding="utf-8"))
    rec = payload[sku]
    return deepcopy(rec["inv"]), deepcopy(rec["offers"][0])


def load_manifest() -> list[cs.ManifestRow]:
    rows, _counts = cs.load_claims_manifest(FIXTURE_CSV)
    return rows


def report_covering(rows: list[cs.ManifestRow], extra: dict[str, list[str]] | None = None) -> dict:
    by_sku: dict[str, list[dict]] = {}
    for row in rows:
        if row.recommendation != cs.STRIP_NOW:
            continue
        by_sku.setdefault(row.sku, []).append(
            {
                "type": "semantic_feature",
                "severity": "HIGH",
                "detail": f"unsourced FactSheet claim: {row.claim}",
            }
        )
    for sku, claims in (extra or {}).items():
        for claim in claims:
            by_sku.setdefault(sku, []).append(
                {
                    "type": "semantic_feature",
                    "severity": "HIGH",
                    "detail": f"unsourced FactSheet claim: {claim}",
                }
            )
    return {"issues": [{"sku": sku, "issues": issues} for sku, issues in by_sku.items()]}


def plan_for(
    sku: str,
    *,
    rows=None,
    extra_rows=None,
    required=(),
    required_available=True,
    mutate_inv=None,
    mutate_offer=None,
    extra_report=None,
) -> cs.SkuPlan:
    manifest = list(rows or load_manifest())
    if extra_rows:
        manifest.extend(extra_rows)
    inv, offer = load_before(sku)
    if mutate_inv:
        mutate_inv(inv)
    if mutate_offer:
        mutate_offer(offer)
    return cs.plan_sku_claim_strip(
        sku,
        manifest,
        inv,
        offer,
        cs.issues_for_sku(report_covering(manifest, extra_report), sku),
        required_aspect_names=required,
        required_aspects_available=required_available,
    )


def result_for(plan: cs.SkuPlan, claim: str) -> cs.ClaimResult:
    matches = [item for item in plan.results if item.claim == claim]
    assert matches, f"missing result for {claim!r}: {[item.claim for item in plan.results]}"
    return matches[0]


def wrap_template(title: str, lis: list[str], *, table_html: str = "", package: str = "", extra: str = "") -> str:
    items = "".join(lis)
    return (
        '<div style="max-width:900px;margin:0 auto;font-family:Arial,sans-serif">'
        f'<h2 style="margin:0;font-size:20px;color:#2d3436;font-weight:500">{title}</h2>'
        "<h3>KEY FEATURES</h3>"
        f"<ul>{items}</ul>"
        "<h3>SPECIFICATIONS</h3>"
        f"<table>{table_html}</table>"
        f"<h3>PACKAGE INCLUDES</h3><p style=\"margin:0;color:#636e72\">{package}</p>"
        f"{extra}"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Spec table
# ---------------------------------------------------------------------------


def test_p1_removes_last_capacity_li_and_keeps_table_and_title():
    plan = plan_for("W465P154347")
    result = result_for(plan, "220-lb load capacity")
    assert result.status == cs.STATUS_WILL_STRIP
    assert "220-Lb Load Capacity" not in plan.offer_description_after
    assert "Compact &amp; Space-Saving Dimensions" in plan.offer_description_after or "Compact & Space-Saving Dimensions" in plan.offer_description_after
    assert "</ul>" in plan.offer_description_after
    assert "28.0 lbs" in plan.offer_description_after
    assert ">Steel<" in plan.offer_description_after
    assert plan.title == load_before("W465P154347")[0]["product"]["title"]
    assert "20 Inch Pneumatic Wheels Red Steel" in plan.title
    removed = next(a.removed_unit_html for a in result.actions if a.removed_unit_html)
    rebuilt = plan.offer_description_before.replace(removed, "", 1)
    assert cs.cleanup_html_document(rebuilt) == plan.offer_description_after


def test_p2_removes_finish_li_and_keeps_features():
    plan = plan_for("W46538540")
    result = result_for(plan, "matte black powder-coated")
    assert result.status == cs.STATUS_WILL_STRIP
    assert "Industrial-Grade Finish" not in plan.offer_description_after
    assert plan.aspects_after["Features"] == [
        "Swivel",
        "Multi-Jaw",
        "360-Degree Rotation",
        "Pipe Jaws",
        "Anvil Work Surface",
    ]


def test_p3_strips_box_spring_li_and_keeps_needs_look_ladder():
    plan = plan_for("W5635P575085")
    assert result_for(plan, "no box spring required").status == cs.STATUS_WILL_STRIP
    assert "No Box Spring Required" not in plan.offer_description_after
    assert "Built-In Ladder" in plan.aspects_after["Features"]
    assert "Built-In Ladder" in plan.offer_description_after
    assert all(item.claim != "built-in ladder" for item in plan.results)


def test_p4_three_claims_remove_the_same_li_once():
    claims = ["non-marking", "quiet", "cushioned ride"]
    rows = [row for row in load_manifest() if row.sku == "W465P453639" and row.claim in claims]
    plan = plan_for("W465P453639", rows=rows)
    for claim in claims:
        assert result_for(plan, claim).status == cs.STATUS_WILL_STRIP
    assert "<strong>PU Foam Tires:</strong>" not in plan.offer_description_after
    assert "Zinc-Plated Top Plate" in plan.offer_description_after
    assert "Black / Matte zinc finish" in plan.offer_description_after
    assert plan.offer_description_before.count("<li") - 1 == plan.offer_description_after.count("<li")


def test_p5_heading_claim_removes_ready_to_install_li():
    plan = plan_for("W465P453639")
    assert result_for(plan, "ready-to-install").status == cs.STATUS_WILL_STRIP
    assert "Ready-to-Install Build" not in plan.offer_description_after
    assert "360° rotation" in plan.offer_description_after
    assert "Precision roller bearings and full 360° rotation" in plan.offer_description_after


def test_p6_removes_each_anchored_li_and_leaves_title_h2():
    plan = plan_for("W465P453639")
    assert result_for(plan, "industrial-grade").status == cs.STATUS_WILL_STRIP
    assert result_for(plan, "heavy-duty").status == cs.STATUS_WILL_STRIP
    assert "660-Pound Load Capacity" not in plan.offer_description_after
    assert "Heavy-Duty Dual-Wheel Design" not in plan.offer_description_after
    assert "660 LBS Capacity Black" in plan.title
    assert "660 LBS Capacity Black" in plan.offer_description_after


def test_p7_removes_only_energy_saving_sentence():
    plan = plan_for("W3596P577884")
    result = result_for(plan, "energy saving")
    assert result.status == cs.STATUS_WILL_STRIP
    after = plan.offer_description_after
    assert "6-Speed Quiet DC Motor" in after
    assert "Ultra-quiet operation with powerful airflow" in after
    assert "Energy-saving motor delivers" not in after
    assert "Energy-saving motor" not in after
    li = None
    for match in __import__("re").finditer(r"<li[^>]*>.*?</li>", after, flags=__import__("re").I | __import__("re").S):
        if "6-Speed Quiet DC Motor" in match.group(0):
            li = match.group(0)
    assert li is not None
    assert not li.endswith(" </li>")
    assert plan.aspects_after.get("Energy Efficiency") is None
    assert "Quiet DC Motor" in plan.aspects_after["Features"]


def test_p7_required_energy_efficiency_is_skipped():
    plan = plan_for("W3596P577884", required=("Energy Efficiency",))
    desc = next(
        action
        for action in result_for(plan, "energy saving").actions
        if action.field == cs.TOKEN_OFFER_DESC
    )
    aspect = next(
        action
        for action in result_for(plan, "energy saving").actions
        if action.token == "aspect:Energy Efficiency"
    )
    assert desc.status == cs.STATUS_WILL_STRIP
    assert aspect.status == cs.STATUS_REQUIRED_ASPECT
    assert plan.aspects_after["Energy Efficiency"] == ["Energy Saving"]


def test_p8_single_sentence_li_removed_keep_neighbor():
    plan = plan_for("W757P412805")
    assert result_for(plan, "wobble-free stability").status == cs.STATUS_WILL_STRIP
    assert "wobble-free stability" not in plan.offer_description_after
    assert "Compact Round Silhouette" in plan.offer_description_after


def test_p9_features_exact_values_keep_order():
    plan = plan_for("W2787P378160")
    for claim in ("carry on", "hardcase", "upright", "anti-theft", "clothing compartment", "expandable"):
        assert result_for(plan, claim).status == cs.STATUS_WILL_STRIP
    assert plan.aspects_after["Features"] == [
        "Lightweight",
        "Retractable Handle",
        "Spinner",
        "Wheels/Rolling",
        "Front Opening",
        "Combination Lock",
        "Waterproof",
    ]
    assert plan.aspects_after["Finish"] == ["Matte"]
    assert plan.aspects_after["Type"] == ["Suitcases"]


def test_p10_features_and_optional_care_instructions():
    plan = plan_for("W5230P450590")
    assert result_for(plan, "easy care").status == cs.STATUS_WILL_STRIP
    assert result_for(plan, "anti-slip").status == cs.STATUS_WILL_STRIP
    assert result_for(plan, "high reliability").status == cs.STATUS_WILL_STRIP
    assert plan.aspects_after["Features"] == ["Unique Appearance", "Compact Design"]
    assert "Care Instructions" not in plan.aspects_after
    assert plan.aspects_after["Finish"] == ["Smooth"]


def test_p10_required_care_instructions_would_empty():
    plan = plan_for("W5230P450590", required=("Care Instructions",))
    care = next(
        action
        for action in result_for(plan, "easy care").actions
        if action.token == "aspect:Care Instructions"
    )
    assert care.status == cs.STATUS_REQUIRED_ASPECT
    assert plan.aspects_after["Care Instructions"] == ["Easy Care"]
    assert plan.aspects_after["Finish"] == ["Smooth"]


def test_p11_fuel_type_removed_when_not_required():
    plan = plan_for("W3131P380454")
    assert result_for(plan, "propane").status == cs.STATUS_WILL_STRIP
    assert "Fuel Type" not in plan.aspects_after
    assert plan.aspects_after["Type"] == ["Fire Pit"]


def test_p11_required_fuel_type_stays():
    plan = plan_for("W3131P380454", required=("Fuel Type",))
    assert result_for(plan, "propane").status == cs.STATUS_REQUIRED_ASPECT
    assert plan.aspects_after["Fuel Type"] == ["Propane"]
    assert plan.aspects_after["Type"] == ["Fire Pit"]


def test_n1_keep_row_is_ignored():
    plan = plan_for("W757P412805")
    assert all(item.claim != "compact round silhouette" for item in plan.results)
    assert "Compact Round Silhouette" in plan.offer_description_after


def test_n2_needs_look_backrest_kept_footrest_stripped():
    plan = plan_for("W3916P563391")
    assert all(item.claim != "with backrest" for item in plan.results)
    assert result_for(plan, "with footrest").status == cs.STATUS_WILL_STRIP
    assert plan.aspects_after["Features"] == ["With Backrest", "Upholstered", "With Cushion"]


def test_n3_in_title_skips_every_location():
    rows = [
        row
        for row in load_manifest()
        if row.sku == "W465P220504" and row.claim == "quick-connect"
    ]
    plan = plan_for("W465P220504", rows=rows)
    result = result_for(plan, "quick-connect")
    assert result.status == cs.STATUS_IN_TITLE
    assert plan.offer_description_after == plan.offer_description_before
    assert plan.aspects_after == plan.aspects_before
    assert plan.title == load_before("W465P220504")[0]["product"]["title"]
    assert "Quick Connect" in plan.title
    assert "Quick-Connect Efficiency" in plan.offer_description_after
    assert "Push-to-connect" in plan.offer_description_after
    assert "Quick-connect" in plan.aspects_after["Features"]


def test_n4_description_li_removed_package_and_required_type_kept():
    plan = plan_for("W5368P517794", required=("Type",))
    result = result_for(plan, "storage ottoman")
    assert result.status == cs.STATUS_WILL_STRIP
    assert "Storage Ottoman feels stable" not in plan.offer_description_after
    assert "1 x Storage Ottoman" in plan.offer_description_after
    type_action = next(
        action for action in result.actions if action.token == "aspect:Type"
    )
    assert type_action.status == cs.STATUS_REQUIRED_ASPECT
    assert plan.aspects_after["Type"] == ["Storage Ottoman"]
    assert "PACKAGE INCLUDES" in plan.offer_description_after


def test_n5_table_only_match_is_protected_zone():
    extra = [
        cs.ManifestRow(
            sku="W465P453639",
            listing_id="366739874413",
            claim="matte",
            appears_in=["offer_listingDescription"],
            live_snippet="Color/Finish Black / Matte zinc finish Assembly Required",
            recommendation=cs.STRIP_NOW,
            raw_appears_in="offer_listingDescription",
        )
    ]
    plan = plan_for("W465P453639", extra_rows=extra, extra_report={"W465P453639": ["matte"]})
    result = result_for(plan, "matte")
    assert result.status == cs.STATUS_PROTECTED_ZONE
    assert "Black / Matte zinc finish" in plan.offer_description_after
    assert plan.offer_description_after.count("Black / Matte zinc finish") == plan.offer_description_before.count(
        "Black / Matte zinc finish"
    )


def test_n6_hyphen_boundary_keeps_non_reflective_and_drops_feature():
    synthetic = (
        '<li style="margin-bottom:10px"><strong>Coating:</strong> '
        "Finished with a non-reflective matte coating for low glare.</li>"
    )

    def mutate(inv):
        return None

    def mutate_offer(offer):
        offer["listingDescription"] = offer["listingDescription"].replace(
            "</ul>",
            synthetic + "</ul>",
            1,
        )

    plan = plan_for("W5257P486990", mutate_offer=mutate_offer)
    assert result_for(plan, "reflective").status == cs.STATUS_WILL_STRIP
    assert result_for(plan, "removable weights").status == cs.STATUS_WILL_STRIP
    assert "Reflective" not in plan.aspects_after["Features"]
    assert "Removable Weights" not in plan.aspects_after["Features"]
    assert "non-reflective matte coating" in plan.offer_description_after
    assert plan.aspects_after["Features"] == [
        "Adjustable",
        "Breathable",
        "Zipper Closure",
        "Lace-Up Design",
        "Ergonomic",
        "Foldable",
    ]
    assert cs.claim_matches_text("reflective", "non-reflective") is False


def test_n7_exact_aspect_value_keeps_longer_phrase():
    def mutate_inv(inv):
        features = list(inv["product"]["aspects"]["Features"])
        features.append("Energy Efficient Motor")
        inv["product"]["aspects"]["Features"] = features

    plan = plan_for("W3596P577884", mutate_inv=mutate_inv)
    assert result_for(plan, "energy efficient").status == cs.STATUS_WILL_STRIP
    assert "Energy Efficient" not in plan.aspects_after["Features"]
    assert "Energy Efficient Motor" in plan.aspects_after["Features"]


def test_n8_listing_id_mismatch_skips_sku():
    rows = load_manifest()
    for row in rows:
        if row.sku == "W465P154347" and row.claim == "220-lb load capacity":
            row.listing_id = "000000000000"
    plan = plan_for("W465P154347", rows=rows)
    assert plan.skip_reason == cs.STATUS_LISTING_ID_MISMATCH
    assert result_for(plan, "220-lb load capacity").status == cs.STATUS_LISTING_ID_MISMATCH
    assert plan.offer_description_after == plan.offer_description_before
    assert not plan.has_changes


# ---------------------------------------------------------------------------
# Extra edge cases required by the spec
# ---------------------------------------------------------------------------


def test_edge_claim_straddling_strong_and_body():
    html = wrap_template(
        "Demo",
        [
            '<li style="margin-bottom:10px"><strong>Heavy-duty build:</strong> frame rated for daily use.</li>',
            '<li style="margin-bottom:10px"><strong>Keep Me:</strong> Neighbor stays.</li>',
        ],
        table_html="<tr><td>Color</td><td>Black</td></tr>",
        package="1 x Demo",
    )
    hit = cs.strip_claim_from_description(
        html,
        "heavy-duty build",
        "KEY FEATURES Heavy-duty build: frame rated for daily use. Keep Me",
    )
    assert hit.status == cs.STATUS_WILL_STRIP
    assert "Heavy-duty build" not in hit.after_html
    assert "Keep Me" in hit.after_html


def test_edge_entities_nbsp_and_curly_quotes_in_snippet():
    html = wrap_template(
        "Demo",
        [
            '<li style="margin-bottom:10px"><strong>Quiet &amp; Smooth:</strong> '
            "A&nbsp;\u201ccushioned ride\u201d on hardwood floors.</li>"
        ],
        table_html="<tr><td>Color</td><td>Black</td></tr>",
        package="1 x Demo",
    )
    snippet = "Quiet & Smooth: A “cushioned ride” on hardwood floors."
    hit = cs.strip_claim_from_description(html, "cushioned ride", snippet)
    assert hit.status == cs.STATUS_WILL_STRIP
    assert "cushioned ride" not in cs.strip_html_to_text(hit.after_html)


def test_edge_inventory_description_differs_from_offer():
    plan = plan_for("W5368P517794", required=("Type",))
    assert plan.inventory_description_before != plan.offer_description_before
    assert len(plan.inventory_description_before) == 3558
    assert len(plan.offer_description_before) == 4160
    assert "Storage Ottoman feels stable" not in plan.offer_description_after
    assert "Storage Ottoman feels stable" not in plan.inventory_description_after
    assert "PACKAGE INCLUDES" in plan.offer_description_after
    assert "PACKAGE INCLUDES" not in plan.inventory_description_before


def test_edge_already_absent_on_rerun():
    first = plan_for("W46538540")
    assert first.has_changes
    inv, offer = load_before("W46538540")
    offer["listingDescription"] = first.offer_description_after
    inv["product"]["aspects"] = first.aspects_after
    second = cs.plan_sku_claim_strip(
        "W46538540",
        load_manifest(),
        inv,
        offer,
        cs.issues_for_sku(report_covering(load_manifest()), "W46538540"),
    )
    assert result_for(second, "matte black powder-coated").status == cs.STATUS_ALREADY_ABSENT
    assert not second.has_changes


def test_edge_absent_appears_in_token_is_already_absent_or_not_found():
    extra = [
        cs.ManifestRow(
            sku="W46538540",
            listing_id="366739827671",
            claim="matte black powder-coated",
            appears_in=["inventory_description", "aspect:DoesNotExist"],
            live_snippet="Industrial-Grade Finish: Matte black powder-coated metal resists",
            recommendation=cs.STRIP_NOW,
            raw_appears_in="inventory_description;aspect:DoesNotExist",
        )
    ]
    # Restrict the original row away by using only the extra row via location filter
    # on a cloned claim: inventory description is empty on this SKU.
    rows = [
        row
        for row in load_manifest()
        if not (row.sku == "W46538540" and row.claim == "matte black powder-coated")
    ] + extra
    plan = plan_for("W46538540", rows=rows)
    result = result_for(plan, "matte black powder-coated")
    statuses = {action.token: action.status for action in result.actions}
    assert statuses[cs.TOKEN_INV_DESC] == cs.STATUS_ALREADY_ABSENT
    assert statuses["aspect:DoesNotExist"] == cs.STATUS_ALREADY_ABSENT
    assert plan.offer_description_after == plan.offer_description_before


def test_edge_section_empty_skips_sku():
    html = wrap_template(
        "Only One",
        ['<li style="margin-bottom:10px"><strong>Only Claim:</strong> Unique phrase here.</li>'],
        table_html="<tr><td>Color</td><td>Black</td></tr>",
        package="1 x Item",
    )
    extra = [
        cs.ManifestRow(
            sku="W46538540",
            listing_id="366739827671",
            claim="unique phrase",
            appears_in=["offer_listingDescription"],
            live_snippet="Only Claim: Unique phrase here. SPECIFICATIONS",
            recommendation=cs.STRIP_NOW,
            raw_appears_in="offer_listingDescription",
        )
    ]

    def mutate_offer(offer):
        offer["listingDescription"] = html

    plan = plan_for(
        "W46538540",
        extra_rows=extra,
        extra_report={"W46538540": ["unique phrase"]},
        mutate_offer=mutate_offer,
    )
    # The real finish claim plus this synthetic last-li-only claim: the synthetic
    # wrapper has a single li, so emptying it must fail closed.
    assert result_for(plan, "unique phrase").status in {
        cs.STATUS_SECTION_EMPTY,
        cs.STATUS_WILL_STRIP,
    }
    if result_for(plan, "unique phrase").status == cs.STATUS_WILL_STRIP:
        # The SKU still has the original live description's other lis in the
        # default fixture. Force a dedicated isolated plan:
        inv, offer = load_before("W46538540")
        offer["listingDescription"] = html
        isolated = cs.plan_sku_claim_strip(
            "W46538540",
            extra,
            inv,
            offer,
            [
                {
                    "type": "semantic_feature",
                    "detail": "unique phrase",
                }
            ],
        )
        assert isolated.skip_reason == cs.STATUS_SECTION_EMPTY
        assert isolated.offer_description_after == html
        assert not isolated.has_changes


def test_claim_not_in_report_is_skipped():
    inv, offer = load_before("W46538540")
    rows = [row for row in load_manifest() if row.sku == "W46538540"]
    plan = cs.plan_sku_claim_strip("W46538540", rows, inv, offer, [])
    assert result_for(plan, "matte black powder-coated").status == cs.STATUS_CLAIM_NOT_IN_REPORT
    assert not plan.has_changes


def test_token_boundary_examples_from_spec():
    assert cs.claim_matches_text("reflective", "non-reflective") is False
    assert cs.claim_matches_text("slip", "anti-slip") is False
    assert cs.claim_matches_text("wobble-free", "wobble free")
    assert cs.claim_matches_text("220-lb", "220-Lb")
    assert cs.claim_matches_text("ready-to-install", "Ready-to-Install")
    assert cs.claim_matches_text("energy efficient", "Energy Efficient Motor")
    assert cs.normalize_claim_value("Anti-Theft") == cs.normalize_claim_value("anti-theft")
    assert cs.normalize_claim_value("Carry On") == cs.normalize_claim_value("carry on")
    hit = cs.strip_claim_from_aspects(
        {"Features": ["Energy Efficient", "Energy Efficient Motor"]},
        "Features",
        "energy efficient",
    )
    assert hit.after_aspects["Features"] == ["Energy Efficient Motor"]


def test_missing_aspect_key_is_already_absent_on_rerun():
    hit = cs.strip_claim_from_aspects({"Features": ["Quiet"]}, "Fuel Type", "propane")
    assert hit.status == cs.STATUS_ALREADY_ABSENT
    plan = plan_for("W3131P380454")
    assert "Fuel Type" not in plan.aspects_after
    inv, offer = load_before("W3131P380454")
    inv["product"]["aspects"] = plan.aspects_after
    rerun = cs.plan_sku_claim_strip(
        "W3131P380454",
        load_manifest(),
        inv,
        offer,
        cs.issues_for_sku(report_covering(load_manifest()), "W3131P380454"),
    )
    assert result_for(rerun, "propane").status == cs.STATUS_ALREADY_ABSENT


def test_taxonomy_unavailable_keeps_real_non_features_keys():
    fuel = plan_for("W3131P380454", required_available=False)
    assert result_for(fuel, "propane").status == cs.STATUS_REQUIRED_ASPECTS_UNAVAILABLE
    assert fuel.aspects_after["Fuel Type"] == ["Propane"]

    energy = plan_for("W3596P577884", required_available=False)
    energy_actions = {
        action.token: action.status for action in result_for(energy, "energy saving").actions
    }
    assert energy_actions["aspect:Energy Efficiency"] == cs.STATUS_REQUIRED_ASPECTS_UNAVAILABLE
    assert energy.aspects_after["Energy Efficiency"] == ["Energy Saving"]
    assert result_for(energy, "energy efficient").status == cs.STATUS_WILL_STRIP

    extra = [
        cs.ManifestRow(
            sku="W465130444",
            listing_id="366000000001",
            claim="wall",
            appears_in=["aspect:Mounting Type"],
            live_snippet="Mounting Type Wall",
            recommendation=cs.STRIP_NOW,
            raw_appears_in="aspect:Mounting Type",
        )
    ]
    inv = {
        "product": {
            "title": "Outdoor Pedestal Fan",
            "aspects": {"Mounting Type": ["Wall"], "Features": ["Quiet"]},
        }
    }
    offer = {
        "listingDescription": "fan",
        "listing": {"listingId": "366000000001"},
        "categoryId": "",
    }
    plan = cs.plan_sku_claim_strip(
        "W465130444",
        extra,
        inv,
        offer,
        [{"type": "semantic_feature", "detail": "wall"}],
        required_aspects_available=False,
    )
    assert result_for(plan, "wall").status == cs.STATUS_REQUIRED_ASPECTS_UNAVAILABLE
    assert plan.aspects_after["Mounting Type"] == ["Wall"]


def test_w5368_protected_zone_readback_allows_package_includes():
    plan = plan_for("W5368P517794", required=("Type",))
    before_inv, before_offer = load_before("W5368P517794")
    after_inv = deepcopy(before_inv)
    after_offer = deepcopy(before_offer)
    after_inv["product"]["description"] = plan.inventory_description_after
    after_inv["product"]["aspects"] = plan.aspects_after
    after_offer["listingDescription"] = plan.offer_description_after
    checks = cs.verify_readback(plan, after_inv, after_offer, before_inv, before_offer)
    assert checks["claims_gone"] is True
    assert checks["invariants_hold"] is True
    assert "1 x Storage Ottoman" in after_offer["listingDescription"]
    assert cs.claim_matches_unprotected("storage ottoman", after_offer["listingDescription"]) is False
    assert cs.claim_matches_text("storage ottoman", after_offer["listingDescription"]) is True


def test_offer_description_only_payload_keeps_map_and_strips_get_only():
    live = {
        "offerId": "1",
        "status": "PUBLISHED",
        "listing": {"listingId": "9"},
        "warnings": [{"message": "x"}],
        "sku": "W1",
        "availableQuantity": 2,
        "categoryId": "123",
        "storeCategoryNames": ["Home"],
        "tax": {"applyTax": True},
        "quantityLimitPerBuyer": 4,
        "regulatory": {"hazmat": "NO"},
        "secondaryCategoryId": "456",
        "lotSize": 1,
        "listingPolicies": {"fulfillmentPolicyId": "F1"},
        "merchantLocationKey": "warehouse-1",
        "pricingSummary": {
            "price": {"value": "99.00", "currency": "USD"},
            "minimumAdvertisedPrice": {"value": "120.00", "currency": "USD"},
            "originallySoldForRetailPriceOn": "ON_EBAY",
            "strikethroughPrice": {"value": "149.00", "currency": "USD"},
        },
        "listingDescription": "BEFORE",
    }
    payload = cs.offer_description_only_put_payload(live, "AFTER")
    assert payload["listingDescription"] == "AFTER"
    assert payload["availableQuantity"] == 2
    assert payload["pricingSummary"] == live["pricingSummary"]
    assert payload["listingPolicies"] == live["listingPolicies"]
    assert payload["merchantLocationKey"] == "warehouse-1"
    assert payload["storeCategoryNames"] == ["Home"]
    assert payload["tax"] == {"applyTax": True}
    assert payload["quantityLimitPerBuyer"] == 4
    assert payload["regulatory"] == {"hazmat": "NO"}
    assert payload["secondaryCategoryId"] == "456"
    assert payload["lotSize"] == 1
    assert "offerId" not in payload
    assert "listing" not in payload
    assert "status" not in payload
    assert "warnings" not in payload
    live["availableQuantity"] = 99
    assert payload["availableQuantity"] == 2


def test_fetches_price_or_qty_changed_detects_both():
    inv_a, offer_a = load_before("W46538540")
    inv_b, offer_b = load_before("W46538540")
    assert cs.fetches_price_or_qty_changed(inv_a, offer_a, inv_b, offer_b) is False
    inv_b["availability"]["shipToLocationAvailability"]["quantity"] = 3
    assert cs.fetches_price_or_qty_changed(inv_a, offer_a, inv_b, offer_b) is True
    inv_b, offer_b = load_before("W46538540")
    offer_b["pricingSummary"]["price"]["value"] = "1.00"
    assert cs.fetches_price_or_qty_changed(inv_a, offer_a, inv_b, offer_b) is True
