"""F31 — MI 流水线端到端冒烟测试。

验证 daily_tasks.run_mi_snapshot 一次完整调用后：
- 快照 JSON 被写入 reports/ 且包含 by_category（F30）
- 日报 HTML 被归档（F25）
- 长周期 trend 历史 JSON 被更新（F27）
- _send_mi_daily_digest 被调用并发送（F24）
- DoD 对比块（F28）出现在邮件正文中（当存在前一日快照时）
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def fake_opps():
    return [
        {"sku": f"SKU{i}", "title": "Modern Sofa Set",
         "opportunity_score": 70 + i, "suggested_price": 400 + i * 10,
         "margin_rate": 25.0 + i, "potential_profit": 100 + i * 5,
         "seller_str_pct": 2.5 if i % 2 == 0 else None,
         "image_url": f"https://img.example.com/{i}.jpg",
         "status": "READY"}
        for i in range(5)
    ]


def test_mi_snapshot_pipeline_e2e(monkeypatch, tmp_path, fake_opps):
    import daily_tasks
    from src.plugins.terapeak_research.intelligence_service import IntelligenceService

    # 1) 把所有以 PROJECT_ROOT 派生的路径指向 tmp
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(daily_tasks, "_MI_LONG_TREND_PATH",
                        reports / "mi_long_window_history.json", raising=False)
    monkeypatch.setattr(daily_tasks, "_MI_ALERTS_STATE_PATH",
                        reports / "mi_alerts_state.json", raising=False)

    # 2) 预置一个"昨日"快照让 F28 DoD 块能渲染
    yest = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d_%H%M%S")
    (reports / f"mi_opportunities_{yest}.json").write_text(json.dumps({
        "generated_at": (datetime.now() - timedelta(days=1)).isoformat(),
        "opportunities": [
            {"sku": "SKU0", "opportunity_score": 60, "seller_str_pct": 1.0},
            {"sku": "EXIT", "opportunity_score": 50, "seller_str_pct": None},
        ],
        "by_category": [{"category": "sofa", "count": 2, "avg_score": 55.0,
                         "median_price": 200, "avg_margin_rate": 20.0,
                         "total_potential_profit": 100, "real_str_count": 1,
                         "top_sku": "SKU0", "top_score": 60, "median_score": 55.0}],
    }, ensure_ascii=False), encoding="utf-8")

    # 3) Mock IntelligenceService 与 send_email
    monkeypatch.setattr(IntelligenceService, "auto_discover_opportunities",
                        lambda self, min_margin=0.20, max_results=30: fake_opps)
    sent = []
    monkeypatch.setattr(daily_tasks, "send_email",
                        lambda subj, html: sent.append((subj, html)) or True)

    # 4) 跑一次完整流水线
    result = daily_tasks.run_mi_snapshot(min_margin=0.20, max_results=30)

    # ---- 断言 ----
    assert result["status"] == "ok"
    assert result["opportunities"] == 5
    assert result["snapshot"] and result["snapshot"].endswith(".json")

    # 快照含 by_category（F30）
    snap_data = json.loads((reports / result["snapshot"]).read_text(encoding="utf-8"))
    assert "by_category" in snap_data
    assert any(r.get("category") == "sofa" for r in snap_data["by_category"])

    # 日报 HTML 归档（F25）
    digest_files = list(reports.glob("mi_digest_*.html"))
    assert len(digest_files) == 1, f"应生成 1 份归档，实际 {len(digest_files)}"
    digest_html = digest_files[0].read_text(encoding="utf-8")
    # 中文 + DoD 块（F28）+ Top 推荐缩略图（F24）
    assert "市场情报" in digest_html
    assert "昨日 vs 今日" in digest_html
    assert "未刊登候选" in digest_html

    # 长周期 trend 历史更新（F27 — 即使快照不足 3，也允许文件不存在或仅有少量条目）
    trend_path = reports / "mi_long_window_history.json"
    if trend_path.exists():
        trend_data = json.loads(trend_path.read_text(encoding="utf-8"))
        assert isinstance(trend_data, list)

    # 至少发了 1 封邮件（F24 日报）
    assert len(sent) >= 1
    assert any("MI 日报" in s[0] for s in sent)


def test_mi_snapshot_auto_prepares_pending_and_collected_candidates(monkeypatch, tmp_path):
    import daily_tasks
    from src.plugins.terapeak_research.intelligence_service import IntelligenceService

    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(daily_tasks, "_MI_LONG_TREND_PATH",
                        reports / "mi_long_window_history.json", raising=False)
    monkeypatch.setattr(daily_tasks, "_MI_ALERTS_STATE_PATH",
                        reports / "mi_alerts_state.json", raising=False)

    fake_opps = [
        {
            "sku": "MI-PENDING",
            "title": "Modern Accent Chair",
            "opportunity_score": 81,
            "suggested_price": 329,
            "margin_rate": 27.5,
            "potential_profit": 92,
            "seller_str_pct": 3.5,
            "image_url": "https://img.example.com/pending.jpg",
            "status": "PENDING",
        },
        {
            "sku": "MI-COLLECTED",
            "title": "Modern Dining Table",
            "opportunity_score": 76,
            "suggested_price": 489,
            "margin_rate": 24.0,
            "potential_profit": 110,
            "seller_str_pct": None,
            "image_url": "https://img.example.com/collected.jpg",
            "status": "COLLECTED",
        },
        {
            "sku": "MI-READY",
            "title": "Modern Sofa",
            "opportunity_score": 73,
            "suggested_price": 699,
            "margin_rate": 22.0,
            "potential_profit": 140,
            "seller_str_pct": None,
            "image_url": "https://img.example.com/ready.jpg",
            "status": "READY",
        },
    ]

    monkeypatch.setattr(
        IntelligenceService,
        "auto_discover_opportunities",
        lambda self, min_margin=0.20, max_results=30: fake_opps,
    )
    monkeypatch.setattr(daily_tasks, "send_email", lambda subj, html: True)

    calls = []

    def fake_analyze_collected_products(*, sku_filter=None, eligible_statuses=("COLLECTED",), draft_origin=None):
        calls.append(
            {
                "sku_filter": list(sku_filter or []),
                "eligible_statuses": tuple(eligible_statuses),
                "draft_origin": draft_origin,
            }
        )
        return {
            "success": 2,
            "failed": 0,
            "requested": 2,
            "prepared_skus": ["MI-PENDING", "MI-COLLECTED"],
        }

    monkeypatch.setattr(daily_tasks, "analyze_collected_products", fake_analyze_collected_products)

    result = daily_tasks.run_mi_snapshot(min_margin=0.20, max_results=30)

    assert result["status"] == "ok"
    assert calls == [
        {
            "sku_filter": ["MI-PENDING", "MI-COLLECTED"],
            "eligible_statuses": ("PENDING", "COLLECTED"),
            "draft_origin": "mi_opportunity",
        }
    ]
    assert result["auto_prepared"]["requested"] == 2
    assert result["auto_prepared"]["success"] == 2

    snap_data = json.loads((reports / result["snapshot"]).read_text(encoding="utf-8"))
    prepared = {row["sku"]: row for row in snap_data["opportunities"]}
    assert prepared["MI-PENDING"]["status"] == "READY"
    assert prepared["MI-COLLECTED"]["status"] == "READY"
    assert prepared["MI-PENDING"]["draft_origin"] == "mi_opportunity"


def test_manual_mi_discovery_auto_prepares_pending_and_collected_candidates():
    from src.utils.mi_opportunity_flow import auto_prepare_mi_opportunity_drafts

    opportunities = [
        {
            "sku": "MI-PENDING",
            "status": "PENDING",
            "opportunity_score": 81,
        },
        {
            "sku": "MI-COLLECTED",
            "status": "COLLECTED",
            "opportunity_score": 76,
        },
        {
            "sku": "MI-READY",
            "status": "READY",
            "opportunity_score": 73,
        },
    ]

    calls = []

    def fake_analyze_collected_products(*, sku_filter=None, eligible_statuses=("COLLECTED",), draft_origin=None):
        calls.append(
            {
                "sku_filter": list(sku_filter or []),
                "eligible_statuses": tuple(eligible_statuses),
                "draft_origin": draft_origin,
            }
        )
        return {
            "success": 2,
            "failed": 0,
            "requested": 2,
            "matched": 2,
            "prepared_skus": ["MI-PENDING", "MI-COLLECTED"],
        }

    auto_prepare_result = auto_prepare_mi_opportunity_drafts(
        opportunities,
        analyze_collected_products=fake_analyze_collected_products,
    )

    assert calls == [
        {
            "sku_filter": ["MI-PENDING", "MI-COLLECTED"],
            "eligible_statuses": ("PENDING", "COLLECTED"),
            "draft_origin": "mi_opportunity",
        }
    ]
    assert auto_prepare_result["success"] == 2

    prepared = {row["sku"]: row for row in opportunities}
    assert prepared["MI-PENDING"]["status"] == "READY"
    assert prepared["MI-COLLECTED"]["status"] == "READY"
    assert prepared["MI-PENDING"]["draft_origin"] == "mi_opportunity"
    assert prepared["MI-COLLECTED"]["draft_origin"] == "mi_opportunity"
    assert prepared["MI-READY"]["status"] == "READY"
    assert prepared["MI-READY"].get("draft_origin") is None
