from __future__ import annotations

import importlib.util
import json
import os
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


@pytest.fixture(scope="module", autouse=True)
def _restore_schedule_module_after_file():
    """Do not leave the collection-time schedule stub in sys.modules."""
    yield
    if sys.modules.get("schedule") is schedule_stub:
        sys.modules.pop("schedule", None)
    loaded = sys.modules.get("scheduler_daemon")
    if loaded is scheduler_daemon:
        sys.modules.pop("scheduler_daemon", None)


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


def test_task_lock_dead_pid_is_stale(monkeypatch):
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    assert scheduler_daemon._task_lock_is_stale(
        {
            'worker_pid': 9101441,
            'started_at': '2026-05-11T09:35:00',
            'timeout_sec': 14400,
        },
        timeout_sec=14400,
        now=FixedNoonDateTime(2026, 5, 11, 12, 47, 30),
    ) is True


def _write_today_dead_lock(
    lock_dir,
    *,
    started_at='2026-05-11T09:30:00',
    pid=555,
    owner_token='crash-owner-token',
):
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / 'daily_tasks.lock').write_text(
        json.dumps({
            'task_name': 'daily_tasks',
            'pid': pid,
            'worker_pid': pid,
            'started_at': started_at,
            'timeout_sec': 14400,
            'owner_token': owner_token,
        }),
        encoding='utf-8',
    )


