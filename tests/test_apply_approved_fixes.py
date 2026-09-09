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


class TestExitCodeSemantics:
    """audit_fix_active_listings 的退出码是"发现的问题数",不是成败。
    2026-07-28 执行方发现:runner 把非零当命令失败,于是每一行都"失败"、
    第一条就停,整个清单形同虚设。--exit-zero-on-issues 把退出码还原成
    真正的成败信号,真失败(异常/鉴权/abort)仍返回非零。"""

    def test_command_always_neutralises_issue_count_exit_code(self):
        for apply in (False, True):
            cmd = aaf.build_command("W1", ["Assembly Required"], apply=apply)
            assert "--exit-zero-on-issues" in cmd, (
                "without this the runner aborts on any SKU that has findings"
            )

    def test_clean_freeze_is_always_ignored(self):
        # 修过的 listing 会被冻结成 clean,审计直接跳过,定向修复静默无事可做
        cmd = aaf.build_command("W1", ["categoryId"], apply=True)
        assert "--ignore-clean-freeze" in cmd

    def test_scoping_flags_survive_alongside_the_exit_code_flag(self):
        cmd = aaf.build_command("W1", ["categoryId"], apply=True)
        assert cmd.count("--fix-key") == 1
        assert "--fix" in cmd and "--sku" in cmd


class TestManifestKeysMustActuallyExist:
    """2026-07-28:清单写了 "Item Height"/"Item Width",审计里没有这两个 key
    (真名是 "Product Dimensions",而这几条压根没有尺寸修复项)。
    filter_fixes_by_key 过滤成空,runner 报 "3 rows, 0 failures",
    live 一个字节没动 —— 静默成功比失败更危险。"""

    def _report(self, tmp_path, sku, fix_keys):
        import json

        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "listing_audit_fix_20260728_999999.json").write_text(
            json.dumps({"issues": [{"sku": sku, "fix_keys": fix_keys}]}), encoding="utf-8"
        )
        return logs

    def test_key_absent_from_audit_is_reported(self, tmp_path, monkeypatch):
        self._report(tmp_path, "W1", ["Product Dimensions"])
        monkeypatch.setattr(aaf, "ROOT", tmp_path)
        assert aaf._keys_not_offered("W1", ["Item Height"]) == ["Item Height"]

    def test_key_present_in_audit_passes(self, tmp_path, monkeypatch):
        self._report(tmp_path, "W1", ["Product Dimensions", "Assembly Required"])
        monkeypatch.setattr(aaf, "ROOT", tmp_path)
        assert aaf._keys_not_offered("W1", ["Product Dimensions"]) == []

    def test_sku_with_no_offered_fixes_rejects_every_key(self, tmp_path, monkeypatch):
        self._report(tmp_path, "W1", [])
        monkeypatch.setattr(aaf, "ROOT", tmp_path)
        assert aaf._keys_not_offered("W1", ["Product Dimensions"]) == ["Product Dimensions"]

    def test_no_report_available_does_not_block(self, tmp_path, monkeypatch):
        (tmp_path / "logs").mkdir(exist_ok=True)
        monkeypatch.setattr(aaf, "ROOT", tmp_path)
        assert aaf._keys_not_offered("W1", ["Product Dimensions"]) == []


class TestApplyReportErrors:
    def test_reported_fix_error_is_exposed_to_manifest_runner(self, tmp_path, monkeypatch):
        import json

        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "listing_audit_fix_20260803_999999.json").write_text(
            json.dumps(
                {
                    "issues": [
                        {
                            "sku": "W1",
                            "fixes_applied": [
                                "Fixed Item Weight",
                                "ERROR [W1]: inventory update failed",
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(aaf, "ROOT", tmp_path)

        assert aaf._apply_errors_for_sku("W1") == [
            "ERROR [W1]: inventory update failed"
        ]
