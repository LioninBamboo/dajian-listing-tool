import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_fix_ready_drafts",
    ROOT / "scripts" / "audit_fix_ready_drafts.py",
)
audit_fix_ready_drafts = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(audit_fix_ready_drafts)


def test_targeted_copy_repair_removes_foldable_kitchen_island_claims():
    opt = {
        "title": "53 Inch Kitchen Island with Drop Leaf Power Outlet Rolling Cart Storage",
        "description": (
            "<li><strong>Integrated Power Outlet & Drop Leaf:</strong> Built-in UL-certified power strip "
            "(3 outlets + 2 USB ports) keeps appliances charged; foldable drop leaf extends surface area.</li>"
        ),
        "aspects": {
            "Features": ["With Storage", "Power Outlet", "Foldable"],
        },
    }

    notes = audit_fix_ready_drafts.apply_targeted_copy_repairs("N707S185531B", opt)

    assert "Foldable" not in opt["aspects"]["Features"]
    assert "foldable drop leaf" not in opt["description"].lower()
    assert "drop leaf extends surface area" in opt["description"].lower()
    assert any("removed unsupported foldable wording" in note for note in notes)


def test_targeted_copy_repair_removes_foldable_sentence_from_bike_description():
    opt = {
        "title": "Indoor Exercise Bike Stationary Cycling Bike Magnetic Resistance Cardio",
        "description": (
            "<p>Compact home bike for daily cardio - no foldable mechanism required thanks to its "
            "space-efficient profile.</p>"
        ),
        "aspects": {
            "Features": ["Portable Design", "Magnetic Brake System"],
        },
    }

    notes = audit_fix_ready_drafts.apply_targeted_copy_repairs("W2531P498304", opt)

    assert "foldable" not in opt["description"].lower()
    assert "space-efficient profile" not in opt["description"].lower()
    assert any("removed unsupported foldable sentence" in note for note in notes)


def test_targeted_copy_repair_rebuilds_unsupported_bed_copy():
    opt = {
        "title": "King Size Platform Bed Frame with Charging Station LED Headboard Walnut Wood",
        "description": (
            "<h2>King Size Platform Bed Frame with Charging Station LED Headboard Walnut Wood</h2>"
            "<li><strong>Integrated Charging Station:</strong> Dual USB-A + USB-C ports built into the "
            "headboard for convenient, clutter-free device charging.</li>"
            "<li><strong>LED Accent Lighting (Optional Upgrade):</strong> Soft ambient LED lighting "
            "(remote-controlled RGB) enhances ambiance.</li>"
            "<p>Ideal for tech-savvy homeowners seeking retro-modern aesthetics with practical upgrades "
            "like charging and ambient lighting - no extra furniture or power strips needed.</p>"
        ),
        "aspects": {
            "Features": [
                "No Box Spring Needed",
                "Charging Station",
                "LED Light",
                "Assembly Instructions",
                "Heavy Duty Metal Slat Support",
                "Squeak Resistant",
                "2 Tier Wooden Storage Headboard",
                "Under Bed Storage",
            ],
        },
    }

    notes = audit_fix_ready_drafts.apply_targeted_copy_repairs("W5819S00010", opt)

    assert opt["title"] == "King Size Spindle Four Poster Platform Bed Frame Curved Headboard Walnut"
    assert opt["aspects"]["Features"] == [
        "No Box Spring Needed",
        "Assembly Instructions",
        "Heavy Duty Metal Slat Support",
        "Squeak Resistant",
        "Under Bed Storage",
    ]
    lowered = opt["description"].lower()
    assert "charging" not in lowered
    assert "usb" not in lowered
    assert "led" not in lowered
    assert "curved headboard walnut" in lowered
    assert any(note.startswith("title -> ") for note in notes)


def test_ready_draft_audit_seeds_measurement_normalization_with_repaired_aspects():
    source = (ROOT / "scripts" / "audit_fix_ready_drafts.py").read_text(encoding="utf-8-sig")
    assert "seed_aspects = dict(opt.get(\"aspects\") or {})" in source
    assert "draft_desc,\n            seed_aspects," in source


