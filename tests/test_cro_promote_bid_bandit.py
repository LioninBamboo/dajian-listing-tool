"""S71 — promote bid bandit tests."""
from __future__ import annotations

from src.services.cro_promote_bid_bandit import (
    BID_LEVELS, LEVEL_TO_PCT, best_bid_for, record_bid_outcome, select_bid,
    stats_for_category,
)


def test_select_bid_default_explores_untried(tmp_path):
    sp = tmp_path / 's.json'
    out = select_bid('Kitchen', state_path=sp)
    assert out['action'] in BID_LEVELS
    assert out['bid_pct'] == LEVEL_TO_PCT[out['action']]
    assert len(out['allowed_levels']) == 4


def test_select_bid_inelastic_caps_levels(tmp_path):
    sp = tmp_path / 's.json'
    out = select_bid('Kitchen', elasticity='inelastic', state_path=sp)
    assert out['bid_pct'] <= 10
    assert 'bid_15' not in out['allowed_levels']
    assert 'bid_20' not in out['allowed_levels']


def test_record_bid_outcome_persists(tmp_path):
    sp = tmp_path / 's.json'
    record_bid_outcome('Kitchen', 'bid_10', 0.5, state_path=sp)
    record_bid_outcome('Kitchen', 'bid_10', 0.7, state_path=sp)
    rows = stats_for_category('Kitchen', state_path=sp)
    bid10 = next(r for r in rows if r['level'] == 'bid_10')
    assert bid10['n'] == 2
    assert abs(bid10['mean'] - 0.6) < 1e-9


def test_record_unknown_level_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        record_bid_outcome('Kitchen', 'bid_99', 0.5, state_path=tmp_path / 's.json')


def test_best_bid_for_picks_highest_mean(tmp_path):
    sp = tmp_path / 's.json'
    for lvl in BID_LEVELS:
        record_bid_outcome('Kitchen', lvl, 0.1, state_path=sp)
    record_bid_outcome('Kitchen', 'bid_10', 1.0, state_path=sp)
    out = best_bid_for('Kitchen', state_path=sp)
    assert out['level'] == 'bid_10'
    assert out['bid_pct'] == 10


def test_best_bid_for_none_when_empty(tmp_path):
    assert best_bid_for('Kitchen', state_path=tmp_path / 's.json') is None


def test_stats_for_category_returns_all_levels(tmp_path):
    rows = stats_for_category('Kitchen', state_path=tmp_path / 's.json')
    assert len(rows) == 4
    assert {r['level'] for r in rows} == set(BID_LEVELS)
    assert all(r['n'] == 0 for r in rows)


def test_state_corruption_recovers(tmp_path):
    sp = tmp_path / 's.json'
    sp.write_text('NOT JSON', encoding='utf-8')
    out = select_bid('Kitchen', state_path=sp)
    assert out['action'] in BID_LEVELS
