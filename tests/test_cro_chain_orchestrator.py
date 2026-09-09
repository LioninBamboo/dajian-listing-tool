"""S52 — 跨 slice 联动 tests."""
from __future__ import annotations

from src.services.cro_chain_orchestrator import (
    chain_inventory_throttle_to_filter, chain_returns_to_blacklist,
    is_actionable, run_chain,
)


def test_chain_returns_adds_each_to_blacklist():
    added: list = []
    def adder(sku, reason):
        added.append((sku, reason))
    out = chain_returns_to_blacklist({'blacklist_skus': ['A', 'B']}, adder)
    assert out == ['A', 'B']
    assert all('high return' in r for _, r in added)


def test_chain_returns_swallows_exception():
    def boom(sku, reason):
        raise RuntimeError('db down')
    added = chain_returns_to_blacklist({'blacklist_skus': ['A']}, boom)
    assert added == []


def test_throttle_to_filter_returns_set():
    s = chain_inventory_throttle_to_filter({'throttle_skus': ['x', 'y', 'x']})
    assert s == {'x', 'y'}


def test_is_actionable_blocks_blacklist():
    assert not is_actionable('A', {'A'}, set(), 'promote')


def test_is_actionable_blocks_throttle_for_promote_only():
    assert not is_actionable('A', set(), {'A'}, 'promote')
    assert not is_actionable('A', set(), {'A'}, 'price_drop')
    # delist 不被 throttle 影响
    assert is_actionable('A', set(), {'A'}, 'delist')


def test_is_actionable_passes_when_clean():
    assert is_actionable('A', set(), set(), 'promote')


def test_run_chain_aggregates(tmp_path):
    added: list = []
    out = run_chain(
        returns_analyzer=lambda: {
            'high_return_count': 3,
            'blacklist_skus': ['A', 'B'],
        },
        inventory_evaluator=lambda: {'throttle_skus': ['C', 'D']},
        add_blacklist=lambda s, r: added.append(s),
    )
    assert out['returns_high_count'] == 3
    assert out['blacklist_added_count'] == 2
    assert out['throttle_count'] == 2
    assert added == ['A', 'B']


def test_run_chain_no_adder_does_not_blacklist():
    out = run_chain(
        returns_analyzer=lambda: {'blacklist_skus': ['A']},
        inventory_evaluator=lambda: {'throttle_skus': []},
    )
    assert out['blacklist_added'] == []
