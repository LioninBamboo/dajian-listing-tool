"""
Tests for the Market Intelligence module's unified scoring and caching.

Focus: regression-safe unit tests, no live eBay calls.
"""
from __future__ import annotations

import os
import sys
import time
import json
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.plugins.terapeak_research.scoring import (
    classify_competition,
    opportunity_score,
    ScoringInputs,
    recommendation_text,
    compute_demand_signal,
)
from src.plugins.terapeak_research._cache import TTLCache


# ---------------------------------------------------------------------------
# classify_competition
# ---------------------------------------------------------------------------

class TestClassifyCompetition:
    def test_uses_active_total_when_present(self):
        assert classify_competition(active_total=50)[0] == "low"
        assert classify_competition(active_total=500)[0] == "medium"
        assert classify_competition(active_total=2000)[0] == "high"
        assert classify_competition(active_total=20000)[0] == "very_high"

    def test_falls_back_to_sample_size_when_total_missing(self):
        # Old behavior path: only sample length known.
        # 50 * 20 = 1000 → high band start.
        level, _ = classify_competition(active_total=None, sample_size=50)
        assert level in ("medium", "high")

    def test_unknown_when_no_data(self):
        level, score = classify_competition(active_total=None, sample_size=0)
        assert level == "unknown"
        assert score == 0

    def test_zero_total_not_treated_as_real_signal(self):
        # active_total=0 should NOT short-circuit to "low"
        level, _ = classify_competition(active_total=0, sample_size=100)
        # Falls back to sample_size path → 100*20=2000 → high
        assert level in ("high", "very_high")


# ---------------------------------------------------------------------------
# opportunity_score
# ---------------------------------------------------------------------------

class TestOpportunityScore:
    def test_high_margin_low_competition_real_str_scores_high(self):
        s = opportunity_score(ScoringInputs(
            margin_rate=0.40,
            competition_level="low",
            price_spread=0.6,
            real_str=25.0,
        ))
        assert s >= 90

    def test_negative_margin_clamped_low(self):
        s = opportunity_score(ScoringInputs(
            margin_rate=-0.10,
            competition_level="very_high",
            price_spread=0.0,
        ))
        # No margin credit, no STR, no spread credit; only comp_credit=0.
        assert s == 0

    def test_real_str_outranks_estimated_str(self):
        real = opportunity_score(ScoringInputs(
            margin_rate=0.20, competition_level="medium",
            real_str=25.0,
        ))
        estimated = opportunity_score(ScoringInputs(
            margin_rate=0.20, competition_level="medium",
            estimated_str=25.0,
        ))
        assert real > estimated, "real STR should always score higher than estimated"

    def test_score_bounded_0_100(self):
        s = opportunity_score(ScoringInputs(
            margin_rate=10.0,        # absurd
            competition_level="low",
            price_spread=10.0,
            real_str=999.0,
        ))
        assert 0 <= s <= 100

    def test_unknown_competition_neutral(self):
        s_unknown = opportunity_score(ScoringInputs(margin_rate=0.20, competition_level="unknown"))
        s_low = opportunity_score(ScoringInputs(margin_rate=0.20, competition_level="low"))
        s_very_high = opportunity_score(ScoringInputs(margin_rate=0.20, competition_level="very_high"))
        assert s_low > s_unknown > s_very_high


# ---------------------------------------------------------------------------
# recommendation_text
# ---------------------------------------------------------------------------

class TestRecommendationText:
    def test_strong_recommendation_uses_fire(self):
        assert "强烈推荐" in recommendation_text(85, 0.30, "low")

    def test_not_recommended_for_low_score(self):
        assert "暂不推荐" in recommendation_text(20, 0.05, "very_high")


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------

class TestTTLCache:
    def test_set_and_get_within_ttl(self):
        c = TTLCache(maxsize=4, ttl=10)
        c.set("k", {"v": 1})
        assert c.get("k") == {"v": 1}

    def test_expires_after_ttl(self):
        c = TTLCache(maxsize=4, ttl=1)
        c.set("k", "v")
        time.sleep(1.1)
        assert c.get("k") is None

    def test_ttl_zero_disables_cache(self):
        c = TTLCache(maxsize=4, ttl=0)
        c.set("k", "v")
        assert c.get("k") is None

    def test_eviction_when_full(self):
        c = TTLCache(maxsize=2, ttl=60)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        # Either a or b evicted; c must be present.
        assert c.get("c") == 3
        assert sum(1 for k in ("a", "b") if c.get(k) is not None) == 1

    def test_make_key_stable_for_dict_param(self):
        c = TTLCache()
        k1 = c._make_key("GET", "/path", {"a": 1, "b": 2})
        k2 = c._make_key("GET", "/path", {"b": 2, "a": 1})
        assert k1 == k2


# ---------------------------------------------------------------------------
# F1 — MI blacklist loader
# ---------------------------------------------------------------------------

class TestMIBlacklist:
    def test_load_blacklist_missing_file(self, tmp_path, monkeypatch):
        from src.plugins.terapeak_research import intelligence_service as svc_mod
        monkeypatch.setattr(svc_mod, "PROJECT_ROOT", tmp_path)
        svc = svc_mod.IntelligenceService(db_path=str(tmp_path / "x.db"))
        assert svc._load_mi_blacklist() == set()

    def test_load_blacklist_reads_json(self, tmp_path, monkeypatch):
        import json as _json
        from src.plugins.terapeak_research import intelligence_service as svc_mod
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "mi_blacklist.json").write_text(
            _json.dumps(["SKU-1", "SKU-2", ""]), encoding="utf-8"
        )
        monkeypatch.setattr(svc_mod, "PROJECT_ROOT", tmp_path)
        svc = svc_mod.IntelligenceService(db_path=str(tmp_path / "x.db"))
        assert svc._load_mi_blacklist() == {"SKU-1", "SKU-2"}

    def test_load_blacklist_corrupt_returns_empty(self, tmp_path, monkeypatch):
        from src.plugins.terapeak_research import intelligence_service as svc_mod
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "mi_blacklist.json").write_text("not json", encoding="utf-8")
        monkeypatch.setattr(svc_mod, "PROJECT_ROOT", tmp_path)
        svc = svc_mod.IntelligenceService(db_path=str(tmp_path / "x.db"))
        assert svc._load_mi_blacklist() == set()

    def test_dict_shape_loads_and_strips_expired(self, tmp_path, monkeypatch):
        """F1.1 — dict 形态 + 已过期项必须被 IntelligenceService 自动剥离。"""
        import json as _json
        from datetime import datetime as _dt, timedelta as _td
        from src.plugins.terapeak_research import intelligence_service as svc_mod
        (tmp_path / "reports").mkdir()
        future = (_dt.now() + _td(days=5)).isoformat()
        past = (_dt.now() - _td(days=1)).isoformat()
        payload = {
            "ALIVE": {"added_at": _dt.now().isoformat(), "expires_at": future, "reason": None},
            "EXPIRED": {"added_at": _dt.now().isoformat(), "expires_at": past, "reason": None},
            "PERMANENT": {"added_at": _dt.now().isoformat(), "expires_at": None, "reason": "test"},
        }
        (tmp_path / "reports" / "mi_blacklist.json").write_text(
            _json.dumps(payload), encoding="utf-8"
        )
        monkeypatch.setattr(svc_mod, "PROJECT_ROOT", tmp_path)
        svc = svc_mod.IntelligenceService(db_path=str(tmp_path / "x.db"))
        active = svc._load_mi_blacklist()
        assert active == {"ALIVE", "PERMANENT"}

    def test_ui_helpers_round_trip_dict_with_ttl(self, tmp_path, monkeypatch):
        """F1.1 — Streamlit 页面的 _add_blacklist_entry / _load_blacklist_entries 闭环。"""
        from src.web.pages import market_intelligence as mi_page
        monkeypatch.setattr(mi_page, "_BLACKLIST_PATH", tmp_path / "mi_blacklist.json")
        mi_page._add_blacklist_entry("SKU-X", ttl_days=7, reason="trial")
        mi_page._add_blacklist_entry("SKU-Y", ttl_days=None)
        entries = mi_page._load_blacklist_entries()
        assert set(entries.keys()) == {"SKU-X", "SKU-Y"}
        assert entries["SKU-X"]["expires_at"] is not None
        assert entries["SKU-X"]["reason"] == "trial"
        assert entries["SKU-Y"]["expires_at"] is None
        # _load_blacklist 仅返回 active 集合
        assert mi_page._load_blacklist() == {"SKU-X", "SKU-Y"}


