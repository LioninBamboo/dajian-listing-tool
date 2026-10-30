"""P12 — bid_experiment tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.services import bid_experiment as bx


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / 'bx.db'


def test_create_and_assign_deterministic(tmp_db):
    eid = bx.create_experiment(
        'test1', control_bid=5.0, variant_bid=7.0,
        sku_pool=['A', 'B', 'C', 'D', 'E', 'F'], duration_days=14,
        db_path=tmp_db,
    )
    a = bx.get_assignments(eid, db_path=tmp_db)
    # 每次相同 (eid, sku) → arm 一致
    for sku in ['A', 'B', 'C']:
        assert bx.assign_arm(eid, sku, db_path=tmp_db) == a[sku]
    arms = list(a.values())
    assert set(arms) == {'control', 'variant'}  # 至少分到两组


def test_create_rejects_same_bid(tmp_db):
    with pytest.raises(ValueError):
        bx.create_experiment('bad', 5.0, 5.0, ['X'], db_path=tmp_db)


def test_evaluate_significant_lift(tmp_db):
    eid = bx.create_experiment('sig', 5.0, 7.0, [f'S{i}' for i in range(20)],
                                db_path=tmp_db)
    assignments = bx.get_assignments(eid, db_path=tmp_db)
    metrics = {}
    for sku, arm in assignments.items():
        if arm == 'variant':
            metrics[sku] = {'impressions': 1000, 'clicks': 50, 'sold_qty': 5}
        else:
            metrics[sku] = {'impressions': 1000, 'clicks': 20, 'sold_qty': 2}
    res = bx.evaluate(eid, metrics, db_path=tmp_db)
    assert res.is_significant is True
    assert res.recommendation == 'adopt_variant'
    assert res.lift_ctr_pct > 100  # variant CTR 5% vs control 2% = +150%


def test_evaluate_inconclusive_low_volume(tmp_db):
    eid = bx.create_experiment('low', 5.0, 7.0, ['A', 'B'], db_path=tmp_db)
    metrics = {'A': {'impressions': 50, 'clicks': 5, 'sold_qty': 0},
               'B': {'impressions': 50, 'clicks': 1, 'sold_qty': 0}}
    res = bx.evaluate(eid, metrics, db_path=tmp_db, min_total_impressions=1000)
    assert res.is_significant is False
    assert res.recommendation == 'inconclusive'


def test_evaluate_no_assignments_raises(tmp_db):
    with pytest.raises(ValueError):
        bx.evaluate('nope', {}, db_path=tmp_db)


def test_list_experiments_filter(tmp_db):
    e1 = bx.create_experiment('a', 1, 2, ['X'], db_path=tmp_db)
    e2 = bx.create_experiment('b', 1, 3, ['Y'], db_path=tmp_db)
    running = bx.list_experiments(status='running', db_path=tmp_db)
    assert {x['experiment_id'] for x in running} >= {e1, e2}
