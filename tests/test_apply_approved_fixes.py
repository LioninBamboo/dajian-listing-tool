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
            "category_id": "",
            "allow_unverified_fee": False,
        }
        assert rows[1]["fix_keys"] == ["categoryId"]
        assert rows[1]["category_id"] == ""

    def test_parses_optional_category_id_target(self, tmp_path):
        path = _manifest(
            tmp_path,
            "sku,fix_keys,category_id,note\nW1,categoryId,176991,remap then stamp\n",
        )
        rows = aaf.load_manifest(path)
        assert rows[0]["category_id"] == "176991"

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

    def test_category_approved_key_is_also_guarded(self):
        rows = [{"sku": "W1", "fix_keys": ["categoryApproved"], "note": ""}]
        with pytest.raises(SystemExit):
            aaf.check_guarded(rows, allow_category=False)
        aaf.check_guarded(rows, allow_category=True)

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

    def test_category_apply_does_not_delegate_stamp_to_audit(self):
        cmd = aaf.build_command("W1", ["categoryId"], apply=True)
        assert "--mark-category-approved" not in cmd

    def test_non_category_apply_does_not_stamp_approved_flag(self):
        cmd = aaf.build_command("W1", ["Assembly Required"], apply=True)
        assert "--mark-category-approved" not in cmd

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


class TestStampAlreadyCorrectCategory:
    def _db(self, tmp_path, sku, opt):
        import json
        import sqlite3

        db = tmp_path / "ebay_collection.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)"
        )
        conn.execute(
            "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
            (sku, json.dumps(opt)),
        )
        conn.commit()
        conn.close()
        return db

    def test_dry_run_does_not_write(self, tmp_path):
        import json
        import sqlite3

        db = self._db(tmp_path, "W808P391671", {"categoryId": "20748"})
        result = aaf.stamp_local_category_approved(
            "W808P391671", approved_category_id="20748", apply=False, db_path=db
        )
        assert result == {"ok": True, "dry_run": True, "category_id": "20748"}
        raw = sqlite3.connect(db).execute(
            "SELECT optimization FROM collected_products WHERE sku=?",
            ("W808P391671",),
        ).fetchone()[0]
        assert json.loads(raw) == {"categoryId": "20748"}

    def test_apply_pins_explicit_leaf(self, tmp_path):
        import json
        import sqlite3

        db = self._db(tmp_path, "W808P391671", {"categoryId": "20748"})
        result = aaf.stamp_local_category_approved(
            "W808P391671", approved_category_id="20748", apply=True, db_path=db
        )
        assert result["ok"] is True
        assert result["category_id"] == "20748"
        opt = json.loads(
            sqlite3.connect(db).execute(
                "SELECT optimization FROM collected_products WHERE sku=?",
                ("W808P391671",),
            ).fetchone()[0]
        )
        assert opt["categoryApproved"] is True
        assert opt["categoryApprovedId"] == "20748"

    def test_missing_category_is_rejected(self, tmp_path):
        db = self._db(tmp_path, "W1", {"title": "No category"})
        result = aaf.stamp_local_category_approved(
            "W1", approved_category_id="20748", apply=True, db_path=db
        )
        assert result["ok"] is False
        assert "no stored categoryId" in result["error"]

    def test_empty_target_is_rejected(self, tmp_path):
        db = self._db(tmp_path, "W1", {"categoryId": "20748"})
        result = aaf.stamp_local_category_approved(
            "W1", approved_category_id="", apply=True, db_path=db
        )
        assert result["ok"] is False
        assert "explicit category_id" in result["error"]

    def test_expected_id_mismatch_is_rejected(self, tmp_path):
        db = self._db(tmp_path, "W1", {"categoryId": "20487"})
        result = aaf.stamp_local_category_approved(
            "W1", approved_category_id="176991", apply=True, db_path=db
        )
        assert result["ok"] is False
        assert "stored categoryId=20487 != category_id=176991" in result["error"]


