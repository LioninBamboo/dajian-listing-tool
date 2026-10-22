from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _ScheduleEveryStub:
    def day(self):
        return self

    def monday(self):
        return self

    def thursday(self):
        return self

    def at(self, *_args, **_kwargs):
        return self

    def do(self, *_args, **_kwargs):
        return self

    def tag(self, *_args, **_kwargs):
        return self


schedule_stub = types.SimpleNamespace(
    every=lambda *_args, **_kwargs: _ScheduleEveryStub(),
    clear=lambda *_args, **_kwargs: None,
    run_pending=lambda *_args, **_kwargs: None,
)
sys.modules.setdefault("schedule", schedule_stub)

DAEMON_SPEC = importlib.util.spec_from_file_location('scheduler_daemon', ROOT / 'scheduler_daemon.py')
scheduler_daemon = importlib.util.module_from_spec(DAEMON_SPEC)
DAEMON_SPEC.loader.exec_module(scheduler_daemon)

import pytest


@pytest.fixture(autouse=True)
def force_full_scheduler_profile(monkeypatch):
    """Recovery tests assert the full profile table; force non-ops on host ops stores."""
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)


class FixedNoonDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 5, 11, 12, 47, 30)


def test_clear_stale_running_tasks_preserves_active_task_lock(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir()
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'running',
                    'at': '2026-05-11T12:47:00',
                    'message': 'watchdog recovery is active',
                },
            },
        }),
        encoding='utf-8',
    )
    (lock_dir / 'daily_tasks.lock').write_text(
        json.dumps({
            'task_name': 'daily_tasks',
            'pid': 1111,
            'worker_pid': 4242,
            'worker_creation_marker': 'worker-4242-created-once',
            'started_at': '2026-05-01T12:47:00',
            'timeout_sec': 10,
        }),
        encoding='utf-8',
    )

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    monkeypatch.setattr(
        scheduler_daemon,
        'is_process_identity_alive',
        lambda pid, marker: pid == 4242 and marker == 'worker-4242-created-once',
    )

    scheduler_daemon.clear_stale_running_tasks()

    task = json.loads(health_path.read_text(encoding='utf-8'))['tasks']['daily_tasks']
    assert task['status'] == 'running'
    assert task['message'] == 'watchdog recovery is active'


def test_clear_stale_running_tasks_interrupts_task_without_active_lock(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'running',
                    'at': '2026-05-11T09:30:00',
                    'message': 'old daemon task',
                },
            },
        }),
        encoding='utf-8',
    )

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)

    scheduler_daemon.clear_stale_running_tasks()

    task = json.loads(health_path.read_text(encoding='utf-8'))['tasks']['daily_tasks']
    assert task['status'] == 'interrupted'
    assert 'Marked stale after daemon restart' in task['message']


def test_run_task_uses_worker_owned_lock_and_reports_success(tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    lock_dir = log_dir / '_task_locks'
    health_path = log_dir / '_scheduler_health.json'
    output_path = tmp_path / 'worker-output.txt'
    target_path = tmp_path / 'target.py'
    log_dir.mkdir()
    target_path.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('completed', encoding='utf-8')\n",
        encoding='utf-8',
    )

    monkeypatch.setattr(scheduler_daemon, 'LOG_DIR', log_dir)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, '_notify_failure', lambda *args: None)

    ok, message = scheduler_daemon.run_task(
        'integration_worker',
        [str(target_path), str(output_path)],
        timeout_sec=10,
    )

    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert ok is True
    assert message.startswith('Success in ')
    assert output_path.read_text(encoding='utf-8') == 'completed'
    assert health['tasks']['integration_worker']['status'] == 'success'
    assert not (lock_dir / 'integration_worker.lock').exists()
    assert list(lock_dir.glob('*.handshake.json')) == []