# ---------------------------------------------------------------------------
# Recommendation scope: unlisted only
# ---------------------------------------------------------------------------

class TestRecommendationScope:
    def test_intelligence_service_local_inventory_excludes_published(self, tmp_path):
        from src.plugins.terapeak_research.intelligence_service import IntelligenceService

        db_path = tmp_path / "mi_scope.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE collected_products (
                sku TEXT,
                title TEXT,
                url TEXT,
                cost_breakdown TEXT,
                status TEXT,
                images TEXT,
                listing_id TEXT
            )
            """
        )
        rows = [
            ("PUBLISHED-1", "Live Sofa", "", json.dumps({"total_dajian_cost": 100}), "PUBLISHED", json.dumps([]), "L1"),
            ("READY-1", "Ready Sofa", "", json.dumps({"total_dajian_cost": 100}), "READY", json.dumps([]), ""),
            ("PENDING-1", "Pending Sofa", "", json.dumps({"total_dajian_cost": 100}), "PENDING", json.dumps([]), ""),
            ("COLLECTED-1", "Collected Sofa", "", json.dumps({"total_dajian_cost": 100}), "COLLECTED", json.dumps([]), ""),
        ]
        conn.executemany(
            "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        svc = IntelligenceService(db_path=str(db_path))
        inventory = svc._get_local_inventory()
        skus = {row["sku"] for row in inventory}

        assert skus == {"READY-1", "PENDING-1", "COLLECTED-1"}

    def test_daily_terapeak_report_inventory_excludes_published(self, tmp_path):
        import daily_terapeak_report as rep_mod

        db_path = tmp_path / "daily_report_scope.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE collected_products (
                sku TEXT,
                title TEXT,
                url TEXT,
                cost_breakdown TEXT,
                status TEXT,
                suggested_price REAL
            )
            """
        )
        rows = [
            ("PUBLISHED-1", "Live Table", "", json.dumps({"total_dajian_cost": 80}), "PUBLISHED", 150),
            ("READY-1", "Ready Table", "", json.dumps({"total_dajian_cost": 80}), "READY", 150),
            ("PENDING-1", "Pending Table", "", json.dumps({"total_dajian_cost": 80}), "PENDING", 150),
            ("COLLECTED-1", "Collected Table", "", json.dumps({"total_dajian_cost": 80}), "COLLECTED", 150),
        ]
        conn.executemany(
            "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        report = rep_mod.DailyTerapeakReport.__new__(rep_mod.DailyTerapeakReport)
        report.db_path = str(db_path)
        inventory = report._get_local_inventory()
        skus = {row["sku"] for row in inventory}

        assert skus == {"READY-1", "PENDING-1", "COLLECTED-1"}


# ---------------------------------------------------------------------------
# F2-pivot — DemandSignal (Browse-only proxy, MI denied)
# ---------------------------------------------------------------------------

class TestDemandSignal:
    def test_unknown_when_no_data(self):
        d = compute_demand_signal(active_total=None, sample_size=0, price_spread=0)
        assert d.score == 0
        assert d.level == "unknown"

    def test_strong_market(self):
        d = compute_demand_signal(
            active_total=8000, sample_size=100, price_spread=0.45, estimated_str=20.0
        )
        # 35 + 20 + 15 + min(30, 24) = 94
        assert d.score >= 75
        assert d.level == "very_strong"
        assert "估算" in d.label

    def test_weak_market(self):
        d = compute_demand_signal(
            active_total=50, sample_size=5, price_spread=0.05, estimated_str=None
        )
        # 6 + 3 + 4 + 0 = 13
        assert d.score < 35
        assert d.level == "weak"

    def test_components_breakdown_present(self):
        d = compute_demand_signal(
            active_total=500, sample_size=20, price_spread=0.25, estimated_str=15.0
        )
        assert set(d.components.keys()) == {"market_size", "density", "spread", "heuristic"}
        assert sum(d.components.values()) == d.score

    def test_label_always_marked_estimated(self):
        # Any non-unknown signal must clearly read as 估算 to keep users from
        # mistaking it for real STR (MI access denied by eBay).
        for at in (10, 100, 1000, 10000):
            d = compute_demand_signal(active_total=at, sample_size=10, price_spread=0.2)
            if d.level != "unknown":
                assert "估算" in d.label


# ---------------------------------------------------------------------------
# F3 — audit_fix_ready_drafts SKU-filter signature
# ---------------------------------------------------------------------------

class TestAuditSkuFilter:
    """Regression test: F3 endpoint depends on audit_and_fix_ready_drafts
    accepting a `sku_filter` kwarg and returning a report dict with
    `changed` + `unresolved` keys.
    """

    def test_signature_accepts_sku_filter(self):
        import inspect
        from scripts.audit_fix_ready_drafts import audit_and_fix_ready_drafts
        sig = inspect.signature(audit_and_fix_ready_drafts)
        assert "sku_filter" in sig.parameters
        assert sig.parameters["sku_filter"].default is None


# ---------------------------------------------------------------------------
# F6 — Competition-monitor → MI performance bridge
# ---------------------------------------------------------------------------

class TestSellerPerformanceBridge:
    """Validate the bridge that feeds 竞争监控 cached perf data into MI scoring."""

    def test_str_below_impression_floor_returns_none(self):
        from src.plugins.terapeak_research.performance_bridge import (
            compute_seller_str, MIN_IMPRESSIONS_FOR_REAL_STR,
        )
        # 49 impressions is below the 50-impression noise floor → noisy → suppress
        assert compute_seller_str(MIN_IMPRESSIONS_FOR_REAL_STR - 1, 5, 0) is None

    def test_str_uses_transactions_only_window_aligned(self):
        """F6.1: impressions are 30d, sold_qty is 90d. Using max() would
        inflate STR. We must use transactions only (also 30d)."""
        from src.plugins.terapeak_research.performance_bridge import compute_seller_str
        # 100 impressions (30d), 1 transaction (30d), 3 sold (90d)
        # → STR must be 1/100 = 1.0%, NOT 3/100
        assert compute_seller_str(100, 1, 3) == 1.0
        # sold_qty arg must be ignored entirely
        assert compute_seller_str(100, 1, 999) == 1.0

    def test_str_zero_when_no_sales(self):
        from src.plugins.terapeak_research.performance_bridge import compute_seller_str
        assert compute_seller_str(500, 0, 0) == 0.0

    def test_index_skips_listings_without_sku_mapping(self, tmp_path, monkeypatch):
        """Traffic-only entries (no by_listing[lid].sku) must NOT pollute the index."""
        from src.plugins.terapeak_research import performance_bridge

        fake_cache = {
            "fetched_at": "2026-05-01T12:00:00",
            "traffic": {
                "v1|1001|0": {"impressions": 200, "views": 10, "transactions": 2},
                "v1|2002|0": {"impressions": 80, "views": 4, "transactions": 0},
            },
            "sales": {
                "by_listing": {
                    "v1|1001|0": {"sku": "SKU_A", "qty": 4},
                    # v1|2002|0 missing on purpose: simulates orphaned traffic row
                },
                "by_sku": {
                    "SKU_A": {"qty": 4, "revenue": 320.0},
                    "SKU_C": {"qty": 1, "revenue": 50.0},  # sales-only, no traffic
                },
            },
        }
        monkeypatch.setattr(
            "src.services.ebay_performance.load_performance_cache",
            lambda max_age_hours=4: fake_cache,
        )

        idx = performance_bridge.load_seller_performance_index(max_age_hours=24)
        assert "SKU_A" in idx
        # Orphaned traffic row (no sku in by_listing) must be dropped
        assert "v1|2002|0" not in idx
        # Sales-only SKU surfaced with str_pct=None and has_sales=True
        assert idx["SKU_C"]["str_pct"] is None
        assert idx["SKU_C"]["has_sales"] is True
        # SKU_A: F6.1 uses transactions only (window-aligned) → STR = 2/200 = 1.0%
        # sold=4 (90d) is shown via sold_qty but NOT used for STR math.
        assert idx["SKU_A"]["str_pct"] == 1.0
        assert idx["SKU_A"]["sold_qty"] == 4
        assert idx["SKU_A"]["has_sales"] is True

    def test_real_str_lifts_opportunity_score_above_estimated(self):
        """The whole point of F6: real_str must outweigh estimated_str."""
        common = dict(margin_rate=0.20, competition_level="medium", price_spread=0.2)
        # Estimated STR = 15 → 15*0.6 = 9 pts (capped at 18)
        est_score = opportunity_score(ScoringInputs(estimated_str=15.0, **common))
        # Real STR = 15 → 15*1.2 = 18 pts (capped at 30)
        real_score = opportunity_score(ScoringInputs(real_str=15.0, **common))
        assert real_score > est_score, (
            f"Real STR ({real_score}) should outweigh estimated ({est_score})"
        )


# ---------------------------------------------------------------------------
# F8 — MI batch audit + publish endpoint contract
# ---------------------------------------------------------------------------

class TestF8BatchEndpoint:
    """The Streamlit Tab1 button posts {skus, max_count} to
    /api/mi/batch-audit-and-publish. Lock the request schema and the
    presence of the endpoint so a refactor can't silently break it.
    """

    def test_request_model_fields(self):
        from server import MIBatchAuditPublishRequest
        m = MIBatchAuditPublishRequest(skus=["A", "B"])
        assert m.skus == ["A", "B"]
        assert m.max_count == 50  # default cap

    def test_endpoint_registered_on_app(self):
        from server import app
        routes = {r.path for r in app.routes}
        assert "/api/mi/batch-audit-and-publish" in routes
        # Single-SKU sibling must remain too (F3 regression guard)
        assert "/api/mi/audit-and-publish/{sku}" in routes


class TestMIHistory:
    """F7 — MI 历史趋势加载与对比的数据准确性"""

    def _write_snapshot(self, root, ts_str, opps):
        import json as _json
        snap_dir = root / "reports"
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / f"mi_opportunities_{ts_str}.json"
        path.write_text(_json.dumps({"opportunities": opps}), encoding="utf-8")
        return path

    def test_load_skips_old_snapshots(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.history import load_mi_snapshots
        # 30 天前的快照应被丢弃
        self._write_snapshot(tmp_path, "20260101_100000", [{"sku": "A", "opportunity_score": 70}])
        self._write_snapshot(tmp_path, "20260420_100000", [{"sku": "A", "opportunity_score": 80}])
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 5, 2, 10, 0, 0))
        assert len(snaps) == 1
        assert "A" in snaps[0]["by_sku"]

    def test_load_dedups_within_same_day(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.history import load_mi_snapshots
        self._write_snapshot(tmp_path, "20260501_080000", [{"sku": "A", "opportunity_score": 50}])
        self._write_snapshot(tmp_path, "20260501_220000", [{"sku": "A", "opportunity_score": 65}])
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 5, 2, 10, 0, 0))
        assert len(snaps) == 1
        # 必须保留较新的那份
        assert snaps[0]["by_sku"]["A"]["opportunity_score"] == 65

    def test_trend_returns_none_when_no_history(self, tmp_path):
        from src.plugins.terapeak_research.history import compute_score_trend
        result = compute_score_trend([], "X", current_score=72, current_has_real_str=False)
        assert result["prior_score"] is None
        assert result["delta"] is None
        assert result["sample_count"] == 0
        assert result["mixed_str_basis"] is False

    def test_trend_flags_estimated_vs_real_mix(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.history import (
            load_mi_snapshots,
            compute_score_trend,
        )
        # 历史只有估算 STR；当前有实测 STR → 应触发 mixed_str_basis
        self._write_snapshot(tmp_path, "20260428_100000",
                             [{"sku": "A", "opportunity_score": 60, "seller_str_pct": None}])
        self._write_snapshot(tmp_path, "20260430_100000",
                             [{"sku": "A", "opportunity_score": 64, "seller_str_pct": None}])
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 5, 2, 10, 0, 0))
        result = compute_score_trend(snaps, "A", current_score=80, current_has_real_str=True)
        assert result["sample_count"] == 2
        assert result["mixed_str_basis"] is True
        # 仍然返回 delta，但 UI 必须根据 mixed_str_basis 给出告警
        assert result["delta"] == 80 - 64

    def test_trend_delta_when_clean_history(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.history import (
            load_mi_snapshots,
            compute_score_trend,
        )
        self._write_snapshot(tmp_path, "20260428_100000",
                             [{"sku": "A", "opportunity_score": 60, "seller_str_pct": 1.2}])
        self._write_snapshot(tmp_path, "20260501_100000",
                             [{"sku": "A", "opportunity_score": 70, "seller_str_pct": 1.4}])
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 5, 2, 10, 0, 0))
        result = compute_score_trend(snaps, "A", current_score=78, current_has_real_str=True)
        assert result["sample_count"] == 2
        assert result["prior_score"] == 70
        assert result["delta"] == 8
        assert result["mixed_str_basis"] is False
        # sparkline 是 历史 + 当前
        assert result["sparkline"] == [60, 70, 78]


