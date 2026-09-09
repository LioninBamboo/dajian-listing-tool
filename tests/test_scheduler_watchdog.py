from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

WATCHDOG_SPEC = importlib.util.spec_from_file_location('scheduler_watchdog', ROOT / 'scheduler_watchdog.py')
scheduler_watchdog = importlib.util.module_from_spec(WATCHDOG_SPEC)
WATCHDOG_SPEC.loader.exec_module(scheduler_watchdog)


@pytest.fixture(autouse=True)
def isolate_maintenance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scheduler_watchdog,
        'MAINTENANCE_FILE',
        tmp_path / '_no_maintenance.lock',
    )
    # Full-scheduler assertions must not depend on the host store_profile
    # (GrovePop/AquaRides default to ops). Ops-specific tests override this.
    monkeypatch.setattr(scheduler_watchdog, '_is_ops_scheduler', lambda: False)


class FixedMondayNoonDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 5, 11, 12, 0, 0)


class FixedTuesdayNoonDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 5, 12, 12, 0, 0)


def test_watchdog_recovers_full_monday_morning_chain(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'success',
                    'at': '2026-05-11T11:30:00',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert aliases == [
        'mi_self_check',
        'ad_restore',
        'blacklist_cleanup',
        'guard_anomaly',
        'cro_monthly_report',
        'cro_consume',
        'cro_image_refresh',
        'cro_fill_specifics',
        'cro_promote',
        'cro_sentinel',
        'cro_delist_email',
        'listing_audit',
    ]


def test_watchdog_honors_tuesday_only_tasks(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(json.dumps({}, ensure_ascii=False), encoding='utf-8')

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedTuesdayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'smart_bid' in aliases
    assert 'bid_rollback' in aliases
    assert 'cro_delist_email' not in aliases


def test_watchdog_does_not_relaunch_daily_tasks_after_today_timeout(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'timeout',
                    'at': '2026-05-11T12:30:10',
                    'message': 'Killed after 10800s',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'daily' not in aliases
    assert 'cro_consume' not in aliases


def test_watchdog_skips_task_already_running_today(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'running',
                    'at': '2026-05-11T11:59:00',
                    'message': 'already launched by daemon recovery',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'daily' not in aliases
    assert 'cro_consume' not in aliases
    assert 'cro_image_refresh' not in aliases
    assert 'cro_fill_specifics' not in aliases
    assert 'cro_promote' not in aliases
    assert 'cro_sentinel' not in aliases


def test_watchdog_skips_short_tasks_while_daily_tasks_running(tmp_path, monkeypatch):
    """Main-loop daily_tasks occupies the daemon; do not Popen ad_restore etc."""
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'running',
                    'at': '2026-05-11T09:35:00',
                    'message': 'sync still running',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []
    logs = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', logs.append)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert aliases == []
    assert 'ad_restore' not in aliases
    assert 'blacklist_cleanup' not in aliases
    assert 'guard_anomaly' not in aliases
    assert any('daily_tasks in progress; skip overdue recovery' in msg for msg in logs)


def test_watchdog_recovers_short_tasks_after_daily_partial_success(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'partial_success',
                    'at': '2026-05-11T10:20:00',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'ad_restore' in aliases
    assert 'blacklist_cleanup' in aliases
    assert 'guard_anomaly' in aliases


def test_watchdog_skips_mi_self_check_while_mi_snapshot_running(tmp_path, monkeypatch):
    """mi_snapshot still occupies the main loop; do not Popen mi_self_check."""
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'success',
                    'at': '2026-05-11T10:15:00',
                },
                'mi_snapshot': {
                    'status': 'running',
                    'at': '2026-05-11T10:16:00',
                    'message': 'opportunity discovery in progress',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []
    logs = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', logs.append)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'mi_self_check' not in aliases
    assert 'ad_restore' in aliases
    assert any(
        'mi_snapshot in progress; skip mi_self_check recovery' in msg for msg in logs
    )


def test_watchdog_recovers_mi_self_check_after_mi_snapshot_success(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'success',
                    'at': '2026-05-11T10:15:00',
                },
                'mi_snapshot': {
                    'status': 'success',
                    'at': '2026-05-11T10:25:00',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'mi_self_check' in aliases


def test_watchdog_recovers_mi_self_check_when_mi_snapshot_failed(tmp_path, monkeypatch):
    """True missing snapshot should still allow overdue mi_self_check recovery."""
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'success',
                    'at': '2026-05-11T10:15:00',
                },
                'mi_snapshot': {
                    'status': 'failed',
                    'at': '2026-05-11T10:20:00',
                    'message': 'timeout',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'mi_self_check' in aliases


def test_watchdog_records_recovery_as_in_progress(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(json.dumps({}, ensure_ascii=False), encoding='utf-8')

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            pass

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'recovering'
    assert health['watchdog_recovery']['daily_tasks']['at'] == '2026-05-11T12:00:00'


def test_watchdog_uses_top_level_mi_self_check_success(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'mi_self_check': {
                    'status': 'recovering',
                    'at': '2026-05-11T09:46:00',
                    'message': 'old watchdog recovery state',
                },
            },
            'mi_self_check': {
                'checked_at': '2026-05-11T10:06:00',
                'ok': True,
                'status': 'ok',
                'severity': 'info',
                'issues': [],
                'details': {},
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert 'mi_self_check' not in aliases


def test_watchdog_restarts_stale_daemon_without_restoring_old_health_pid(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    pid_path = tmp_path / '_scheduler.pid'
    stale_pid = 35108
    health_path.write_text(
        json.dumps({
            'daemon_pid': stale_pid,
            'daemon_alive_at': '2026-05-11T22:30:00',
            'tasks': {
                '_daemon': {
                    'status': 'running',
                    'message': 'Heartbeat OK | Next: 2026-05-11 22:44:30',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    pid_path.write_text(str(stale_pid), encoding='utf-8')

    launched = []
    stopped = []

    class FakePopen:
        pid = 99999

        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'PID_FILE', pid_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedTuesdayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog, 'is_pid_alive', lambda pid: pid == stale_pid)
    monkeypatch.setattr(scheduler_watchdog, 'stop_pid', lambda pid, reason: stopped.append((pid, reason)))
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)

    scheduler_watchdog.check_and_restart()

    assert stopped == [(stale_pid, 'stale heartbeat > 15m')]
    assert launched == [[scheduler_watchdog.PYTHONW, scheduler_watchdog.DAEMON_SCRIPT]]


def test_watchdog_tick_skips_all_recovery_during_maintenance(tmp_path, monkeypatch):
    maintenance_file = tmp_path / '_maintenance.lock'
    maintenance_file.write_text('database recovery in progress', encoding='utf-8')
    calls = []

    monkeypatch.setattr(scheduler_watchdog, 'MAINTENANCE_FILE', maintenance_file, raising=False)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda message: calls.append(('log', message)))
    monkeypatch.setattr(scheduler_watchdog, 'check_and_restart', lambda: calls.append(('restart', None)))
    monkeypatch.setattr(
        scheduler_watchdog,
        'check_overdue_critical_tasks',
        lambda: calls.append(('recover', None)),
    )

    assert scheduler_watchdog.run_watchdog_tick() is False
    assert [name for name, _ in calls] == ['log']


def test_watchdog_ops_profile_recovers_only_ops_daily(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(json.dumps({}, ensure_ascii=False), encoding='utf-8')

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_watchdog, 'datetime', FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda msg: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, 'Popen', FakePopen)
    monkeypatch.setattr(scheduler_watchdog, '_is_ops_scheduler', lambda: True)

    scheduler_watchdog.check_overdue_critical_tasks()

    aliases = [cmd[-1] for cmd in launched]
    assert aliases == ['ops_daily']
    assert 'daily' not in aliases
    assert 'listing_audit' not in aliases
    assert 'cro_sentinel' not in aliases
    assert 'ad_restore' not in aliases


def test_watchdog_tick_stops_when_maintenance_starts_between_phases(tmp_path, monkeypatch):
    maintenance_file = tmp_path / '_maintenance.lock'
    calls = []

    def restart_then_lock():
        calls.append('restart')
        maintenance_file.write_text('maintenance started', encoding='utf-8')

    monkeypatch.setattr(scheduler_watchdog, 'MAINTENANCE_FILE', maintenance_file)
    monkeypatch.setattr(scheduler_watchdog, 'log', lambda message: calls.append('log'))
    monkeypatch.setattr(scheduler_watchdog, 'check_and_restart', restart_then_lock)
    monkeypatch.setattr(
        scheduler_watchdog,
        'check_overdue_critical_tasks',
        lambda: calls.append('recover'),
    )

    assert scheduler_watchdog.run_watchdog_tick() is False
    assert calls == ['restart', 'log']
