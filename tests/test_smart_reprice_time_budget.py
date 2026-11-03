"""Wall-clock budget and resume checkpoint for catalog smart reprice."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.utils.smart_reprice_budget import (
    CHECKPOINT_FILENAME,
    DEFAULT_TIME_BUDGET_MIN,
    TIME_BUDGET_ENV,
    checkpoint_path,
    clear_checkpoint,
    load_checkpoint,
    normalize_sku,
    normalize_sku_list,
    order_products_for_resume,
    resolve_time_budget_min,
    write_checkpoint,
    write_json_atomic,
)
from src.utils.task_result_status import classify_daily_task_outcome, exit_code_for_task_results


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float = 0, *_args, **_kwargs) -> None:
        self.now += float(seconds or 0)

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _product(sku, category_id="20349", current=180.0, cost=100.0):
    return {
        "sku": sku,
        "title": f"Outdoor Bench {sku}",
        "image_url": "",
        "category_id": category_id,
        "total_cost": cost,
        "listing_id": f"L-{sku}",
        "cost_breakdown": {"total_dajian_cost": cost, "selling_price": current},
        "suggested_price": current,
    }


def _patch_reprice(monkeypatch, tmp_path, reprice, products, clock, *, read_advance=0.0):
    client = MagicMock()
    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    market_client = MagicMock()
    market_client.market_mode = "browse_only"
    market_client.get_market_price_snapshot.return_value = {
        "avg_price": 200.0,
        "median_price": 200.0,
        "min_price": 180.0,
        "max_price": 220.0,
        "total_listings": 5,
        "sample_size": 5,
        "sold_count": 0,
        "source": "BROWSE",
    }

    started = []
    puts = []
    verifies = []

    def read_price(*_a, **_k):
        if read_advance:
            clock.advance(read_advance)
        sku = _a[1] if len(_a) > 1 else _k.get("sku")
        started.append(sku)
        return 180.0

    def update_price(*_a, **_k):
        sku = _a[1] if len(_a) > 1 else _k.get("sku")
        puts.append(sku)
        clock.advance(90)
        return True

    def verify_price(*_a, **_k):
        sku = _a[1] if len(_a) > 1 else _k.get("sku")
        verifies.append(sku)
        return True, 190.0

    db_connects = []

    def fake_connect(*_a, **_k):
        db_connects.append((_a, _k))
        conn = MagicMock()
        conn.__enter__.return_value = conn
        return conn

    monkeypatch.setattr(reprice, "get_published_products", lambda: list(products))
    monkeypatch.setattr(reprice, "get_ebay_client", lambda: (client, oauth))
    monkeypatch.setattr(reprice, "TerapeakClient", lambda: market_client)
    monkeypatch.setattr(reprice, "load_sales_cooldown", lambda: (set(), {"available": True}))
    monkeypatch.setattr(reprice, "read_current_listing_price", read_price)
    monkeypatch.setattr(reprice, "update_ebay_price", update_price)
    monkeypatch.setattr(reprice, "verify_ebay_price_update", verify_price)
    monkeypatch.setattr(reprice.time, "sleep", clock.sleep)
    monkeypatch.setattr(reprice.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(reprice, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(reprice.sqlite3, "connect", fake_connect)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    return {
        "started": started,
        "puts": puts,
        "verifies": verifies,
        "db_connects": db_connects,
        "clock": clock,
    }


# ── resolve_time_budget_min ──────────────────────────────────────────────


def test_budget_default_is_150_when_unset(monkeypatch):
    monkeypatch.delenv(TIME_BUDGET_ENV, raising=False)
    assert resolve_time_budget_min(None, environ={}) == float(DEFAULT_TIME_BUDGET_MIN)
    assert resolve_time_budget_min(environ={}) == 150


def test_budget_zero_or_negative_is_unlimited():
    """Documented decision: 0 / negative means no budget, not 'stop immediately'."""
    assert resolve_time_budget_min(0) is None
    assert resolve_time_budget_min(-5) is None
    assert resolve_time_budget_min("-1") is None
    assert resolve_time_budget_min(0.0, environ={TIME_BUDGET_ENV: "150"}) is None


def test_budget_explicit_overrides_env():
    assert resolve_time_budget_min(12, environ={TIME_BUDGET_ENV: "99"}) == 12


def test_budget_env_used_when_explicit_unset():
    assert resolve_time_budget_min(None, environ={TIME_BUDGET_ENV: "90"}) == 90


def test_budget_invalid_env_falls_back_to_default():
    assert resolve_time_budget_min(None, environ={TIME_BUDGET_ENV: "nope"}) == 150
    assert resolve_time_budget_min(None, environ={TIME_BUDGET_ENV: "NaN"}) == 150


# ── SKU identity ─────────────────────────────────────────────────────────


def test_normalize_sku_strip_and_int_and_none():
    assert normalize_sku(" W465P194603 ") == "W465P194603"
    assert normalize_sku(194603) == "194603"
    assert normalize_sku(None) is None
    assert normalize_sku("") is None
    assert normalize_sku("   ") is None
    assert normalize_sku(True) is None
    assert normalize_sku(12.5) is None


def test_sku_dedup_is_whole_value_after_strip_not_substring():
    assert normalize_sku("W465P194603") == normalize_sku(" W465P194603 ")
    assert normalize_sku("W465P19460") != normalize_sku("W465P194603")
    ordered = order_products_for_resume(
        [
            _product("W465P194603"),
            _product("W465P19460"),
            _product("OTHER"),
        ],
        [" W465P194603 ", "W465P19460"],
    )
    assert [p["sku"] for p in ordered] == ["W465P194603", "W465P19460", "OTHER"]
    # Prefix leftover does not steal the longer published SKU.
    prefixed = order_products_for_resume(
        [_product("W465P194603"), _product("KEEP")],
        ["W465P19460"],
    )
    assert [p["sku"] for p in prefixed] == ["W465P194603", "KEEP"]


def test_order_products_skips_missing_and_dedupes():
    products = [_product("A"), _product("B"), _product("C")]
    ordered = order_products_for_resume(products, ["C", "C", None, 12, "GONE", " A "])
    assert [p["sku"] for p in ordered] == ["C", "A", "B"]


def test_normalize_sku_list_skips_corrupt_entries():
    assert normalize_sku_list(None) == []
    assert normalize_sku_list("W465P194603") == []
    assert normalize_sku_list(["A", None, 7, "", " A ", True, {"sku": "X"}]) == ["A", "7"]


# ── checkpoint I/O ───────────────────────────────────────────────────────


def test_checkpoint_path_uses_pathlib_reports_dir(tmp_path):
    path = checkpoint_path(tmp_path)
    assert path == tmp_path / "reports" / CHECKPOINT_FILENAME
    assert isinstance(path, Path)


def test_atomic_write_tmp_then_replace(tmp_path, monkeypatch):
    dest = tmp_path / "reports" / "smart_reprice_checkpoint.json"
    seen = []
    real_replace = __import__("os").replace

    def spy_replace(src, dst):
        seen.append((Path(src).name, Path(dst).name))
        return real_replace(src, dst)

    monkeypatch.setattr("src.utils.smart_reprice_budget.os.replace", spy_replace)
    write_json_atomic(dest, {"run_date": "2026-11-02", "remaining_skus": ["A"]})
    assert dest.is_file()
    assert seen == [(dest.name + ".tmp", dest.name)]
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert json.loads(dest.read_text(encoding="utf-8"))["remaining_skus"] == ["A"]


def test_load_checkpoint_missing_empty_corrupt_stale(tmp_path, caplog):
    path = checkpoint_path(tmp_path)
    assert load_checkpoint(path) is None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_checkpoint(path) is None
        assert "empty" in caplog.text.lower()

    path.write_text("{not-json", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert load_checkpoint(path) is None
        assert "corrupt" in caplog.text.lower()

    path.write_text('["not", "an", "object"]', encoding="utf-8")
    assert load_checkpoint(path) is None

    write_checkpoint(path, ["A"], "2026-10-01")
    with caplog.at_level(logging.WARNING):
        ignored = load_checkpoint(path, now=datetime(2026, 10, 9, 12, 0, 0))
        assert ignored is None
        assert "older than 7 days" in caplog.text

    still_valid = load_checkpoint(path, now=datetime(2026, 10, 8, 12, 0, 0))
    assert still_valid is not None
    assert still_valid["remaining_skus"] == ["A"]


def test_load_checkpoint_accepts_int_and_none_skus(tmp_path):
    path = checkpoint_path(tmp_path)
    write_json_atomic(
        path,
        {
            "run_date": "2026-11-02",
            "remaining_skus": ["A", None, 99, " B "],
        },
    )
    loaded = load_checkpoint(path, now=datetime(2026, 11, 2, 8, 0, 0))
    assert loaded["remaining_skus"] == ["A", "99", "B"]


def test_clear_checkpoint_is_noop_when_missing(tmp_path):
    path = checkpoint_path(tmp_path)
    clear_checkpoint(path)
    write_checkpoint(path, ["A"], "2026-11-02")
    clear_checkpoint(path, dry_run=True)
    assert path.is_file()
    clear_checkpoint(path)
    assert not path.exists()


# ── run_batch_reprice budget / resume ────────────────────────────────────


def test_budget_hit_stops_before_next_sku_never_between_put_and_verify(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    products = [_product("SKU-A"), _product("SKU-B"), _product("SKU-C")]
    clock = FakeClock()
    spies = _patch_reprice(monkeypatch, tmp_path, reprice, products, clock)

    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, time_budget_min=1
    )

    assert report["status"] == "partial"
    assert report["stopped_reason"] == "time_budget"
    assert report["processed"] == 1
    assert report["remaining"] == 2
    assert report["remaining_skus"] == ["SKU-B", "SKU-C"]
    assert spies["started"] == ["SKU-A"]
    assert spies["puts"] == ["SKU-A"]
    assert spies["verifies"] == ["SKU-A"]
    ckpt = json.loads((tmp_path / "reports" / CHECKPOINT_FILENAME).read_text(encoding="utf-8"))
    assert ckpt["remaining_skus"] == ["SKU-B", "SKU-C"]
    assert ckpt["run_date"]


def test_phase1_time_counts_toward_budget(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    products = [_product("SKU-A"), _product("SKU-B")]
    clock = FakeClock()
    spies = _patch_reprice(monkeypatch, tmp_path, reprice, products, clock)

    original_fetch = reprice.fetch_market_price

    def slow_fetch(*a, **k):
        clock.advance(120)
        return original_fetch(*a, **k)

    monkeypatch.setattr(reprice, "fetch_market_price", slow_fetch)
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, time_budget_min=1
    )
    assert report["status"] == "partial"
    assert report["processed"] == 0
    assert report["remaining_skus"] == ["SKU-A", "SKU-B"]
    assert spies["started"] == []
    assert spies["puts"] == []


def test_resume_orders_checkpoint_first_dedups_and_clears_on_complete(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    products = [
        _product("W465P194603"),
        _product("W465P19460"),
        _product("OTHER"),
    ]
    write_checkpoint(
        tmp_path / "reports" / CHECKPOINT_FILENAME,
        [" W465P194603 ", "GONE", "W465P19460"],
        datetime.now().date(),
    )
    clock = FakeClock()
    spies = _patch_reprice(monkeypatch, tmp_path, reprice, products, clock)
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, time_budget_min=0
    )
    assert report["status"] == "ok"
    assert spies["started"] == ["W465P194603", "W465P19460", "OTHER"]
    assert not (tmp_path / "reports" / CHECKPOINT_FILENAME).exists()


def test_resume_partial_rewrites_remaining_checkpoint(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    products = [_product("A"), _product("B"), _product("C"), _product("D")]
    write_checkpoint(
        tmp_path / "reports" / CHECKPOINT_FILENAME,
        ["C", "D"],
        datetime.now().date(),
    )
    clock = FakeClock()
    spies = _patch_reprice(monkeypatch, tmp_path, reprice, products, clock)
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, time_budget_min=1
    )
    assert report["status"] == "partial"
    assert spies["started"] == ["C"]
    assert report["remaining_skus"] == ["D", "A", "B"]
    ckpt = json.loads((tmp_path / "reports" / CHECKPOINT_FILENAME).read_text(encoding="utf-8"))
    assert ckpt["remaining_skus"] == ["D", "A", "B"]


def test_stale_checkpoint_is_ignored_and_natural_order_used(monkeypatch, tmp_path, caplog):
    from scripts import batch_smart_reprice as reprice

    products = [_product("A"), _product("B")]
    write_checkpoint(
        tmp_path / "reports" / CHECKPOINT_FILENAME,
        ["B"],
        (datetime.now().date() - timedelta(days=8)).isoformat(),
    )
    clock = FakeClock()
    spies = _patch_reprice(monkeypatch, tmp_path, reprice, products, clock)
    with caplog.at_level(logging.WARNING):
        report = reprice.run_batch_reprice(
            dry_run=False, send_email=False, time_budget_min=0
        )
    assert report["status"] == "ok"
    assert spies["started"] == ["A", "B"]
    assert "older than 7 days" in caplog.text
    assert not (tmp_path / "reports" / CHECKPOINT_FILENAME).exists()


def test_corrupt_checkpoint_does_not_crash(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    ckpt = tmp_path / "reports" / CHECKPOINT_FILENAME
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    ckpt.write_text("{broken", encoding="utf-8")
    clock = FakeClock()
    spies = _patch_reprice(monkeypatch, tmp_path, reprice, [_product("A")], clock)
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, time_budget_min=0
    )
    assert report["status"] == "ok"
    assert spies["started"] == ["A"]


def test_default_budget_is_150_minutes_on_run(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    clock = FakeClock()
    captured = {}

    def fake_resolve(explicit, environ=None):
        captured["explicit"] = explicit
        return 150.0

    monkeypatch.delenv(TIME_BUDGET_ENV, raising=False)
    _patch_reprice(monkeypatch, tmp_path, reprice, [_product("A")], clock)
    monkeypatch.setattr(reprice, "resolve_time_budget_min", fake_resolve)
    reprice.run_batch_reprice(dry_run=True, send_email=False)
    assert captured["explicit"] is None
    # And the resolved default really is 150 when nothing is set.
    assert resolve_time_budget_min(None, environ={}) == 150


def test_dry_run_writes_no_checkpoint_and_no_db(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    products = [_product("SKU-A"), _product("SKU-B")]
    clock = FakeClock()
    spies = _patch_reprice(
        monkeypatch, tmp_path, reprice, products, clock, read_advance=90
    )
    report = reprice.run_batch_reprice(
        dry_run=True, send_email=False, time_budget_min=1
    )
    assert report["status"] == "partial"
    assert report["remaining_skus"] == ["SKU-B"]
    assert not (tmp_path / "reports" / CHECKPOINT_FILENAME).exists()
    assert spies["db_connects"] == []
    assert spies["puts"] == []
    assert spies["verifies"] == []
    leftover = tmp_path / "reports" / CHECKPOINT_FILENAME
    leftover.write_text("keep-me", encoding="utf-8")
    clock2 = FakeClock()
    _patch_reprice(monkeypatch, tmp_path, reprice, [_product("SKU-A")], clock2)
    complete = reprice.run_batch_reprice(
        dry_run=True, send_email=False, time_budget_min=0
    )
    assert complete["status"] == "ok"
    assert leftover.read_text(encoding="utf-8") == "keep-me"


def test_scoped_only_skus_run_does_not_touch_checkpoint(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    ckpt = tmp_path / "reports" / CHECKPOINT_FILENAME
    write_checkpoint(ckpt, ["OTHER"], datetime.now().date())
    before = ckpt.read_text(encoding="utf-8")
    clock = FakeClock()
    _patch_reprice(
        monkeypatch, tmp_path, reprice, [_product("SKU-A"), _product("OTHER")], clock
    )
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, only_skus={"SKU-A"}, time_budget_min=0
    )
    assert report["status"] == "ok"
    assert ckpt.read_text(encoding="utf-8") == before


def test_from_cro_queue_does_not_write_or_clear_checkpoint(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    ckpt = tmp_path / "reports" / CHECKPOINT_FILENAME
    write_checkpoint(ckpt, ["KEEP-ME"], datetime.now().date())
    before = ckpt.read_text(encoding="utf-8")
    clock = FakeClock()
    _patch_reprice(monkeypatch, tmp_path, reprice, [_product("SKU-A")], clock)
    monkeypatch.setattr(
        "src.services.cro_action_queue.load_pending",
        lambda action_type="price_drop": [{"sku": "SKU-A", "priority": 1}],
    )
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, from_cro_queue=True, time_budget_min=0
    )
    assert report["status"] == "ok"
    assert ckpt.read_text(encoding="utf-8") == before


def test_checkpoint_remaining_skus_string_is_not_a_sku_list(tmp_path):
    path = checkpoint_path(tmp_path)
    write_json_atomic(
        path,
        {"run_date": "2026-11-02", "remaining_skus": "W465P194603,W465P19460"},
    )
    loaded = load_checkpoint(path, now=datetime(2026, 11, 2))
    assert loaded is not None
    assert loaded["remaining_skus"] == []


def test_int_sku_in_checkpoint_matches_string_product(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    write_json_atomic(
        tmp_path / "reports" / CHECKPOINT_FILENAME,
        {"run_date": datetime.now().date().isoformat(), "remaining_skus": [194603, "B"]},
    )
    clock = FakeClock()
    spies = _patch_reprice(
        monkeypatch,
        tmp_path,
        reprice,
        [_product("B"), _product("194603")],
        clock,
    )
    report = reprice.run_batch_reprice(
        dry_run=False, send_email=False, time_budget_min=0
    )
    assert report["status"] == "ok"
    assert spies["started"] == ["194603", "B"]


# ── daily_tasks / health ────────────────────────────────────────────────


def test_run_smart_reprice_logs_partial_and_returns_report(monkeypatch, caplog):
    import daily_tasks
    import scripts.batch_smart_reprice as bsr

    partial = {
        "status": "partial",
        "processed": 4,
        "remaining": 9,
        "stopped_reason": "time_budget",
        "summary": {"total": 13, "price_changes": 2, "errors": 0},
        "changed_rows": [],
        "path": "reports/x.json",
    }
    monkeypatch.setitem(__import__("sys").modules, "batch_smart_reprice", bsr)
    monkeypatch.setattr(bsr, "run_batch_reprice", lambda **_k: partial)
    with caplog.at_level(logging.WARNING):
        report = daily_tasks.run_smart_reprice()
    assert report["status"] == "partial"
    assert "SMART_REPRICE partial: processed=4 remaining=9" in caplog.text


def test_daily_tasks_continues_after_partial_reprice(monkeypatch):
    import daily_tasks

    calls = []

    monkeypatch.setattr(daily_tasks, "assert_runtime_not_in_maintenance", lambda *_a, **_k: None)
    monkeypatch.setattr(
        daily_tasks,
        "validate_runtime_database",
        lambda *_a, **_k: type("R", (), {"integrity_check": "ok", "link_count": 0, "page_count": 0})(),
    )
    monkeypatch.setattr(daily_tasks, "run_report_retention_cleanup", lambda: None)
    monkeypatch.setattr(
        daily_tasks, "analyze_collected_products", lambda: calls.append("analyze") or {"success": 1}
    )
    monkeypatch.setattr(
        daily_tasks, "sync_inventory", lambda: calls.append("inventory") or {"checked": 1, "errors": 0}
    )
    monkeypatch.setattr(
        daily_tasks,
        "run_ghost_oos_recovery",
        lambda auto_fix=True: calls.append("ghost") or {"status": "ok"},
    )
    monkeypatch.setattr(daily_tasks, "should_run_smart_reprice", lambda: True)
    monkeypatch.setattr(
        daily_tasks,
        "run_smart_reprice",
        lambda: calls.append("reprice")
        or {
            "status": "partial",
            "processed": 2,
            "remaining": 5,
            "stopped_reason": "time_budget",
            "summary": {"errors": 0},
            "changed_rows": [],
        },
    )
    monkeypatch.setattr(
        daily_tasks,
        "run_sales_health_check",
        lambda **_k: calls.append("health") or {"status": "ok"},
    )
    monkeypatch.setattr(
        daily_tasks,
        "run_listing_audit",
        lambda **_k: calls.append("audit") or {"status": "ok"},
    )
    monkeypatch.setattr(
        daily_tasks, "run_cro_diagnose", lambda **_k: calls.append("cro") or {"status": "ok"}
    )
    monkeypatch.setattr(
        daily_tasks, "send_daily_summary_email", lambda results: calls.append(("email", results)) or True
    )
    monkeypatch.setattr(daily_tasks.sys, "argv", ["daily_tasks.py"])
    monkeypatch.setattr(
        "src.services.finance_orders.sync_orders_from_ebay",
        lambda days=30: calls.append("finance")
        or {"orders_pulled": 0, "upserted": 0, "errors": 0, "giga_links_updated": 0},
    )
    monkeypatch.setattr(
        "src.services.finance_orders.query_finance_summary",
        lambda _conn: {"gmv": 0, "net": 0, "not_pushed": 0},
    )
    monkeypatch.setattr(
        "sqlite3.connect",
        lambda *_a, **_k: type(
            "C",
            (),
            {"close": lambda self: None, "__enter__": lambda self: self, "__exit__": lambda *a: None},
        )(),
    )

    results = daily_tasks.main()
    assert calls[:6] == ["analyze", "inventory", "ghost", "reprice", "health", "audit"]
    assert "cro" in calls
    assert results["smart_reprice"]["status"] == "partial"
    assert results["health_check"]["status"] == "ok"
    assert results["listing_audit"]["status"] == "ok"
    assert classify_daily_task_outcome(results) == "partial_success"
    assert exit_code_for_task_results(results) == 2


def test_partial_reprice_is_not_silent_success_or_failure():
    results = {
        "inventory": {"checked": 10, "errors": 0},
        "smart_reprice": {
            "status": "partial",
            "processed": 918,
            "remaining": 484,
            "stopped_reason": "time_budget",
            "summary": {"errors": 0, "price_changes": 100},
        },
    }
    assert classify_daily_task_outcome(results) == "partial_success"
    assert exit_code_for_task_results(results) == 2


def test_partial_reprice_with_item_errors_stays_partial():
    results = {
        "smart_reprice": {
            "status": "partial",
            "summary": {"errors": 2},
            "results": [{"sku": "X", "status": "error", "error": "timeout"}],
        }
    }
    assert classify_daily_task_outcome(results) == "partial_success"
