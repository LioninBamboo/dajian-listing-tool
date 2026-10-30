"""S125 — alert router tests."""
from __future__ import annotations

from src.services.cro_alert_router import flush_pending, route_alert


def test_critical_sent_immediately(tmp_path):
    sent = []
    def sender(items):
        sent.extend(items)
        return True
    out = route_alert({'severity': 'critical', 'message': 'down'},
                      immediate_sender=sender)
    assert out['action'] == 'sent_immediate'
    assert sent[0]['severity'] == 'critical'


def test_critical_send_failure_marked(tmp_path):
    def bad(items):
        raise RuntimeError('smtp')
    out = route_alert({'severity': 'critical'}, immediate_sender=bad)
    assert out['action'] == 'send_failed'


def test_critical_falls_back_to_hourly_when_no_sender(tmp_path):
    p = str(tmp_path / 'h.jsonl')
    out = route_alert({'severity': 'critical'}, hourly_log_path=p)
    assert out['action'] == 'queued_hourly_fallback'


def test_critical_dropped_no_sender_no_log():
    out = route_alert({'severity': 'critical'})
    assert out['action'] == 'dropped'


def test_high_queued_hourly(tmp_path):
    p = str(tmp_path / 'h.jsonl')
    out = route_alert({'severity': 'high'}, hourly_log_path=p)
    assert out['action'] == 'queued_hourly'
    assert open(p).read().strip() != ''


def test_medium_queued_daily(tmp_path):
    p = str(tmp_path / 'd.jsonl')
    out = route_alert({'severity': 'medium'}, daily_log_path=p)
    assert out['action'] == 'queued_daily'


def test_low_dropped(tmp_path):
    out = route_alert({'severity': 'low'})
    assert out['action'] == 'dropped'


def test_unknown_severity_treated_as_low(tmp_path):
    out = route_alert({'severity': 'weird'})
    assert out['action'] == 'dropped'


def test_flush_pending_sends_and_clears(tmp_path):
    p = str(tmp_path / 'h.jsonl')
    for i in range(3):
        route_alert({'severity': 'high', 'i': i}, hourly_log_path=p)
    sent_batches = []
    def sender(batch):
        sent_batches.append(batch)
        return True
    out = flush_pending(p, sender)
    assert out['count'] == 3
    assert out['sent'] is True
    assert out['cleared'] is True
    assert open(p).read() == ''


def test_flush_send_failure_keeps_file(tmp_path):
    p = str(tmp_path / 'h.jsonl')
    route_alert({'severity': 'high'}, hourly_log_path=p)
    def bad(_):
        return False
    out = flush_pending(p, bad)
    assert out['sent'] is False
    assert open(p).read().strip() != ''
