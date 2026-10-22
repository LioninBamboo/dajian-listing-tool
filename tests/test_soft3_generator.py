from src.services.auto_technical_prompt import whole_sentence_snippet, build_package_lines_without_bom
from tests.test_soft3_content_gates import WRENCH_TITLE

SRC = ("<h3>Key Features</h3><ul><li>Comprehensive Set: This set includes 7 high-quality wrenches that exceed ANSI "
       "standards, featuring sizes: 33,34,36,38,41,46,50mm, with surface pressed marking for easy identification, "
       "providing a versatile range to meet various fastening needs.</li><li>Comes with a storage bag.</li></ul>")


def test_snippet_whole_sentence_or_omit():
    s = whole_sentence_snippet("Short first. " + "y" * 400 + ".")
    assert s == "Short first."
    assert whole_sentence_snippet("z" * 400 + ".") == ""


def test_package_lines_expand_multi_piece_no_bom():
    lines = build_package_lines_without_bom(
        title=WRENCH_TITLE, item_type="Combination Wrench Set, Mixed Set",
        aspects={"Number of Pieces": ["7"], "Included Components": ["Wrench Set, Storage Bag"]},
        source_description=SRC,
    )
    assert lines == ["7 x Combination Wrench", "1 x Storage Bag"]


def test_package_lines_set_without_count_refuses():
    assert build_package_lines_without_bom(title="Scissor Jack Set", item_type="Scissor Jack Set", aspects={}) is None
    assert build_package_lines_without_bom(title="Floor Jack", item_type="Floor Jack", aspects={}) == ["1 x Floor Jack"]
