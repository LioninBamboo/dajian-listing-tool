"""Tests for scripts/apply_approved_fixes.py.

Written after two consecutive handoffs where prose constraints failed. The
first executor reinterpreted a hard stop as "end this batch and continue"; the
second ran `--fix` without `--fix-key` for an assembly-flag batch and thereby
re-categorised two indoor storage benches into Outdoor Daybeds on live. The
allowlist has to be mechanical, so these tests pin the mechanism: the command
is built from the manifest, and category keys cannot slip through.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "apply_approved_fixes", ROOT / "scripts" / "apply_approved_fixes.py"
)
aaf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aaf)


def _manifest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "manifest.csv"
    path.write_text(text, encoding="utf-8")
    return path


class TestManifestParsing:
    def test_parses_rows_and_splits_fix_keys(self, tmp_path):
        path = _manifest(
            tmp_path,
            "sku,fix_keys,note\nW1,Assembly Required|Item Length,two keys\nW2,categoryId,\n",
        )
        rows = aaf.load_manifest(path)
        assert rows[0] == {
            "sku": "W1",
            "fix_keys": ["Assembly Required", "Item Length"],
            "note": "two keys",
        }
        assert rows[1]["fix_keys"] == ["categoryId"]

    def test_row_without_fix_keys_is_rejected(self, tmp_path):
        path = _manifest(tmp_path, "sku,fix_keys,note\nW1,,oops\n")
        with pytest.raises(SystemExit):
            aaf.load_manifest(path)

    def test_missing_column_is_rejected(self, tmp_path):
        path = _manifest(tmp_path, "sku,note\nW1,no fix_keys column\n")
        with pytest.raises(SystemExit):
            aaf.load_manifest(path)

    def test_empty_manifest_is_rejected(self, tmp_path):
        path = _manifest(tmp_path, "sku,fix_keys,note\n")
        with pytest.raises(SystemExit):
            aaf.load_manifest(path)

    def test_comment_and_blank_rows_are_skipped(self, tmp_path):
        path = _manifest(
            tmp_path, "sku,fix_keys,note\n#W0,categoryId,skip me\n,,\nW1,Assembly Required,\n"
        )
        rows = aaf.load_manifest(path)
        assert [r["sku"] for r in rows] == ["W1"]


class TestCategoryGuard:
    def test_category_key_blocks_without_optin(self):
        rows = [{"sku": "W1", "fix_keys": ["categoryId"], "note": ""}]
        with pytest.raises(SystemExit) as excinfo:
            aaf.check_guarded(rows, allow_category=False)
        assert excinfo.value.code == 2

    def test_category_name_is_also_guarded(self):
        rows = [{"sku": "W1", "fix_keys": ["categoryName"], "note": ""}]
        with pytest.raises(SystemExit):
            aaf.check_guarded(rows, allow_category=False)

    def test_category_key_passes_with_optin(self):
        rows = [{"sku": "W1", "fix_keys": ["categoryId"], "note": "approved"}]
        aaf.check_guarded(rows, allow_category=True)  # must not raise

    def test_non_guarded_keys_need_no_optin(self):
        rows = [{"sku": "W1", "fix_keys": ["Assembly Required", "Item Length"], "note": ""}]
        aaf.check_guarded(rows, allow_category=False)  # must not raise

    def test_guard_reports_every_offending_row(self, capsys):
        rows = [
            {"sku": "W1", "fix_keys": ["categoryId"], "note": ""},
            {"sku": "W2", "fix_keys": ["Assembly Required"], "note": ""},
            {"sku": "W3", "fix_keys": ["categoryName"], "note": ""},
        ]
        with pytest.raises(SystemExit):
            aaf.check_guarded(rows, allow_category=False)
        out = capsys.readouterr().out
        assert "W1" in out and "W3" in out
        assert "W2" not in out


class TestCommandConstruction:
    def test_every_key_becomes_its_own_fix_key_flag(self):
        cmd = aaf.build_command("W1", ["Assembly Required", "Item Length"], apply=False)
        assert cmd.count("--fix-key") == 2
        assert "Assembly Required" in cmd and "Item Length" in cmd
        assert "--sku" in cmd and "W1" in cmd

    def test_dry_run_omits_the_write_flag(self):
        assert "--fix" not in aaf.build_command("W1", ["Assembly Required"], apply=False)

    def test_apply_adds_the_write_flag(self):
        assert "--fix" in aaf.build_command("W1", ["Assembly Required"], apply=True)

    def test_scope_is_always_a_single_sku(self):
        cmd = aaf.build_command("W1", ["Assembly Required"], apply=True)
        assert cmd.count("--sku") == 1

    def test_unscoped_fix_is_never_constructible(self):
        """The failure mode being prevented: `--fix` with no `--fix-key`."""
        cmd = aaf.build_command("W1", ["Assembly Required"], apply=True)
        assert "--fix-key" in cmd, "an apply command must always carry a key scope"
