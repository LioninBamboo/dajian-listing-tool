"""Promote at-cap 冷却与升级候选测试."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.services.cro_promote_escalation import (
    load_at_cap_hits, at_cap_cooldown_skus, escalation_candidates,
)
from src.services.cro_action_queue import load_pending
from src.services.cro_daily_runner import run_cro_daily


def _write_promote_log(logs_dir, dt, rows):
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"cro_promote_{dt:%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({'rows': rows}, ensure_ascii=False),
                    encoding='utf-8')
    return path


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    d = tmp_path / 'logs'
    import src.services.cro_promote_escalation as esc
    monkeypatch.setattr(esc, 'DEFAULT_LOGS_DIR', d)
    return d


def test_load_at_cap_hits_parses_recent_logs(logs_dir):
    now = datetime.now()
    _write_promote_log(logs_dir, now, [
        {'sku': 'CAP1', 'status': 'skipped',
         'reason': 'already at cap (12.0% + 5.0% > 12.0%)', 'terminal': True},
        {'sku': 'OK1', 'status': 'done', 'new_bid': 10.0},
        {'sku': 'OTHER', 'status': 'skipped', 'reason': 'no ebay_item_id in DB'},
    ])
    _write_promote_log(logs_dir, now - timedelta(days=1), [
        {'sku': 'CAP1', 'status': 'skipped',
         'reason': 'already at cap (12.0% + 5.0% > 12.0%)'},
        {'sku': 'CAP2', 'status': 'skipped',
         'reason': 'already at cap (25.0% + 5.0% > 25.0%)'},
    ])
    hits = load_at_cap_hits(days=7)
    assert set(hits) == {'CAP1', 'CAP2'}
    assert hits['CAP1']['hits'] == 2
    assert hits['CAP1']['last_reason'].startswith('already at cap')
    assert at_cap_cooldown_skus(days=7) == {'CAP1', 'CAP2'}


def test_load_at_cap_hits_ignores_old_and_malformed(logs_dir):
    now = datetime.now()
    _write_promote_log(logs_dir, now - timedelta(days=30), [
        {'sku': 'OLD', 'status': 'skipped',
         'reason': 'already at cap (10% + 5% > 10%)'},
    ])
    bad = logs_dir / f"cro_promote_{now:%Y%m%d}_235959.json"
    bad.write_text('{not json', encoding='utf-8')
    assert load_at_cap_hits(days=7) == {}
    assert at_cap_cooldown_skus(days=7) == set()


def test_escalation_candidates_sorted_by_hits(logs_dir):
    now = datetime.now()
    for i in range(3):
        _write_promote_log(logs_dir, now - timedelta(days=i), [
            {'sku': 'HOT', 'status': 'skipped',
             'reason': 'already at cap (12% + 5% > 12%)'},
        ])
    _write_promote_log(logs_dir, now - timedelta(hours=1), [
        {'sku': 'WARM', 'status': 'skipped',
         'reason': 'already at cap (8% + 5% > 8%)'},
    ])
    cands = escalation_candidates(days=7)
    assert [c['sku'] for c in cands] == ['HOT', 'WARM']
    assert cands[0]['hits'] == 3


def test_missing_logs_dir_degrades_to_empty(tmp_path, monkeypatch):
    import src.services.cro_promote_escalation as esc
    monkeypatch.setattr(esc, 'DEFAULT_LOGS_DIR', tmp_path / 'nope')
    assert at_cap_cooldown_skus(days=7) == set()
    assert escalation_candidates(days=7) == []


# ── daily runner 集成 ────────────────────────────────────────

def _promote_prod(sku):
    # no_impression + age>=14 + 无销售 → promote P1
    return {
        'sku': sku, 'listing_id': f'L_{sku}',
        'impressions': 0, 'views': 0,
        'transactions': 0, 'sold_qty': 0,
        'selling_price': 100, 'age_days': 30,
        'title': 'x' * 70, 'images': ['u'],
        'categoryId': 'X',
    }


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_cro.db"
    import src.services.cro_snapshot_store as s
    monkeypatch.setattr(s, 'DEFAULT_DB', db)
    return db


@pytest.fixture
def tmp_queue(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'DEFAULT_QUEUE', q)
    # promote 是 A/B 动作, 周哈希可能分 control 被 load_pending 隐藏; 测试需确定性
    monkeypatch.setattr(q_mod, 'assign_cohort', lambda sku, action: 'treatment')
    return q


def test_runner_drops_at_cap_promote_and_reports_escalation(
        tmp_db, tmp_queue, logs_dir, tmp_path):
    _write_promote_log(logs_dir, datetime.now(), [
        {'sku': 'CAPPED', 'status': 'skipped',
         'reason': 'already at cap (12% + 5% > 12%)'},
    ])
    rep = run_cro_daily(
        [_promote_prod('CAPPED'), _promote_prod('FREE')],
        market_data={'X': {'median': 100}},
        enqueue_action_types=('promote',),
        report_dir=tmp_path / 'reports',
    )
    pend = load_pending(action_type='promote')
    assert [p['sku'] for p in pend] == ['FREE']
    assert rep['promote_at_cap_cooldown_dropped'] == 1
    assert rep['promote_escalation']['count'] == 1
    assert rep['promote_escalation']['candidates'][0]['sku'] == 'CAPPED'


def test_runner_cooldown_disabled_keeps_at_cap_sku(
        tmp_db, tmp_queue, logs_dir, tmp_path):
    _write_promote_log(logs_dir, datetime.now(), [
        {'sku': 'CAPPED', 'status': 'skipped',
         'reason': 'already at cap (12% + 5% > 12%)'},
    ])
    rep = run_cro_daily(
        [_promote_prod('CAPPED')],
        market_data={'X': {'median': 100}},
        enqueue_action_types=('promote',),
        promote_at_cap_cooldown_days=0,
        report_dir=tmp_path / 'reports',
    )
    pend = load_pending(action_type='promote')
    assert [p['sku'] for p in pend] == ['CAPPED']
    assert rep['promote_at_cap_cooldown_dropped'] == 0
    assert rep['promote_escalation'] == {'count': 0, 'candidates': []}
