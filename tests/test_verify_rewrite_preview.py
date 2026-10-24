"""Tests for scripts/verify_rewrite_preview.py.

The checker exists because a hand-rolled `grep -l "adds practical seating"`
over whole preview files raised a false alarm on 2026-07-27: previews contain
both the current live copy (### Before) and the proposal (### After), so the
grep always matched the very defect the rewrite removes. Section awareness and
the blocking/warning split are the two properties worth locking down.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "verify_rewrite_preview", ROOT / "scripts" / "verify_rewrite_preview.py"
)
vrp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vrp)


def _preview(after: str, before: str = "old copy", pushable: str = "True") -> str:
    return (
        f"# SKU — Semantic Rewrite Preview (dry-run)\n\n"
        f"- **pushable:** {pushable}\n"
        f"- **needs_human:** {'False' if pushable == 'True' else 'True'}\n\n"
        f"## Description (plain text, truncated)\n"
        f"### Before\n```\n{before}\n```\n\n"
        f"### After\n```\n{after}\n```\n"
    )


CLEAN_AFTER = (
    "AQUAVERVE PREMIUM HOME FURNISHINGS Sofa KEY FEATURES Sturdy steel frame "
    "supports 300 lbs. PERFECT FOR Ideal for living room and small apartments. "
    "SPECIFICATIONS Weight 40 lbs ✦ Ships from US Warehouse ✦"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestSectionAwareness:
    def test_defect_only_in_before_is_not_reported(self, tmp_path):
        """The regression that caused the false alarm: live copy still has the
        old fabricated line, the proposal does not. That is a clean rewrite."""
        path = _write(
            tmp_path,
            "a.md",
            _preview(
                after=CLEAN_AFTER,
                before="Ideal for outdoor where a compact garden statue adds practical seating and everyday comfort.",
            ),
        )
        blocking, warnings = vrp.check_one(path)
        assert blocking == []
        assert warnings == []

    def test_defect_in_after_is_reported(self, tmp_path):
        path = _write(
            tmp_path,
            "b.md",
            _preview(
                after=CLEAN_AFTER.replace(
                    "Ideal for living room and small apartments.",
                    "Ideal for outdoor where a compact circular saw adds practical seating.",
                )
            ),
        )
        blocking, _ = vrp.check_one(path)
        assert any("fabricated benefit" in d for d in blocking)


class TestSeveritySplit:
    def test_unsupported_claim_blocks(self, tmp_path):
        path = _write(
            tmp_path, "c.md", _preview(after=CLEAN_AFTER + " adds practical seating")
        )
        blocking, _ = vrp.check_one(path)
        assert blocking

    def test_duplicate_copy_only_warns(self, tmp_path):
        bullet = "Sturdy steel frame supports 300 lbs and resists wobbling in daily use."
        after = (
            f"AQUAVERVE Sofa KEY FEATURES {bullet} PERFECT FOR {bullet} "
            "SPECIFICATIONS ✦ Ships from US Warehouse ✦"
        )
        path = _write(tmp_path, "d.md", _preview(after=after))
        blocking, warnings = vrp.check_one(path)
        assert blocking == []
        assert any("duplicates" in w for w in warnings)

    def test_missing_template_marker_blocks(self, tmp_path):
        path = _write(
            tmp_path, "e.md", _preview(after=CLEAN_AFTER.replace("Ships from", "Somewhere"))
        )
        blocking, _ = vrp.check_one(path)
        assert any("template marker missing" in d for d in blocking)

    def test_chinese_in_after_blocks(self, tmp_path):
        path = _write(tmp_path, "f.md", _preview(after=CLEAN_AFTER + " 产品规格 组装长度"))
        blocking, _ = vrp.check_one(path)
        assert any("Chinese" in d for d in blocking)


class TestNonCandidates:
    def test_needs_human_preview_is_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            "g.md",
            _preview(after="anything adds practical seating", pushable="False"),
        )
        assert vrp.check_one(path) == ([], [])

    def test_skip_report_without_after_block_is_fine(self, tmp_path):
        path = _write(tmp_path, "h.md", "# SKU — SKIP\n\n- code: `unavailable`\n")
        blocking, warnings = vrp.check_one(path)
        assert blocking == []
        assert warnings == []


class TestExitCodes:
    def _run(self, tmp_path, monkeypatch, argv):
        monkeypatch.setattr("sys.argv", ["verify_rewrite_preview.py", *argv])
        return vrp.main()

    def test_exit_1_when_blocking(self, tmp_path, monkeypatch):
        _write(tmp_path, "a.md", _preview(after=CLEAN_AFTER + " adds practical seating"))
        assert self._run(tmp_path, monkeypatch, [str(tmp_path), "--quiet"]) == 1

    def test_exit_0_when_only_warnings(self, tmp_path, monkeypatch):
        bullet = "Sturdy steel frame supports 300 lbs and resists wobbling in daily use."
        after = (
            f"AQUAVERVE Sofa KEY FEATURES {bullet} PERFECT FOR {bullet} "
            "SPECIFICATIONS ✦ Ships from US Warehouse ✦"
        )
        _write(tmp_path, "a.md", _preview(after=after))
        assert self._run(tmp_path, monkeypatch, [str(tmp_path), "--quiet"]) == 0

    def test_exit_0_when_clean(self, tmp_path, monkeypatch):
        _write(tmp_path, "a.md", _preview(after=CLEAN_AFTER))
        assert self._run(tmp_path, monkeypatch, [str(tmp_path), "--quiet"]) == 0

    def test_exit_2_on_bad_directory(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, [str(tmp_path / "nope"), "--quiet"]) == 2