def test_run_task_timeout_kills_actual_worker_and_next_run_reaps_lock(tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    lock_dir = log_dir / '_task_locks'
    health_path = log_dir / '_scheduler_health.json'
    slow_target = tmp_path / 'slow.py'
    quick_target = tmp_path / 'quick.py'
    output_path = tmp_path / 'quick-output.txt'
    log_dir.mkdir()
    slow_target.write_text('import time\ntime.sleep(30)\n', encoding='utf-8')
    quick_target.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('recovered', encoding='utf-8')\n",
        encoding='utf-8',
    )

    monkeypatch.setattr(scheduler_daemon, 'LOG_DIR', log_dir)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, '_notify_failure', lambda *args: None)

    first_ok, first_message = scheduler_daemon.run_task(
        'timeout_worker',
        [str(slow_target)],
        timeout_sec=1,
    )
    second_ok, second_message = scheduler_daemon.run_task(
        'timeout_worker',
        [str(quick_target), str(output_path)],
        timeout_sec=10,
    )

    assert first_ok is False
    assert first_message == 'Timeout after 1s'
    assert second_ok is True
    assert second_message.startswith('Success in ')
    assert output_path.read_text(encoding='utf-8') == 'recovered'
    assert not (lock_dir / 'timeout_worker.lock').exists()


def _stub_recent_recovery_tasks(monkeypatch, record):
    for attr, name in (
        ('task_listing_status_sync', 'listing_status_sync'),
        ('task_mi_snapshot', 'mi_snapshot'),
        ('task_auto_publish', 'auto_publish'),
        ('task_cro_send_offer', 'cro_send_offer'),
        ('task_cro_lifecycle_detect', 'cro_lifecycle_detect'),
        ('task_cro_title_rewrite', 'cro_title_rewrite'),
        ('task_cro_lifecycle_evaluate', 'cro_lifecycle_evaluate'),
        ('task_source_refresh', 'source_refresh'),
        ('task_semantic_rewrite', 'semantic_rewrite'),
    ):
        monkeypatch.setattr(scheduler_daemon, attr, record(name))


