"""S107 — competitor image diff tests."""
from __future__ import annotations

from src.services.cro_competitor_image_diff import (
    differentiation_score, find_most_similar, histogram_distance,
)


def test_distance_identical_zero():
    h = [1, 2, 3, 4]
    assert histogram_distance(h, h, method='euclidean') == 0.0
    assert histogram_distance(h, h, method='chi_square') == 0.0
    assert histogram_distance(h, h, method='cosine') < 1e-9


def test_distance_completely_different_positive():
    a = [1, 0, 0, 0]
    b = [0, 0, 0, 1]
    assert histogram_distance(a, b, method='euclidean') > 0
    assert histogram_distance(a, b, method='cosine') > 0.5


def test_dict_input_supported():
    a = {'r': 10, 'g': 20, 'b': 30}
    b = {'r': 10, 'g': 20, 'b': 30}
    assert histogram_distance(a, b, method='chi_square') == 0.0


def test_align_different_length():
    a = [1, 1]
    b = [1, 1, 0, 0]
    assert histogram_distance(a, b, method='euclidean') == 0.0


def test_intersection_method():
    d = histogram_distance([1, 0], [0, 1], method='intersection')
    assert 0 <= d <= 1


def test_differentiation_score_no_competitors():
    out = differentiation_score([1, 2, 3], [])
    assert out['score'] == 1.0
    assert out['competitor_count'] == 0


def test_differentiation_score_identical_competitors_low():
    target = [1, 2, 3]
    out = differentiation_score(target, [target, target])
    assert out['score'] < 0.05


def test_differentiation_score_distinct_competitors_high():
    target = [10, 0, 0, 0]
    competitors = [[0, 0, 0, 10], [0, 0, 10, 0]]
    out = differentiation_score(target, competitors,
                                 method='cosine')
    assert out['score'] > 0.5


def test_find_most_similar_picks_min_distance():
    target = [1, 0, 0]
    candidates = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    out = find_most_similar(target, candidates)
    assert out['index'] == 1
    assert out['distance'] == 0.0


def test_find_most_similar_empty():
    out = find_most_similar([1], [])
    assert out['index'] == -1
