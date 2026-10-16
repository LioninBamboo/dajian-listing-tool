"""S108 — meta optimizer tests."""
from __future__ import annotations

import random

from src.services.cro_meta_optimizer import (
    evaluate_variants, is_significant, posterior_mean, select_variant,
    thompson_sample,
)


def test_posterior_mean_uniform_when_no_data():
    assert abs(posterior_mean({'impressions': 0, 'conversions': 0}) - 0.5) < 1e-9


def test_posterior_mean_high_cvr_high_mean():
    high = {'impressions': 100, 'conversions': 80}
    low = {'impressions': 100, 'conversions': 5}
    assert posterior_mean(high) > posterior_mean(low)


def test_thompson_sample_in_range():
    rng = random.Random(42)
    s = thompson_sample({'impressions': 10, 'conversions': 5}, rng)
    assert 0 <= s <= 1


def test_select_variant_empty():
    out = select_variant([])
    assert out['id'] is None


def test_select_variant_thompson_picks_one():
    rng = random.Random(0)
    variants = [{'id': 'A', 'impressions': 100, 'conversions': 80},
                {'id': 'B', 'impressions': 100, 'conversions': 5}]
    counts = {'A': 0, 'B': 0}
    for _ in range(100):
        out = select_variant(variants, rng=rng)
        counts[out['id']] += 1
    assert counts['A'] > counts['B']  # 高转化的应被选更多次


def test_select_variant_epsilon_greedy_exploit():
    rng = random.Random(0)
    variants = [{'id': 'A', 'impressions': 100, 'conversions': 80},
                {'id': 'B', 'impressions': 100, 'conversions': 5}]
    out = select_variant(variants, strategy='epsilon_greedy',
                          epsilon=0.0, rng=rng)
    assert out['id'] == 'A'
    assert out['reason'] == 'exploit'


def test_is_significant_obvious_winner():
    a = {'id': 'A', 'impressions': 1000, 'conversions': 200}
    b = {'id': 'B', 'impressions': 1000, 'conversions': 50}
    out = is_significant(a, b)
    assert out['significant'] is True
    assert out['winner'] == 'A'


def test_is_significant_small_sample_not_significant():
    a = {'id': 'A', 'impressions': 5, 'conversions': 2}
    b = {'id': 'B', 'impressions': 5, 'conversions': 1}
    out = is_significant(a, b)
    assert out['significant'] is False


def test_evaluate_variants_sorts_by_posterior():
    variants = [
        {'id': 'A', 'impressions': 100, 'conversions': 5},
        {'id': 'B', 'impressions': 100, 'conversions': 80},
    ]
    out = evaluate_variants(variants)
    assert out['winner_id'] == 'B'
    assert out['count'] == 2
    assert out['items'][0]['id'] == 'B'