class TestF9SnapshotRetentionAndSparkline:
    """F9 — 历史快照保留策略 + sparkline 渲染"""

    def _write(self, tmp_path, ts_str, content="{}"):
        snap_dir = tmp_path / "reports"
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / f"mi_opportunities_{ts_str}.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_cleanup_only_deletes_old_snapshots(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.history import cleanup_old_snapshots
        old = self._write(tmp_path, "20260101_100000")
        recent = self._write(tmp_path, "20260420_100000")
        # 不应误删非匹配命名的文件
        unrelated = tmp_path / "reports" / "other_report.json"
        unrelated.write_text("{}", encoding="utf-8")
        deleted = cleanup_old_snapshots(
            keep_days=30, project_root=tmp_path,
            now=datetime(2026, 5, 2, 10, 0, 0),
        )
        assert deleted == 1
        assert not old.exists()
        assert recent.exists()
        assert unrelated.exists()

    def test_cleanup_handles_missing_reports_dir(self, tmp_path):
        from src.plugins.terapeak_research.history import cleanup_old_snapshots
        # 无 reports/ 时不应抛错
        assert cleanup_old_snapshots(keep_days=30, project_root=tmp_path) == 0

    def test_sparkline_renders_with_gaps(self):
        from src.plugins.terapeak_research.history import render_sparkline
        out = render_sparkline([10, None, 50, 100])
        assert len(out) == 4
        assert out[0] == "▁"   # 最低
        assert out[1] == " "   # 缺测
        assert out[3] == "█"   # 最高

    def test_sparkline_flat_values(self):
        from src.plugins.terapeak_research.history import render_sparkline
        out = render_sparkline([55, 55, 55])
        # 全相同时不应误暗示波动
        assert out == "▄▄▄" or len(set(out)) == 1

    def test_sparkline_empty_input(self):
        from src.plugins.terapeak_research.history import render_sparkline
        assert render_sparkline([]) == ""
        assert render_sparkline([None, None]) == "  "


