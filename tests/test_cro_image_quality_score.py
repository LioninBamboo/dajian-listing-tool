"""S94 — image quality score tests."""
from __future__ import annotations

from src.services.cro_image_quality_score import (
    batch_score, grade_for, score_meta,
)


def test_perfect_image_grade_A():
    out = score_meta({'width': 1600, 'height': 1600,
                       'file_size_kb': 500, 'whitespace_ratio': 0.40})
    assert out['score'] >= 85
    assert out['grade'] == 'A'


def test_below_ebay_minimum_heavy_penalty():
    out = score_meta({'width': 400, 'height': 400, 'file_size_kb': 50})
    assert out['score'] <= 60
    assert any('500px' in i for i in out['issues'])


def test_watermark_penalty():
    out = score_meta({'width': 1600, 'height': 1600,
                       'file_size_kb': 500, 'has_watermark': True})
    assert any('水印' in i for i in out['issues'])
    assert out['score'] <= 70


def test_text_overlay_penalty():
    out = score_meta({'width': 1600, 'height': 1600,
                       'file_size_kb': 500, 'has_text_overlay': True})
    assert any('文字' in i for i in out['issues'])


def test_low_whitespace_penalty():
    out = score_meta({'width': 1600, 'height': 1600,
                       'file_size_kb': 500, 'whitespace_ratio': 0.10})
    assert any('白底' in i for i in out['issues'])


def test_oversized_file_minor_penalty():
    out = score_meta({'width': 1600, 'height': 1600,
                       'file_size_kb': 8000})
    assert any('过大' in i for i in out['issues'])


def test_score_clamped_to_zero():
    out = score_meta({'width': 100, 'height': 100, 'file_size_kb': 10,
                       'has_watermark': True, 'has_text_overlay': True,
                       'whitespace_ratio': 0.05})
    assert out['score'] >= 0
    assert out['grade'] == 'D'


def test_grade_thresholds():
    assert grade_for(95) == 'A'
    assert grade_for(75) == 'B'
    assert grade_for(60) == 'C'
    assert grade_for(30) == 'D'


def test_batch_score_aggregates():
    metas = [
        {'width': 1600, 'height': 1600, 'file_size_kb': 500,
         'whitespace_ratio': 0.40},
        {'width': 400, 'height': 400, 'file_size_kb': 50},
    ]
    out = batch_score(metas)
    assert out['count'] == 2
    assert out['min_grade'] in ('C', 'D')


def test_batch_score_empty():
    out = batch_score([])
    assert out['count'] == 0 if 'count' in out else True
    assert out['avg_score'] == 0