class TestRowClassification:
    def test_approve_only_is_not_a_remap(self):
        row = {"sku": "W1", "fix_keys": ["categoryApproved"], "category_id": "20748"}
        assert aaf.is_approve_only_row(row)
        assert not aaf.is_remap_row(row)

    def test_category_id_row_is_a_remap(self):
        for keys in (["categoryId"], ["categoryName"], ["categoryId", "categoryApproved"]):
            row = {"sku": "W1", "fix_keys": keys, "category_id": "176991"}
            assert aaf.is_remap_row(row)
            assert not aaf.is_approve_only_row(row)

    def test_stamp_requires_the_mark_flag(self):
        row = {"sku": "W1", "fix_keys": ["categoryId"], "category_id": "176991"}
        assert aaf.wants_approval_stamp(row, True)
        assert not aaf.wants_approval_stamp(row, False)


class TestCanStampAfterRemap:
    def test_empty_target_refuses(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="",
            returncode=0,
            apply_errors=[],
            remap_offered=True,
            stored_category_id="176991",
            apply=True,
        )
        assert ok is False
        assert "explicit category_id" in reason

    def test_nonzero_returncode_refuses(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="176991",
            returncode=1,
            apply_errors=[],
            remap_offered=True,
            stored_category_id="176991",
            apply=True,
        )
        assert ok is False
        assert "exited 1" in reason

    def test_apply_errors_refuse(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="176991",
            returncode=0,
            apply_errors=["ERROR [W1]: inventory update failed"],
            remap_offered=True,
            stored_category_id="176991",
            apply=True,
        )
        assert ok is False
        assert "apply errors" in reason

    def test_remap_not_offered_refuses(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="176991",
            returncode=0,
            apply_errors=[],
            remap_offered=False,
            stored_category_id="176991",
            apply=True,
        )
        assert ok is False
        assert "did not produce a categoryId fix" in reason

    def test_apply_mismatch_refuses(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="176991",
            returncode=0,
            apply_errors=[],
            remap_offered=True,
            stored_category_id="20487",
            apply=True,
        )
        assert ok is False
        assert "remap was not applied" in reason

    def test_dry_run_skips_stored_comparison(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="176991",
            returncode=0,
            apply_errors=[],
            remap_offered=True,
            stored_category_id="20487",
            apply=False,
        )
        assert ok is True
        assert reason == ""

    def test_successful_remap_allows_explicit_leaf(self):
        ok, reason = aaf.can_stamp_after_remap(
            requested_id="176991",
            returncode=0,
            apply_errors=[],
            remap_offered=True,
            stored_category_id="176991",
            apply=True,
        )
        assert ok is True
        assert reason == ""


