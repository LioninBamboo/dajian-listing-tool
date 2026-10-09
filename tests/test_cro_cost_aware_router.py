"""S79 — cost-aware router tests."""
from __future__ import annotations

from src.services.cro_cost_aware_router import (
    cost_aware_decide, remaining_budget, today_cost_usd,
)


def test_under_budget_falls_through_to_decide_action(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    final, meta = cost_aware_decide(
        'price_drop', 'Kitchen',
        budget_today_usd=10.0,
        cost_lookup=lambda: 1.0,
        state_path=sp, shadow_log=log,
    )
    assert final == 'price_drop'
    assert meta['mode_overridden'] is False
    assert meta['final_source'] == 'rule'  # shadow 模式默认 rule


def test_over_budget_overrides_to_rule(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    final, meta = cost_aware_decide(
        'price_drop', 'Kitchen',
        budget_today_usd=5.0,
        cost_lookup=lambda: 5.0,
        state_path=sp, shadow_log=log,
    )
    assert final == 'price_drop'
    assert meta['mode_overridden'] is True
    assert meta['override_reason'] == 'cost_budget_exceeded'
    assert meta['spent_usd'] == 5.0
    # shadow log not written (no enqueue)
    assert not log.exists()


def test_no_budget_specified_falls_through(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    final, meta = cost_aware_decide(
        'promote', 'Garden',
        state_path=sp, shadow_log=log,
    )
    assert final == 'promote'
    assert meta['mode_overridden'] is False


def test_cost_lookup_exception_treated_as_zero(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'

    def bad():
        raise RuntimeError('boom')

    final, meta = cost_aware_decide(
        'price_drop', 'Kitchen',
        budget_today_usd=10.0,
        cost_lookup=bad,
        state_path=sp, shadow_log=log,
    )
    assert meta['mode_overridden'] is False  # treated as 0 spent


def test_today_cost_usd_empty_log(tmp_path):
    assert today_cost_usd(cost_log=tmp_path / 'none.jsonl') == 0.0


def test_remaining_budget_basic(tmp_path):
    log = tmp_path / 'c.jsonl'
    log.write_text('{"cost_usd": 1.5}\n{"cost_usd": 0.5}\n',
                   encoding='utf-8')
    assert remaining_budget(5.0, cost_log=log) == 3.0


def test_remaining_budget_clamps_to_zero(tmp_path):
    log = tmp_path / 'c.jsonl'
    log.write_text('{"cost_usd": 100}\n', encoding='utf-8')
    assert remaining_budget(5.0, cost_log=log) == 0.0
