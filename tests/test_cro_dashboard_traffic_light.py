"""S50 — traffic-light dashboard tests."""
from __future__ import annotations

from src.services.cro_dashboard_traffic_light import (
    build_dashboard, render_traffic_light,
)


def _const(d):
    return lambda: d


def test_all_green_when_all_signals_low():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 0}),
        alerts_fetcher=_const({'high_priority_count': 0}),
        returns_fetcher=_const({'high_return_count': 0}),
        inventory_fetcher=_const({'throttle_skus': []}),
    )
    assert rep['status_overall'] == 'green'
    assert '平稳' in rep['recommended_action']


def test_red_when_high_returns_present():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 0}),
        alerts_fetcher=_const({'high_priority_count': 0}),
        returns_fetcher=_const({'high_return_count': 8}),
        inventory_fetcher=_const({'throttle_skus': []}),
    )
    assert rep['status_overall'] == 'red'
    assert any(p['name'] == 'returns' and p['status'] == 'red'
               for p in rep['pillars'])


def test_yellow_when_some_throttle_few_returns():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 5}),
        alerts_fetcher=_const({'high_priority_count': 0}),
        returns_fetcher=_const({'high_return_count': 2}),
        inventory_fetcher=_const({'throttle_skus': ['a', 'b']}),
    )
    assert rep['status_overall'] == 'yellow'


def test_red_overall_overrides_yellow():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 35}),
        alerts_fetcher=_const({'high_priority_count': 2}),
        returns_fetcher=_const({'high_return_count': 1}),
        inventory_fetcher=_const({'throttle_skus': []}),
    )
    assert rep['status_overall'] == 'red'
    assert 'approvals' in rep['recommended_action']


def test_funnel_pillar_optional():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 0}),
        alerts_fetcher=_const({'high_priority_count': 0}),
        returns_fetcher=_const({'high_return_count': 0}),
        inventory_fetcher=_const({'throttle_skus': []}),
        funnel_fetcher=_const({'unhealthy_count': 60}),
    )
    funnel_pillars = [p for p in rep['pillars'] if p['name'] == 'funnel']
    assert funnel_pillars and funnel_pillars[0]['status'] == 'red'


def test_render_traffic_light_smoke():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 0}),
        alerts_fetcher=_const({'high_priority_count': 0}),
        returns_fetcher=_const({'high_return_count': 0}),
        inventory_fetcher=_const({'throttle_skus': []}),
    )
    out = render_traffic_light(rep)
    assert 'GREEN' in out
    assert 'approvals' in out


def test_recommended_action_targets_worst_pillar():
    rep = build_dashboard(
        approvals_fetcher=_const({'total': 0}),
        alerts_fetcher=_const({'high_priority_count': 0}),
        returns_fetcher=_const({'high_return_count': 0}),
        inventory_fetcher=_const({'throttle_skus': ['a'] * 12}),
    )
    assert rep['status_overall'] == 'red'
    assert 'inventory' in rep['recommended_action']
    assert '采购' in rep['recommended_action']
