"""S135 — oncall rotation tests."""
from __future__ import annotations

from datetime import date

import pytest

from src.services.cro_oncall_rotation import (
    current_oncall, next_oncall, notify_oncall, schedule_for_window,
)


M = ['alice', 'bob', 'carol']
SD = date(2026, 5, 1)


def test_empty_members_raises():
    with pytest.raises(ValueError):
        current_oncall([], start_date=SD, today=SD)


def test_invalid_rotation_raises():
    with pytest.raises(ValueError):
        current_oncall(M, start_date=SD, today=SD, rotation_days=0)


def test_before_start_returns_none():
    out = current_oncall(M, start_date=SD, today=date(2026, 4, 30))
    assert out['on_call'] is None
    assert out['reason'] == 'before_start'


def test_first_week_alice():
    out = current_oncall(M, start_date=SD, today=date(2026, 5, 3))
    assert out['on_call'] == 'alice'
    assert out['rotation_index'] == 0


def test_second_week_bob():
    out = current_oncall(M, start_date=SD, today=date(2026, 5, 10))
    assert out['on_call'] == 'bob'


def test_wraps_after_full_cycle():
    # 3 weeks → carol, 4th week → alice again
    out = current_oncall(M, start_date=SD, today=date(2026, 5, 22))
    assert out['on_call'] == 'alice'


def test_skip_date_advances_to_next():
    out = current_oncall(M, start_date=SD, today=date(2026, 5, 3),
                          skip_dates=[date(2026, 5, 3)])
    assert out['skipped'] is True
    assert out['on_call'] == 'bob'


def test_next_oncall_after_alice():
    out = next_oncall(M, start_date=SD, today=date(2026, 5, 3))
    assert out['next_on_call'] == 'bob'


def test_next_oncall_before_start_returns_first():
    out = next_oncall(M, start_date=SD, today=date(2026, 4, 1))
    assert out['next_on_call'] == 'alice'


def test_schedule_for_window():
    sch = schedule_for_window(M, start_date=SD, weeks=4)
    assert len(sch) == 4
    assert [s['on_call'] for s in sch] == ['alice', 'bob', 'carol', 'alice']
    assert sch[0]['date'] == '2026-05-01'


def test_notify_oncall_success():
    sent = {}
    def notify(person, msg):
        sent['p'] = person
        sent['m'] = msg
        return True
    out = notify_oncall(M, start_date=SD, today=date(2026, 5, 3),
                         notify_callable=notify, message='heads up')
    assert out['notified'] is True
    assert sent == {'p': 'alice', 'm': 'heads up'}


def test_notify_oncall_no_sender():
    out = notify_oncall(M, start_date=SD, today=date(2026, 5, 3))
    assert out['notified'] is False