class TestF10AutoBlacklist:
    """F10 — 审计阻塞 SKU 自动 30d 屏蔽"""

    def test_writes_dict_shape_with_ttl(self, tmp_path):
        from datetime import datetime, timedelta
        from src.plugins.terapeak_research.blacklist import (
            auto_blacklist_audit_failures, load_entries,
        )
        now = datetime(2026, 5, 1, 12, 0, 0)
        n = auto_blacklist_audit_failures(
            ["SKU-X", "SKU-Y"], reason="audit_blocked",
            ttl_days=30, project_root=tmp_path, now=now,
        )
        assert n == 2
        entries = load_entries(tmp_path)
        assert set(entries) == {"SKU-X", "SKU-Y"}
        assert entries["SKU-X"]["reason"] == "audit_blocked"
        # TTL 必须是 30 天后
        from datetime import datetime as _dt
        exp = _dt.fromisoformat(entries["SKU-X"]["expires_at"])
        assert (exp - now) == timedelta(days=30)

    def test_does_not_overwrite_active_entries(self, tmp_path):
        """已生效的条目不应被覆盖（避免重复审计无限延期 TTL）。"""
        from datetime import datetime
        from src.plugins.terapeak_research.blacklist import (
            auto_blacklist_audit_failures, save_entries, load_entries,
        )
        now = datetime(2026, 5, 1, 12, 0, 0)
        # 用户手动加的条目（reason=manual）
        save_entries({
            "SKU-Z": {
                "added_at": "2026-04-15T10:00:00",
                "expires_at": "2026-05-20T10:00:00",
                "reason": "manual",
            }
        }, project_root=tmp_path)
        n = auto_blacklist_audit_failures(
            ["SKU-Z"], reason="audit_blocked",
            ttl_days=30, project_root=tmp_path, now=now,
        )
        assert n == 0  # 未触动
        assert load_entries(tmp_path)["SKU-Z"]["reason"] == "manual"

    def test_permanent_entries_untouched(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.blacklist import (
            auto_blacklist_audit_failures, save_entries, load_entries,
        )
        save_entries({
            "SKU-P": {"added_at": "2026-01-01T00:00:00",
                      "expires_at": None, "reason": "permanent_ban"}
        }, project_root=tmp_path)
        n = auto_blacklist_audit_failures(
            ["SKU-P"], project_root=tmp_path,
            now=datetime(2026, 5, 1),
        )
        assert n == 0
        assert load_entries(tmp_path)["SKU-P"]["expires_at"] is None

    def test_expired_entries_get_reactivated(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.blacklist import (
            auto_blacklist_audit_failures, save_entries, load_entries,
        )
        save_entries({
            "SKU-E": {"added_at": "2026-01-01T00:00:00",
                      "expires_at": "2026-02-01T00:00:00", "reason": "audit_blocked"}
        }, project_root=tmp_path)
        n = auto_blacklist_audit_failures(
            ["SKU-E"], project_root=tmp_path,
            now=datetime(2026, 5, 1),
        )
        assert n == 1
        # 重新激活后 expires_at 必须更新到未来
        new_exp = load_entries(tmp_path)["SKU-E"]["expires_at"]
        from datetime import datetime as _dt
        assert _dt.fromisoformat(new_exp) > datetime(2026, 5, 1)

    def test_audit_blocked_response_includes_auto_blacklisted_flag(self):
        """单 SKU 端点必须保留 auto_blacklisted 字段（API 锁定）。"""
        from server import app
        # 仅做静态字段存在性检查，避免真实跑 audit
        # 通过源码扫描：auto_blacklisted 应出现在 audit-and-publish 路由代码里
        import inspect, server
        src = inspect.getsource(server)
        assert '"auto_blacklisted": True' in src or 'auto_blacklisted' in src


class TestF11KPIBanner:
    """F11 — Tab1 顶部 KPI 横幅"""

    def test_summarize_with_no_history(self):
        from src.plugins.terapeak_research.history import summarize_recent_history
        opps = [
            {"sku": "A", "seller_str_pct": 1.5},
            {"sku": "B", "seller_str_pct": None},
        ]
        kpi = summarize_recent_history([], opps)
        assert kpi["snapshots_analyzed"] == 0
        assert kpi["avg_recent_count"] == 0.0
        assert kpi["current_count"] == 2
        assert kpi["real_str_coverage_pct"] == 50.0

    def test_summarize_with_history(self):
        from datetime import datetime
        from src.plugins.terapeak_research.history import summarize_recent_history
        snaps = [
            {"generated_at": datetime(2026, 4, 28),
             "by_sku": {"A": {}, "B": {}, "C": {}}},
            {"generated_at": datetime(2026, 4, 29),
             "by_sku": {"A": {}}},
        ]
        opps = [{"sku": x, "seller_str_pct": 1.0} for x in ["A", "B", "C", "D"]]
        kpi = summarize_recent_history(snaps, opps)
        # avg = (3 + 1) / 2 = 2.0
        assert kpi["avg_recent_count"] == 2.0
        assert kpi["current_count"] == 4
        assert kpi["delta_vs_avg"] == 2  # 4 - round(2.0)
        assert kpi["real_str_coverage_pct"] == 100.0
        assert kpi["snapshots_analyzed"] == 2

    def test_summarize_handles_empty_current(self):
        from src.plugins.terapeak_research.history import summarize_recent_history
        kpi = summarize_recent_history([], [])
        assert kpi["current_count"] == 0
        assert kpi["real_str_coverage_pct"] == 0.0


class TestF12FieldTrend:
    """F12 — 通用字段趋势（用于 demand_signal_score）"""

    def _write(self, tmp_path, ts, opps):
        import json as _json
        snap_dir = tmp_path / "reports"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / f"mi_opportunities_{ts}.json").write_text(
            _json.dumps({"opportunities": opps}), encoding="utf-8"
        )

    def test_demand_signal_trend(self, tmp_path):
        from datetime import datetime
        from src.plugins.terapeak_research.history import (
            load_mi_snapshots, compute_field_trend,
        )
        self._write(tmp_path, "20260428_100000",
                    [{"sku": "A", "demand_signal_score": 40, "opportunity_score": 60}])
        self._write(tmp_path, "20260501_100000",
                    [{"sku": "A", "demand_signal_score": 55, "opportunity_score": 65}])
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 5, 2, 10, 0, 0))
        trend = compute_field_trend(snaps, "A", current_value=70,
                                    field_name="demand_signal_score")
        assert trend["prior_score"] == 55
        assert trend["delta"] == 15
        # 字段独立性：不应被 STR 口径检查污染
        assert trend["mixed_str_basis"] is False
        assert trend["sparkline"] == [40, 55, 70]

    def test_field_trend_handles_missing_field(self, tmp_path):
        """快照中缺该字段时不应崩溃，sparkline 用 None 占位。"""
        from datetime import datetime
        from src.plugins.terapeak_research.history import (
            load_mi_snapshots, compute_field_trend,
        )
        self._write(tmp_path, "20260428_100000",
                    [{"sku": "A", "opportunity_score": 60}])  # 无 demand_signal_score
        self._write(tmp_path, "20260501_100000",
                    [{"sku": "A", "demand_signal_score": 55, "opportunity_score": 65}])
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 5, 2, 10, 0, 0))
        trend = compute_field_trend(snaps, "A", current_value=70,
                                    field_name="demand_signal_score")
        # 第一份快照该字段为 None；第二份为 55；当前 70
        assert trend["sparkline"] == [None, 55, 70]
        assert trend["sample_count"] == 2  # SKU 出现两次（即使一个字段缺失）
        assert trend["prior_score"] == 55


class TestF13AsyncBatchJob:
    """F13 — 异步批量审计+发布的 API 字段锁定与 job 注册表"""

    def test_async_endpoint_registered(self):
        from server import app
        routes = {r.path for r in app.routes}
        assert "/api/mi/batch-audit-and-publish-async" in routes
        assert "/api/mi/batch-job/{job_id}" in routes

    def test_job_status_404_for_unknown_id(self):
        from fastapi.testclient import TestClient
        from server import app
        client = TestClient(app)
        resp = client.get("/api/mi/batch-job/nonexistent")
        assert resp.status_code == 404

    def test_job_registry_lru_eviction(self):
        """超过上限时旧 job 应被驱逐，避免内存泄漏。"""
        import server
        # 临时缩小上限 + 清空注册表，避免影响其它测试
        old_max = server._MI_BATCH_JOBS_MAX
        old_jobs = dict(server._MI_BATCH_JOBS)
        server._MI_BATCH_JOBS.clear()
        server._MI_BATCH_JOBS_MAX = 8
        try:
            for i in range(20):
                server._mi_batch_jobs_set(f"job_{i}", {"job_id": f"job_{i}"})
            assert len(server._MI_BATCH_JOBS) <= server._MI_BATCH_JOBS_MAX + 1
            # 最新 job 必在
            assert server._mi_batch_jobs_get("job_19") is not None
        finally:
            server._MI_BATCH_JOBS_MAX = old_max
            server._MI_BATCH_JOBS.clear()
            server._MI_BATCH_JOBS.update(old_jobs)


