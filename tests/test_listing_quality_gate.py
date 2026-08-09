from types import SimpleNamespace

import src.utils.listing_quality_gate as listing_quality_gate
from src.utils.listing_quality_gate import (
    description_contains_cjk,
    description_uses_store_template,
    ensure_store_description_template,
    infer_assembly_decision,
    normalize_generated_listing,
    sanitize_generated_description_html,
    validate_listing_quality,
)

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_description_cjk_and_store_template_helpers():
    assert description_contains_cjk("产品规格 基础信息")
    assert not description_contains_cjk("Premium end table with USB ports")
    chinese_supplier = (
        "<div><h3>KEY FEATURES</h3><ul><li>Storage</li></ul>"
        "<p>产品规格 基础信息 产品类型</p></div>"
    )
    rebuilt = ensure_store_description_template(
        chinese_supplier,
        title='19.6" Farmhouse End Table with Charging Station',
        source_description=(
            "End table with USB charging ports, adjustable shelf, and easy assembly. "
            "Particle board and MDF construction."
        ),
        attributes={
            "Assembled Length (in.)": "19.6",
            "Assembled Width (in.)": "19.6",
            "Assembled Height (in.)": "23.6",
            "Product Weight (lbs.)": "36.38",
            "Main Material": "Particle Board",
            "Color": "White",
        },
        aspects={
            "Item Length": ["19.6 in"],
            "Item Width": ["19.6 in"],
            "Item Height": ["23.6 in"],
            "Item Weight": ["36.38 lbs"],
            "Type": ["End Table"],
            "Material": ["Particle Board"],
            "Color": ["White"],
        },
    )
    assert not description_contains_cjk(rebuilt)
    assert description_uses_store_template(rebuilt)
    assert "aquaverve" in rebuilt.lower()
    assert "ships from" in rebuilt.lower()
    assert "KEY FEATURES" in rebuilt


def test_validate_blocks_cjk_and_missing_store_template():
    issues = validate_listing_quality(
        {
            "title": "Farmhouse End Table",
            "description": (
                "<div><h3>KEY FEATURES</h3><ul><li>USB ports</li></ul>"
                "<p>产品规格 基础信息</p></div>"
            ),
            "categoryId": "38199",
            "aspects": {
                "Item Length": ["19.6 in"],
                "Item Width": ["19.6 in"],
                "Item Height": ["23.6 in"],
                "Item Weight": ["36 lbs"],
                "Type": ["End Table"],
                "Material": ["Wood"],
                "Color": ["White"],
                "Brand": ["AquaVerve"],
            },
        },
        source_title="Farmhouse End Table",
        source_description="End table with shelf",
        images=["https://example.com/a.jpg", "https://example.com/b.jpg"],
    )
    codes = {i.code for i in issues}
    assert "description_contains_cjk" in codes
    assert "missing_store_banner" in codes or "missing_store_footer" in codes


def test_normalize_rebuilds_chinese_supplier_html_into_store_template():
    normalized = normalize_generated_listing(
        {
            "title": '19.6" Farmhouse End Table with Charging Station',
            "description": (
                "<div><h3>KEY FEATURES</h3><ul><li>充电口</li></ul>"
                "<p>产品规格 基础信息 产品类型 产品名称</p></div>"
            ),
            "categoryId": "38199",
            "categoryName": "Nightstands",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["End Table"],
                "Item Length": ["19.6 in"],
                "Item Width": ["19.6 in"],
                "Item Height": ["23.6 in"],
                "Item Weight": ["36.38 lbs"],
                "Material": ["Particle Board"],
                "Color": ["White"],
            },
        },
        source_title='19.6" Farmhouse End Table with Charging Station',
        source_description=(
            "Farmhouse end table with built-in USB charging station, adjustable shelf, "
            "particle board construction, easy assembly."
        ),
        attributes={
            "Assembled Length (in.)": "19.6",
            "Assembled Width (in.)": "19.6",
            "Assembled Height (in.)": "23.6",
            "Product Weight (lbs.)": "36.38",
            "Main Material": "Particle Board",
            "Color": "White",
        },
        images=["https://example.com/a.jpg", "https://example.com/b.jpg"],
    )
    assert not description_contains_cjk(normalized["description"])
    assert description_uses_store_template(normalized["description"])
    issues = validate_listing_quality(
        normalized,
        source_title='19.6" Farmhouse End Table with Charging Station',
        source_description="Farmhouse end table with USB charging station",
        images=["https://example.com/a.jpg", "https://example.com/b.jpg"],
    )
    codes = {i.code for i in issues}
    assert "description_contains_cjk" not in codes
    assert "missing_store_banner" not in codes
    assert "missing_store_footer" not in codes


def test_quality_gate_sanitizes_literal_escape_and_quote_artifacts():
    dirty = (
        '<div style="max-width:900px">\\n'
        '<div><p>PREMIUM HOME FURNISHINGS</p>"></div>'
        '<div>"><h2>Accent Chair</h2></div>'
        '</div>'
    )

    cleaned = sanitize_generated_description_html(dirty)

    assert "\\n" not in cleaned
    assert '</p>"></div>' not in cleaned
    assert '>"><h2>' not in cleaned
    assert "<h2>Accent Chair</h2>" in cleaned


