"""P8 — guard_anomaly_alert tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts import guard_anomaly_alert as ga


def test_evaluate_anomaly_no_trigger():
    history = {f"2026-05-{d:02d}": {'rejects': 5, 'ad_offs': 2} for d in range(1, 9)}
    res = ga.evaluate_anomaly(history, today='2026-05-08')
    assert res['is_anomaly'] is False
    assert res['triggers'] == []


def test_evaluate_anomaly_factor_trigger():
    history = {f"2026-05-{d:02d}": {'rejects': 2, 'ad_offs': 0} for d in range(1, 8)}
    history['2026-05-08'] = {'rejects': 30, 'ad_offs': 0}  # 15× baseline 2
    res = ga.evaluate_anomaly(history, today='2026-05-08')
    assert res['is_anomaly'] is True
    assert any('reject' in t for t in res['triggers'])


def test_evaluate_anomaly_absolute_trigger():
    history = {f"2026-05-{d:02d}": {'rejects': 0, 'ad_offs': 0} for d in range(1, 8)}
    history['2026-05-08'] = {'rejects': 35, 'ad_offs': 0}  # >= ABS_REJECT=30
    res = ga.evaluate_anomaly(history, today='2026-05-08')
    assert res['is_anomaly'] is True
    assert any('绝对阈值' in t for t in res['triggers'])


def test_min_count_filters_low_baseline_noise():
    # baseline avg 0.1, today=3 — factor would say yes but min_count=5 blocks
    history = {f"2026-05-{d:02d}": {'rejects': 0, 'ad_offs': 0} for d in range(1, 8)}
    history['2026-05-02']['rejects'] = 1  # avg ~0.14
    history['2026-05-08'] = {'rejects': 3, 'ad_offs': 0}
    res = ga.evaluate_anomaly(history, today='2026-05-08')
    assert res['is_anomaly'] is False


def test_run_skips_when_already_alerted(tmp_path, monkeypatch):
    today = datetime.now().strftime('%Y-%m-%d')
    (tmp_path / f'guard_anomaly_{today}.json').write_text('{}', encoding='utf-8')
    r = ga.run(send_email=False, log_dir=tmp_path)
    assert r.get('skipped') is True


def test_run_writes_report_on_anomaly(tmp_path, monkeypatch):
    # craft a fake scheduler.log with today's spike
    today = datetime.now().strftime('%Y-%m-%d')
    log = [f"{today} 10:00:00 INFO 🚫 [PRICE GUARD] SKU-{i}: blocked"
           for i in range(40)]
    (tmp_path / 'scheduler.log').write_text('\n'.join(log), encoding='utf-8')
    r = ga.run(send_email=False, log_dir=tmp_path)
    assert r['is_anomaly'] is True
    assert (tmp_path / f'guard_anomaly_{today}.json').exists()