class TestF14BlacklistReasonAggregation:
    """F14 — 屏蔽名单按 reason 聚合（UI 端逻辑，不需要 server）"""

    def test_reason_counts_aggregate_correctly(self, tmp_path, monkeypatch):
        from src.web.pages import market_intelligence as mi_page
        monkeypatch.setattr(mi_page, "_BLACKLIST_PATH", tmp_path / "mi_blacklist.json")
        mi_page._add_blacklist_entry("A", ttl_days=30, reason="audit_blocked")
        mi_page._add_blacklist_entry("B", ttl_days=30, reason="audit_blocked")
        mi_page._add_blacklist_entry("C", ttl_days=None, reason="manual")
        entries = mi_page._load_blacklist_entries()
        # 复用与 sidebar 同样的聚合逻辑
        counts: dict = {}
        for _meta in entries.values():
            r = _meta.get("reason") or "(未标注)"
            counts[r] = counts.get(r, 0) + 1
        assert counts == {"audit_blocked": 2, "manual": 1}

    def test_unlabeled_entries_grouped(self, tmp_path, monkeypatch):
        from src.web.pages import market_intelligence as mi_page
        monkeypatch.setattr(mi_page, "_BLACKLIST_PATH", tmp_path / "mi_blacklist.json")
        # 用底层 writer 写入无 reason 的项（模拟旧数据）
        mi_page._save_blacklist_entries({
            "X": {"added_at": None, "expires_at": None, "reason": None},
            "Y": {"added_at": None, "expires_at": None, "reason": None},
        })
        entries = mi_page._load_blacklist_entries()
        counts: dict = {}
        for _meta in entries.values():
            r = _meta.get("reason") or "(未标注)"
            counts[r] = counts.get(r, 0) + 1
        assert counts == {"(未标注)": 2}


class TestF15KPITimeseries:
    """F15 — 14d KPI 折线图数据"""

    def test_timeseries_has_one_point_per_snapshot(self):
        from datetime import datetime
        from src.plugins.terapeak_research.history import build_kpi_timeseries
        snaps = [
            {"generated_at": datetime(2026, 4, 28),
             "by_sku": {"A": {"opportunity_score": 60, "seller_str_pct": 1.5},
                        "B": {"opportunity_score": 80, "seller_str_pct": None}}},
            {"generated_at": datetime(2026, 4, 29),
             "by_sku": {"A": {"opportunity_score": 70, "seller_str_pct": 2.0}}},
        ]
        ts = build_kpi_timeseries(snaps)
        assert len(ts) == 2
        assert ts[0]["date"] == "2026-04-28"
        assert ts[0]["opportunity_count"] == 2
        assert ts[0]["avg_score"] == 70.0  # (60+80)/2
        assert ts[0]["real_str_coverage_pct"] == 50.0
        assert ts[1]["opportunity_count"] == 1
        assert ts[1]["real_str_coverage_pct"] == 100.0

    def test_empty_snapshots_returns_empty_series(self):
        from src.plugins.terapeak_research.history import build_kpi_timeseries
        assert build_kpi_timeseries([]) == []

    def test_handles_snapshot_with_no_opps(self):
        from datetime import datetime
        from src.plugins.terapeak_research.history import build_kpi_timeseries
        snaps = [{"generated_at": datetime(2026, 5, 1), "by_sku": {}}]
        ts = build_kpi_timeseries(snaps)
        assert ts[0]["opportunity_count"] == 0
        assert ts[0]["avg_score"] == 0.0
        assert ts[0]["real_str_coverage_pct"] == 0.0


class TestF16JobPersistence:
    """F16 — 异步 job 注册表落盘 + 启动恢复"""

    def test_jobs_persist_to_disk(self, tmp_path, monkeypatch):
        import server
        # 重定向持久化路径到 tmp_path
        monkeypatch.setattr(server, "_MI_JOBS_PATH", tmp_path / "mi_jobs.json")
        # 备份并清空
        old_jobs = dict(server._MI_BATCH_JOBS)
        server._MI_BATCH_JOBS.clear()
        try:
            server._mi_batch_jobs_set("job_persist_1", {
                "job_id": "job_persist_1", "status": "queued", "counters": {}
            })
            assert (tmp_path / "mi_jobs.json").exists()
            data = json.loads((tmp_path / "mi_jobs.json").read_text(encoding="utf-8"))
            assert "job_persist_1" in data
            assert data["job_persist_1"]["status"] == "queued"
        finally:
            server._MI_BATCH_JOBS.clear()
            server._MI_BATCH_JOBS.update(old_jobs)

    def test_running_jobs_marked_interrupted_on_load(self, tmp_path, monkeypatch):
        import server
        monkeypatch.setattr(server, "_MI_JOBS_PATH", tmp_path / "mi_jobs.json")
        # 模拟前一进程留下的 running job
        (tmp_path / "mi_jobs.json").write_text(json.dumps({
            "job_X": {"job_id": "job_X", "status": "running", "counters": {}},
            "job_Y": {"job_id": "job_Y", "status": "done", "counters": {}},
        }), encoding="utf-8")
        old_jobs = dict(server._MI_BATCH_JOBS)
        server._MI_BATCH_JOBS.clear()
        try:
            server._mi_jobs_load()
            assert server._MI_BATCH_JOBS["job_X"]["status"] == "interrupted"
            assert "FastAPI restarted" in server._MI_BATCH_JOBS["job_X"]["error"]
            # 已完成的不应被改
            assert server._MI_BATCH_JOBS["job_Y"]["status"] == "done"
        finally:
            server._MI_BATCH_JOBS.clear()
            server._MI_BATCH_JOBS.update(old_jobs)


class TestF18KPIAlerts:
    """F18 — KPI 异常告警规则"""

    def test_opportunity_drop_triggers_alert(self):
        from daily_tasks import _check_mi_alerts
        kpi = {"current_count": 3, "avg_recent_count": 10.0,
               "delta_vs_avg": -7.0, "real_str_coverage_pct": 80.0}
        alerts = _check_mi_alerts(kpi, opportunities=[{}] * 3)
        types = {a["type"] for a in alerts}
        assert "opportunity_drop" in types

    def test_low_str_coverage_triggers_when_enough_opps(self):
        from daily_tasks import _check_mi_alerts
        kpi = {"current_count": 8, "avg_recent_count": 8.0,
               "delta_vs_avg": 0.0, "real_str_coverage_pct": 30.0}
        alerts = _check_mi_alerts(kpi, opportunities=[{}] * 8)
        types = {a["type"] for a in alerts}
        assert "low_str_coverage" in types

    def test_no_alerts_when_healthy(self):
        from daily_tasks import _check_mi_alerts
        kpi = {"current_count": 10, "avg_recent_count": 10.0,
               "delta_vs_avg": 0.0, "real_str_coverage_pct": 80.0}
        alerts = _check_mi_alerts(kpi, opportunities=[{}] * 10)
        assert alerts == []

    def test_audit_blocked_dominant_alert(self, tmp_path, monkeypatch):
        import daily_tasks
        # 重定向 PROJECT_ROOT 让 _check_mi_alerts 读 tmp 路径
        monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "mi_blacklist.json").write_text(json.dumps({
            "S1": {"reason": "audit_blocked"},
            "S2": {"reason": "audit_blocked"},
            "S3": {"reason": "audit_blocked"},
            "S4": {"reason": "audit_blocked"},
            "S5": {"reason": "manual"},
        }), encoding="utf-8")
        kpi = {"current_count": 10, "avg_recent_count": 10.0,
               "delta_vs_avg": 0.0, "real_str_coverage_pct": 80.0}
        alerts = daily_tasks._check_mi_alerts(kpi, opportunities=[{}] * 10)
        types = {a["type"] for a in alerts}
        assert "audit_blocked_dominant" in types