def test_should_resume_requires_today_dead_pid_lock(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'recovering',
                    'at': '2026-05-11T11:15:00',
                    'message': 'Watchdog launched recovery after 09:50',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is True


def test_should_not_resume_on_recovering_without_lock_evidence(tmp_path, monkeypatch):
    """Watchdog late-first-launch sets recovering; without a today lock, no resume."""
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir()
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'recovering',
                    'at': '2026-05-11T11:15:00',
                    'message': 'Watchdog launched recovery after 09:50',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False
    assert scheduler_daemon._task_already_started_today('daily_tasks') is False


def test_should_not_resume_yesterday_stale_lock(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(json.dumps({'tasks': {}}), encoding='utf-8')
    _write_today_dead_lock(lock_dir, started_at='2026-05-10T09:30:00')
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)

    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is False
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False


def test_should_not_resume_when_worker_lock_still_alive(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir()
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'recovering',
                    'at': '2026-05-11T11:15:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (lock_dir / 'daily_tasks.lock').write_text(
        json.dumps({
            'worker_pid': 777,
            'worker_creation_marker': 'live-777',
            'started_at': '2026-05-11T11:15:00',
            'timeout_sec': 14400,
        }),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(
        scheduler_daemon,
        'is_process_identity_alive',
        lambda pid, marker: pid == 777,
    )

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False


def test_task_daily_full_late_first_launch_runs_full_daily(tmp_path, monkeypatch):
    """recovering with no prior run today must not become --sync-only."""
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir()
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'recovering',
                    'at': '2026-05-11T11:12:00',
                    'message': 'Watchdog launched recovery after 09:50',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    calls = []

    def _fake_run_task(name, cmd, timeout_sec=0, **kwargs):
        calls.append((name, [str(x) for x in cmd], timeout_sec))
        return True, 'Success in 12s'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    scheduler_daemon.task_daily_full()

    assert len(calls) == 1
    assert calls[0][0] == 'daily_tasks'
    assert '--sync-only' not in calls[0][1]
    assert calls[0][1][0].endswith('daily_tasks.py')


def test_task_daily_full_resumes_inventory_on_today_dead_lock(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T11:12:00',
                    'message': 'Marked stale after daemon restart',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    calls = []

    def _fake_run_task(name, cmd, timeout_sec=0, **kwargs):
        calls.append((name, [str(x) for x in cmd], timeout_sec))
        return True, 'Success in 12s'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    ok, message = scheduler_daemon.task_daily_full()

    assert ok is True
    assert 'resumed inventory' in message.lower()
    assert len(calls) == 1
    assert '--sync-only' in calls[0][1]
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'resumed_inventory'


def test_task_daily_full_yesterday_stale_lock_runs_full_daily(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(json.dumps({'tasks': {}}), encoding='utf-8')
    _write_today_dead_lock(lock_dir, started_at='2026-05-10T09:30:00')
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    calls = []

    def _fake_run_task(name, cmd, timeout_sec=0, **kwargs):
        calls.append((name, [str(x) for x in cmd], timeout_sec))
        return True, 'Success in 12s'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    scheduler_daemon.task_daily_full()

    assert len(calls) == 1
    assert '--sync-only' not in calls[0][1]


def test_timeout_leftover_lock_does_not_resume(tmp_path, monkeypatch):
    """Daemon timeout leftover (or timeout health) must never re-trigger --sync-only."""
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'timeout',
                    'at': '2026-05-11T12:30:00',
                    'message': 'Killed after 14400s',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir, started_at='2026-05-11T09:30:00')
    # Also cover daemon-kill marking (timeout path writes this).
    lock_path = lock_dir / 'daily_tasks.lock'
    payload = json.loads(lock_path.read_text(encoding='utf-8'))
    payload['killed_by_daemon'] = True
    payload['daemon_kill_reason'] = 'timeout'
    lock_path.write_text(json.dumps(payload), encoding='utf-8')

    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False
    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is False


def test_timeout_health_blocks_resume_even_without_kill_marker(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'timeout',
                    'at': '2026-05-11T12:30:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False


def test_invalidate_lock_requires_matching_owner_token(tmp_path, monkeypatch):
    lock_dir = tmp_path / '_task_locks'
    own = 'own-token-aaa'
    _write_today_dead_lock(lock_dir, owner_token=own)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)

    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is True
    # Missing / foreign token must leave the crash lock for resume.
    assert scheduler_daemon._invalidate_lock_as_daemon_kill('daily_tasks', reason='timeout') is False
    assert (lock_dir / 'daily_tasks.lock').exists()
    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is True
    assert scheduler_daemon._invalidate_lock_as_daemon_kill(
        'daily_tasks', reason='timeout', owner_token='foreign-token'
    ) is False
    assert (lock_dir / 'daily_tasks.lock').exists()
    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is True

    # Own-token timeout => invalidated (no longer crash evidence).
    assert scheduler_daemon._invalidate_lock_as_daemon_kill(
        'daily_tasks', reason='timeout', owner_token=own
    ) is True
    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is False


def test_run_task_unclaimed_handshake_leaves_foreign_lock(tmp_path, monkeypatch):
    """Exit-75 / unclaimed path must never invalidate another worker's lock."""
    import subprocess

    log_dir = tmp_path / 'logs'
    lock_dir = log_dir / '_task_locks'
    health_path = log_dir / '_scheduler_health.json'
    log_dir.mkdir()
    lock_dir.mkdir()
    foreign = {
        'task_name': 'integration_worker',
        'pid': 4242,
        'worker_pid': 4242,
        'owner_token': 'foreign-live-token',
        'started_at': '2026-05-11T12:00:00',
        'timeout_sec': 600,
    }
    lock_path = lock_dir / 'integration_worker.lock'
    lock_path.write_text(json.dumps(foreign), encoding='utf-8')

    class _FakeProc:
        pid = 9999
        returncode = 75

        def poll(self):
            return 75

        def wait(self, timeout=None):
            return 75

        def terminate(self):
            return None

        def kill(self):
            return None

    class _Token:
        hex = 'this-run-owner-token'

    def _popen_writes_unclaimed(*args, **kwargs):
        cmd = args[0] if args else kwargs.get('args')
        hs = None
        for i, part in enumerate(cmd):
            if part == '--handshake-path':
                hs = Path(cmd[i + 1])
                break
        assert hs is not None
        hs.write_text(json.dumps({'claimed': False, 'worker_pid': 0}), encoding='utf-8')
        return _FakeProc()

    monkeypatch.setattr(scheduler_daemon, 'LOG_DIR', log_dir)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, '_notify_failure', lambda *args: None)
    monkeypatch.setattr(scheduler_daemon, '_terminate_task_worker', lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler_daemon.uuid, 'uuid4', lambda: _Token())
    monkeypatch.setattr(subprocess, 'Popen', _popen_writes_unclaimed)

    ok, message = scheduler_daemon.run_task(
        'integration_worker',
        [sys.executable, '-c', 'pass'],
        timeout_sec=5,
    )

    assert ok is True
    assert 'skip' in message.lower() or 'already' in message.lower()
    left = json.loads(lock_path.read_text(encoding='utf-8'))
    assert left['owner_token'] == 'foreign-live-token'
    assert left.get('killed_by_daemon') is not True
    assert lock_path.exists()


def test_never_claimed_pending_marker_allows_one_extra_resume(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    # Simulate reboot right after launch: pending marker, no handshake, dead launcher.
    scheduler_daemon_mod = scheduler_daemon
    monkeypatch.setattr(scheduler_daemon_mod, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon_mod, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon_mod, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon_mod, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon_mod, '_is_pid_alive', lambda pid: False)
    monkeypatch.setattr(
        scheduler_daemon_mod,
        'is_process_identity_alive',
        lambda pid, marker: False,
    )
    day = '20260511'
    marker_path = lock_dir / f'daily_tasks.inventory_resume.{day}.json'
    lock_dir.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({
            'date': day,
            'launch_token': 'old-launch',
            'launched_at': '2026-05-11T09:41:00',
            'prior_status': 'interrupted',
            'outcome': 'pending',
            'handshake_claimed': False,
            'never_claimed_retry_used': False,
            'exit75_retries': 0,
            'launcher_pid': 1,
            'launcher_creation_marker': 'dead-launcher',
        }),
        encoding='utf-8',
    )
    calls = []

    def _fake_run_task(name, cmd, timeout_sec=0, on_worker_claimed=None, **kwargs):
        calls.append([str(x) for x in cmd])
        if on_worker_claimed is not None:
            on_worker_claimed('tok', {'claimed': True, 'worker_pid': 1})
        return True, 'Success in 3s'

    monkeypatch.setattr(scheduler_daemon_mod, 'run_task', _fake_run_task)

    assert scheduler_daemon_mod._should_resume_daily_inventory('daily_tasks') is True
    ok1, _ = scheduler_daemon_mod.task_daily_full()
    assert ok1 is True
    assert len(calls) == 1
    marker = scheduler_daemon_mod._read_inventory_resume_marker('daily_tasks')
    assert marker['never_claimed_retry_used'] is True or marker['outcome'] == 'success'
    # After success, further resume is blocked even with dead lock left behind.
    _write_today_dead_lock(lock_dir, pid=777)
    health = json.loads(health_path.read_text(encoding='utf-8'))
    health['tasks']['daily_tasks']['status'] = 'interrupted'
    health_path.write_text(json.dumps(health), encoding='utf-8')
    # Force marker back to never-claimed-retry-used pending without handshake for block check.
    marker['outcome'] = 'pending'
    marker['handshake_claimed'] = False
    marker['never_claimed_retry_used'] = True
    marker['launcher_pid'] = 1
    scheduler_daemon_mod._write_inventory_resume_marker('daily_tasks', marker)
    assert scheduler_daemon_mod._should_resume_daily_inventory('daily_tasks') is False
    ok2, msg2 = scheduler_daemon_mod.task_daily_full()
    assert ok2 is True
    assert len(calls) == 1
    assert 'claim lost' in msg2.lower() or 'already' in msg2.lower() or 'consumed' in msg2.lower()


def test_claimed_marker_blocks_extra_resume(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / 'daily_tasks.inventory_resume.20260511.json').write_text(
        json.dumps({
            'date': '20260511',
            'launch_token': 'claimed-launch',
            'launched_at': '2026-05-11T09:41:00',
            'outcome': 'claimed',
            'handshake_claimed': True,
            'never_claimed_retry_used': False,
            'launcher_pid': 1,
        }),
        encoding='utf-8',
    )
    assert scheduler_daemon._inventory_resume_already_consumed('daily_tasks') is True
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False


def test_concurrent_resume_marker_claim_one_winner(tmp_path, monkeypatch):
    import os
    import threading

    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    # Launcher for the O_EXCL winner is this test process — keep it "alive" so the
    # loser cannot fall through into the never-claimed retry path.
    self_pid = os.getpid()
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: pid == self_pid)
    monkeypatch.setattr(
        scheduler_daemon,
        'is_process_identity_alive',
        lambda pid, marker: pid == self_pid,
    )

    results = []

    def _claim(token):
        results.append(
            scheduler_daemon._try_claim_inventory_resume_marker(
                'daily_tasks', token, prior_status='interrupted'
            )
        )

    t1 = threading.Thread(target=_claim, args=('token-a',))
    t2 = threading.Thread(target=_claim, args=('token-b',))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    winners = [claimed for claimed, _ in results if claimed]
    assert len(winners) == 1
    losers = [claimed for claimed, _ in results if not claimed]
    assert len(losers) == 1
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker['launch_token'] in {'token-a', 'token-b'}
    assert marker.get('never_claimed_retry_used') is not True


def test_concurrent_never_claimed_retry_one_winner(tmp_path, monkeypatch):
    import threading

    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    monkeypatch.setattr(
        scheduler_daemon,
        'is_process_identity_alive',
        lambda pid, marker: False,
    )
    (lock_dir / 'daily_tasks.inventory_resume.20260511.json').write_text(
        json.dumps({
            'date': '20260511',
            'launch_token': 'old',
            'launched_at': '2026-05-11T09:41:00',
            'outcome': 'pending',
            'handshake_claimed': False,
            'never_claimed_retry_used': False,
            'launcher_pid': 1,
            'launcher_creation_marker': 'dead',
        }),
        encoding='utf-8',
    )
    results = []

    def _claim(token):
        results.append(
            scheduler_daemon._try_claim_inventory_resume_marker(
                'daily_tasks', token, prior_status='interrupted'
            )
        )

    t1 = threading.Thread(target=_claim, args=('retry-a',))
    t2 = threading.Thread(target=_claim, args=('retry-b',))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    winners = [claimed for claimed, _ in results if claimed]
    assert len(winners) == 1
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker['never_claimed_retry_used'] is True
    assert marker['launch_token'] in {'retry-a', 'retry-b'}


def test_crash_lock_resumes_exactly_once_second_heartbeat_skips(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    calls = []

    def _fake_run_task(name, cmd, timeout_sec=0, **kwargs):
        calls.append([str(x) for x in cmd])
        return True, 'Success in 12s'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is True
    ok1, _ = scheduler_daemon.task_daily_full()
    assert ok1 is True
    assert len(calls) == 1
    assert '--sync-only' in calls[0]

    # Leave a dead lock behind (as if sync finished but lock file lingered).
    _write_today_dead_lock(lock_dir, pid=999)
    # Health is resumed_inventory; even with a dead lock, no second resume.
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False
    ok2, msg2 = scheduler_daemon.task_daily_full()
    assert ok2 is True
    assert 'already started' in msg2.lower()
    assert len(calls) == 1


def test_task_daily_full_resume_exit_75_leaves_status_unchanged(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                    'message': 'partial before crash',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: (True, scheduler_daemon._RESUME_LOCK_SKIP_MESSAGE),
    )

    ok, message = scheduler_daemon.task_daily_full()

    assert ok is True
    assert message == scheduler_daemon._RESUME_LOCK_SKIP_MESSAGE
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'interrupted'
    assert health['tasks']['daily_tasks']['at'] == '2026-05-11T09:40:00'
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker['outcome'] == 'exit75'
    assert marker['exit75_retries'] == 1


def test_exit75_resume_allows_bounded_retry_after_backoff(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    calls = []

    def _fake_run_task(*args, **kwargs):
        calls.append('run')
        return True, scheduler_daemon._RESUME_LOCK_SKIP_MESSAGE

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    scheduler_daemon.task_daily_full()
    assert len(calls) == 1
    # Immediate second attempt blocked by backoff.
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False

    # Pretend backoff elapsed.
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    marker['last_exit75_at'] = '2026-05-11T11:00:00'
    scheduler_daemon._write_inventory_resume_marker('daily_tasks', marker)

    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is True
    scheduler_daemon.task_daily_full()
    assert len(calls) == 2
    marker2 = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker2['exit75_retries'] == 2

    # Exhaust retries.
    marker2['exit75_retries'] = scheduler_daemon._RESUME_EXIT75_MAX
    marker2['last_exit75_at'] = '2026-05-11T11:00:00'
    marker2['outcome'] = 'exit75'
    scheduler_daemon._write_inventory_resume_marker('daily_tasks', marker2)
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False


def test_resume_handshake_timeout_keeps_status_for_bounded_retry(tmp_path, monkeypatch):
    """Resume handshake timeout must not write health=failed (blocks retry + full daily)."""
    import subprocess

    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                    'message': 'partial before crash',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir, owner_token='crash-owner-token')
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'LOG_DIR', log_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    monkeypatch.setattr(scheduler_daemon, '_notify_failure', lambda *a, **k: None)
    monkeypatch.setattr(scheduler_daemon, '_terminate_task_worker', lambda *a, **k: None)
    invalidate_calls = []
    monkeypatch.setattr(
        scheduler_daemon,
        '_invalidate_lock_as_daemon_kill',
        lambda *a, **k: invalidate_calls.append((a, k)) or False,
    )

    class _FakeProc:
        pid = 4242
        returncode = 1

        def poll(self):
            # Exit immediately so handshake wait ends without sleeping 15s.
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            return None

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: _FakeProc())
    monkeypatch.setattr(scheduler_daemon.time, 'sleep', lambda *_: None)

    ok, message = scheduler_daemon.run_task(
        'daily_tasks',
        [sys.executable, '-c', 'pass'],
        timeout_sec=5,
        preserve_health_on_handshake_miss=True,
    )
    assert ok is False
    assert 'handshake timed out' in message.lower()
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'interrupted'
    assert health['tasks']['daily_tasks']['at'] == '2026-05-11T09:40:00'
    # Resume path must not wipe today's crash lock / partial-run evidence.
    assert invalidate_calls == []
    assert (lock_dir / 'daily_tasks.lock').exists()
    assert scheduler_daemon._has_partial_daily_run_evidence_today('daily_tasks') is True

    # Still resume-eligible after a preserve-health handshake miss + exit75 marker.
    scheduler_daemon._write_inventory_resume_marker(
        'daily_tasks',
        {
            'date': '20260511',
            'outcome': 'exit75',
            'exit75_retries': 1,
            'last_exit75_at': '2026-05-11T11:00:00',
            'handshake_claimed': False,
        },
    )
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is True


def test_resume_marker_blocks_full_daily_even_when_recovering(tmp_path, monkeypatch):
    """Today's resume marker counts as started — recovering health must not open full apply."""
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir(parents=True)
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'recovering',
                    'at': '2026-05-11T11:15:00',
                    'message': 'Watchdog launched recovery after 09:50',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (lock_dir / 'daily_tasks.inventory_resume.20260511.json').write_text(
        json.dumps({
            'date': '20260511',
            'outcome': 'exit75',
            'exit75_retries': 1,
            'handshake_claimed': False,
        }),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    calls = []
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *a, **k: calls.append((a, k)) or (True, 'nope'),
    )

    assert scheduler_daemon._task_already_started_today('daily_tasks') is True
    # No dead lock => resume path skipped; marker still blocks full daily.
    ok, message = scheduler_daemon.task_daily_full()
    assert ok is True
    assert calls == []
    assert 'already started' in message.lower()


def test_backoff_alert_does_not_steal_exhausted_alert(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    alerts = []
    monkeypatch.setattr(
        scheduler_daemon,
        '_alert_inventory_resume',
        lambda msg, **kwargs: alerts.append(msg),
    )
    # In backoff window (retry 1, 5s ago; first backoff is 30s).
    scheduler_daemon._write_inventory_resume_marker(
        'daily_tasks',
        {
            'date': '20260511',
            'outcome': 'exit75',
            'exit75_retries': 1,
            'last_exit75_at': '2026-05-11T12:47:25',
            'handshake_claimed': False,
        },
    )
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False
    assert any('backoff' in a.lower() for a in alerts)
    assert not any('exhaust' in a.lower() for a in alerts)

    # Exhaust later — must still be able to fire exhausted alert (separate gate).
    alerts.clear()
    scheduler_daemon._write_inventory_resume_marker(
        'daily_tasks',
        {
            'date': '20260511',
            'outcome': 'exit75',
            'exit75_retries': scheduler_daemon._RESUME_EXIT75_MAX,
            'last_exit75_at': '2026-05-11T11:00:00',
            'handshake_claimed': False,
        },
    )
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False
    assert any('exhaust' in a.lower() for a in alerts)


def test_non75_claim_miss_writes_failed_and_alerts(tmp_path, monkeypatch):
    import subprocess

    log_dir = tmp_path / 'logs'
    lock_dir = log_dir / '_task_locks'
    health_path = log_dir / '_scheduler_health.json'
    log_dir.mkdir()
    lock_dir.mkdir()
    health_path.write_text(json.dumps({'tasks': {}}), encoding='utf-8')
    monkeypatch.setattr(scheduler_daemon, 'LOG_DIR', log_dir)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, '_notify_failure', lambda *a, **k: None)
    monkeypatch.setattr(scheduler_daemon, '_terminate_task_worker', lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(
        scheduler_daemon,
        '_alert_inventory_resume',
        lambda msg, **kwargs: alerts.append(msg),
    )

    class _FakeProc:
        pid = 9999
        returncode = 42

        def poll(self):
            return 42

        def wait(self, timeout=None):
            return 42

        def terminate(self):
            return None

        def kill(self):
            return None

    def _popen_unclaimed(*args, **kwargs):
        cmd = args[0] if args else kwargs.get('args')
        hs = None
        for i, part in enumerate(cmd):
            if part == '--handshake-path':
                hs = Path(cmd[i + 1])
                break
        assert hs is not None
        hs.write_text(json.dumps({'claimed': False, 'worker_pid': 0}), encoding='utf-8')
        return _FakeProc()

    monkeypatch.setattr(subprocess, 'Popen', _popen_unclaimed)

    ok, message = scheduler_daemon.run_task(
        'daily_tasks',
        [sys.executable, '-c', 'pass'],
        timeout_sec=5,
        preserve_health_on_handshake_miss=False,
    )
    assert ok is False
    assert 'handshake failed' in message.lower()
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'failed'
    assert any('handshake failed' in a.lower() for a in alerts)


def test_write_inventory_resume_marker_retries_permission_error(tmp_path, monkeypatch):
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir()
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    scheduler_daemon._RESUME_MARKER_MEMORY.clear()
    calls = {'n': 0}
    real_replace = os.replace

    def _flaky_replace(src, dst):
        calls['n'] += 1
        if calls['n'] < 3:
            raise PermissionError('win32 sharing')
        return real_replace(src, dst)

    monkeypatch.setattr(scheduler_daemon.os, 'replace', _flaky_replace)
    monkeypatch.setattr(scheduler_daemon.time, 'sleep', lambda *_: None)
    assert scheduler_daemon._write_inventory_resume_marker(
        'daily_tasks',
        {'date': '20260511', 'outcome': 'pending'},
    ) is True
    assert calls['n'] == 3
    assert scheduler_daemon._read_inventory_resume_marker('daily_tasks')['outcome'] == 'pending'


def test_marker_write_failure_does_not_launch_resume(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    scheduler_daemon._RESUME_MARKER_MEMORY.clear()
    # Seed exit75 so claim takes the rewrite path (not O_CREAT).
    scheduler_daemon._write_inventory_resume_marker(
        'daily_tasks',
        {
            'date': '20260511',
            'outcome': 'exit75',
            'exit75_retries': 1,
            'last_exit75_at': '2026-05-11T11:00:00',
            'handshake_claimed': False,
        },
    )
    monkeypatch.setattr(
        scheduler_daemon,
        '_write_inventory_resume_marker',
        lambda *a, **k: False,
    )
    calls = []
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *a, **k: calls.append('run') or (True, 'nope'),
    )
    ok, message = scheduler_daemon.task_daily_full()
    assert ok is True
    assert calls == []
    assert 'claim lost' in message.lower() or 'consumed' in message.lower()


def test_stale_pending_marker_allows_bounded_retry_with_alert(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    lock_dir.mkdir(parents=True)
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    # Pending launched well past handshake window; launcher PID still "alive".
    (lock_dir / 'daily_tasks.inventory_resume.20260511.json').write_text(
        json.dumps({
            'date': '20260511',
            'launch_token': 'old',
            'launched_at': '2026-05-11T12:40:00',
            'outcome': 'pending',
            'handshake_claimed': False,
            'never_claimed_retry_used': False,
            'launcher_pid': os.getpid(),
            'launcher_creation_marker': 'alive-marker',
        }),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    self_pid = os.getpid()
    # Crash lock worker (pid 555) is dead; marker launcher (this process) looks alive.
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: pid == self_pid)
    monkeypatch.setattr(
        scheduler_daemon,
        'is_process_identity_alive',
        lambda pid, marker: pid == self_pid,
    )
    scheduler_daemon._RESUME_MARKER_MEMORY.clear()
    alerts = []
    monkeypatch.setattr(
        scheduler_daemon,
        '_alert_inventory_resume',
        lambda msg, **kwargs: alerts.append(msg),
    )
    calls = []

    def _fake_run_task(*args, **kwargs):
        calls.append('run')
        return True, 'Success in 1s'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    assert scheduler_daemon._pending_resume_launch_is_stale(
        scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    ) is True
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is True
    ok, message = scheduler_daemon.task_daily_full()
    assert ok is True
    assert len(calls) == 1
    assert any('stale' in a.lower() or 'never-claimed' in a.lower() for a in alerts)
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker.get('never_claimed_retry_used') is True or marker.get('outcome') == 'success'


def test_resume_non75_claim_miss_keeps_unclaimed_marker_and_alerts(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    scheduler_daemon._RESUME_MARKER_MEMORY.clear()
    alerts = []
    monkeypatch.setattr(
        scheduler_daemon,
        '_alert_inventory_resume',
        lambda msg, **kwargs: alerts.append(msg),
    )

    def _fake_run_task(*args, **kwargs):
        assert kwargs.get('preserve_health_on_handshake_miss') is True
        # Mirror run_task resume-path claim-miss alert.
        scheduler_daemon._emit_resume_alert_once(
            'daily_tasks',
            scheduler_daemon._RESUME_ALERT_KIND_CLAIM_MISS,
            'daily_tasks: worker lock handshake failed (exit=42)',
        )
        return False, 'Worker lock handshake failed (exit=42)'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    ok, message = scheduler_daemon.task_daily_full()
    assert ok is True
    assert message == scheduler_daemon._RESUME_LOCK_SKIP_MESSAGE
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'interrupted'
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker['handshake_claimed'] is False
    assert marker['outcome'] == 'exit75'
    assert any('handshake failed' in a.lower() for a in alerts)


def test_resume_handshake_timeout_exhaust_alerts_no_full_daily(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    alerts = []
    monkeypatch.setattr(
        scheduler_daemon,
        '_alert_inventory_resume',
        lambda msg, **kwargs: alerts.append(msg),
    )
    # Seed marker just below the exhaust threshold so one more timeout finishes it.
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / 'daily_tasks.inventory_resume.20260511.json').write_text(
        json.dumps({
            'date': '20260511',
            'outcome': 'exit75',
            'exit75_retries': scheduler_daemon._RESUME_EXIT75_MAX - 1,
            'last_exit75_at': '2026-05-11T11:00:00',
            'handshake_claimed': False,
            'never_claimed_retry_used': False,
        }),
        encoding='utf-8',
    )
    calls = []

    def _fake_run_task(*args, **kwargs):
        assert kwargs.get('preserve_health_on_handshake_miss') is True
        calls.append('run')
        return False, 'Worker lock handshake timed out'

    monkeypatch.setattr(scheduler_daemon, 'run_task', _fake_run_task)

    ok, message = scheduler_daemon.task_daily_full()
    assert ok is True
    assert message == scheduler_daemon._RESUME_LOCK_SKIP_MESSAGE
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'interrupted'
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker['exit75_retries'] == scheduler_daemon._RESUME_EXIT75_MAX
    assert marker['outcome'] == 'exit75'
    assert any('exhausted' in a.lower() or 'unsynced' in a.lower() for a in alerts)

    # Exhausted: no more --sync-only, and full daily must not re-apply.
    assert scheduler_daemon._should_resume_daily_inventory('daily_tasks') is False
    calls.clear()
    ok2, msg2 = scheduler_daemon.task_daily_full()
    assert ok2 is True
    assert calls == []
    assert 'already' in msg2.lower() or 'started' in msg2.lower() or 'claim' in msg2.lower()
    assert '--sync-only' not in msg2.lower()


def test_task_daily_full_resume_exit_0_marks_resumed_inventory(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:40:00',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: (True, 'Success in 9s'),
    )

    ok, message = scheduler_daemon.task_daily_full()

    assert ok is True
    assert 'resumed inventory' in message.lower()
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['tasks']['daily_tasks']['status'] == 'resumed_inventory'
    marker = scheduler_daemon._read_inventory_resume_marker('daily_tasks')
    assert marker['outcome'] == 'success'


def test_task_daily_full_skips_completed_success_not_resume(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'success',
                    'at': '2026-05-11T10:00:00',
                    'message': 'OK',
                },
            }
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_ops_scheduler', lambda: False)
    calls = []
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: calls.append(args) or (True, 'nope'),
    )

    result = scheduler_daemon.task_daily_full()

    assert calls == []
    assert result[0] is True
    assert 'already started' in result[1].lower()


def test_recover_missed_tasks_resumes_when_today_dead_lock(tmp_path, monkeypatch):
    health_path = tmp_path / '_scheduler_health.json'
    lock_dir = tmp_path / '_task_locks'
    health_path.write_text(
        json.dumps({
            'tasks': {
                'daily_tasks': {
                    'status': 'interrupted',
                    'at': '2026-05-11T09:45:00',
                    'message': 'Marked stale after daemon restart',
                },
            },
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    _write_today_dead_lock(lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'HEALTH_FILE', health_path)
    monkeypatch.setattr(scheduler_daemon, 'TASK_LOCK_DIR', lock_dir)
    monkeypatch.setattr(scheduler_daemon, 'datetime', FixedNoonDateTime)
    monkeypatch.setattr(scheduler_daemon, '_is_pid_alive', lambda pid: False)
    calls: list[str] = []

    def _record(name: str):
        def runner():
            calls.append(name)
            return True, f'mocked {name}'
        return runner

    def _resume_daily():
        calls.append('daily_tasks_resume')
        return True, 'Resumed inventory after interrupted'

    monkeypatch.setattr(scheduler_daemon, 'task_title_optimize', _record('title_optimize'))
    monkeypatch.setattr(scheduler_daemon, 'task_listing_audit', _record('listing_audit'))
    monkeypatch.setattr(scheduler_daemon, 'task_daily_full', _resume_daily)
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

    assert 'daily_tasks_resume' in calls
    assert 'cro_consume' not in calls
    assert 'cro_promote' not in calls


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


def test_promotion_rotate_args_exclude_email(monkeypatch):
    calls = []

    monkeypatch.setattr(scheduler_daemon, 'run_task', lambda *args, **kwargs: calls.append((args, kwargs)))

    scheduler_daemon.task_promotion_rotate()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == 'promotion_rotate'
    command = [str(part) for part in args[1]]
    assert command[0].endswith('scripts\\auto_rotate_promotions.py') or command[0].endswith(
        'scripts/auto_rotate_promotions.py'
    )
    assert '--email' not in command
    assert kwargs['timeout_sec'] == 1800


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
    assert "80" in cmd
    assert "--email" in cmd
    assert "--fix" in cmd
    assert "categoryId" not in cmd
    assert "Assembly Required" not in cmd


def test_semantic_rewrite_task_uses_daily_limit_and_apply_flags(monkeypatch):
    calls = []

    monkeypatch.setattr(
        scheduler_daemon,
        '_task_succeeded_today',
        lambda name: False,
    )
    monkeypatch.setattr(
        scheduler_daemon,
        '_pick_fresh_daily_live_audit_or_skip',
        lambda name: (__import__('pathlib').Path('listing_audit_fix_today.json'), {'issues': [{'sku': 'X'}]}),
    )
    monkeypatch.setattr(
        scheduler_daemon,
        'run_task',
        lambda *args, **kwargs: calls.append((args, kwargs)) or (True, 'ok'),
    )

    scheduler_daemon.task_semantic_rewrite()

    assert calls, 'run_task should be invoked'
    args, kwargs = calls[0]
    assert args[0] == 'semantic_rewrite'
    cmd = list(args[1])
    assert '--from-daily-audit' in cmd
    assert '--limit' in cmd
    assert cmd[cmd.index('--limit') + 1] == '80'
    assert '--apply' in cmd
    assert '--email' in cmd
    assert kwargs.get('timeout_sec') == 7200
