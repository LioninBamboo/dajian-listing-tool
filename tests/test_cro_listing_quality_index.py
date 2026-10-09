"""S97 — LQI tests."""
from __future__ import annotations

from src.services.cro_listing_quality_index import (
    compute_lqi, find_weakest_dimension, grade_for, score_images,
    score_inventory, score_item_specifics, score_price_position, score_title,
)


def test_score_title_short_zero():
    assert score_title('') == 0
    assert score_title('short') == 40


def test_score_title_optimal():
    assert score_title('a' * 70) == 95


def test_score_title_too_long():
    assert score_title('a' * 110) == 60


def test_score_specs_empty_zero():
    assert score_item_specifics({}) == 0


def test_score_specs_full():
    specs = {f'k{i}': 'v' for i in range(8)}
    assert score_item_specifics(specs) == 100


def test_score_specs_skips_empty_values():
    specs = {'a': 'v', 'b': '', 'c': 'N/A', 'd': None}
    out = score_item_specifics(specs, required_fields=4)
    assert out == 25  # 1/4


def test_score_images_zero():
    assert score_images(0) == 0


def test_score_images_full_count_default_quality():
    out = score_images(6)
    assert out == 88   # 60 + 70*0.4 = 60+28


def test_score_price_position_table():
    assert score_price_position('below_median') == 90
    assert score_price_position('expensive') == 50
    assert score_price_position('unknown') == 60


def test_score_inventory_stockout():
    assert score_inventory(0) == 0


def test_score_inventory_healthy():
    assert score_inventory(20, days_runway=14) == 90


def test_score_inventory_critical_runway():
    assert score_inventory(2, days_runway=2) == 30


def test_grade_for_thresholds():
    assert grade_for(95) == 'A'
    assert grade_for(75) == 'B'
    assert grade_for(60) == 'C'
    assert grade_for(40) == 'D'


def test_compute_lqi_full_pipeline():
    features = {
        'title': 'a' * 70,
        'item_specifics': {f'k{i}': 'v' for i in range(8)},
        'image_count': 6,
        'avg_image_score': 90,
        'price_position': 'below_median',
        'stock': 20,
        'days_runway': 14,
    }
    out = compute_lqi(features)
    assert out['lqi'] >= 85
    assert out['grade'] == 'A'
    assert set(out['parts'].keys()) == {
        'title', 'item_specifics', 'images', 'price_position', 'inventory',
    }


def test_compute_lqi_weak_listing_low_score():
    features = {
        'title': 'short',
        'item_specifics': {},
        'image_count': 1,
        'price_position': 'expensive',
        'stock': 0,
    }
    out = compute_lqi(features)
    assert out['lqi'] < 50
    assert out['grade'] == 'D'


def test_find_weakest_dimension():
    parts = {'title': 95, 'images': 30, 'inventory': 80}
    assert find_weakest_dimension(parts) == 'images'