class TestF19CategoryAggregation:
    """F19 — 按品类聚合机会列表"""

    def test_derive_product_category(self):
        from src.plugins.terapeak_research.aggregation import derive_product_category
        assert derive_product_category("L-shape Sectional Sofa 84\"") == "sectional sofa"
        assert derive_product_category("Modern Loveseat Couch") == "sofa"
        assert derive_product_category("Queen Platform Bed Frame") == "bed"
        assert derive_product_category("Coffee Table Walnut") == "table"
        assert derive_product_category("Recliner Armchair") == "chair"
        assert derive_product_category("Outdoor Patio Umbrella") == "outdoor"
        assert derive_product_category("Mystery Box") == "other"
        assert derive_product_category("") == "other"
        assert derive_product_category(None) == "other"

    def test_aggregate_by_category_basic(self):
        from src.plugins.terapeak_research.aggregation import aggregate_by_category
        opps = [
            {"sku": "S1", "title": "Modern Sofa", "opportunity_score": 80,
             "suggested_price": 400, "margin_rate": 30.0, "potential_profit": 100,
             "seller_str_pct": 2.5},
            {"sku": "S2", "title": "Sectional Sofa Large", "opportunity_score": 60,
             "suggested_price": 600, "margin_rate": 25.0, "potential_profit": 150,
             "seller_str_pct": None},
            {"sku": "S3", "title": "Coffee Table", "opportunity_score": 90,
             "suggested_price": 200, "margin_rate": 35.0, "potential_profit": 70,
             "seller_str_pct": 3.0},
        ]
        rows = aggregate_by_category(opps)
        # 按 count 倒序：sofa=1, sectional sofa=1, table=1 (都是 1) — 至少包含 3 类
        cats = {r["category"]: r for r in rows}
        assert "sofa" in cats and "sectional sofa" in cats and "table" in cats
        assert cats["sofa"]["count"] == 1
        assert cats["sofa"]["avg_score"] == 80.0
        assert cats["sofa"]["real_str_count"] == 1
        assert cats["sectional sofa"]["real_str_count"] == 0
        assert cats["table"]["top_sku"] == "S3"
        assert cats["table"]["top_score"] == 90

    def test_aggregate_handles_empty_input(self):
        from src.plugins.terapeak_research.aggregation import aggregate_by_category
        assert aggregate_by_category([]) == []
        assert aggregate_by_category(None) == []

    def test_aggregate_skips_invalid_items(self):
        from src.plugins.terapeak_research.aggregation import aggregate_by_category
        # 含 None / 字符串 / 缺字段
        rows = aggregate_by_category([None, "garbage", {"sku": "X", "title": "Sofa"}])
        assert len(rows) == 1
        assert rows[0]["category"] == "sofa"
        assert rows[0]["count"] == 1


class TestF20RetryEndpoint:
    """F20 — 失败 SKU 重排队端点"""

    def test_retry_endpoint_registered(self):
        from server import app
        routes = {r.path for r in app.routes}
        assert "/api/mi/batch-job/{job_id}/retry-errors" in routes

    def test_retry_404_for_unknown(self):
        from fastapi.testclient import TestClient
        from server import app
        client = TestClient(app)
        resp = client.post("/api/mi/batch-job/missing/retry-errors")
        assert resp.status_code == 404

    def test_retry_409_when_source_still_running(self):
        import server
        from fastapi.testclient import TestClient
        old_jobs = dict(server._MI_BATCH_JOBS)
        try:
            server._MI_BATCH_JOBS["job_running"] = {
                "job_id": "job_running",
                "status": "running",
                "results": [{"sku": "S1", "status": "error"}],
                "counters": {"error": 1},
            }
            client = TestClient(server.app)
            resp = client.post("/api/mi/batch-job/job_running/retry-errors")
            assert resp.status_code == 409
        finally:
            server._MI_BATCH_JOBS.clear()
            server._MI_BATCH_JOBS.update(old_jobs)

    def test_retry_noop_when_no_errors(self):
        import server
        from fastapi.testclient import TestClient
        old_jobs = dict(server._MI_BATCH_JOBS)
        try:
            server._MI_BATCH_JOBS["job_clean"] = {
                "job_id": "job_clean",
                "status": "done",
                "results": [{"sku": "S1", "status": "success"}],
                "counters": {"error": 0},
            }
            client = TestClient(server.app)
            resp = client.post("/api/mi/batch-job/job_clean/retry-errors")
            assert resp.status_code == 200
            assert resp.json()["status"] == "noop"
        finally:
            server._MI_BATCH_JOBS.clear()
            server._MI_BATCH_JOBS.update(old_jobs)


class TestF21AlertSuppression:
    """F21 — 24h 告警去重抑制 + 邮件缩略图"""

    def test_filter_suppresses_recent_alerts(self, tmp_path, monkeypatch):
        import daily_tasks
        from datetime import datetime as _dt
        monkeypatch.setattr(daily_tasks, "_MI_ALERTS_STATE_PATH",
                            tmp_path / "mi_alerts_state.json")
        # 写入"刚刚发过"的状态
        (tmp_path / "mi_alerts_state.json").write_text(json.dumps({
            "opportunity_drop": {"last_sent_at": _dt(2026, 5, 2, 12, 0).isoformat()},
        }), encoding="utf-8")
        alerts = [
            {"type": "opportunity_drop", "severity": "high", "message": "x"},
            {"type": "low_str_coverage", "severity": "medium", "message": "y"},
        ]
        # 当前时间设为 4h 后 — 抑制窗口 24h，所以 opportunity_drop 应被过滤
        out = daily_tasks._filter_suppressed_alerts(
            alerts, window_hours=24, now=_dt(2026, 5, 2, 16, 0)
        )
        types = {a["type"] for a in out}
        assert "opportunity_drop" not in types
        assert "low_str_coverage" in types

    def test_filter_passes_after_window(self, tmp_path, monkeypatch):
        import daily_tasks
        from datetime import datetime as _dt
        monkeypatch.setattr(daily_tasks, "_MI_ALERTS_STATE_PATH",
                            tmp_path / "mi_alerts_state.json")
        (tmp_path / "mi_alerts_state.json").write_text(json.dumps({
            "opportunity_drop": {"last_sent_at": _dt(2026, 5, 1, 10, 0).isoformat()},
        }), encoding="utf-8")
        alerts = [{"type": "opportunity_drop", "severity": "high", "message": "x"}]
        # 26h 后应解除抑制
        out = daily_tasks._filter_suppressed_alerts(
            alerts, window_hours=24, now=_dt(2026, 5, 2, 12, 0)
        )
        assert len(out) == 1

    def test_record_alerts_writes_state(self, tmp_path, monkeypatch):
        import daily_tasks
        from datetime import datetime as _dt
        monkeypatch.setattr(daily_tasks, "_MI_ALERTS_STATE_PATH",
                            tmp_path / "mi_alerts_state.json")
        daily_tasks._record_alerts_sent(
            [{"type": "opportunity_drop", "severity": "high"}],
            now=_dt(2026, 5, 2, 9, 0),
        )
        state = json.loads((tmp_path / "mi_alerts_state.json").read_text(encoding="utf-8"))
        assert "opportunity_drop" in state
        assert state["opportunity_drop"]["last_sent_at"].startswith("2026-05-02T09:00")

    def test_thumbnails_html_for_opportunities(self):
        import daily_tasks
        opps = [
            {"sku": "S1", "title": "Sofa", "opportunity_score": 80,
             "suggested_price": 400, "image_url": "https://img.example.com/s1.jpg"},
            {"sku": "S2", "title": "Chair", "opportunity_score": 75,
             "suggested_price": 100, "images": ["https://img.example.com/s2.jpg"]},
            {"sku": "S3", "title": "无图产品", "opportunity_score": 60,
             "suggested_price": 50},
        ]
        html = daily_tasks._build_opportunities_thumbnails_html(opps, limit=3)
        assert "S1" in html and "S2" in html and "S3" in html
        # 至少应渲染 image_url（哪怕被 normalize_thumbnail_url 改写）
        assert "img" in html.lower() or "无图" in html
        assert "Top 3 未刊登候选" in html

    def test_thumbnails_html_empty_on_empty_input(self):
        import daily_tasks
        assert daily_tasks._build_opportunities_thumbnails_html([], limit=6) == ""