def test_recover_missed_tasks_catches_morning_gap_after_noon_restart(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'title_optimize': {
                    'status': 'success',
                    'at': '2026-05-10T21:55:40.364092',
                },
                'daily_tasks': {
                    'status': 'success',
                    'at': '2026-05-10T21:37:40.138205',
                },
                'ad_restore': {
                    'status': 'success',
                    'at': '2026-05-09T10:10:01.886910',
                },
                'health_check': {
                    'status': 'success',
                    'at': '2026-05-10T22:14:10.528127',
                },
            }
        }, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    calls: list[str] = []

    def _record(name: str):
        def runner():
            calls.append(name)
            return True, f'mocked {name}'
        return runner

    def _record_daily_success():
        calls.append('daily_tasks')
        health = json.loads(health_path.read_text(encoding='utf-8'))
        health.setdefault('tasks', {})['daily_tasks'] = {
            'status': 'success',
            'at': '2026-05-11T12:47:30',
        }
        health_path.write_text(json.dumps(health, ensure_ascii=False), encoding='utf-8')
        return True, 'mocked daily_tasks'

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, 'task_title_optimize', _record('title_optimize'))
    monkeypatch.setattr(scheduler_daemon, 'task_listing_audit', _record('listing_audit'))
    monkeypatch.setattr(scheduler_daemon, 'task_daily_full', _record_daily_success)
    monkeypatch.setattr(scheduler_daemon, 'task_mi_self_check', _record('mi_self_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_ad_restore', _record('ad_restore'))
    monkeypatch.setattr(scheduler_daemon, 'task_blacklist_cleanup', _record('blacklist_cleanup'))
    monkeypatch.setattr(scheduler_daemon, 'task_guard_anomaly', _record('guard_anomaly'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_monthly_report', _record('cro_monthly_report'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_consume', _record('cro_consume'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_image_refresh', _record('cro_image_refresh'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_fill_specifics', _record('cro_fill_specifics'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote', _record('cro_promote'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_sentinel', _record('cro_sentinel'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_delist_email', _record('cro_delist_email'))
    monkeypatch.setattr(scheduler_daemon, 'task_health_check', _record('health_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_smart_bid', _record('smart_bid'))
    monkeypatch.setattr(scheduler_daemon, 'task_bid_rollback', _record('bid_rollback'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_learn_thresholds', _record('cro_learn_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote_thresholds', _record('cro_promote_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_ops_snapshot', _record('cro_ops_snapshot'))
    _stub_recent_recovery_tasks(monkeypatch, _record)

    scheduler_daemon.recover_missed_tasks(log_when_clean=False)

    assert 'title_optimize' not in calls
    assert 'daily_tasks' in calls
    assert 'listing_audit' in calls
    assert 'ad_restore' in calls
    assert 'cro_promote' in calls
    assert 'health_check' not in calls


def test_recover_missed_tasks_waits_for_daily_before_cro(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'running',
                    'at': '2026-05-11T09:30:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    calls: list[str] = []

    def _record(name: str):
        def runner():
            calls.append(name)
            return True, f'mocked {name}'
        return runner

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, 'task_title_optimize', _record('title_optimize'))
    monkeypatch.setattr(scheduler_daemon, 'task_listing_audit', _record('listing_audit'))
    monkeypatch.setattr(scheduler_daemon, 'task_daily_full', _record('daily_tasks'))
    monkeypatch.setattr(scheduler_daemon, 'task_mi_self_check', _record('mi_self_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_ad_restore', _record('ad_restore'))
    monkeypatch.setattr(scheduler_daemon, 'task_blacklist_cleanup', _record('blacklist_cleanup'))
    monkeypatch.setattr(scheduler_daemon, 'task_guard_anomaly', _record('guard_anomaly'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_monthly_report', _record('cro_monthly_report'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_consume', _record('cro_consume'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_image_refresh', _record('cro_image_refresh'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_fill_specifics', _record('cro_fill_specifics'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote', _record('cro_promote'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_sentinel', _record('cro_sentinel'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_delist_email', _record('cro_delist_email'))
    monkeypatch.setattr(scheduler_daemon, 'task_health_check', _record('health_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_smart_bid', _record('smart_bid'))
    monkeypatch.setattr(scheduler_daemon, 'task_bid_rollback', _record('bid_rollback'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_learn_thresholds', _record('cro_learn_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote_thresholds', _record('cro_promote_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_ops_snapshot', _record('cro_ops_snapshot'))
    _stub_recent_recovery_tasks(monkeypatch, _record)

    scheduler_daemon.recover_missed_tasks(log_when_clean=False)

    assert 'cro_consume' not in calls
    assert 'cro_image_refresh' not in calls
    assert 'cro_fill_specifics' not in calls
    assert 'cro_promote' not in calls
    assert 'cro_sentinel' not in calls


def test_recover_missed_tasks_does_not_rerun_daily_after_timeout(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'timeout',
                    'at': '2026-05-11T12:30:10',
                    'message': 'Killed after 10800s',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    calls: list[str] = []

    def _record(name: str):
        def runner():
            calls.append(name)
            return True, f'mocked {name}'
        return runner

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, 'task_title_optimize', _record('title_optimize'))
    monkeypatch.setattr(scheduler_daemon, 'task_listing_audit', _record('listing_audit'))
    monkeypatch.setattr(scheduler_daemon, 'task_daily_full', _record('daily_tasks'))
    monkeypatch.setattr(scheduler_daemon, 'task_mi_self_check', _record('mi_self_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_ad_restore', _record('ad_restore'))
    monkeypatch.setattr(scheduler_daemon, 'task_blacklist_cleanup', _record('blacklist_cleanup'))
    monkeypatch.setattr(scheduler_daemon, 'task_guard_anomaly', _record('guard_anomaly'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_monthly_report', _record('cro_monthly_report'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_consume', _record('cro_consume'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_image_refresh', _record('cro_image_refresh'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_fill_specifics', _record('cro_fill_specifics'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote', _record('cro_promote'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_sentinel', _record('cro_sentinel'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_delist_email', _record('cro_delist_email'))
    monkeypatch.setattr(scheduler_daemon, 'task_health_check', _record('health_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_smart_bid', _record('smart_bid'))
    monkeypatch.setattr(scheduler_daemon, 'task_bid_rollback', _record('bid_rollback'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_learn_thresholds', _record('cro_learn_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote_thresholds', _record('cro_promote_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_ops_snapshot', _record('cro_ops_snapshot'))
    _stub_recent_recovery_tasks(monkeypatch, _record)

    scheduler_daemon.recover_missed_tasks(log_when_clean=False)

    assert 'daily_tasks' not in calls
    assert 'cro_consume' not in calls
    assert 'cro_promote' not in calls


def test_task_daily_full_skips_after_today_timeout(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'timeout',
                    'at': '2026-05-11T12:30:10',
                    'message': 'Killed after 10800s',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    calls = []
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: calls.append(args) or (True, 'should not run'),
    )

    result = scheduler_daemon.task_daily_full()

    assert calls == []
    assert result[0] is True
    assert 'already' in result[1].lower() or 'timeout' in result[1].lower()


def test_daily_tasks_timeout_is_four_hours():
    assert scheduler_daemon.TASK_TIMEOUT['daily_tasks'] == 14400


def test_recover_missed_tasks_skips_watchdog_recovery_in_progress(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {},
            'watchdog_recovery': {
                'title_optimize': {
                    'at': '2026-05-11T12:00:00',
                    'reason': 'overdue past 09:10',
                },
            },
        }, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    calls: list[str] = []

    def _record(name: str):
        def runner():
            calls.append(name)
            return True, f'mocked {name}'
        return runner

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, 'task_title_optimize', _record('title_optimize'))
    monkeypatch.setattr(scheduler_daemon, 'task_listing_audit', _record('listing_audit'))
    monkeypatch.setattr(scheduler_daemon, 'task_daily_full', _record('daily_tasks'))
    monkeypatch.setattr(scheduler_daemon, 'task_mi_self_check', _record('mi_self_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_ad_restore', _record('ad_restore'))
    monkeypatch.setattr(scheduler_daemon, 'task_blacklist_cleanup', _record('blacklist_cleanup'))
    monkeypatch.setattr(scheduler_daemon, 'task_guard_anomaly', _record('guard_anomaly'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_monthly_report', _record('cro_monthly_report'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_consume', _record('cro_consume'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_image_refresh', _record('cro_image_refresh'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_fill_specifics', _record('cro_fill_specifics'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote', _record('cro_promote'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_sentinel', _record('cro_sentinel'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_delist_email', _record('cro_delist_email'))
    monkeypatch.setattr(scheduler_daemon, 'task_health_check', _record('health_check'))
    monkeypatch.setattr(scheduler_daemon, 'task_smart_bid', _record('smart_bid'))
    monkeypatch.setattr(scheduler_daemon, 'task_bid_rollback', _record('bid_rollback'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_learn_thresholds', _record('cro_learn_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_promote_thresholds', _record('cro_promote_thresholds'))
    monkeypatch.setattr(scheduler_daemon, 'task_cro_ops_snapshot', _record('cro_ops_snapshot'))
    _stub_recent_recovery_tasks(monkeypatch, _record)

    scheduler_daemon.recover_missed_tasks(log_when_clean=False)

    assert 'title_optimize' not in calls
    assert 'daily_tasks' in calls


def test_title_optimize_task_is_hard_disabled(monkeypatch):
    calls = []

    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('title optimization must not run')),
    )
    monkeypatch.setattr(
        scheduler_daemon,
        'update_health',
        lambda *args, **kwargs: calls.append(args),
    )

    ok, message = scheduler_daemon.task_title_optimize()

    assert ok is True
    assert 'disabled' in message.lower()
    assert calls == [('title_optimize', 'success', message)]


def test_listing_audit_task_runs_live_email_audit(monkeypatch):
    calls = []

    monkeypatch.setattr(scheduler_daemon, '_task_succeeded_today', lambda *args, **kwargs: False)
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_listing_audit()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == 'listing_audit'
    command = [str(part) for part in args[1]]
    assert command[0].endswith('scripts\\audit_fix_active_listings.py') or command[0].endswith('scripts/audit_fix_active_listings.py')
    assert '--live' in command
    assert '--email' in command
    assert '--record-clean-state' in command
    assert '--exit-zero-on-issues' in command
    assert kwargs['timeout_sec'] == scheduler_daemon.TASK_TIMEOUT['listing_audit']


def test_task_mode_health_update_preserves_daemon_pid_and_pid_file(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    pid_path = tmp_path / '_scheduler.pid'
    health_path.write_text(
        json.dumps({
            'daemon_pid': 999,
            'daemon_alive_at': '2026-05-12T08:00:00',
            'tasks': {
                '_daemon': {
                    'status': 'running',
                    'message': 'Heartbeat OK',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    pid_path.write_text('999', encoding='utf-8')

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'PID_FILE', pid_path)
    monkeypatch.setattr(scheduler_daemon.os, 'getpid', lambda: 12345)

    scheduler_daemon.update_health('daily_tasks', 'success', 'OK in 10s', 10)

    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['daemon_pid'] == 999
    assert health['daemon_alive_at'] == '2026-05-12T08:00:00'
    assert health['tasks']['daily_tasks']['status'] == 'success'
    assert pid_path.read_text(encoding='utf-8') == '999'


def test_cro_monthly_gate_records_non_month_start_skip(monkeypatch):
    calls = []

    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(
        scheduler_daemon,
        'update_health',
        lambda *args, **kwargs: calls.append(args),
    )
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('run_task should not be called')),
    )

    ok, message = scheduler_daemon.task_cro_monthly_report()

    assert ok is True
    assert message == 'Skipped (not month start)'
    assert calls == [('cro_monthly_report', 'success', 'Skipped (not month start)')]


def test_task_auto_publish_scopes_to_latest_mi_ready_skus(monkeypatch):
    calls = []

    monkeypatch.setattr(scheduler_daemon, '_task_succeeded_today', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(scheduler_daemon, '_latest_mi_ready_skus', lambda limit, **_kwargs: ['MI-1', 'MI-2'])
    monkeypatch.setenv('ENABLE_MI_AUTO_PUBLISH', '1')
    monkeypatch.setenv('MI_AUTO_PUBLISH_LIMIT', '10')
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_auto_publish()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == 'auto_publish'
    command = [str(part) for part in args[1]]
    assert '--sku-list' in command
    assert command[command.index('--sku-list') + 1] == 'MI-1,MI-2'
    assert '--dry-run' not in command
    assert kwargs['timeout_sec'] == scheduler_daemon.TASK_TIMEOUT['auto_publish']


def test_env_flag_enabled_defaults():
    assert scheduler_daemon._env_flag_enabled('NO_SUCH_FLAG_XYZ', default=True) is True
    assert scheduler_daemon._env_flag_enabled('NO_SUCH_FLAG_XYZ', default=False) is False


def test_task_giga_dropship_push_defaults_to_dry_run(monkeypatch):
    calls = []
    monkeypatch.delenv('ENABLE_GIGA_DROPSHIP_PUSH', raising=False)
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_giga_dropship_push()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == 'giga_dropship_push'
    command = [str(part) for part in args[1]]
    assert '--apply' not in command
    assert kwargs['timeout_sec'] == scheduler_daemon.TASK_TIMEOUT['giga_dropship_push']


def test_task_giga_dropship_push_requires_explicit_enable(monkeypatch):
    calls = []
    monkeypatch.setenv('ENABLE_GIGA_DROPSHIP_PUSH', '1')
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_giga_dropship_push()

    command = [str(part) for part in calls[0][0][1]]
    assert '--apply' in command


def test_task_giga_dropship_sync_defaults_to_dry_run(monkeypatch):
    calls = []
    monkeypatch.delenv('ENABLE_GIGA_EBAY_FULFILL', raising=False)
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_giga_dropship_sync()

    command = [str(part) for part in calls[0][0][1]]
    assert '--apply' not in command


def test_task_giga_dropship_sync_requires_explicit_enable(monkeypatch):
    calls = []
    monkeypatch.setenv('ENABLE_GIGA_EBAY_FULFILL', '1')
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_giga_dropship_sync()

    command = [str(part) for part in calls[0][0][1]]
    assert '--apply' in command


def test_task_finance_sync_invokes_script(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_finance_sync()

    assert calls[0][0][0] == 'finance_sync'
    command = [str(part) for part in calls[0][0][1]]
    assert 'finance_sync_orders.py' in command[0] or command[0].endswith('finance_sync_orders.py')
    assert '--days' in command


def test_latest_mi_ready_skus_reads_top_level_list_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    (tmp_path / 'mi_opportunities_20260511_100000.json').write_text(
        json.dumps(
            [
                {'sku': 'MI-1', 'status': 'READY', 'opportunity_score': 70, 'title': 'Patio Chair'},
                {'sku': 'OLD', 'status': 'PUBLISHED', 'opportunity_score': 90, 'title': 'Patio Chair'},
                {'sku': 'MI-2', 'status': 'READY_TO_PUBLISH', 'opportunity_score': 70, 'title': 'Patio Chair'},
                {'sku': 'MI-1', 'status': 'READY', 'opportunity_score': 70, 'title': 'Patio Chair'},
            ],
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )

    assert scheduler_daemon._latest_mi_ready_skus(10, tmp_path) == ['MI-1', 'MI-2']


def test_source_aspect_autofix_cmd_includes_first_batch_safe_types():
    cmd = scheduler_daemon.build_source_aspect_autofix_cmd(Path("logs/listing_audit_fix_demo.json"))
    text = cmd
    for issue_type in (
        "source_aspect_mismatch",
        "desc_dimension_mismatch",
        "desc_weight_mismatch",
        "wrong_dimension",
        "description_structure_missing_key_features",
        "wrong_weight",
        "missing_weight",
        "missing_dimension",
        "description_raw_source_dump",
        "assembly_description_missing",
        "assembly_status_unsupported",
        "assembly_required_mismatch",
        "non_applicable_aspect",
        "incomplete_title",
    ):
        assert issue_type in text
    for fix_key in (
        "Color",
        "Material",
        "__source_parameter_rebuild__",
        "Item Length",
        "Item Width",
        "Item Height",
        "Item Weight",
        "__desc_needs_update__",
        "__restore_live_description_from_local__",
        "__rebuild_description_from_source__",
        "__assembly_desc_update__",
        "__remove__Assembly Status",
        "Assembly Required",
        "__title__",
    ):
        assert fix_key in text
    assert "categoryId" not in text
    assert "missing_foldable" not in text
    assert "assembly_package_conflict" not in text
    assert "--fix" in text
    assert "--email" in text


def test_missing_video_autofix_cmd_is_capped_and_scoped():
    cmd = scheduler_daemon.build_missing_video_autofix_cmd(Path("logs/listing_audit_fix_demo.json"))
    assert "--issue-type" in cmd
    assert "missing_video" in cmd
    assert "--fix-key" in cmd
    assert "__sync_video__" in cmd
    assert "--limit" in cmd
    assert "30" in cmd
    assert "--email" in cmd
    assert "--fix" in cmd
    assert "categoryId" not in cmd
    assert "Assembly Required" not in cmd