def test_quality_gate_normalizes_patio_set_category_aspects_and_description_measurements():
    opt = {
        "title": "Luxury Patio Furniture Set with Removable Cushions Coffee Table L-Shaped Acacia",
        "description": "<div><p>Outdoor set.</p></div>",
        "categoryId": "38208",
        "categoryName": "Sofas, Armchairs & Couches",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Sofa"],
            "Set Includes": ["Coffee Table"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "89.4",
            "Assembled Width (in.)": "62.3",
            "Assembled Height (in.)": "31.9",
            "Product Weight (lbs.)": "113.54",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "139849"
    assert normalized["categoryName"] == "Patio & Garden Furniture Sets"
    assert normalized["aspects"]["Type"] == ["Patio Furniture Set"]
    assert normalized["aspects"]["Set Includes"] == ["Sofa Set"]
    assert normalized["aspects"]["Indoor/Outdoor"] == ["Outdoor"]
    assert normalized["aspects"]["Item Length"] == ["89.4 in"]
    assert "89.4" in normalized["description"]
    assert "62.3" in normalized["description"]
    assert "31.9" in normalized["description"]


def test_normalize_generated_listing_adds_key_features_heading_when_bullets_exist():
    normalized = normalize_generated_listing(
        {
            "title": '54" Mobile Kitchen Island with Storage',
            "description": "<ul><li>Rolling kitchen island with storage.</li><li>Power outlet included.</li></ul>",
            "categoryId": "177000",
            "categoryName": "Kitchen Islands & Carts",
            "aspects": {
                "Item Length": ["54.3 in"],
                "Item Width": ["26.6 in"],
                "Item Height": ["35.4 in"],
                "Item Weight": ["97.66 lbs"],
                "Type": ["Kitchen Island"],
                "Color": ["Black"],
                "Material": ["Wood"],
            },
        },
        source_title='54" Mobile Kitchen Island with Storage',
        source_description="<div>Kitchen island with rolling casters.</div>",
    )

    assert "KEY FEATURES" in normalized["description"]
    issues = validate_listing_quality(
        normalized,
        source_title='54" Mobile Kitchen Island with Storage',
        source_description="<div>Kitchen island with rolling casters.</div>",
    )
    assert not any(issue.code == "description_incomplete" for issue in issues)


def test_normalize_generated_listing_adds_fallback_bullets_when_heading_exists_without_list():
    normalized = normalize_generated_listing(
        {
            "title": "Classic Sofa",
            "description": "<h3>KEY FEATURES</h3><p>Comfortable sofa for living room use.</p>",
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {
                "Item Length": ["74.5 in"],
                "Item Width": ["31.5 in"],
                "Item Height": ["35.0 in"],
                "Item Weight": ["99.0 lbs"],
                "Type": ["Sofa"],
                "Color": ["Black"],
                "Material": ["PU Leather"],
            },
        },
        source_title="Classic Sofa",
        source_description="<div>Assembly Required No</div>",
    )

    assert "<li" in normalized["description"]  # may be <li> or <li style=...>
    issues = validate_listing_quality(
        normalized,
        source_title="Classic Sofa",
        source_description="<div>Assembly Required No</div>",
    )
    assert not any(issue.code == "description_incomplete" for issue in issues)


def test_quality_gate_uses_source_assembly_required_over_ai_copy():
    opt = {
        "title": "Transformer Coffee Table 2 in 1 Extendable Dining Table Walnut",
        "description": (
            "<div><p>Space-Smart & Assembly-Free: Ships fully assembled - ready to use right out of the box. "
            "Saves time, eliminates frustration, and guarantees structural integrity.</p>"
            "<p>Expandable mechanism - no tools required.</p></div>"
        ),
        "categoryId": "38204",
        "categoryName": "Coffee Tables",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "Assembly Required": ["No"],
            "Packaging": ["Fully Assembled"],
        },
    }

    source_description = "<div>Product Size 31.5 x 27.5 x 16.1 in Assembly Required Yes</div>"
    normalized = normalize_generated_listing(
        opt,
        source_title="Transformer coffee table, 2 in 1 table, Expandable dining table",
        source_description=source_description,
        attributes={
            "Assembled Length (in.)": "31.50",
            "Assembled Width (in.)": "27.50",
            "Assembled Height (in.)": "16.10",
            "Product Weight (lbs.)": "52.80",
        },
        specs={},
        images=["img1", "img2"],
    )

    desc_lower = normalized["description"].lower()
    assert normalized["aspects"]["Assembly Required"] == ["Yes"]
    assert "Packaging" not in normalized["aspects"]
    assert "assembly-free" not in desc_lower
    assert "fully assembled" not in desc_lower
    assert "right out of the box" not in desc_lower

    issues = validate_listing_quality(
        normalized,
        source_title="Transformer coffee table, 2 in 1 table, Expandable dining table",
        source_description=source_description,
        attributes={
            "Assembled Length (in.)": "31.50",
            "Assembled Width (in.)": "27.50",
            "Assembled Height (in.)": "16.10",
            "Product Weight (lbs.)": "52.80",
        },
        specs={},
        images=["img1", "img2"],
    )
    assert not any(issue.code.startswith("assembly_") for issue in issues)


def test_quality_gate_blocks_assembly_required_mismatch_before_publish():
    opt = {
        "title": "Transformer Coffee Table 2 in 1 Extendable Dining Table Walnut",
        "description": "<div>No assembly required. Ships fully assembled.</div>",
        "categoryId": "38204",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "Assembly Required": ["No"],
            "Item Length": ["31.5 in"],
            "Item Width": ["27.5 in"],
            "Item Height": ["16.1 in"],
            "Packaging": ["Fully Assembled"],
        },
    }

    issues = validate_listing_quality(
        opt,
        source_title="Transformer coffee table",
        source_description="<div>Assembly Required Yes</div>",
        attributes={
            "Assembled Length (in.)": "31.50",
            "Assembled Width (in.)": "27.50",
            "Assembled Height (in.)": "16.10",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert {issue.code for issue in issues} >= {
        "assembly_required_mismatch",
        "assembly_description_contradiction",
        "assembly_packaging_contradiction",
    }


def test_flat_pack_rigid_product_overrides_generated_no_assembly_default():
    attrs = {
        "Assembled Length (in.)": "21.70",
        "Assembled Width (in.)": "14.20",
        "Assembled Height (in.)": "37.40",
        "Product Weight (lbs.)": "41.33",
        "Main Material": "Particle Board",
    }
    specs = {
        "Package Length (in.)": "39.37",
        "Package Width (in.)": "21.97",
        "Package Height (in.)": "5.40",
        "Package Weight (lbs.)": "45.88",
    }
    decision = infer_assembly_decision(
        source_title="13 Gallon Tilt Out Trash Cabinet Freestanding Trash Bin Cabinet",
        source_description="Made of Particle Board with a tilt-out cabinet design.",
        attributes=attrs,
        specs=specs,
        current_assembly="No",
    )

    assert decision["required"] == "Yes"
    assert decision["source"] is None
    assert decision["package"]["strong"] is True
    assert decision["package"]["product_family"] == "rigid"


def test_explicit_no_with_flat_pack_geometry_requires_manual_review():
    decision = infer_assembly_decision(
        source_title="Tilt Out Trash Cabinet",
        source_description="Assembly Required: No",
        attributes={
            "Assembled Length (in.)": "21.70",
            "Assembled Width (in.)": "14.20",
            "Assembled Height (in.)": "37.40",
        },
        specs={
            "Package Length (in.)": "39.37",
            "Package Width (in.)": "21.97",
            "Package Height (in.)": "5.40",
        },
        current_assembly="No",
    )

    assert decision["required"] is None
    assert decision["status"] == "review"
    assert decision["conflict"] is True


def test_quality_gate_catches_w808_style_description_no_assembly_claim():
    opt = {
        "title": "13 Gallon Tilt Out Trash Cabinet Freestanding Trash Bin Cabinet",
        "description": (
            "<div><h3>SPECIFICATIONS</h3><table>"
            '<tr data-assembly-note="true"><td>Assembly Required</td><td>No</td></tr>'
            "</table></div>"
        ),
        "categoryId": "20487",
        "aspects": {
            "Type": ["Storage Cabinet"],
            "Material": ["Particle Board"],
            "Item Length": ["21.7 in"],
            "Item Width": ["14.2 in"],
            "Item Height": ["37.4 in"],
            "Item Weight": ["41.33 lbs"],
        },
    }
    attrs = {
        "Assembled Length (in.)": "21.70",
        "Assembled Width (in.)": "14.20",
        "Assembled Height (in.)": "37.40",
        "Product Weight (lbs.)": "41.33",
        "Main Material": "Particle Board",
    }
    specs = {
        "Package Length (in.)": "39.37",
        "Package Width (in.)": "21.97",
        "Package Height (in.)": "5.40",
        "Package Weight (lbs.)": "45.88",
    }

    issues = validate_listing_quality(
        opt,
        source_title="13 Gallon Tilt Out Trash Cabinet Freestanding Trash Bin Cabinet",
        source_description="Made of Particle Board with a tilt-out cabinet design.",
        attributes=attrs,
        specs=specs,
        images=["img1", "img2"],
    )

    assert any(issue.code == "assembly_required_mismatch" for issue in issues)
    assert any(issue.code == "assembly_description_contradiction" for issue in issues)


def test_normalize_generated_listing_uses_flat_pack_assembly_evidence():
    normalized = normalize_generated_listing(
        {
            "title": "13 Gallon Tilt Out Trash Cabinet Freestanding Trash Bin Cabinet",
            "description": "<div><h3>KEY FEATURES</h3><ul><li>Particle Board cabinet.</li></ul></div>",
            "categoryId": "20487",
            "aspects": {
                "Type": ["Storage Cabinet"],
                "Material": ["Particle Board"],
                "Assembly Required": ["No"],
            },
        },
        source_title="13 Gallon Tilt Out Trash Cabinet Freestanding Trash Bin Cabinet",
        source_description="Made of Particle Board with a tilt-out cabinet design.",
        attributes={
            "Assembled Length (in.)": "21.70",
            "Assembled Width (in.)": "14.20",
            "Assembled Height (in.)": "37.40",
            "Product Weight (lbs.)": "41.33",
            "Main Material": "Particle Board",
        },
        specs={
            "Package Length (in.)": "39.37",
            "Package Width (in.)": "21.97",
            "Package Height (in.)": "5.40",
            "Package Weight (lbs.)": "45.88",
        },
        images=["img1", "img2"],
    )

    assert normalized["aspects"]["Assembly Required"] == ["Yes"]
    assert "Assembly Required" in normalized["description"]
    assert "No - Ready for use without assembly" not in normalized["description"]


def test_normalize_generated_listing_replaces_unbranded_with_house_brand(monkeypatch):
    profile = SimpleNamespace(
        brand_name="AquaVerve",
        default_brand="AquaVerve",
        force_house_brand=True,
        template_style="furniture_classic",
        quality_banner_marker="aquaverve",
        quality_footer_marker="ships from",
        brand_tagline="PREMIUM HOME FURNISHINGS",
        description_footer_line1="Ships from US Warehouse",
        description_footer_line2="Quality Guaranteed",
        footer_html="",
    )
    monkeypatch.setattr(listing_quality_gate, "_store_profile_or_none", lambda: profile)

    normalized = listing_quality_gate.normalize_generated_listing(
        {
            "title": "Tilt Out Trash Cabinet",
            "description": "<div><h3>KEY FEATURES</h3><ul><li>Storage cabinet.</li></ul></div>",
            "categoryId": "20487",
            "aspects": {"Brand": ["Unbranded"], "Type": ["Cabinet"]},
        },
        source_title="Tilt Out Trash Cabinet",
        source_description="Particle board storage cabinet.",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["aspects"]["Brand"] == ["AquaVerve"]


def test_quality_gate_blocks_unbranded_house_brand(monkeypatch):
    profile = SimpleNamespace(
        brand_name="AquaVerve",
        default_brand="AquaVerve",
        force_house_brand=True,
        quality_banner_marker="aquaverve",
        quality_footer_marker="ships from",
    )
    monkeypatch.setattr(listing_quality_gate, "_store_profile_or_none", lambda: profile)

    issues = validate_listing_quality(
        {
            "title": "Tilt Out Trash Cabinet",
            "description": "<div>aquaverve <h3>KEY FEATURES</h3><ul><li>Storage cabinet.</li></ul>ships from</div>",
            "categoryId": "20487",
            "aspects": {"Brand": ["Unbranded"]},
        },
        source_title="Tilt Out Trash Cabinet",
        images=["img1", "img2"],
    )

    assert any(issue.code == "store_brand_missing" for issue in issues)


def test_quality_gate_does_not_flag_no_assembly_required_as_contradiction():
    opt = {
        "title": "Round Storage Ottoman with Wood Legs",
        "description": "<div>No Assembly Required: Ships fully assembled-unbox, place, and enjoy.</div>",
        "categoryId": "20490",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Storage Ottoman"],
            "Assembly Required": ["No"],
            "Item Length": ["20 in"],
            "Item Width": ["20 in"],
            "Item Height": ["18 in"],
        },
    }

    issues = validate_listing_quality(
        opt,
        source_title="Round Storage Ottoman with Wood Legs",
        source_description="<div>Assembly Required No</div>",
        attributes={
            "Assembled Length (in.)": "20",
            "Assembled Width (in.)": "20",
            "Assembled Height (in.)": "18",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert not any(issue.code.startswith("assembly_") for issue in issues)


def test_normalize_generated_listing_adds_assembly_copy_from_aspect_value():
    normalized = normalize_generated_listing(
        {
            "title": "Wicker Rocking Chair with Cushions",
            "description": "<div><h3>KEY FEATURES</h3><ul><li>Outdoor rocker with cushion.</li></ul></div>",
            "categoryId": "79684",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["Yes"],
                "Item Length": ["32 in"],
                "Item Width": ["28 in"],
                "Item Height": ["36 in"],
            },
        },
        source_title="Wicker Rocking Chair with Cushions",
        source_description="<div>Outdoor rocking chair.</div>",
        images=["img1", "img2"],
    )

    assert "assembly is required" in normalized["description"].lower()
    issues = validate_listing_quality(
        normalized,
        source_title="Wicker Rocking Chair with Cushions",
        source_description="<div>Outdoor rocking chair.</div>",
        images=["img1", "img2"],
    )
    assert not any(issue.code == "assembly_description_missing" for issue in issues)


def test_quality_gate_blocks_assembly_yes_without_description_copy():
    issues = validate_listing_quality(
        {
            "title": "Wicker Rocking Chair with Cushions",
            "description": "<div><h3>KEY FEATURES</h3><ul><li>Outdoor rocker with cushion.</li></ul></div>",
            "categoryId": "79684",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["Yes"],
                "Item Length": ["32 in"],
                "Item Width": ["28 in"],
                "Item Height": ["36 in"],
            },
        },
        source_title="Wicker Rocking Chair with Cushions",
        source_description="<div>Outdoor rocking chair.</div>",
        images=["img1", "img2"],
    )

    assert any(issue.code == "assembly_description_missing" for issue in issues)


def test_quality_gate_does_not_treat_no_tools_as_no_assembly():
    yes_opt = {
        "title": "Twin Platform Bed Frame",
        "description": "<div>Effortless Assembly: requires assembly before use; no tools required beyond included Allen wrench.</div>",
        "categoryId": "175758",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Assembly Required": ["Yes"],
            "Item Length": ["80 in"],
            "Item Width": ["43 in"],
            "Item Height": ["41 in"],
        },
    }
    no_opt = {
        "title": "Compressed Sofa",
        "description": "<div>Expands to full size in minutes with no tools or assembly required.</div>",
        "categoryId": "38208",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Assembly Required": ["No"],
            "Item Length": ["116 in"],
            "Item Width": ["39 in"],
            "Item Height": ["29 in"],
        },
    }

    yes_issues = validate_listing_quality(
        yes_opt,
        source_title="Twin Platform Bed Frame",
        source_description="<div>Assembly Required Yes</div>",
        attributes={
            "Assembled Length (in.)": "80",
            "Assembled Width (in.)": "43",
            "Assembled Height (in.)": "41",
        },
        specs={},
        images=["img1", "img2"],
    )
    no_issues = validate_listing_quality(
        no_opt,
        source_title="Compressed Sofa",
        source_description="<div>Assembly Required No</div>",
        attributes={
            "Assembled Length (in.)": "116",
            "Assembled Width (in.)": "39",
            "Assembled Height (in.)": "29",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert not any(issue.code == "assembly_description_contradiction" for issue in yes_issues)
    assert not any(issue.code == "assembly_description_contradiction" for issue in no_issues)


def test_quality_gate_adds_missing_assembly_required_copy_and_removes_unsupported_status():
    normalized = normalize_generated_listing(
        {
            "title": "Heated Massage Swivel Recliner Chair with Ottoman",
            "description": (
                '<div><p>Comfortable swivel recliner chair with ottoman for daily use.</p>'
                '<h3>SPECIFICATIONS</h3><table>'
                '<tr><td>Overall Dimensions (L×W×H)</td><td>44.09 × 30.71 × 42.12 inches</td></tr>'
                '<tr style="background:#fafafa"><td>Weight</td><td>48.0 lbs</td></tr>'
                "</table></div>"
            ),
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Accent Chair"],
                "Assembly Status": ["Part Assembled"],
            },
        },
        source_title="Heated Massage Swivel Recliner Chair with Ottoman",
        source_description="<div>Assembly Required Yes</div>",
        attributes={
            "Assembled Length (in.)": "44.09",
            "Assembled Width (in.)": "30.71",
            "Assembled Height (in.)": "42.12",
        },
        specs={},
        images=["img1", "img2"],
    )

    desc_lower = normalized["description"].lower()
    assert normalized["aspects"]["Assembly Required"] == ["Yes"]
    assert "Assembly Status" not in normalized["aspects"]
    assert "Overall Dimensions (L×W×H)" in normalized["description"]
    assert "Weight" in normalized["description"]
    assert "Assembly Required" in normalized["description"]
    assert normalized["description"].count("<tr") == 3
    assert "assembly required" in desc_lower
    assert "setup required before use" in desc_lower


def test_quality_gate_preserves_explicit_source_assembly_status():
    normalized = normalize_generated_listing(
        {
            "title": "Storage Bench",
            "description": "<div>Entryway bench with hidden storage.</div>",
            "categoryId": "20490",
            "categoryName": "Ottomans, Footstools & Poufs",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Storage Ottoman"],
                "Assembly Status": ["Part Assembled"],
            },
        },
        source_title="Storage Bench",
        source_description="<div>Assembly Required Yes. Ships partially assembled.</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["aspects"]["Assembly Status"] == ["Part Assembled"]
    assert normalized["aspects"]["Assembly Required"] == ["Yes"]


def test_quality_gate_moves_plain_assembly_copy_into_specifications_row():
    normalized = normalize_generated_listing(
        {
            "title": "Garden Cart",
            "description": (
                '<div style="max-width:900px;">'
                '<p><strong>Assembly Required:</strong> Assembly is required before use. '
                "Follow the included instructions and hardware guide.</p>"
                "<h3>SPECIFICATIONS</h3><table>"
                '<tr><td>Weight</td><td>41.51 lbs</td></tr>'
                "</table></div>"
            ),
            "categoryId": "139939",
            "categoryName": "Greenhouses",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["Yes"],
            },
        },
        source_title="Garden Cart",
        source_description="<div>Assembly Required Yes</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["description"].count("Assembly Required") == 1
    assert "<strong>Assembly Required:</strong>" not in normalized["description"]
    assert normalized["description"].count("<tr") == 2
    assert "Weight" in normalized["description"]


def test_quality_gate_strips_stray_div_closers_inside_specifications_table():
    normalized = normalize_generated_listing(
        {
            "title": "Storage Bed Frame",
            "description": (
                '<div><h3>SPECIFICATIONS</h3><table>'
                '<tr><td>Overall Dimensions (L×W×H)</td><td>77.0 × 53.5 × 43.4 inches</td></tr>'
                '<tr style="background:#fafafa"><td>Weight</td><td>125.66 lbs</td></tr>'
                '<tr><td>Material</td><td>Plywood + MDF + LVL</td></tr>'
                "</div></div></table></div>"
            ),
            "categoryId": "20487",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["Yes"],
            },
        },
        source_title="Storage Bed Frame",
        source_description="<div>Assembly Required Yes</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    desc = normalized["description"]
    assert "</div></div></table>" not in desc
    assert "Assembly Required" in desc
    specs_section = desc[desc.index("<h3>SPECIFICATIONS</h3>"):]
    assert "</div>" not in specs_section.split("</table>", 1)[0]


def test_quality_gate_sanitizes_specifications_table_even_without_assembly_rule():
    normalized = normalize_generated_listing(
        {
            "title": "Accent Cabinet",
            "description": (
                '<div><h3>SPECIFICATIONS</h3><table>'
                '<tr><td>Weight</td><td>72.64 lbs</td></tr></div></div></table></div>'
            ),
            "categoryId": "20487",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["No"],
            },
        },
        source_title="Accent Cabinet",
        source_description="<div>Assembly Required No</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    desc = normalized["description"]
    specs_section = desc[desc.index("<h3>SPECIFICATIONS</h3>"):]
    assert "</div>" not in specs_section.split("</table>", 1)[0]


def test_quality_gate_repairs_bench_and_rejects_table_or_pet_specifics():
    opt = {
        "title": "50 Inch Upholstered Long Bench Modern Dining Bench with Soft Chenille Fabric",
        "description": "<div>Bench description</div>",
        "categoryId": "20740",
        "categoryName": "Furniture & Scratchers",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "Set Includes": ["Table"],
            "Pet Type": ["Cat"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "50.25",
            "Assembled Width (in.)": "15.75",
            "Assembled Height (in.)": "18.50",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "262980"
    assert normalized["aspects"]["Type"] == ["Bench"]
    assert normalized["aspects"]["Set Includes"] == ["Bench"]
    assert "Pet Type" not in normalized["aspects"]


def test_quality_gate_cleans_furniture_specifics_and_mojibake():
    opt = {
        "title": "Round Storage Ottoman Boucl谷 Fabric Footstool 每 Navy",
        "description": "<div>Boucl谷 ottoman 每 hidden storage.</div>",
        "categoryId": "38204",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "US Shoe Size": ["8"],
            "Sport": ["Athletic"],
            "Top Material": ["Wood"],
            "Upholstery Material": ["Boucl谷"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "30",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "17.25",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "20490"
    assert "Boucl谷" not in normalized["title"]
    assert "每" not in normalized["description"]
    assert aspects["Type"] == ["Storage Ottoman"]
    assert aspects["Upholstery Material"] == ["Boucle"]
    assert "US Shoe Size" not in aspects
    assert "Sport" not in aspects
    assert "Top Material" not in aspects


def test_quality_gate_blocks_missing_dimensions_and_one_image():
    opt = {
        "title": "Set of 2 Mid Century Upholstered Dining Chairs",
        "description": "<div>Dining chairs</div>",
        "categoryId": "54235",
        "aspects": {
            "Brand": ["AquaVerve"],
            "US Shoe Size": ["10"],
            "Set Includes": ["Dining Table & Chairs"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={},
        specs={},
        images=["img1"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        attributes={},
        specs={},
        images=["img1"],
    )

    assert normalized["aspects"]["Set Includes"] == ["Chairs"]
    assert "US Shoe Size" not in normalized["aspects"]
    assert {issue.code for issue in issues} >= {
        "missing_measurement",
        "insufficient_images",
    }


def test_quality_gate_keeps_board_game_table_out_of_chair_category():
    opt = {
        "title": "Board Game Table with Removable Top Cup Holders for 4–6 Players 63x40in Rustic Brown",
        "description": "<div>Pairs with standard dining chairs for game night.</div>",
        "categoryId": "54235",
        "categoryName": "Chairs",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Dining Chair"],
            "Set Includes": ["Chairs"],
            "Game Type": ["Board Game"],
            "Game Title": ["Poker"],
            "Min. Number of Players": ["2 players"],
            "Upholstery Material": ["PVC"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title='63" x 40" Board Game Table with Removable Table Top for 4-6 Players',
        attributes={
            "Assembled Length (in.)": "62.99",
            "Assembled Width (in.)": "40.16",
            "Assembled Height (in.)": "33.46",
            "Product Weight (lbs.)": "121.92",
        },
        specs={},
        images=["img1", "img2"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "62.99",
            "Assembled Width (in.)": "40.16",
            "Assembled Height (in.)": "33.46",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "38204"
    assert normalized["categoryName"] == "Tables"
    assert "4-6 Players" in normalized["title"]
    assert "4–6" not in normalized["title"]
    assert aspects["Type"] == ["Game Table"]
    assert aspects["Set Includes"] == ["Table"]
    assert aspects["Room"] == ["Game Room"]
    assert "Game Type" not in aspects
    assert "Game Title" not in aspects
    assert "Min. Number of Players" not in aspects
    assert "Upholstery Material" not in aspects
    assert not issues


def test_quality_gate_keeps_ping_pong_table_out_of_dining_set_category():
    opt = {
        "title": "4.5ft Foldable Table Tennis Table Set with Net and 2 Paddles Black",
        "description": "<div>Portable ping pong table for indoor and outdoor play.</div>",
        "categoryId": "107578",
        "categoryName": "Dining Sets",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Dining Set"],
            "Set Includes": ["Dining Table & Chairs"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "53.9",
            "Assembled Width (in.)": "29.9",
            "Assembled Height (in.)": "26.4",
            "Product Weight (lbs.)": "30.82",
        },
        specs={},
        images=["img1", "img2"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "53.9",
            "Assembled Width (in.)": "29.9",
            "Assembled Height (in.)": "26.4",
            "Product Weight (lbs.)": "30.82",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "97075"
    assert normalized["categoryName"] == "Tables"
    assert aspects["Type"] == ["Table Tennis Table"]
    assert aspects["Set Includes"] == ["Table"]
    assert not any(issue.code == "category_mismatch" for issue in issues)


def test_quality_gate_keeps_coffee_table_set_out_of_dining_set_category():
    opt = {
        "title": "2-Piece Round Nesting Coffee Table Set Black Metal Modern Indoor Outdoor",
        "description": "<div>Nesting coffee table set for living room, bedroom, balcony, patio, indoor and outdoor use.</div>",
        "categoryId": "107578",
        "categoryName": "Dining Sets",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Dining Set"],
            "Set Includes": ["Dining Table & Chairs"],
            "Number of Items in Set": ["2"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "31.0",
            "Assembled Width (in.)": "31.0",
            "Assembled Height (in.)": "28.0",
        },
        specs={},
        images=["img1", "img2"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        source_description=opt["description"],
        attributes={
            "Assembled Length (in.)": "31.0",
            "Assembled Width (in.)": "31.0",
            "Assembled Height (in.)": "28.0",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "38204"
    assert normalized["categoryName"] == "Coffee Tables"
    assert aspects["Type"] == ["Coffee Table"]
    assert aspects["Set Includes"] == ["Table"]
    assert not any(issue.code == "category_mismatch" for issue in issues)


def test_quality_gate_keeps_bean_bag_chair_out_of_sofa_category():
    opt = {
        "title": "Giant Bean Bag Chair for Adults with Lumbar Pillow Oversized Corduroy Sofa Dark Grey",
        "description": "<div>Oversized lazy sofa style bean bag chair for living room.</div>",
        "categoryId": "38208",
        "categoryName": "Sofas, Armchairs & Couches",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Sofa"],
            "Set Includes": ["Sofa"],
            "Seating Capacity": ["1"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "39.37",
            "Assembled Width (in.)": "47.24",
            "Assembled Height (in.)": "29.92",
            "Product Weight (lbs.)": "36.16",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "48319"
    assert normalized["categoryName"] == "Bean Bags & Inflatables"
    assert aspects["Type"] == ["Beanbag"]
    assert "Set Includes" not in aspects


def test_quality_gate_classifies_modular_sectional_and_armchair_type():
    sectional = normalize_generated_listing(
        {
            "title": "Modern U-Shaped Modular Sectional Sofa for Living Room Corduroy Fabric 4 Seat",
            "description": "<div>Overall Dimensions (L x W x H): 151.9 x 69.7 x 27.1 inches</div>",
            "categoryId": "38208",
            "aspects": {"Brand": ["AquaVerve"], "Type": ["Sofa"]},
        },
        source_title="Modern U-Shaped Modular Sectional Sofa for Living Room Corduroy Fabric 4 Seat",
        attributes={
            "Assembled Length (in.)": "151.9",
            "Assembled Width (in.)": "69.7",
            "Assembled Height (in.)": "27.1",
        },
        specs={},
        images=["img1", "img2"],
    )
    armchair = normalize_generated_listing(
        {
            "title": "Comfy Deep Single Seat Sofa Upholstered Reading Armchair Living Room Chair Green Chenille",
            "description": "<div>Overall Dimensions (L x W x H): 40.9 x 34.2 x 33 inches</div>",
            "categoryId": "38208",
            "aspects": {"Brand": ["AquaVerve"], "Type": ["Sofa"]},
        },
        source_title="Comfy Deep Single Seat Sofa Upholstered Reading Armchair Living Room Chair Green Chenille",
        attributes={
            "Assembled Length (in.)": "40.9",
            "Assembled Width (in.)": "34.2",
            "Assembled Height (in.)": "33",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert sectional["aspects"]["Type"] == ["Sectional"]
    assert sectional["aspects"]["Set Includes"] == ["Sofa Set"]
    assert armchair["aspects"]["Type"] == ["Armchair"]
    assert armchair["aspects"]["Set Includes"] == ["Chair"]
    assert armchair["aspects"]["Color"] == ["Green"]


def test_quality_gate_keeps_sofa_with_storage_ottoman_out_of_ottoman_category():
    opt = {
        "title": '42" Oversized Single Sofa, Faux Rabbit Fur Armchair with Cup Holders, Upholstered Chaise Lounge with Storage Ottoman and Armrests',
        "description": "<div>Deep seat sofa chair with hidden side storage and wide ottoman-style chaise support.</div>",
        "categoryId": "20490",
        "categoryName": "Ottomans, Footstools & Poufs",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Storage Ottoman"],
            "Set Includes": ["Ottoman"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "42",
            "Assembled Width (in.)": "53",
            "Assembled Height (in.)": "33.5",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "38208"
    assert normalized["categoryName"] == "Sofas, Armchairs & Couches"
    assert normalized["aspects"]["Type"] == ["Armchair"]
    assert normalized["aspects"]["Set Includes"] == ["Chair"]


def test_quality_gate_handles_outdoor_daybed_and_outdoor_chair_categories():
    daybed = normalize_generated_listing(
        {
            "title": "Outdoor Acacia Wood Round Daybed, Patio Lounger with Curved Slatted Backrest and Cushion",
            "description": "<div>Round outdoor daybed for patio and poolside use.</div>",
            "categoryId": "175758",
            "categoryName": "Beds & Bedframes",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Outdoor Acacia Wood Round Daybed, Patio Lounger with Curved Slatted Backrest and Cushion",
        attributes={
            "Assembled Length (in.)": "59",
            "Assembled Width (in.)": "60.3",
            "Assembled Height (in.)": "25.5",
        },
        specs={},
        images=["img1", "img2"],
    )
    chair = normalize_generated_listing(
        {
            "title": "Set of 2 Outdoor Acacia Wood Club Chairs with Modern Square Arms, Patio Conversation Set",
            "description": "<div>Outdoor chair set for patio, backyard, deck, and poolside.</div>",
            "categoryId": "139849",
            "categoryName": "Patio & Garden Furniture Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Set of 2 Outdoor Acacia Wood Club Chairs with Modern Square Arms, Patio Conversation Set",
        attributes={
            "Assembled Length (in.)": "27.55",
            "Assembled Width (in.)": "26.77",
            "Assembled Height (in.)": "31.88",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert daybed["categoryId"] == "138996"
    assert daybed["categoryName"] == "Outdoor Daybeds"
    assert daybed["aspects"]["Indoor/Outdoor"] == ["Outdoor"]
    assert chair["categoryId"] == "79684"
    assert chair["categoryName"] == "Outdoor Chairs"
    assert chair["aspects"]["Indoor/Outdoor"] == ["Outdoor"]
    assert chair["aspects"]["Type"] == ["Outdoor Chair"]
    assert chair["aspects"]["Set Includes"] == ["Chairs"]


def test_quality_gate_routes_desk_and_golf_profiles_to_correct_categories():
    desk = normalize_generated_listing(
        {
            "title": "L Shaped Desk with Drawers Bookshelf LED Light Modern Corner Desk Home Office Desk",
            "description": "<div>Space-saving computer desk for home office.</div>",
            "categoryId": "3199",
            "categoryName": "Bookcases",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="L Shaped Desk with Drawers Bookshelf LED Light Modern Corner Desk Home Office Desk",
        attributes={
            "Assembled Length (in.)": "59.44",
            "Assembled Width (in.)": "48.6",
            "Assembled Height (in.)": "63.38",
        },
        specs={},
        images=["img1", "img2"],
    )
    putting_green = normalize_generated_listing(
        {
            "title": "12x5 FT Golf Putting Green, Golf Training Mat with Ball Return",
            "description": "<div>Indoor outdoor golf putting green mat.</div>",
            "categoryId": "20740",
            "categoryName": "Furniture & Scratchers",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="12x5 FT Golf Putting Green, Golf Training Mat with Ball Return",
        attributes={
            "Assembled Length (in.)": "141.73",
            "Assembled Width (in.)": "59.06",
            "Assembled Height (in.)": "1.97",
        },
        specs={},
        images=["img1", "img2"],
    )
    hitting_mat = normalize_generated_listing(
        {
            "title": "5x4 FT Golf Hitting Mat with Cross Stance Guide and 7 Tee Positions",
            "description": "<div>Heavy-duty golf swing mat for practice.</div>",
            "categoryId": "20740",
            "categoryName": "Furniture & Scratchers",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="5x4 FT Golf Hitting Mat with Cross Stance Guide and 7 Tee Positions",
        attributes={
            "Assembled Length (in.)": "59.06",
            "Assembled Width (in.)": "47.24",
            "Assembled Height (in.)": "1.57",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert desk["categoryId"] == "88057"
    assert desk["categoryName"] == "Desks & Tables"
    assert putting_green["categoryId"] == "36234"
    assert putting_green["categoryName"] == "Putting Greens & Aids"
    assert hitting_mat["categoryId"] == "50876"
    assert hitting_mat["categoryName"] == "Nets, Cages & Mats"


def test_quality_gate_routes_kids_table_sets_out_of_dining_category():
    normalized = normalize_generated_listing(
        {
            "title": "Kids Activity Table and Chair Set with Storage Bins Wooden Play Table",
            "description": "<div>Children play table and chair set for arts, crafts, and reading.</div>",
            "categoryId": "107578",
            "categoryName": "Dining Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Kids Activity Table and Chair Set with Storage Bins Wooden Play Table",
        attributes={
            "Assembled Length (in.)": "31.5",
            "Assembled Width (in.)": "23.6",
            "Assembled Height (in.)": "19.7",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "66743"
    assert normalized["categoryName"] == "Play Table & Chair Sets"
    assert normalized["aspects"]["Type"] == ["Play Table & Chair Set"]
    assert normalized["aspects"]["Set Includes"] == ["Table & Chairs"]
    assert normalized["aspects"]["Room"] == ["Playroom"]


def test_quality_gate_routes_bed_storage_titles_out_of_cabinet_category():
    bunk_bed = normalize_generated_listing(
        {
            "title": "Queen Over Queen Bunk Bed with Storage Cabinets and USB Ports",
            "description": "<div>Heavy-duty bunk bed with underbed storage and guardrails.</div>",
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Queen Over Queen Bunk Bed with Storage Cabinets and USB Ports",
        attributes={
            "Assembled Length (in.)": "83.1",
            "Assembled Width (in.)": "63.5",
            "Assembled Height (in.)": "65.2",
        },
        specs={},
        images=["img1", "img2"],
    )
    loft_bed = normalize_generated_listing(
        {
            "title": "Full Size Metal Loft Bed with Stairs Wardrobe Storage Shelves LED Light",
            "description": "<div>Modern loft bed frame with integrated wardrobe and staircase.</div>",
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Full Size Metal Loft Bed with Stairs Wardrobe Storage Shelves LED Light",
        attributes={
            "Assembled Length (in.)": "93.3",
            "Assembled Width (in.)": "57.2",
            "Assembled Height (in.)": "70.9",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert bunk_bed["categoryId"] == "175758"
    assert bunk_bed["categoryName"] == "Beds & Bed Frames"
    assert bunk_bed["aspects"]["Type"] == ["Bunk Bed"]
    assert loft_bed["categoryId"] == "175758"
    assert loft_bed["categoryName"] == "Beds & Bed Frames"
    assert loft_bed["aspects"]["Type"] == ["Loft Bed"]


def test_quality_gate_routes_bar_stools_and_outdoor_lounge_chairs():
    stool = normalize_generated_listing(
        {
            "title": "Set of 2 Upgraded Version Modern Ash Wood Counter Height Bar Stools - Walnut",
            "description": "<div>Counter height bar stools with wood frame.</div>",
            "categoryId": "107578",
            "categoryName": "Dining Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Set of 2 Upgraded Version Modern Ash Wood Counter Height Bar Stools - Walnut",
        attributes={
            "Assembled Length (in.)": "21.3",
            "Assembled Width (in.)": "21.3",
            "Assembled Height (in.)": "36.6",
        },
        specs={},
        images=["img1", "img2"],
    )
    lounge = normalize_generated_listing(
        {
            "title": "[Set of 2] Bohemian Outdoor Lounge Chair with Handwoven Rope & Powder",
            "description": "<div>Outdoor lounge chair set with rope weave and cushions.</div>",
            "categoryId": "54235",
            "categoryName": "Chairs",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="[Set of 2] Bohemian Outdoor Lounge Chair with Handwoven Rope & Powder",
        attributes={
            "Assembled Length (in.)": "27.2",
            "Assembled Width (in.)": "27.0",
            "Assembled Height (in.)": "26.8",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert stool["categoryId"] == "103431"
    assert stool["categoryName"] == "Bar Stools & Stools"
    assert stool["aspects"]["Type"] == ["Bar Stool"]
    assert stool["aspects"]["Set Includes"] == ["Stools"]

    assert lounge["categoryId"] == "79684"
    assert lounge["categoryName"] == "Outdoor Chairs"
    assert lounge["aspects"]["Type"] == ["Outdoor Chair"]
    assert lounge["aspects"]["Set Includes"] == ["Chairs"]


def test_quality_gate_normalizes_pantry_titles_and_specifics():
    normalized = normalize_generated_listing(
        {
            "title": "[Assembly Video Provided] Modern 83.7in High Kitchen Pantry, Freestanding Tall Hutch with Faux Marble Top, 5 Drawers, Adjustable Shelf, Gray",
            "description": "<div>Kitchen pantry cabinet with storage shelves and faux marble top.</div>",
            "categoryId": "38217",
            "categoryName": "China Cabinets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="[Assembly Video Provided] Modern 83.7in High Kitchen Pantry, Freestanding Tall Hutch with Faux Marble Top, 5 Drawers, Adjustable Shelf, Gray",
        attributes={
            "Assembled Length (in.)": "15.7",
            "Assembled Width (in.)": "41.7",
            "Assembled Height (in.)": "83.7",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["title"].startswith("Modern 83.7in High Kitchen Pantry")
    assert "[Assembly Video Provided]" not in normalized["title"]
    assert normalized["categoryId"] == "20487"
    assert normalized["categoryName"] == "Cabinets & Cupboards"
    assert normalized["aspects"]["Type"] == ["Cabinet"]


def test_quality_gate_normalizes_treadmill_from_motors_category():
    normalized = normalize_generated_listing(
        {
            "title": "Electric Treadmill with Auto Incline Foldable 3.5HP Running Machine 61x28x48in Black",
            "description": "<div>Foldable home treadmill with incline and LCD display.</div>",
            "categoryId": "262210",
            "categoryName": "Running Boards & Nerf Bars",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Electric Treadmill with Auto Incline Foldable 3.5HP Running Machine 61x28x48in Black",
        attributes={
            "Assembled Length (in.)": "61",
            "Assembled Width (in.)": "28",
            "Assembled Height (in.)": "48",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "15280"
    assert normalized["categoryName"] == "Treadmills"
    assert normalized["aspects"]["Type"] == ["Treadmill"]


def test_quality_gate_routes_potting_bench_to_greenhouses():
    normalized = normalize_generated_listing(
        {
            "title": "Tall Garden Potting Bench Table with Hutch 65.7 in H Brown Wood Rustic",
            "description": "<div>Outdoor potting bench workstation with hutch shelves and storage.</div>",
            "categoryId": "38217",
            "categoryName": "China Cabinets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Tall Garden Potting Bench Table with Hutch 65.7 in H Brown Wood Rustic",
        attributes={
            "Assembled Length (in.)": "50.0",
            "Assembled Width (in.)": "20.3",
            "Assembled Height (in.)": "65.7",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "139939"
    assert normalized["categoryName"] == "Greenhouses"
    assert normalized["aspects"]["Type"] == ["Potting Bench"]


def test_quality_gate_sets_dining_set_item_count_from_title():
    normalized = normalize_generated_listing(
        {
            "title": "5-Piece Outdoor Acacia Wood Round Dining Set Solid Wood Patio Table with 4 Barrel Chairs",
            "description": "<div>Round patio dining set with 4 chairs and one table.</div>",
            "categoryId": "107578",
            "categoryName": "Dining Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="5-Piece Outdoor Acacia Wood Round Dining Set Solid Wood Patio Table with 4 Barrel Chairs",
        attributes={
            "Assembled Length (in.)": "44.5",
            "Assembled Width (in.)": "44.5",
            "Assembled Height (in.)": "29.5",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["aspects"]["Number of Items in Set"] == ["5"]
    assert normalized["aspects"]["Number of Pieces"] == ["5"]


def test_quality_gate_blocks_unsupported_foldable_claim_from_ai_draft():
    normalized = normalize_generated_listing(
        {
            "title": "Modern Accent Chair Foldable Linen Lounge Chair",
            "description": "<div>Foldable living room chair for compact storage.</div>",
            "categoryId": "54235",
            "categoryName": "Chairs",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Modern Accent Chair Linen Lounge Chair",
        source_description="<div>Accent chair for living room seating.</div>",
        attributes={
            "Assembled Length (in.)": "32",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "34",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["source_facts"]["claims"]["foldable"]["supported"] is False

    issues = validate_listing_quality(
        normalized,
        source_title="Modern Accent Chair Linen Lounge Chair",
        source_description="<div>Accent chair for living room seating.</div>",
        attributes={
            "Assembled Length (in.)": "32",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "34",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert any(issue.code == "unsupported_foldable_claim" for issue in issues)


def test_normalize_generated_listing_restores_supported_foldable_feature():
    normalized = normalize_generated_listing(
        {
            "title": "Pet Stroller for Dogs and Cats",
            "description": "<div>Compact stroller for dogs and cats.</div>",
            "categoryId": "139971",
            "categoryName": "Pet Strollers",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Pet Stroller for Dogs and Cats",
        source_description="<div>Foldable pet stroller with storage basket.</div>",
        attributes={
            "Assembled Length (in.)": "30",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "39",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert "Foldable" in normalized["aspects"]["Features"]


def test_quality_gate_blocks_missing_supported_foldable_feature():
    issues = validate_listing_quality(
        {
            "title": "Pet Stroller for Dogs and Cats",
            "description": "<div>Compact stroller for dogs and cats.</div>",
            "categoryId": "139971",
            "categoryName": "Pet Strollers",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Item Length": ["30 in"],
                "Item Width": ["18 in"],
                "Item Height": ["39 in"],
            },
        },
        source_title="Pet Stroller for Dogs and Cats",
        source_description="<div>Foldable pet stroller with storage basket.</div>",
        attributes={
            "Assembled Length (in.)": "30",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "39",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert any(issue.code == "missing_supported_foldable_feature" for issue in issues)


def test_foldable_arbiter_agrees_with_claim_diff_on_real_titles():
    """Oscillation regression: both detectors must agree on the same source inputs.

    Uses real source titles from W5532P458418 / XW000029AAB handoff residual SKUs.
    """
    from src.utils.claim_diff_engine import build_source_constraints
    from src.utils.listing_quality_gate import build_source_facts, source_supports_foldable

    cases = [
        (
            "Convertible Sleeper Sofa Bed,two-tone blended fabric Folding Mattress "
            "Couch with Fixed-Shape Frame, Floor Sofa Lounge Couch",
            "Comfortable sleeper sofa for living room.",
            True,
        ),
        (
            "Extendable Dining Table with Extra-Long Folding Top, Rolling Kitchen Island "
            "with Drawers, Power Outlet and Brake lock",
            "Rolling kitchen island table.",
            True,
        ),
        (
            "Extendable Dining Table with Drop Leaf Mobile Rolling Island",
            "Extendable tabletop with drop leaf only.",
            False,
        ),
    ]
    for source_title, source_description, expected in cases:
        arbiter = source_supports_foldable(
            source_title=source_title,
            source_description=source_description,
            attributes={},
            specs={},
        )
        facts = build_source_facts(
            source_title=source_title,
            source_description=source_description,
            attributes={},
            specs={},
        )
        constraints = build_source_constraints(
            {},
            {},
            source_description,
            source_title,
        )
        assert arbiter is expected, source_title
        assert facts["claims"]["foldable"]["supported"] is expected, source_title
        assert ("foldable" in constraints["supported_features"]) is expected, source_title
        # Detectors must not disagree
        assert facts["claims"]["foldable"]["supported"] == (
            "foldable" in constraints["supported_features"]
        )


def test_quality_gate_blocks_unsupported_charging_claim_from_ai_draft():
    normalized = normalize_generated_listing(
        {
            "title": "Modern Style L-Shaped Sectional Sofa with USB Ports",
            "description": "<div>Sectional sofa with hidden USB charging for daily use.</div>",
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Modern Style L-Shaped Sectional Sofa 102 in Black",
        source_description="<div>Sectional sofa with chaise lounge and storage seat.</div>",
        attributes={
            "Assembled Length (in.)": "102",
            "Assembled Width (in.)": "61",
            "Assembled Height (in.)": "35",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["source_facts"]["claims"]["charging"]["supported"] is False

    issues = validate_listing_quality(
        normalized,
        source_title="Modern Style L-Shaped Sectional Sofa 102 in Black",
        source_description="<div>Sectional sofa with chaise lounge and storage seat.</div>",
        attributes={
            "Assembled Length (in.)": "102",
            "Assembled Width (in.)": "61",
            "Assembled Height (in.)": "35",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert any(issue.code == "unsupported_charging_claim" for issue in issues)


def test_quality_gate_blocks_non_publishable_source_video_manifest():
    normalized = normalize_generated_listing(
        {
            "title": "Large Wooden Chicken Coop Outdoor Hen House",
            "description": "<div>Outdoor chicken coop with nesting box and run.</div>",
            "categoryId": "63108",
            "categoryName": "Cages, Hutches & Enclosures",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Large Wooden Chicken Coop Outdoor Hen House",
        source_description="<div>Outdoor chicken coop with nesting box and run.</div>",
        attributes={
            "Assembled Length (in.)": "120",
            "Assembled Width (in.)": "27",
            "Assembled Height (in.)": "42",
        },
        specs={},
        images=["img1", "img2"],
        videos=["https://media.example.com/video_links.txt"],
    )

    assert normalized["source_facts"]["video"]["preflight_status"] == "blocked"
    assert "source_video_txt_manifest" in normalized["source_facts"]["video"]["issue_codes"]

    issues = validate_listing_quality(
        normalized,
        source_title="Large Wooden Chicken Coop Outdoor Hen House",
        source_description="<div>Outdoor chicken coop with nesting box and run.</div>",
        attributes={
            "Assembled Length (in.)": "120",
            "Assembled Width (in.)": "27",
            "Assembled Height (in.)": "42",
        },
        specs={},
        images=["img1", "img2"],
        videos=["https://media.example.com/video_links.txt"],
    )

    assert any(issue.code == "source_video_not_publishable" for issue in issues)


def test_generation_and_publish_entrypoints_call_quality_gate():
    for relative_path in ("batch_analyze.py", "daily_tasks.py", "server.py", "batch_publish.py"):
        source = (ROOT / relative_path).read_text(encoding="utf-8-sig")

        assert "normalize_generated_listing" in source or "_normalize_generated_listing" in source
        if relative_path in {"daily_tasks.py", "batch_publish.py"}:
            assert "run_listing_qc" in source
        else:
            assert "validate_listing_quality" in source or "_validate_listing_quality" in source
            assert "blocking_issue_messages" in source or "_quality_blocking_messages" in source


def test_analysis_entrypoints_pass_shared_category_matcher():
    assert "category_matcher=CATEGORY_MATCHER" in (ROOT / "batch_analyze.py").read_text(encoding="utf-8-sig")
    assert "category_matcher=CATEGORY_MATCHER" in (ROOT / "daily_tasks.py").read_text(encoding="utf-8-sig")
    assert "category_matcher=_get_quality_gate_category_matcher()" in (ROOT / "server.py").read_text(encoding="utf-8-sig")


class TestSofaAccessoryTableExclusion:
    """2026-07-13 live incident: 'Sofa Side Table' nightstand and 'Sofa Table
    Behind Couch' console table were recategorized into 38208 (sofas)."""

    def test_nightstand_with_sofa_side_table_phrase_is_not_sofa(self):
        from src.utils.listing_quality_gate import classify_listing_profile
        profile = classify_listing_profile(
            "Solid Wood Nightstand with Two Drawers and Pull-out Panel Storage Bedside Table and Sofa Side Table",
            "", "38199",
        )
        assert profile.kind != "sofa"

    def test_console_table_behind_couch_is_not_sofa(self):
        from src.utils.listing_quality_gate import classify_listing_profile
        profile = classify_listing_profile(
            "60 Inch Narrow Console Table with Built-in Power Outlet, Farmhouse Sofa Table Behind Couch, Entryway",
            "", "38204",
        )
        assert profile.kind != "sofa"

    def test_side_table_with_room_couch_reference_is_a_table(self):
        from src.utils.listing_quality_gate import classify_listing_profile
        profile = classify_listing_profile(
            '23" Mid-Century Side Table with Woven Shelf for Living Room Couch',
            "", "38208",
        )
        assert profile.kind == "side_table"
        assert profile.category_id == "54235"
        assert profile.category_name == "End Tables"
        assert profile.type_value == "End & Side Tables"

    def test_real_sofa_still_classified(self):
        from src.utils.listing_quality_gate import classify_listing_profile
        profile = classify_listing_profile(
            '71" 3 Seater Sofa, Corduroy Fabric, Deep Seat Couches, Comfy Loveseat Sofa',
            "", "38208",
        )
        assert profile.kind == "sofa"
        assert profile.category_id == "38208"


class TestFoldableArbiterInflections:
    """2026-07-14 foldable 回归:源写 'can be folded' 但 \bfold\b 漏了 -ed 词形,
    drop-leaf kitchen island 被误判不支持折叠,改写反复卡住。"""

    def test_folded_inflection_supported(self):
        from src.utils.listing_quality_gate import source_supports_foldable
        assert source_supports_foldable(
            source_title="53inch Kitchen Island with Drop Leaf",
            source_description="the drop leaf can be unfolded or folded according to needs",
        ) is True

    def test_folds_inflection_supported(self):
        from src.utils.listing_quality_gate import source_supports_foldable
        assert source_supports_foldable(source_description="the side leaf folds down for storage") is True

    def test_extendable_alone_still_not_supported(self):
        from src.utils.listing_quality_gate import source_supports_foldable
        assert source_supports_foldable(
            source_title="Extendable Dining Table with Expandable Top",
            source_description="pull to extend the table surface",
        ) is False

    def test_folder_noun_not_false_positive(self):
        from src.utils.listing_quality_gate import source_supports_foldable
        assert source_supports_foldable(source_description="includes a paper folder organizer") is False

    def test_cushion_will_not_collapse_is_not_foldable_evidence(self):
        from src.utils.listing_quality_gate import source_supports_foldable
        assert source_supports_foldable(
            source_description="The high-density sponge cushion will not collapse after long-term sitting."
        ) is False


def test_bedside_table_profile_is_nightstand_not_side_table():
    from src.utils.listing_quality_gate import classify_listing_profile

    profile = classify_listing_profile(
        'Set of 2 with 2 Drawers, 15.4" Modern Storage Bedside Table with Handles',
        "Bedroom storage",
        "38199",
    )

    assert profile.kind == "nightstand"
    assert profile.category_id == "38199"


def test_plain_ottoman_bench_is_not_invented_as_storage_ottoman():
    from src.utils.listing_quality_gate import classify_listing_profile

    profile = classify_listing_profile(
        "Upholstered Ottoman Bench with Solid Wood Legs",
        "A padded footrest and extra seat for the living room or bedroom.",
        "20490",
    )

    assert profile.kind == "ottoman_bench"
    assert profile.category_id == "20490"
    assert profile.type_value == "Ottoman"


def test_indoor_armchair_with_balcony_in_room_list_stays_indoor():
    from src.utils.listing_quality_gate import classify_listing_profile

    profile = classify_listing_profile(
        "Foil Fabric Wood Armchair",
        "Suitable for the living room, bedroom, office, or balcony.",
        "38208",
    )

    assert profile.kind != "outdoor_chair"
    assert profile.category_id != "79684"
    assert profile.type_value == "Armchair"


class TestIndoorCopyBeatsAnIncidentalOutdoorMention:
    """2026-07-27 live 误伤:两条软包储物凳被判 outdoor_daybed 并改到 138996。
    源标题写明 For Living Room, Entryway, Dormitory, Bedroom,描述末尾一句
    "or a leisure bench on the balcony" 就使 outdoor_context 成立,叠加标题里的
    "Bench Daybed" 即命中。

    注意:ebay_category_matcher 里有同一判定的第二份实现。上一轮只修了那份,
    而审计走的是本函数,所以 live 上的错误类目依旧不报——两处都得修。"""

    TITLE = (
        '65.75" Wide Modern Upholstered Storage Bench With Double Lids, '
        "Napped fabric Foot Stool With Rolled Armrest, Bench Daybed With Rubberwood Legs "
        "For Living Room, Entryway, Dormitory, Bedroom"
    )
    DESC = "Use it as a temporary seat in the study, or a leisure bench on the balcony."

    def test_indoor_bench_is_not_an_outdoor_daybed(self):
        from src.utils.listing_quality_gate import classify_listing_profile

        profile = classify_listing_profile(self.TITLE, self.DESC, "138996")
        assert profile.kind != "outdoor_daybed"
        assert profile.category_id != "138996"

    def test_audit_would_now_report_the_mismatch(self):
        from src.utils.listing_quality_gate import classify_listing_profile

        profile = classify_listing_profile(self.TITLE, self.DESC, "138996")
        assert profile.category_id and profile.category_id != "138996"

    def test_genuine_outdoor_daybed_still_classified(self):
        from src.utils.listing_quality_gate import classify_listing_profile

        for title, desc in (
            ("Patio Rattan Daybed with Canopy and Cushions", "Weather resistant wicker."),
            ("Outdoor Daybed Round Rattan Sun Lounger", "Poolside lounging."),
            ("Rattan Daybed with Canopy", "Perfect for the patio and poolside."),
        ):
            profile = classify_listing_profile(title, desc, "38204")
            assert profile.kind == "outdoor_daybed", title
            assert profile.category_id == "138996", title

    def test_outdoor_word_in_title_wins_even_with_indoor_rooms(self):
        from src.utils.listing_quality_gate import classify_listing_profile

        profile = classify_listing_profile(
            "Outdoor Patio Daybed with Canopy",
            "Also works in the living room or bedroom.",
            "38204",
        )
        assert profile.kind == "outdoor_daybed"