class TestF22ConfigurableCategoryMapping:
    """F22 — 品类规则可配置（mi_categories.json + mtime 缓存）"""

    def test_default_config_loaded(self):
        from src.plugins.terapeak_research.aggregation import (
            derive_product_category, _DEFAULT_CONFIG_PATH,
        )
        # 项目根 mi_categories.json 应当存在并匹配 sofa
        assert _DEFAULT_CONFIG_PATH.exists()
        assert derive_product_category("Modern Sofa") == "sofa"
        assert derive_product_category("Sectional Sofa Large") == "sectional sofa"

    def test_falls_back_when_config_missing(self, tmp_path):
        from src.plugins.terapeak_research.aggregation import derive_product_category
        missing = tmp_path / "missing.json"
        # 文件不存在时使用内置默认
        assert derive_product_category("Modern Sofa", config_path=missing) == "sofa"
        assert derive_product_category("Mystery Box", config_path=missing) == "other"

    def test_custom_config_overrides_defaults(self, tmp_path):
        from src.plugins.terapeak_research.aggregation import derive_product_category
        cfg = tmp_path / "custom.json"
        cfg.write_text(json.dumps({
            "categories": [{"label": "gizmo", "keywords": ["widget"]}],
            "fallback": "unknown",
        }), encoding="utf-8")
        assert derive_product_category("Shiny Widget Pro", config_path=cfg) == "gizmo"
        assert derive_product_category("Modern Sofa", config_path=cfg) == "unknown"

    def test_mtime_cache_invalidates_on_change(self, tmp_path):
        from src.plugins.terapeak_research.aggregation import derive_product_category
        cfg = tmp_path / "rules.json"
        cfg.write_text(json.dumps({
            "categories": [{"label": "alpha", "keywords": ["foo"]}],
            "fallback": "x",
        }), encoding="utf-8")
        assert derive_product_category("foo bar", config_path=cfg) == "alpha"
        # 修改文件并强制 mtime 改变
        time.sleep(1.1)
        cfg.write_text(json.dumps({
            "categories": [{"label": "beta", "keywords": ["foo"]}],
            "fallback": "x",
        }), encoding="utf-8")
        os.utime(cfg, None)
        assert derive_product_category("foo bar", config_path=cfg) == "beta"

    def test_corrupted_config_falls_back_safely(self, tmp_path):
        from src.plugins.terapeak_research.aggregation import derive_product_category
        cfg = tmp_path / "broken.json"
        cfg.write_text("{not valid json", encoding="utf-8")
        # 不应抛异常；应回退到内置默认
        assert derive_product_category("Modern Sofa", config_path=cfg) == "sofa"


class TestF23LongWindow:
    """F23 — 30/60d 长周期透视"""

    def _make_snap(self, generated_at, count, score=70.0):
        return {
            "generated_at": generated_at,
            "by_sku": {f"S{i}": {"opportunity_score": score} for i in range(count)},
        }

    def test_insufficient_when_few_snapshots(self):
        from src.plugins.terapeak_research.history import summarize_long_window
        from datetime import datetime, timedelta
        now = datetime(2026, 1, 30)
        snaps = [self._make_snap(now - timedelta(days=1), 5)]
        out = summarize_long_window(snaps, current_opps=[{}] * 5,
                                    short_days=14, long_days=30, now=now)
        assert out["trend_label"] == "insufficient"
        assert out["snapshots_long"] == 1
        assert out["current_count"] == 5

    def test_rising_trend(self):
        from src.plugins.terapeak_research.history import summarize_long_window
        from datetime import datetime, timedelta
        now = datetime(2026, 1, 30)
        # 14d 内 4 份高频快照 (count=10)；超出 14d 的 long 只有低频 (count=2)
        snaps = []
        for i in range(4):
            snaps.append(self._make_snap(now - timedelta(days=i + 1), 10))
        for i in range(3):
            snaps.append(self._make_snap(now - timedelta(days=20 + i), 2))
        out = summarize_long_window(snaps, current_opps=[{}] * 10,
                                    short_days=14, long_days=30, now=now)
        assert out["short_avg_count"] == 10.0
        assert out["long_avg_count"] < 10.0
        assert out["drift"] > 0
        assert out["trend_label"] == "rising"

    def test_falling_trend(self):
        from src.plugins.terapeak_research.history import summarize_long_window
        from datetime import datetime, timedelta
        now = datetime(2026, 1, 30)
        snaps = []
        for i in range(4):
            snaps.append(self._make_snap(now - timedelta(days=i + 1), 2))
        for i in range(3):
            snaps.append(self._make_snap(now - timedelta(days=20 + i), 10))
        out = summarize_long_window(snaps, current_opps=[{}] * 2,
                                    short_days=14, long_days=30, now=now)
        assert out["drift"] < 0
        assert out["trend_label"] == "falling"


class TestF24DailyDigest:
    """F24 — 每日 MI 摘要邮件"""

    def test_digest_sends_with_thumbnails_and_categories(self, monkeypatch):
        import daily_tasks
        sent = {}

        def fake_send_email(subject, html):
            sent["subject"] = subject
            sent["html"] = html
            return True

        monkeypatch.setattr(
            daily_tasks,
            "_archive_mi_digest_html",
            lambda html, keep_days=3, reports_dir=None, now=None: None,
        )
        monkeypatch.setattr(daily_tasks, "send_email", fake_send_email)
        opps = [
            {"sku": "A1", "title": "Modern Sofa", "opportunity_score": 80,
             "suggested_price": 400, "margin_rate": 30.0, "potential_profit": 120,
             "seller_str_pct": 2.5, "image_url": "https://img.example.com/a1.jpg"},
            {"sku": "A2", "title": "Coffee Table", "opportunity_score": 70,
             "suggested_price": 200, "margin_rate": 25.0, "potential_profit": 50,
             "seller_str_pct": None, "image_url": "https://img.example.com/a2.jpg"},
        ]
        kpi = {"current_count": 2, "avg_recent_count": 5.0, "delta_vs_avg": -3.0,
               "real_str_coverage_pct": 50.0, "snapshots_analyzed": 7}
        long_window = {"short_days": 14, "long_days": 30,
                       "short_avg_count": 6.0, "long_avg_count": 4.0,
                       "drift": 2.0, "snapshots_long": 5,
                       "long_avg_score": 72.0, "trend_label": "rising"}

        ok = daily_tasks._send_mi_daily_digest(opps, kpi, "mi_opportunities_x.json",
                                                long_window=long_window)
        assert ok is True
        html = sent["html"]
        # 中文标题
        assert "市场情报" in html and "每日摘要" in html
        # KPI 中文字段
        assert "本次=" in html or "本次" in html
        # 长周期透视
        assert "长周期透视" in html and "回暖" in html
        # 品类聚合表
        assert "品类聚合" in html and "sofa" in html
        # 缩略图（_build_opportunities_thumbnails_html 的固定标题）
        assert "未刊登候选" in html
        # subject
        assert "MI 日报" in sent["subject"]

    def test_digest_archives_but_does_not_email_when_no_opportunities(self, monkeypatch):
        import daily_tasks
        called = {"n": 0}
        archived = {}

        def fake_archive(html, keep_days=3, reports_dir=None, now=None):
            archived["html"] = html
            archived["keep_days"] = keep_days
            return None

        def fake_send_email(subject, html):
            called["n"] += 1
            return True

        monkeypatch.setattr(daily_tasks, "_archive_mi_digest_html", fake_archive)
        monkeypatch.setattr(daily_tasks, "send_email", fake_send_email)
        ok = daily_tasks._send_mi_daily_digest([], kpi={}, snap_name="mi_opportunities_x.json")
        assert ok is False
        assert called["n"] == 0
        assert archived["keep_days"] == 3
        assert "mi_opportunities_x.json" in archived["html"]
        assert "共发现 <b>0</b> 条机会" in archived["html"]


class TestF25DigestArchive:
    """F25 — MI 日报 HTML 归档 + 3 天保留"""

    def test_archive_writes_html_with_dated_filename(self, tmp_path):
        import daily_tasks
        from datetime import datetime
        out = daily_tasks._archive_mi_digest_html(
            "<html>hi</html>", keep_days=3,
            reports_dir=tmp_path, now=datetime(2026, 1, 30),
        )
        assert out.name == "mi_digest_20260130.html"
        assert out.read_text(encoding="utf-8") == "<html>hi</html>"

    def test_archive_purges_old_files(self, tmp_path):
        import daily_tasks
        from datetime import datetime
        # 旧文件：超过 3 天
        old = tmp_path / "mi_digest_20260101.html"
        old.write_text("old", encoding="utf-8")
        # 较新但仍在保留期
        keep = tmp_path / "mi_digest_20260128.html"
        keep.write_text("keep", encoding="utf-8")

        daily_tasks._archive_mi_digest_html(
            "<html>new</html>", keep_days=3,
            reports_dir=tmp_path, now=datetime(2026, 1, 30),
        )
        assert not old.exists()
        assert keep.exists()
        assert (tmp_path / "mi_digest_20260130.html").exists()

    def test_digest_send_triggers_archive(self, monkeypatch, tmp_path):
        import daily_tasks
        captured = {}

        def fake_archive(html, keep_days=3, reports_dir=None, now=None):
            captured["html"] = html
            captured["keep_days"] = keep_days
            return tmp_path / "x.html"

        monkeypatch.setattr(daily_tasks, "_archive_mi_digest_html", fake_archive)
        monkeypatch.setattr(daily_tasks, "send_email", lambda s, h: True)
        opps = [{"sku": "A1", "title": "Sofa", "opportunity_score": 80,
                 "suggested_price": 400, "margin_rate": 30.0, "potential_profit": 120,
                 "seller_str_pct": 2.5, "image_url": "https://img.example.com/a1.jpg"}]
        ok = daily_tasks._send_mi_daily_digest(opps, {"avg_recent_count": 1,
            "current_count": 1, "delta_vs_avg": 0, "real_str_coverage_pct": 100},
            "snap.json")
        assert ok is True
        assert "市场情报" in captured["html"]
        assert captured["keep_days"] == 3


