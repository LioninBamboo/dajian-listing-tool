"""execute_approved — 两步安全流程的薄编排器契约.

历史: Phase 1 时 execute_approved(apply_changes=True) 以 NotImplementedError
作为闸门. 收口后它必须等价于 precheck_approved → execute_prechecked_relist
顺序执行, 共用同一 apply/limit, 不得存在绕过 precheck 的直发路径.
"""
from __future__ import annotations

import pytest

import src.services.cro_relist_lifecycle as lc


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "lifecycle.db"
    lc.ensure_schema(db)
    return db


def test_execute_approved_orchestrates_two_step_flow(tmp_db, monkeypatch):
    calls = []

    def fake_precheck(limit, apply_changes, operator, db_path):
        calls.append(('precheck', limit, apply_changes, operator))
        return {'apply_changes': apply_changes, 'selected': 3,
                'prechecked': 2, 'skipped': 1, 'results': []}

    def fake_execute(limit, apply_changes, operator, db_path):
        calls.append(('execute', limit, apply_changes, operator))
        return {'apply_changes': apply_changes, 'selected': 2,
                'executed': 2, 'results': []}

    monkeypatch.setattr(lc, 'precheck_approved', fake_precheck)
    monkeypatch.setattr(lc, 'execute_prechecked_relist', fake_execute)

    rep = lc.execute_approved(limit=7, apply_changes=True,
                              operator='op1', db_path=tmp_db)

    # 顺序: precheck 先, execute 后; apply/limit/operator 原样透传
    assert [c[0] for c in calls] == ['precheck', 'execute']
    assert all(c[1] == 7 and c[2] is True and c[3] == 'op1' for c in calls)
    assert rep['apply_changes'] is True
    assert rep['selected'] == 3
    assert rep['precheck']['prechecked'] == 2
    assert rep['execute']['executed'] == 2


def test_execute_approved_apply_no_longer_raises(tmp_db):
    # 空库上 apply=True: 两阶段都选中 0 行, 不再抛 NotImplementedError
    rep = lc.execute_approved(limit=5, apply_changes=True, db_path=tmp_db)
    assert rep['apply_changes'] is True
    assert rep['selected'] == 0
    assert rep['precheck']['selected'] == 0
    assert rep['execute']['selected'] == 0


def test_execute_approved_dry_run_defaults_safe(tmp_db):
    rep = lc.execute_approved(db_path=tmp_db)
    assert rep['apply_changes'] is False
    assert rep['precheck']['apply_changes'] is False
    assert rep['execute']['apply_changes'] is False


def test_ensure_schema_migrates_pre_phase4_table(tmp_path):
    """老库的 lifecycle 表缺 Phase 4 新列时, ensure_schema 必须 ALTER 补齐."""
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("""
            CREATE TABLE cro_listing_lifecycle_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                reason TEXT, old_listing_id TEXT, new_listing_id TEXT,
                old_offer_id TEXT, new_offer_id TEXT,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                metrics_before_json TEXT, db_snapshot_json TEXT,
                live_snapshot_json TEXT, created_at TEXT NOT NULL,
                approved_at TEXT, approved_by TEXT, started_at TEXT,
                finished_at TEXT, observe_until TEXT,
                error TEXT, source TEXT, updated_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO cro_listing_lifecycle_actions "
            "(sku, action_type, status, priority, created_at) "
            "VALUES ('S1', 'revive_relist', 'observing', 'P3', '2026-06-01')")
        conn.commit()

    lc.ensure_schema(db)

    with sqlite3.connect(str(db)) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(cro_listing_lifecycle_actions)")}
        assert 'observe_window_days' in cols
        assert 'conversion_help_attempts' in cols
        # 既有行保留且新列有默认值
        row = conn.execute(
            "SELECT sku, conversion_help_attempts "
            "FROM cro_listing_lifecycle_actions").fetchone()
        assert row == ('S1', 0)
    # 幂等
    lc.ensure_schema(db)
