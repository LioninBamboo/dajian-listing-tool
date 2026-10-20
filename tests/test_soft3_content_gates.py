"""Soft3 regressions: W465P451141 Table Set / generic 1 x box / cut Source Spec."""
from src.utils import listing_quality_gate as g
from src.utils.publish_aspect_completion import infer_set_includes

WRENCH_TITLE = "7-Piece Metric Jumbo Combination Wrench Set - 33-50mm Black Oxide Carbon Steel"
OLD_DESC = (
    "<div>AQUARIDES</div><p><strong>Source Spec:</strong> Key Features Comprehensive Set: This set includes 7 "
    "high-quality wrenches that exceed ANSI standards, featuring sizes: 33,34,36,38,41,46,50mm, with surface "
    "pressed marking for easy identification, providing a versatile r</p>"
    "<div>WHAT'S IN THE BOX</div><p>1 x Wrench Set, Mixed Set</p><h3>Specifications</h3>"
)
ASPECTS = {"Type": ["Wrench Set"], "Number of Pieces": ["7"], "Set Includes": ["Table Set"]}


def test_infer_set_includes_never_table_set_for_tools():
    assert infer_set_includes(WRENCH_TITLE) is None
    assert infer_set_includes("4-Piece Heavy Duty Straight Pipe Wrench Set") is None
    assert infer_set_includes("5 Piece Dining Table Set with 4 Chairs") == "Dining Table & Chairs"
    assert infer_set_includes("3-Piece Bistro Patio Table Set") == "Table Set"


def test_infer_set_includes_table_set_only_for_table_domain():
    assert infer_set_includes("7-Piece Wrench Set") is None
    assert infer_set_includes("3-Piece Ottoman Set") is None
    assert infer_set_includes("Set of 2 Chairs") is None
    assert infer_set_includes("5-Piece Dining Set") == "Table Set"
    assert infer_set_includes("3-Piece Tablet Stand Set") is None
    assert infer_set_includes("3-Piece Stable Storage Set") is None
    assert infer_set_includes("Marble Masterpiece Dining Table") == "Table"
    assert infer_set_includes("W465P451141 7-Piece Wrench Set") is None
    assert infer_set_includes("Round Dining Table Set with 4 Chairs") == "Dining Table & Chairs"


def test_w465p451141_regression_all_three_blocked():
    codes = {i.code for i in g.find_soft3_content_issues(WRENCH_TITLE, OLD_DESC, ASPECTS)}
    assert codes == {"wrong_domain_set_includes", "package_includes_generic", "source_spec_truncated"}


def test_furniture_set_includes_allowed_on_furniture():
    assert g.find_wrong_domain_set_includes("5 Piece Dining Table Set", {"Set Includes": ["Table Set"]}) is None


def test_fixed_listing_passes():
    desc = (
        "<p><strong>Source Spec:</strong> This set includes 7 high-quality wrenches that exceed ANSI standards.</p>"
        "<div>WHAT'S IN THE BOX</div><p>7 x Combination Wrench<br/>1 x Storage Bag</p><h3>Specifications</h3>"
    )
    aspects = {"Type": ["Wrench Set"], "Number of Pieces": ["7"], "Set Includes": ["7 Metric Combination Wrenches, Storage Bag"]}
    assert g.find_soft3_content_issues(WRENCH_TITLE, desc, aspects) == []


def test_single_item_1x_type_ok():
    desc = "<p><strong>Source Spec:</strong> Heavy steel build.</p><div>WHAT'S IN THE BOX</div><p>1 x Floor Jack</p>"
    assert g.find_generic_package_box("3 Ton Low Profile Floor Jack", desc, {"Type": ["Floor Jack"]}) is None


def test_truncation_independent_of_bom():
    assert g.is_truncated_snippet("x" * 50 + " providing a versatile r")
    assert g.is_truncated_snippet("word " * 44)  # ~220 chars, no period
    assert not g.is_truncated_snippet("Overall Height: 22.5 in")
    assert not g.is_truncated_snippet("Built tough for daily use.")


def test_giga_verbatim_kit_box_allowed():
    giga = "1/2 Inch OD x 60 Feet PU Air Tube Kit,Garage Air Compressor Accessories Kit"
    desc = "<p><strong>Source Spec:</strong> Flexible PU hose.</p><div>WHAT'S IN THE BOX</div><p>1 x 1/2 Inch OD x 60 Feet PU Air Tube Kit</p>"
    title = "60 ft PU Air Hose Kit with Quick Connect Couplers"
    assert g.find_generic_package_box(title, desc, {"Type": ["Air Hose Kit"]}) is not None
    assert g.find_generic_package_box(title, desc, {"Type": ["Air Hose Kit"]}, giga) is None
    mixed = desc.replace("Tube Kit</p>", "Tube Kit, Mixed Set</p>")
    assert g.find_generic_package_box(title, mixed, {}, giga) is not None
