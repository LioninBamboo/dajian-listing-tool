"""analyze_collected_products must skip corrupt logs and degrade to exit 2."""

from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.collection_models import Base, CollectedProduct
from src.utils.task_result_status import classify_daily_task_outcome, exit_code_for_task_results
from tests.test_copy_skip_before_llm import CLEAR_DIMS
from tests.test_logs_codec import GARBAGE, TRAILING_SHORT, VALID


def _bind_temp_db(monkeypatch, db_path):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("src.db.collection_db.SessionLocal", Session)
    return engine, Session


def _seed_collected(session, sku, *, logs):
    session.add(
        CollectedProduct(
            sku=sku,
            title="Oak Side Table",
            price=40,
            shipping=10,
            status="COLLECTED",
            attributes=dict(CLEAR_DIMS),
            specs={},
            images=["http://example.com/a.jpg", "http://example.com/b.jpg"],
            description="Solid oak side table.",
            logs=logs if isinstance(logs, list) else ["seed"],
        )
    )


def _stub_analyze(monkeypatch):
    import daily_tasks

    def _optimize(*args, **kwargs):
        return {
            "title": "Oak Side Table 40 in",
            "description": "<p>Oak side table.</p>",
            "aspects": {"Brand": ["Test"]},
            "categoryId": "38204",
        }

    class _Qwen:
        def fetch_market_intelligence(self, title, category_id=None):
            return None

    monkeypatch.setenv("QWEN_API_KEY", "dummy")
    monkeypatch.setattr("qwen_optimizer.optimize_product_full_with_timeout", _optimize)
    monkeypatch.setattr("qwen_optimizer.QwenOptimizer", lambda api_key: _Qwen())
    monkeypatch.setattr("src.utils.copy_skip_gate.lookup_live_supplier_quote", lambda sku: None)
    monkeypatch.setattr(daily_tasks, "_open_listing_qc_connection", lambda: None)
    monkeypatch.setattr(
        "src.services.listing_qc.run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": [],
            "violations": [],
            "source_fingerprint": "s",
            "candidate_fingerprint": "c",
        },
    )
    monkeypatch.setattr(
        "src.utils.listing_quality_gate.normalize_generated_listing",
        lambda opt_data, **kwargs: opt_data,
    )
    return daily_tasks


def test_analyze_skips_corrupt_rows_and_leaves_logs_bytes(tmp_path, monkeypatch):
    db_path = tmp_path / "ebay_collection.db"
    engine, Session = _bind_temp_db(monkeypatch, db_path)
    session = Session()
    try:
        _seed_collected(session, "N710P300001", logs=["ok"])
        _seed_collected(session, "N710P300002", logs=["seed"])
        _seed_collected(session, "N710P300003", logs=["seed"])
        session.commit()
    finally:
        session.close()

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("UPDATE collected_products SET logs = ? WHERE sku = ?", (VALID, "N710P300001"))
        conn.execute("UPDATE collected_products SET logs = ? WHERE sku = ?", (TRAILING_SHORT, "N710P300002"))
        conn.execute("UPDATE collected_products SET logs = ? WHERE sku = ?", (GARBAGE, "N710P300003"))
        conn.commit()
        before = {
            sku: raw
            for sku, raw in conn.execute(
                "SELECT sku, logs FROM collected_products WHERE sku IN (?,?,?)",
                ("N710P300001", "N710P300002", "N710P300003"),
            )
        }
    finally:
        conn.close()

    daily_tasks = _stub_analyze(monkeypatch)
    result = daily_tasks.analyze_collected_products()

    assert result["success"] == 1
    assert result["skipped_corrupt"] == 2
    assert result["failed"] == 0
    assert sorted(result["corrupt_skus"]) == ["N710P300002", "N710P300003"]
    assert result["skip_reasons"]["N710P300002"] == ["corrupt_logs:trailing_text"]
    assert result["skip_reasons"]["N710P300003"] == ["corrupt_logs:garbage"]

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        after = {
            sku: (status, logs)
            for sku, status, logs in conn.execute(
                "SELECT sku, status, logs FROM collected_products"
            )
        }
    finally:
        conn.close()
        engine.dispose()

    assert after["N710P300001"][0] == "READY"
    assert after["N710P300002"] == ("COLLECTED", before["N710P300002"])
    assert after["N710P300003"] == ("COLLECTED", before["N710P300003"])
    assert after["N710P300002"][1] == TRAILING_SHORT
    assert after["N710P300003"][1] == GARBAGE


def test_classify_analyze_outcomes():
    assert (
        classify_daily_task_outcome({"analyze": {"success": 1, "failed": 0, "skipped_corrupt": 0}})
        == "success"
    )
    assert (
        classify_daily_task_outcome({"analyze": {"success": 1, "failed": 0, "skipped_corrupt": 2}})
        == "partial_success"
    )
    assert (
        classify_daily_task_outcome({"analyze": {"success": 0, "failed": 0, "load_errors": 1}})
        == "partial_success"
    )
    assert classify_daily_task_outcome({"analyze": {"error": "boom"}}) == "failed"
    assert exit_code_for_task_results({"analyze": {"skipped_corrupt": 2, "failed": 0}}) == 2
    assert exit_code_for_task_results({"analyze": {"error": "boom"}}) == 1
    assert exit_code_for_task_results({"analyze": {"success": 1, "failed": 0}}) == 0


def test_analyze_only_main_exits_partial_success(monkeypatch):
    import daily_tasks

    monkeypatch.setattr(daily_tasks.sys, "argv", ["daily_tasks.py", "--analyze-only"])
    monkeypatch.setattr(daily_tasks, "assert_runtime_not_in_maintenance", lambda *_a, **_k: None)
    monkeypatch.setattr(
        daily_tasks,
        "validate_runtime_database",
        lambda *_a, **_k: type("R", (), {"integrity_check": "ok", "link_count": 1, "page_count": 1})(),
    )
    monkeypatch.setattr(daily_tasks, "run_report_retention_cleanup", lambda: None)
    monkeypatch.setattr(
        daily_tasks,
        "analyze_collected_products",
        lambda: {"success": 1, "failed": 0, "skipped_corrupt": 2, "load_errors": 0, "corrupt_skus": ["A", "B"]},
    )

    results = daily_tasks.main()
    assert classify_daily_task_outcome(results) == "partial_success"
    assert exit_code_for_task_results(results) == 2


def test_mi_opportunity_flow_surfaces_corrupt_skip(monkeypatch):
    from src.utils.mi_opportunity_flow import auto_prepare_mi_opportunity_drafts

    def fake_analyze(*, sku_filter=None, eligible_statuses=("COLLECTED",), draft_origin=None):
        return {
            "success": 0,
            "failed": 0,
            "skipped_corrupt": 1,
            "corrupt_skus": list(sku_filter or []),
            "prepared_skus": [],
            "skip_reasons": {list(sku_filter or ["X"])[0]: ["corrupt_logs:trailing_text"]},
        }

    result = auto_prepare_mi_opportunity_drafts(
        [{"sku": "N710P300009", "status": "COLLECTED"}],
        analyze_collected_products=fake_analyze,
    )
    assert result["skipped_corrupt"] == 1
    assert result["prepared_skus"] == []
