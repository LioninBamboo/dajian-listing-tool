"""S73 — daily_runner shadow wiring tests."""
from __future__ import annotations

from src.services.cro_daily_runner_shadow_wire import shadow_log_actions


def test_shadow_log_actions_basic(tmp_path):
    sp = tmp_path / 'state.json'
    log = tmp_path / 'shadow.jsonl'
    actions = [
        {'sku': 'A', 'action': 'price_drop', 'category': 'Kitchen'},
        {'sku': 'B', 'action': 'promote', 'category': 'Garden'},
    ]
    out = shadow_log_actions(actions, state_path=sp, shadow_log=log)
    assert out['logged'] == 2
    assert out['skipped'] == 0
    assert out['errors'] == 0
    assert log.exists()


def test_shadow_log_actions_skips_missing_fields(tmp_path):
    sp = tmp_path / 'state.json'
    log = tmp_path / 'shadow.jsonl'
    actions = [
        {'sku': 'A', 'action': 'price_drop'},          # 缺 category
        {'sku': 'B', 'category': 'Kitchen'},           # 缺 action
        {'sku': 'C', 'action': 'promote', 'category': '   '},  # 空白
    ]
    out = shadow_log_actions(actions, state_path=sp, shadow_log=log)
    assert out['logged'] == 0
    assert out['skipped'] == 3


def test_shadow_log_actions_extracts_nested_category(tmp_path):
    sp = tmp_path / 'state.json'
    log = tmp_path / 'shadow.jsonl'
    actions = [
        {'sku': 'A', 'action': 'price_drop',
         'product': {'category': 'Kitchen'}},
    ]
    out = shadow_log_actions(actions, state_path=sp, shadow_log=log)
    assert out['logged'] == 1


def test_shadow_log_actions_empty_iterable(tmp_path):
    out = shadow_log_actions([], state_path=tmp_path / 's.json',
                             shadow_log=tmp_path / 'l.jsonl')
    assert out == {'logged': 0, 'skipped': 0, 'errors': 0, 'records': []}


def test_shadow_log_actions_uses_product_category_alt_field(tmp_path):
    sp = tmp_path / 'state.json'
    log = tmp_path / 'shadow.jsonl'
    out = shadow_log_actions(
        [{'sku': 'A', 'action': 'promote', 'product_category': 'Apparel'}],
        state_path=sp, shadow_log=log,
    )
    assert out['logged'] == 1
