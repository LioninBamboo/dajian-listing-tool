"""S74 — RCA hint tests."""
from __future__ import annotations

from src.services.cro_alert_rca import (
    annotate_clusters, diagnose_features, summarise_rca,
)


def test_returns_pct_high_image_old_matches_image_rule():
    out = diagnose_features({'returns_pct': 0.20, 'image_age_days': 60})
    assert out['matched']
    assert '主图' in out['root_cause_hypothesis']
    assert '主图轮换' in out['suggested_action']


def test_returns_high_size_complaints():
    out = diagnose_features({
        'returns_pct': 0.20, 'image_age_days': 5,
        'size_complaint_pct': 0.50,
    })
    assert '尺寸' in out['root_cause_hypothesis']


def test_low_ctr_matches_image_or_title():
    out = diagnose_features({'impressions': 500, 'ctr': 0.002})
    assert 'CTR' in out['root_cause_hypothesis']
    assert 'A/B' in out['suggested_action'] or '标题' in out['suggested_action']


def test_low_cvr_matches_price():
    out = diagnose_features({'ctr': 0.02, 'cvr': 0.001})
    assert '价格' in out['root_cause_hypothesis']


def test_zero_stock_matches():
    out = diagnose_features({'stock': 0})
    assert '断' in out['root_cause_hypothesis'] or '库存' in out['root_cause_hypothesis']


def test_high_ad_low_roi():
    out = diagnose_features({'ad_spend': 200, 'roi': 0.2})
    assert 'ROI' in out['root_cause_hypothesis']


def test_no_match_returns_manual_review():
    out = diagnose_features({})
    assert not out['matched']
    assert out['suggested_action'] == 'manual_review'


def test_invalid_field_type_does_not_crash():
    out = diagnose_features({'returns_pct': 'oops', 'image_age_days': 60})
    # returns_pct invalid string → first rule fails 0/0 numeric, eventually
    # not matched OR hits another rule. Should not raise.
    assert 'root_cause_hypothesis' in out


def test_annotate_clusters_attaches_hypothesis():
    clusters = [
        {'cluster_id': 1, 'features': {'returns_pct': 0.20,
                                        'image_age_days': 60}},
        {'cluster_id': 2, 'features': {}},
    ]
    out = annotate_clusters(clusters)
    assert out[0]['matched'] is True
    assert out[1]['matched'] is False


def test_summarise_rca_counts():
    annotated = [
        {'matched': True, 'root_cause_hypothesis': 'A'},
        {'matched': True, 'root_cause_hypothesis': 'A'},
        {'matched': True, 'root_cause_hypothesis': 'B'},
        {'matched': False, 'root_cause_hypothesis': '无明显模式, 建议人工复核'},
    ]
    s = summarise_rca(annotated)
    assert s['total'] == 4
    assert s['matched_count'] == 3
    assert s['unmatched_count'] == 1
    assert s['by_hypothesis'] == {'A': 2, 'B': 1}
