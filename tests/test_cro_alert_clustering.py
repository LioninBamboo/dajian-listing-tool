"""S41 \u2014 \u5f02\u5e38\u805a\u7c7b tests."""
from __future__ import annotations

from src.services.cro_alert_clustering import (
    CLUSTER_MIN_SIZE, cluster_alerts, render_cluster_text,
)


def _alerts():
    return [
        # \u96c6\u7fa4 1: BBQ + low_ctr \u00d7 4
        {'sku': 'A1', 'category': 'BBQ', 'root_cause': 'low_ctr', 'severity': 0.3},
        {'sku': 'A2', 'category': 'BBQ', 'root_cause': 'low_ctr', 'severity': 0.4},
        {'sku': 'A3', 'category': 'BBQ', 'root_cause': 'low_ctr', 'severity': 0.5},
        {'sku': 'A4', 'category': 'BBQ', 'root_cause': 'low_ctr', 'severity': 0.6},
        # \u96c6\u7fa4 2: Patio + low_cvr \u00d7 3
        {'sku': 'B1', 'category': 'Patio', 'root_cause': 'low_cvr', 'severity': 0.2},
        {'sku': 'B2', 'category': 'Patio', 'root_cause': 'low_cvr', 'severity': 0.2},
        {'sku': 'B3', 'category': 'Patio', 'root_cause': 'low_cvr', 'severity': 0.2},
        # singleton
        {'sku': 'C1', 'category': 'Tools', 'root_cause': 'low_str', 'severity': 0.9},
    ]


def test_cluster_alerts_groups_by_cat_and_cause():
    rep = cluster_alerts(_alerts())
    assert rep['total_alerts'] == 8
    assert len(rep['clusters']) == 2
    assert rep['singleton_count'] == 1
    keys = {(c['category'], c['root_cause']) for c in rep['clusters']}
    assert ('BBQ', 'low_ctr') in keys


def test_clusters_sorted_by_severity_total():
    rep = cluster_alerts(_alerts())
    # BBQ severity_total = 1.8 vs Patio 0.6 \u2192 BBQ first
    assert rep['clusters'][0]['category'] == 'BBQ'


def test_min_size_filters_small_groups():
    short = [{'sku': 'X', 'category': 'C', 'root_cause': 'r', 'severity': 1}]
    rep = cluster_alerts(short, min_size=CLUSTER_MIN_SIZE)
    assert rep['clusters'] == []
    assert rep['singleton_count'] == 1


def test_severity_falls_back_to_drop_pct():
    items = [
        {'sku': str(i), 'category': 'C', 'root_cause': 'r', 'drop_pct': 0.10}
        for i in range(3)
    ]
    rep = cluster_alerts(items)
    assert rep['clusters'][0]['severity_avg'] == 0.10


def test_top_categories_aggregates():
    rep = cluster_alerts(_alerts())
    cats = {c['category']: c['n'] for c in rep['top_categories']}
    assert cats.get('BBQ') == 4
    assert cats.get('Patio') == 3


def test_render_text_smoke():
    rep = cluster_alerts(_alerts())
    text = render_cluster_text(rep)
    assert 'BBQ/low_ctr' in text
