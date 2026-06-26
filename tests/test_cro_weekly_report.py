"""S53 — 周报 tests."""
from __future__ import annotations

from datetime import date

from src.services.cro_weekly_report import (
    build_weekly_report, current_iso_week_label, render_weekly_text,
    send_weekly_report,
)


def _const(d):
    return lambda: d


def _empty():
    return build_weekly_report(
        '2026-W18',
        cohort_fetcher=_const({}),
        approval_fetcher=_const({}),
        blacklist_fetcher=_const({}),
        returns_fetcher=_const({}),
        inventory_fetcher=_const({}),
    )


def test_build_weekly_lift_calculation():
    rep = build_weekly_report(
        '2026-W18',
        cohort_fetcher=_const({'control_sold': 100, 'treatment_sold': 130}),
        approval_fetcher=_const({'total': 20, 'approved': 16, 'rejected': 2}),
        blacklist_fetcher=_const({'added': 3, 'total': 47}),
        returns_fetcher=_const({'high_return': []}),
        inventory_fetcher=_const({'throttle_count': 5}),
    )
    assert rep['cohort']['lift_pct'] == 0.30
    assert rep['approvals']['approval_rate'] == 0.8


def test_build_weekly_zero_control_no_div_zero():
    rep = _empty()
    assert rep['cohort']['lift_pct'] == 0.0
    assert rep['approvals']['approval_rate'] == 0.0


def test_returns_top10_truncated():
    many = [{'sku': f's{i}', 'return_rate': 0.2,
             'return_count': 10, 'sold_count': 50}
            for i in range(15)]
    rep = build_weekly_report(
        '2026-W18',
        cohort_fetcher=_const({}), approval_fetcher=_const({}),
        blacklist_fetcher=_const({}),
        returns_fetcher=_const({'high_return': many}),
        inventory_fetcher=_const({}),
    )
    assert len(rep['returns_top10']) == 10


def test_render_weekly_smoke():
    rep = build_weekly_report(
        '2026-W18',
        cohort_fetcher=_const({'control_sold': 100, 'treatment_sold': 110}),
        approval_fetcher=_const({'total': 5, 'approved': 5, 'rejected': 0}),
        blacklist_fetcher=_const({'added': 2, 'total': 10}),
        returns_fetcher=_const({'high_return': [
            {'sku': 'X', 'return_rate': 0.25,
             'return_count': 5, 'sold_count': 20},
        ]}),
        inventory_fetcher=_const({'throttle_count': 3}),
    )
    text = render_weekly_text(rep)
    assert 'W18' in text
    assert '10.0%' in text  # lift
    assert 'X' in text


def test_send_weekly_returns_false_without_sender():
    assert send_weekly_report(_empty()) is False


def test_send_weekly_invokes_sender():
    captured: list = []
    ok = send_weekly_report(_empty(),
                            sender=lambda s, b: captured.append((s, b)))
    assert ok
    assert '周报' in captured[0][0]


def test_send_weekly_swallows_sender_error():
    def boom(s, b):
        raise RuntimeError('smtp down')
    assert send_weekly_report(_empty(), sender=boom) is False


def test_iso_week_label_format():
    label = current_iso_week_label(date(2026, 5, 4))
    assert label.startswith('2026-W')
    assert len(label.split('-W')[1]) == 2
