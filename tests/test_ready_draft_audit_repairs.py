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