def test_rocking_egg_chair_keeps_rocking_type():
    aspects = audit_fix_ready_drafts.normalize_semantic_aspects(
        "Papasan Rocking Egg Chair Outdoor Rocker",
        "79682",
        {"Type": ["Hanging Chair"]},
    )

    assert aspects["Type"] == ["Rocking Chair"]


def test_category_defaults_use_supported_upholstery_material_before_polyester():
    aspects = audit_fix_ready_drafts.apply_category_defaults(
        "38208",
        {"Upholstery Material": ["Chenille"]},
    )

    assert aspects["Upholstery Fabric"] == ["Chenille"]


def test_targeted_rocker_repair_removes_unsupported_assembly_and_hanging_type():
    opt = {
        "description": (
            '<table><tr data-assembly-note="true"><td>Assembly Required</td>'
            '<td>No - Ready for use without assembly.</td></tr></table>'
        ),
        "aspects": {"Type": ["Hanging Chair"], "Assembly Required": ["No"]},
    }

    audit_fix_ready_drafts.apply_targeted_copy_repairs("W2887P511377", opt)

    assert opt["aspects"]["Type"] == ["Rocking Chair"]
    assert "Assembly Required" not in opt["aspects"]
    assert "assembly required" not in opt["description"].lower()


def test_targeted_bench_repair_removes_collapse_wording_and_floor_standing():
    opt = {
        "description": "<li>The cushion will not collapse after long-term sitting.</li>",
        "aspects": {"Features": ["Foldable"], "Mounting": ["Floor Standing"]},
    }

    audit_fix_ready_drafts.apply_targeted_copy_repairs("W5368P503714", opt)

    assert "Foldable" not in opt["aspects"]["Features"]
    assert "Mounting" not in opt["aspects"]
    assert "collapse" not in opt["description"].lower()


def test_targeted_nightstand_repair_uses_category_explicit_title():
    opt = {
        "title": 'Set of 2 with 2 Drawers, 15.4" Modern Storage Bedside Table with Handles',
        "description": "<li>Two bedside tables in the supplier-listed Natural Wood color.</li>",
        "aspects": {},
    }

    audit_fix_ready_drafts.apply_targeted_copy_repairs("W5368P460480", opt)

    assert "Nightstands" in opt["title"]
    assert len(opt["title"]) <= 80


def test_targeted_game_table_repair_avoids_conflicted_material_sentence():
    opt = {
        "description": (
            '<li style="margin-bottom:10px">Crafted from particle board, the gaming table '
            "features a sturdy structure and long lasting durability.</li>"
        ),
        "aspects": {},
    }

    audit_fix_ready_drafts.apply_targeted_copy_repairs("W3393S00009", opt)

    lowered = opt["description"].lower()
    assert "particle board" not in lowered
    assert "iron" not in lowered
    assert "removable top" in lowered


def test_targeted_ottoman_and_armchair_types_do_not_regress():
    ottoman = {"aspects": {"Type": ["Storage Ottoman"]}}
    armchair = {"aspects": {"Type": ["Outdoor Chair"], "Indoor/Outdoor": ["Outdoor"]}}

    audit_fix_ready_drafts.apply_targeted_copy_repairs("W5368P517797", ottoman)
    audit_fix_ready_drafts.apply_targeted_copy_repairs("W5568P506758", armchair)

    assert ottoman["aspects"]["Type"] == ["Ottoman"]
    assert armchair["aspects"]["Type"] == ["Armchair"]
    assert armchair["aspects"]["Indoor/Outdoor"] == ["Indoor"]


def test_targeted_trunk_repair_removes_unsupported_assembly_claim():
    opt = {
        "description": (
            '<table><tr data-assembly-note="true"><td>Assembly Required</td>'
            "<td>No</td></tr></table>"
        ),
        "aspects": {"Type": ["Trunk"], "Assembly Required": ["No"]},
    }

    audit_fix_ready_drafts.apply_targeted_copy_repairs("B2765P523551", opt)

    assert "Assembly Required" not in opt["aspects"]
    assert "assembly required" not in opt["description"].lower()
