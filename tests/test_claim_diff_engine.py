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


def test_foldable_title_only_folding_mattress_is_supported_not_claim_violation():
    """Real W5532-class source: fold evidence only in title ('Folding Mattress')."""
    title = (
        "Convertible Sleeper Sofa Bed,two-tone blended fabric Folding Mattress "
        "Couch with Fixed-Shape Frame, Floor Sofa Lounge Couch"
    )
    source = build_source_constraints({}, {}, "Comfortable sleeper sofa for living room.", title)
    assert "foldable" in source["supported_features"]
    violations = detect_claim_violations(
        source,
        "Sleeper Sofa Bed Pull Out Couch",
        "Features Foldable design.",
        {"Features": ["Foldable"]},
    )
    assert not any(v.claim_text == "foldable" for v in violations)


def test_source_two_in_one_phrase_supports_convertible_feature():
    source = build_source_constraints(
        {},
        {},
        "A versatile desk for home office use.",
        "L-Shaped Desk with 2-in-1 Storage Design",
    )

    assert "convertible" in source["supported_features"]
    violations = detect_claim_violations(
        source,
        "L-Shaped Desk with 2-in-1 Storage Design",
        "2-in-1 workspace and storage.",
        {},
    )
    assert not any(v.claim_text == "convertible" for v in violations)


def test_extendable_alone_does_not_support_foldable():
    source = build_source_constraints(
        {},
        {},
        "Rolling kitchen island with extendable tabletop and drop leaf.",
        "Extendable Dining Table with Drop Leaf",
    )
    assert "foldable" not in source["supported_features"]
    violations = detect_claim_violations(
        source,
        "Extendable Dining Table Foldable",
        "Foldable top for storage.",
        {"Features": ["Foldable"]},
    )
    assert any(v.claim_text == "foldable" for v in violations)


def test_folding_top_plus_extendable_supports_foldable():
    """Real XW000029-class source: Extendable + Extra-Long Folding Top."""
    title = (
        "Extendable Dining Table with Extra-Long Folding Top, Rolling Kitchen Island "
        "with Drawers, Power Outlet and Brake lock"
    )
    source = build_source_constraints({}, {}, "Rolling kitchen island table.", title)
    assert "foldable" in source["supported_features"]

def test_no_false_positive_chinese_source():
    source = build_source_constraints({}, {}, "这款床带蓝牙音响功能。", "床")
    violations = detect_claim_violations(source, "Bed with Bluetooth", "Built-in speaker", {})
    assert not any(v.claim_text == "bluetooth" for v in violations)


def test_detects_unsupported_removable_and_zippered_tent_floor():
    source = build_source_constraints(
        {"Material": "oxford fabric"},
        {},
        "600D Oxford cloth bell tent with two doors, mesh windows, stove jack, and roll-up sidewalls.",
        "600D Oxford Bell Tent",
    )

    violations = detect_claim_violations(
        source,
        "Bell Tent with Stove Jack",
        "Includes a zipped removable floor with full-length dual zipper.",
        {
            "Features": ["Stove Jack", "Zipped", "Removable Floor"],
            "Closure Type": ["Zipper"],
        },
    )

    claim_texts = {v.claim_text for v in violations}
    assert "removable_floor" in claim_texts
    assert "zippered_floor" in claim_texts


def test_no_false_positive_when_source_supports_zippered_floor():
    source = build_source_constraints(
        {},
        {},
        "The tent includes a detachable floor with zipper access.",
        "Bell Tent",
    )

    violations = detect_claim_violations(
        source,
        "Bell Tent",
        "Includes a zipped removable floor.",
        {"Features": ["Zipped", "Removable Floor"], "Closure Type": ["Zipper"]},
    )

    assert not any(v.claim_text in {"removable_floor", "zippered_floor"} for v in violations)


def test_replace_description_measurements_numeric_weight_after_backreference():
    """W2699P456 handoff incident: weight "19.7 lbs" after a \1 backreference
    parsed as group \19 and raised re.PatternError."""
    from src.utils.dimension_helpers import replace_description_measurements

    description = (
        '<div><span style="a">产品重量 (磅) :</span> <span style="b">Not specified</span></div>'
    )
    updated = replace_description_measurements(description, weight=19.7)
    assert "19.7 lbs" in updated


def test_soft_close_vibration_is_not_massage_hallucination():
    """Cabinet soft-close damping mentions vibration, not massage (N728 incident)."""
    soft_close = (
        "Soft-close hinges enable quiet door closing while reducing impact and vibration. "
        "They help protect the cabinet structure and wine."
    )
    source = build_source_constraints({}, {}, soft_close, '70" Tall Arched Bar Cabinet')
    violations = detect_claim_violations(
        source,
        '70" Tall Arched Bar Cabinet',
        soft_close,
        {"Type": ["Apothecary Cabinet"], "Material": ["MDF+Metal"]},
    )
    assert not any(v.claim_text == "massage" for v in violations)


def test_still_detects_real_massage_claim():
    source = build_source_constraints({}, {}, "Upholstered recliner with heat only.", "Recliner")
    violations = detect_claim_violations(
        source,
        "Massage Recliner",
        "Built-in massage functions and vibrating seat modes.",
        {"Massage Functions": ["Full Body"]},
    )
    assert any(v.claim_text == "massage" for v in violations)