class TestExplicitCategoryStamp:
    """Table of stamp-path cases. Approval is never inferred from storage."""

    def _isolate_reports(self, tmp_path, monkeypatch, offered=None):
        import json

        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        if offered is not None:
            (logs / "listing_audit_fix_20261021_999999.json").write_text(
                json.dumps({"issues": [{"sku": "W1", "fix_keys": offered}]}),
                encoding="utf-8",
            )
        monkeypatch.setattr(aaf, "ROOT", tmp_path)

        def _furniture_path(category_id, cache_dir=None, index=None):
            cid = str(category_id or "").strip()
            furniture = {
                "176991": "Home & Garden > Furniture > Cabinets & Cupboards > Bathroom Cabinets",
                "20748": "Home & Garden > Furniture > Cabinets & Cupboards",
                "20487": "Home & Garden > Furniture > Cabinets & Cupboards",
                "38200": "Home & Garden > Furniture > Tables > End Tables",
            }
            return furniture.get(cid, "")

        # Stub the tree index only — the high-fee gate itself still runs.
        monkeypatch.setattr(
            "src.utils.category_tree_cache.build_category_path",
            _furniture_path,
        )

    def _db(self, tmp_path, sku, opt):
        import json
        import sqlite3

        db = tmp_path / "ebay_collection.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)"
        )
        conn.execute(
            "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
            (sku, json.dumps(opt)),
        )
        conn.commit()
        conn.close()
        return db

    def _opt(self, db, sku):
        import json
        import sqlite3

        raw = sqlite3.connect(db).execute(
            "SELECT optimization FROM collected_products WHERE sku=?",
            (sku,),
        ).fetchone()[0]
        return json.loads(raw)

    def _run(self, rows, *, db, apply, run_audit=None):
        return aaf.process_rows(
            rows,
            apply=apply,
            mark_category_approved=True,
            db_path=db,
            run_audit=run_audit or (lambda sku, keys, apply: (_ for _ in ()).throw(
                AssertionError("audit should not run")
            )),
        )

    def test_remap_row_without_category_id_is_rejected(self, tmp_path, monkeypatch, capsys):
        self._isolate_reports(tmp_path, monkeypatch)
        db = self._db(tmp_path, "W1", {"categoryId": "20487"})
        calls = []

        def run_audit(sku, keys, apply):
            calls.append(sku)
            raise AssertionError("missing category_id must be rejected before audit")

        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryId"], "note": "", "category_id": ""}],
            db=db,
            apply=True,
            run_audit=run_audit,
        )
        assert rc == 1
        assert calls == []
        assert "categoryApproved" not in self._opt(db, "W1")
        assert "explicit category_id" in capsys.readouterr().out

    def test_remap_not_offered_does_not_stamp(self, tmp_path, monkeypatch):
        self._isolate_reports(tmp_path, monkeypatch, offered=[])
        from types import SimpleNamespace

        db = self._db(tmp_path, "W1", {"categoryId": "20487"})

        def run_audit(sku, keys, apply):
            return SimpleNamespace(returncode=0, stdout="no category fix", stderr="")

        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryId"], "note": "", "category_id": "176991"}],
            db=db,
            apply=True,
            run_audit=run_audit,
        )
        assert rc == 1
        assert self._opt(db, "W1") == {"categoryId": "20487"}

    def test_failed_audit_does_not_stamp(self, tmp_path, monkeypatch):
        self._isolate_reports(tmp_path, monkeypatch, offered=["categoryId"])
        from types import SimpleNamespace

        db = self._db(tmp_path, "W1", {"categoryId": "20487"})

        def run_audit(sku, keys, apply):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryId"], "note": "", "category_id": "176991"}],
            db=db,
            apply=True,
            run_audit=run_audit,
        )
        assert rc == 1
        assert self._opt(db, "W1") == {"categoryId": "20487"}

    def test_successful_remap_stamps_new_id(self, tmp_path, monkeypatch):
        self._isolate_reports(tmp_path, monkeypatch, offered=["categoryId"])
        import json
        import sqlite3
        from types import SimpleNamespace

        db = self._db(tmp_path, "W1", {"categoryId": "20487"})
        calls = []

        def run_audit(sku, keys, apply):
            calls.append((sku, list(keys), apply))
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE collected_products SET optimization=? WHERE sku=?",
                (json.dumps({"categoryId": "176991", "categoryName": "Bathroom Cabinets"}), sku),
            )
            conn.commit()
            conn.close()
            return SimpleNamespace(returncode=0, stdout="remapped", stderr="")

        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryId"], "note": "", "category_id": "176991"}],
            db=db,
            apply=True,
            run_audit=run_audit,
        )
        assert rc == 0
        assert calls == [("W1", ["categoryId"], True)]
        opt = self._opt(db, "W1")
        assert opt["categoryId"] == "176991"
        assert opt["categoryApproved"] is True
        assert opt["categoryApprovedId"] == "176991"

    def test_approve_only_matching_stamps(self, tmp_path, monkeypatch):
        self._isolate_reports(tmp_path, monkeypatch)
        db = self._db(tmp_path, "W1", {"categoryId": "20748"})
        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryApproved"], "note": "", "category_id": "20748"}],
            db=db,
            apply=True,
        )
        assert rc == 0
        opt = self._opt(db, "W1")
        assert opt["categoryApproved"] is True
        assert opt["categoryApprovedId"] == "20748"

    def test_approve_only_mismatching_fails(self, tmp_path, monkeypatch):
        self._isolate_reports(tmp_path, monkeypatch)
        db = self._db(tmp_path, "W1", {"categoryId": "20487"})
        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryApproved"], "note": "", "category_id": "176991"}],
            db=db,
            apply=True,
        )
        assert rc == 1
        assert self._opt(db, "W1") == {"categoryId": "20487"}

    def test_approve_only_without_category_id_is_rejected(self, tmp_path, monkeypatch, capsys):
        self._isolate_reports(tmp_path, monkeypatch)
        db = self._db(tmp_path, "W1", {"categoryId": "20748"})
        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryApproved"], "note": "", "category_id": ""}],
            db=db,
            apply=True,
        )
        assert rc == 1
        assert "categoryApproved" not in self._opt(db, "W1")
        assert "explicit category_id" in capsys.readouterr().out

    def test_dry_run_remap_with_target_continues(self, tmp_path, monkeypatch, capsys):
        self._isolate_reports(tmp_path, monkeypatch, offered=["categoryId"])
        from types import SimpleNamespace

        db = self._db(tmp_path, "W1", {"categoryId": "20487"})

        def run_audit(sku, keys, apply):
            assert apply is False
            return SimpleNamespace(returncode=0, stdout="would remap", stderr="")

        rc = self._run(
            [{"sku": "W1", "fix_keys": ["categoryId"], "note": "", "category_id": "176991"}],
            db=db,
            apply=False,
            run_audit=run_audit,
        )
        assert rc == 0
        assert self._opt(db, "W1") == {"categoryId": "20487"}
        assert "would stamp 176991 after remap" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "stored,requested,apply_flag",
        [
            ("20487", "", True),
            ("20487", "", False),
            ("20487", "176991", True),
            ("20748", "20748", True),
            ("20748", "20748", False),
            ("38200", "20748", True),
        ],
    )
    def test_stamp_only_writes_explicit_category_id(
        self, tmp_path, stored, requested, apply_flag
    ):
        db = self._db(tmp_path, "W1", {"categoryId": stored})
        result = aaf.stamp_local_category_approved(
            "W1",
            approved_category_id=requested,
            apply=apply_flag,
            db_path=db,
        )
        opt = self._opt(db, "W1")
        if result.get("applied"):
            assert requested, "a successful stamp must have an explicit category_id"
            assert result["category_id"] == requested
            assert opt["categoryApprovedId"] == requested
        else:
            assert "categoryApprovedId" not in opt
            assert "categoryApproved" not in opt
        if not requested:
            assert result["ok"] is False

    @pytest.mark.parametrize(
        "keys,stored,requested,expect_stamp",
        [
            (["categoryApproved"], "20748", "20748", True),
            (["categoryApproved"], "20487", "176991", False),
            (["categoryApproved"], "20748", "", False),
            (["categoryId"], "20487", "", False),
        ],
    )
    def test_process_rows_stamp_only_writes_explicit_id(
        self, tmp_path, monkeypatch, keys, stored, requested, expect_stamp
    ):
        self._isolate_reports(tmp_path, monkeypatch)
        db = self._db(tmp_path, "W1", {"categoryId": stored})
        rc = self._run(
            [{"sku": "W1", "fix_keys": keys, "note": "", "category_id": requested}],
            db=db,
            apply=True,
        )
        opt = self._opt(db, "W1")
        if expect_stamp:
            assert rc == 0
            assert opt["categoryApprovedId"] == requested
        else:
            assert "categoryApprovedId" not in opt
            assert rc == 1