class TestF27PersistentFallingTrend:
    """F27 — 长周期持续衰退告警"""

    def test_record_long_window_trend_dedupes_same_day(self, tmp_path):
        import daily_tasks
        from datetime import datetime
        path = tmp_path / "trend.json"
        now = datetime(2026, 1, 30)
        h1 = daily_tasks._record_long_window_trend(
            {"trend_label": "falling", "drift": -3.0, "snapshots_long": 5,
             "short_avg_count": 2, "long_avg_count": 5},
            history_path=path, now=now)
        h2 = daily_tasks._record_long_window_trend(
            {"trend_label": "falling", "drift": -3.5, "snapshots_long": 5,
             "short_avg_count": 1.5, "long_avg_count": 5},
            history_path=path, now=now)
        assert len(h1) == 1
        # 同一天写入，仍只有 1 条，但 drift 更新为 -3.5
        assert len(h2) == 1
        assert h2[0]["drift"] == -3.5

    def test_record_skips_invalid_label(self, tmp_path):
        import daily_tasks
        path = tmp_path / "t.json"
        h = daily_tasks._record_long_window_trend(
            {"trend_label": "insufficient"}, history_path=path)
        assert h == []
        assert not path.exists()

    def test_persistent_falling_after_three_days(self):
        import daily_tasks
        history = [
            {"date": "2026-01-28", "trend_label": "falling"},
            {"date": "2026-01-29", "trend_label": "falling"},
            {"date": "2026-01-30", "trend_label": "falling"},
        ]
        alert = daily_tasks._check_persistent_falling_trend(history, min_days=3)
        assert alert is not None
        assert alert["type"] == "persistent_falling_trend"
        assert alert["severity"] == "high"
        assert "2026-01-28" in alert["message"]

    def test_no_alert_when_one_day_breaks_streak(self):
        import daily_tasks
        history = [
            {"date": "2026-01-28", "trend_label": "falling"},
            {"date": "2026-01-29", "trend_label": "stable"},
            {"date": "2026-01-30", "trend_label": "falling"},
        ]
        assert daily_tasks._check_persistent_falling_trend(history, min_days=3) is None

    def test_no_alert_when_history_too_short(self):
        import daily_tasks
        history = [{"date": "2026-01-30", "trend_label": "falling"}]
        assert daily_tasks._check_persistent_falling_trend(history, min_days=3) is None


class TestF28DayOverDayCompare:
    """F28 — 昨日 vs 今日对比块"""

    def test_no_yesterday_returns_today_only(self):
        from src.plugins.terapeak_research.history import compare_kpi_day_over_day
        opps = [
            {"sku": "A", "opportunity_score": 80, "seller_str_pct": 1.0},
            {"sku": "B", "opportunity_score": 60, "seller_str_pct": None},
        ]
        out = compare_kpi_day_over_day([], opps)
        assert out["has_yesterday"] is False
        assert out["today_count"] == 2
        assert out["yesterday_count"] == 0
        assert out["count_delta"] == 2
        assert out["today_avg_score"] == 70.0
        assert out["today_real_str_pct"] == 50.0
        assert "A" in out["new_skus"] and "B" in out["new_skus"]

    def test_full_compare_with_new_and_exited(self):
        from src.plugins.terapeak_research.history import compare_kpi_day_over_day
        from datetime import datetime
        snap = {
            "generated_at": datetime(2026, 1, 29),
            "by_sku": {
                "A": {"opportunity_score": 70, "seller_str_pct": 2.0},
                "B": {"opportunity_score": 50, "seller_str_pct": None},
                "C": {"opportunity_score": 90, "seller_str_pct": 3.0},
            },
        }
        today_opps = [
            {"sku": "A", "opportunity_score": 80, "seller_str_pct": 1.5},
            {"sku": "C", "opportunity_score": 85, "seller_str_pct": 2.5},
            {"sku": "D", "opportunity_score": 60, "seller_str_pct": None},
        ]
        out = compare_kpi_day_over_day([snap], today_opps)
        assert out["has_yesterday"] is True
        assert out["yesterday_date"] == "2026-01-29"
        assert out["yesterday_count"] == 3
        assert out["today_count"] == 3
        assert out["count_delta"] == 0
        assert out["yesterday_avg_score"] == 70.0
        assert out["today_avg_score"] == 75.0
        assert out["score_delta"] == 5.0
        assert "D" in out["new_skus"]
        assert "B" in out["exited_skus"]
        assert "A" not in out["new_skus"]


class TestF30CategorySlicedTrend:
    """F30 — 按品类切片的历史趋势"""

    def test_list_known_categories_orders_by_frequency(self):
        from src.plugins.terapeak_research.history import list_known_categories
        snaps = [
            {"by_category": [{"category": "sofa"}, {"category": "table"}]},
            {"by_category": [{"category": "sofa"}, {"category": "chair"}]},
            {"by_category": [{"category": "sofa"}]},
        ]
        cats = list_known_categories(snaps)
        # sofa 出现 3 次最多，应在最前
        assert cats[0] == "sofa"
        assert "table" in cats and "chair" in cats

    def test_build_category_timeseries_filters_to_category(self):
        from src.plugins.terapeak_research.history import build_category_timeseries
        from datetime import datetime
        snaps = [
            {"generated_at": datetime(2026, 1, 28),
             "by_category": [{"category": "sofa", "count": 5, "avg_score": 70.0,
                              "median_price": 400, "total_potential_profit": 500}]},
            {"generated_at": datetime(2026, 1, 29),
             "by_category": [{"category": "table", "count": 3, "avg_score": 60.0,
                              "median_price": 200, "total_potential_profit": 200}]},
            {"generated_at": datetime(2026, 1, 30),
             "by_category": [{"category": "sofa", "count": 8, "avg_score": 75.0,
                              "median_price": 420, "total_potential_profit": 700}]},
        ]
        ts = build_category_timeseries(snaps, "sofa")
        assert len(ts) == 2
        assert ts[0]["date"] == "2026-01-28" and ts[0]["count"] == 5
        assert ts[1]["date"] == "2026-01-30" and ts[1]["count"] == 8

    def test_build_category_timeseries_empty_for_unknown(self):
        from src.plugins.terapeak_research.history import build_category_timeseries
        snaps = [{"generated_at": __import__("datetime").datetime(2026, 1, 30),
                  "by_category": []}]
        assert build_category_timeseries(snaps, "ghost") == []

    def test_load_mi_snapshots_passes_through_by_category(self, tmp_path):
        from src.plugins.terapeak_research.history import load_mi_snapshots
        from datetime import datetime
        reports = tmp_path / "reports"
        reports.mkdir()
        path = reports / "mi_opportunities_20260130_120000.json"
        path.write_text(json.dumps({
            "generated_at": "2026-01-30T12:00:00",
            "opportunities": [{"sku": "S1", "opportunity_score": 80}],
            "by_category": [{"category": "sofa", "count": 1, "avg_score": 80.0,
                             "median_price": 400, "total_potential_profit": 100}],
        }), encoding="utf-8")
        snaps = load_mi_snapshots(window_days=14, project_root=tmp_path,
                                  now=datetime(2026, 1, 30, 13, 0))
        assert len(snaps) == 1
        assert snaps[0]["by_category"][0]["category"] == "sofa"




