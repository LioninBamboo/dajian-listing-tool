"""S64 — monthly drill scheduler tests."""
from __future__ import annotations

import json
from datetime import date

from src.services.cro_monthly_drill import (
    run_monthly_drill, should_run_today,
)


def test_should_run_today_first_of_month():
    assert should_run_today(date(2026, 4, 1))


def test_should_run_today_other_days():
    assert not should_run_today(date(2026, 4, 2))
    assert not should_run_today(date(2026, 4, 15))


def _fake_simulator(days, seed, total_reward=15.0):
    return {
        'total_reward': total_reward,
        'days': days,
        'final_best_action': 'promote',
        'traffic_light': 'green',
    }


def test_run_monthly_drill_writes_report_and_history(tmp_path):
    rep = tmp_path / 'r.txt'
    hist = tmp_path / 'h.jsonl'
    rec = run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 15.0),
    )
    assert rep.exists()
    assert hist.exists()
    assert rec['avg_reward'] == round(15.0 / 30, 4)
    assert rec['alerted'] is False
    assert rec['prev_avg_reward'] is None


def test_run_monthly_drill_no_alert_when_improving(tmp_path):
    rep = tmp_path / 'r.txt'
    hist = tmp_path / 'h.jsonl'
    sent = []
    run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 15.0),
    )
    rec2 = run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 18.0),
        alerter=lambda s, b: sent.append((s, b)),
    )
    assert sent == []
    assert rec2['alerted'] is False
    assert rec2['delta_pct'] > 0


def test_run_monthly_drill_alerts_when_drop_exceeds_threshold(tmp_path):
    rep = tmp_path / 'r.txt'
    hist = tmp_path / 'h.jsonl'
    sent = []
    run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 30.0),
    )
    rec2 = run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 24.0),
        alerter=lambda s, b: sent.append((s, b)),
        threshold=0.10,
    )
    assert len(sent) == 1
    assert rec2['alerted']
    assert rec2['delta_pct'] < -10


def test_run_monthly_drill_does_not_alert_within_threshold(tmp_path):
    rep = tmp_path / 'r.txt'
    hist = tmp_path / 'h.jsonl'
    sent = []
    run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 30.0),
    )
    rec2 = run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 28.5),
        alerter=lambda s, b: sent.append((s, b)),
        threshold=0.10,
    )
    assert sent == []
    assert rec2['alerted'] is False


def test_history_file_appended_jsonl(tmp_path):
    hist = tmp_path / 'h.jsonl'
    rep = tmp_path / 'r.txt'
    run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 10.0),
    )
    run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 11.0),
    )
    lines = hist.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert all(json.loads(l)['avg_reward'] for l in lines)


def test_alert_callback_exception_does_not_propagate(tmp_path):
    rep = tmp_path / 'r.txt'
    hist = tmp_path / 'h.jsonl'
    run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 30.0),
    )

    def bad_alerter(s, b):
        raise RuntimeError('smtp down')

    rec = run_monthly_drill(
        report_path=rep, history_path=hist,
        simulator=lambda days, seed: _fake_simulator(days, seed, 20.0),
        alerter=bad_alerter,
    )
    assert rec['alerted'] is False  # 异常吞掉, 标记未告警
