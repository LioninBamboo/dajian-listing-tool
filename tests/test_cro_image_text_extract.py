"""S112 — image text extract tests."""
from __future__ import annotations

from src.services.cro_image_text_extract import (
    batch_evaluate, evaluate_main_image, find_prohibited, text_area_ratio,
)


def _box(text, x, y, w, h, conf=0.9):
    return {'text': text, 'bbox': [x, y, w, h], 'confidence': conf}


def test_text_area_ratio_zero_image():
    assert text_area_ratio([_box('a', 0, 0, 10, 10)], 0, 0) == 0.0


def test_text_area_ratio_basic():
    boxes = [_box('a', 0, 0, 100, 100)]
    assert text_area_ratio(boxes, 1000, 1000) == 0.01


def test_text_area_ratio_low_confidence_ignored():
    boxes = [_box('a', 0, 0, 500, 500, conf=0.1)]
    assert text_area_ratio(boxes, 1000, 1000) == 0.0


def test_find_prohibited_hits_sale():
    boxes = [_box('Big SALE today', 0, 0, 10, 10)]
    assert 'sale' in find_prohibited(boxes)


def test_find_prohibited_chinese():
    boxes = [_box('限时促销', 0, 0, 10, 10)]
    hits = find_prohibited(boxes)
    assert '限时' in hits or '促销' in hits


def test_find_prohibited_low_confidence_ignored():
    boxes = [_box('SALE', 0, 0, 10, 10, conf=0.1)]
    assert find_prohibited(boxes) == []


def test_evaluate_pass():
    boxes = [_box('Brand', 0, 0, 50, 50)]
    out = evaluate_main_image(boxes, 1000, 1000)
    assert out['verdict'] == 'pass'


def test_evaluate_review_when_medium_text():
    boxes = [_box('text', 0, 0, 400, 400)]   # 16% area
    out = evaluate_main_image(boxes, 1000, 1000)
    assert out['verdict'] == 'review'


def test_evaluate_reject_when_high_text():
    boxes = [_box('lots', 0, 0, 600, 600)]   # 36% area
    out = evaluate_main_image(boxes, 1000, 1000)
    assert out['verdict'] == 'reject'


def test_evaluate_reject_on_prohibited_even_if_small():
    boxes = [_box('SALE 50% OFF', 0, 0, 10, 10)]
    out = evaluate_main_image(boxes, 1000, 1000)
    assert out['verdict'] == 'reject'
    assert 'sale' in out['prohibited_hits']


def test_batch_evaluate_counts_rejects():
    rows = [
        {'sku': 'A', 'width': 1000, 'height': 1000,
         'text_boxes': [_box('SALE', 0, 0, 10, 10)]},
        {'sku': 'B', 'width': 1000, 'height': 1000,
         'text_boxes': [_box('Brand', 0, 0, 50, 50)]},
    ]
    out = batch_evaluate(rows)
    assert out['count'] == 2
    assert out['reject_count'] == 1
