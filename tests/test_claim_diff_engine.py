from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations

def test_detects_tsa_lock_hallucination():
    source = build_source_constraints({}, {}, "A simple combination lock for safety.", "Lock")
    violations = detect_claim_violations(source, "Great Lock", "Features TSA approved lock", {"Features": ["TSA lock"]})
    assert len(violations) > 0
    assert any(v.claim_text == "tsa_approved" for v in violations)
    assert any(v.severity == "CRITICAL" for v in violations)

def test_detects_charging_hallucination():
    source = build_source_constraints({}, {}, "Modern desk.", "Desk")
    violations = detect_claim_violations(source, "Desk with USB", "Includes charging station", {"Features": ["USB port"]})
    assert any(v.claim_text == "charging" for v in violations)

def test_detects_genuine_leather_hallucination():
    source = build_source_constraints({"Material": "PU Leather"}, {}, "Nice chair", "Chair")
    violations = detect_claim_violations(source, "Chair", "Made of genuine leather", {"Material": "Genuine Leather"})
    assert any(v.claim_text == "genuine leather" for v in violations)

def test_detects_solid_wood_hallucination():
    source = build_source_constraints({"Material": "MDF"}, {}, "Nice table", "Table")
    violations = detect_claim_violations(source, "Solid wood table", "Solid wood construction", {})
    assert any(v.claim_text == "solid wood" for v in violations)

def test_detects_foldable_hallucination():
    source = build_source_constraints({}, {}, "A nice chair.", "Chair")
    violations = detect_claim_violations(source, "Foldable chair", "Easy to fold", {})
    assert any(v.claim_text == "foldable" for v in violations)

def test_skips_foldable_for_naturally_foldable_products():
    source = build_source_constraints({}, {}, "Outdoor protection.", "Patio Umbrella")
    violations = detect_claim_violations(source, "Foldable Umbrella", "Easy to fold", {})
    assert not any(v.claim_text == "foldable" for v in violations)

def test_detects_bluetooth_hallucination():
    source = build_source_constraints({}, {}, "Modern bed.", "Bed")
    violations = detect_claim_violations(source, "Bed with Bluetooth", "Built-in speakers", {})
    assert any(v.claim_text == "bluetooth" for v in violations)

def test_detects_iron_to_aluminum_upgrade():
    source = build_source_constraints({"Material": "Iron"}, {}, "Metal frame.", "Canopy")
    violations = detect_claim_violations(source, "Aluminum frame canopy", "Aluminum", {})
    assert any(v.claim_text == "aluminum" for v in violations)

def test_detects_tempered_glass_hallucination():
    source = build_source_constraints({"Material": "Glass"}, {}, "Glass table.", "Table")
    violations = detect_claim_violations(source, "Tempered glass table", "Tempered glass top", {})
    assert any(v.claim_text == "tempered glass" for v in violations)

def test_detects_wood_species_hallucination():
    source = build_source_constraints({"Material": "Solid Wood"}, {}, "Wooden table.", "Table")
    violations = detect_claim_violations(source, "Acacia wood table", "Made of acacia", {})
    assert any(v.claim_text == "Acacia Wood" for v in violations)

def test_detects_reclining_hallucination():
    source = build_source_constraints({}, {}, "Comfortable sofa.", "Sofa")
    violations = detect_claim_violations(source, "Reclining sofa", "Reclines easily", {})
    assert any(v.claim_text == "reclining" for v in violations)

def test_detects_swivel_hallucination():
    source = build_source_constraints({}, {}, "Office chair.", "Chair")
    violations = detect_claim_violations(source, "360 Swivel Chair", "Rotates fully", {})
    assert any(v.claim_text == "swivel" for v in violations)

def test_detects_quantity_mismatch():
    source = build_source_constraints({}, {}, "A nice 3-tier shelf.", "Shelf")
    violations = detect_claim_violations(source, "5-tier shelf", "Has 5 tiers", {})
    assert any("tier" in v.claim_text and "5" in v.claim_text for v in violations)

def test_detects_unsupported_quantity():
    source = build_source_constraints({}, {}, "Adjustable chair.", "Chair")
    violations = detect_claim_violations(source, "5-position chair", "Adjusts to 5 positions", {})
    assert any(v.claim_type == "unsupported_quantity" and "position" in v.claim_text for v in violations)

def test_detects_cushion_hallucination():
    source = build_source_constraints({}, {}, "Wooden dining chair.", "Chair")
    violations = detect_claim_violations(source, "Chair with cushion", "Soft cushion included", {})
    assert any(v.claim_text == "cushion" for v in violations)

def test_upholstery_aspect_name_alone_does_not_trigger_cushion_claim():
    source = build_source_constraints({}, {}, "Indoor accent chair.", "Chair")
    violations = detect_claim_violations(
        source,
        "Accent Chair",
        "<div>Comfortable seating.</div>",
        {"Upholstery Fabric": "Fabric"},
    )
    assert not any(v.claim_text == "cushion" for v in violations)

def test_quantity_detection_does_not_cross_aspect_boundaries():
    source = build_source_constraints({}, {}, "Outdoor chair.", "Chair")
    violations = detect_claim_violations(
        source,
        "Outdoor Club Chair",
        "Comfortable patio chair.",
        {
            "Seating Capacity": "Up to 2",
            "Seat Depth": "25 in",
        },
    )
    assert not any(v.claim_type == "unsupported_quantity" and "seat" in v.claim_text for v in violations)

def test_detects_weather_resistance_hallucination():
    source = build_source_constraints({}, {}, "Outdoor bench.", "Bench")
    violations = detect_claim_violations(source, "Weather resistant bench", "UV resistant", {})
    assert any(v.claim_text in ["weather_resistant", "uv_resistant"] for v in violations)

def test_no_false_positive_when_source_supports():
    source = build_source_constraints({}, {}, "This chair is foldable.", "Chair")
    violations = detect_claim_violations(source, "Foldable chair", "Easy to fold", {})
    assert not any(v.claim_text == "foldable" for v in violations)

def test_no_false_positive_chinese_source():
    source = build_source_constraints({}, {}, "这款床带蓝牙音响功能。", "床")
    violations = detect_claim_violations(source, "Bed with Bluetooth", "Built-in speaker", {})
    assert not any(v.claim_text == "bluetooth" for v in violations)
