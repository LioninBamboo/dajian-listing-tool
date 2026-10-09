"""S47 — 主图轮换 tests."""
from __future__ import annotations

from src.services.cro_image_ab_rotation import (
    pick_next_idx, schedule_rotation,
)


def test_pick_next_when_no_history_returns_first_untested():
    rec = {'candidates': ['a', 'b', 'c'], 'history': [], 'current_idx': 0}
    assert pick_next_idx(rec) == 0


def test_pick_next_skips_tested_until_all_seen():
    rec = {
        'candidates': ['a', 'b', 'c'],
        'history': [{'idx': 0, 'views': 200, 'sold': 10}],
        'current_idx': 0,
    }
    assert pick_next_idx(rec) == 1


def test_pick_next_returns_winner_after_all_tested():
    rec = {
        'candidates': ['a', 'b'],
        'history': [
            {'idx': 0, 'views': 200, 'sold': 5},
            {'idx': 1, 'views': 200, 'sold': 20},
        ],
        'current_idx': 1,
    }
    assert pick_next_idx(rec) == 1


def test_low_view_image_not_counted_as_tested():
    # idx=1 only got 50 views — under threshold → still considered untested
    rec = {
        'candidates': ['a', 'b'],
        'history': [
            {'idx': 0, 'views': 200, 'sold': 10},
            {'idx': 1, 'views': 50, 'sold': 0},
        ],
        'current_idx': 1,
    }
    assert pick_next_idx(rec) == 1  # still need more samples on idx 1


def test_schedule_rotation_persists_state(tmp_path):
    p = tmp_path / 'rot.jsonl'
    out1 = schedule_rotation('SKU1', ['u1', 'u2'], week_now=10, path=p)
    assert out1['idx'] == 0
    assert out1['image_url'] == 'u1'
    out2 = schedule_rotation('SKU1', ['u1', 'u2'], week_now=11,
                             last_week_metrics={'sold': 5, 'views': 200},
                             path=p)
    # idx 0 已测完 → 应转 idx 1
    assert out2['idx'] == 1
    assert out2['image_url'] == 'u2'


def test_schedule_rotation_picks_winner_after_all_tested(tmp_path):
    p = tmp_path / 'rot.jsonl'
    schedule_rotation('S', ['a', 'b'], week_now=1, path=p)
    schedule_rotation('S', ['a', 'b'], week_now=2,
                      last_week_metrics={'sold': 1, 'views': 200}, path=p)
    out = schedule_rotation('S', ['a', 'b'], week_now=3,
                            last_week_metrics={'sold': 30, 'views': 200},
                            path=p)
    # b 转化率高 → 选 b
    assert out['idx'] == 1
