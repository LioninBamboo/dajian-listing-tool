from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.utils import task_worker
from src.utils.process_identity import get_process_creation_marker


def test_worker_owns_lock_in_same_process_as_target(tmp_path):
    lock_path = tmp_path / 'daily_tasks.lock'
    handshake_path = tmp_path / 'handshake.json'
    health_path = tmp_path / 'health.json'
    observed_path = tmp_path / 'observed.json'
    target_path = tmp_path / 'target.py'
    target_path.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "lock = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "Path(sys.argv[2]).write_text(json.dumps({'pid': os.getpid(), 'lock': lock}), encoding='utf-8')\n",
        encoding='utf-8',
    )

    marker = get_process_creation_marker(os.getpid())
    result = task_worker.main([
        '--task-name', 'daily_tasks',
        '--lock-path', str(lock_path),
        '--handshake-path', str(handshake_path),
        '--health-path', str(health_path),
        '--owner-token', 'test-owner-token',
        '--timeout-sec', '10800',
        '--parent-pid', str(os.getpid()),
        '--parent-creation-marker', marker or '',
        '--', str(target_path), str(lock_path), str(observed_path),
    ])

    observed = json.loads(observed_path.read_text(encoding='utf-8'))
    handshake = json.loads(handshake_path.read_text(encoding='utf-8'))
    assert result == 0
    assert handshake == {'claimed': True, 'worker_pid': os.getpid()}
    assert observed['pid'] == os.getpid()
    assert observed['lock']['worker_pid'] == os.getpid()
    assert observed['lock']['worker_creation_marker'] == marker
    assert observed['lock']['owner_token'] == 'test-owner-token'
    assert not lock_path.exists()


def test_worker_refuses_to_run_when_live_worker_owns_lock(tmp_path):
    lock_path = tmp_path / 'daily_tasks.lock'
    handshake_path = tmp_path / 'handshake.json'
    health_path = tmp_path / 'health.json'
    target_output = tmp_path / 'must-not-exist.txt'
    target_path = tmp_path / 'target.py'
    target_path.write_text(
        "from pathlib import Path\nPath(__import__('sys').argv[1]).write_text('ran')\n",
        encoding='utf-8',
    )
    lock_path.write_text(
        json.dumps({
            'task_name': 'daily_tasks',
            'pid': os.getpid(),
            'worker_pid': os.getpid(),
            'worker_creation_marker': get_process_creation_marker(os.getpid()),
            'owner_token': 'existing-worker',
            'started_at': task_worker.datetime.now().isoformat(),
            'timeout_sec': 10800,
        }),
        encoding='utf-8',
    )

    result = task_worker.main([
        '--task-name', 'daily_tasks',
        '--lock-path', str(lock_path),
        '--handshake-path', str(handshake_path),
        '--health-path', str(health_path),
        '--owner-token', 'duplicate-worker',
        '--timeout-sec', '10800',
        '--parent-pid', str(os.getpid()),
        '--parent-creation-marker', get_process_creation_marker(os.getpid()) or '',
        '--', str(target_path), str(target_output),
    ])

    handshake = json.loads(handshake_path.read_text(encoding='utf-8'))
    assert result == task_worker.ALREADY_RUNNING_EXIT_CODE
    assert handshake == {'claimed': False, 'worker_pid': os.getpid()}
    assert not target_output.exists()
    assert json.loads(lock_path.read_text(encoding='utf-8'))['owner_token'] == 'existing-worker'


def test_live_worker_lock_never_expires_only_because_timeout_elapsed(monkeypatch):
    monkeypatch.setattr(task_worker, 'is_process_identity_alive', lambda pid, marker: True)
    old_started_at = datetime.now() - timedelta(days=2)

    assert task_worker._lock_is_stale(
        {
            'worker_pid': 4242,
            'worker_creation_marker': 'same-process-instance',
            'started_at': old_started_at.isoformat(),
            'timeout_sec': 10,
        },
        timeout_sec=10,
        now=datetime.now(),
    ) is False


def test_worker_fails_closed_on_recent_unreadable_lock(tmp_path):
    lock_path = tmp_path / 'daily_tasks.lock'
    handshake_path = tmp_path / 'handshake.json'
    health_path = tmp_path / 'health.json'
    target_output = tmp_path / 'must-not-exist.txt'
    target_path = tmp_path / 'target.py'
    target_path.write_text(
        "from pathlib import Path\nPath(__import__('sys').argv[1]).write_text('ran')\n",
        encoding='utf-8',
    )
    lock_path.write_text('{', encoding='utf-8')

    result = task_worker.main([
        '--task-name', 'daily_tasks',
        '--lock-path', str(lock_path),
        '--handshake-path', str(handshake_path),
        '--health-path', str(health_path),
        '--owner-token', 'duplicate-worker',
        '--timeout-sec', '10',
        '--parent-pid', str(os.getpid()),
        '--parent-creation-marker', get_process_creation_marker(os.getpid()) or '',
        '--', str(target_path), str(target_output),
    ])

    assert result == task_worker.ALREADY_RUNNING_EXIT_CODE
    assert not target_output.exists()
    assert lock_path.read_text(encoding='utf-8') == '{'


@pytest.mark.skipif(sys.platform != 'win32', reason='validates Windows delete-sharing semantics')
def test_windows_live_worker_handle_prevents_lock_deletion(tmp_path):
    lock_path = tmp_path / 'daily_tasks.lock'
    handshake_path = tmp_path / 'handshake.json'
    health_path = tmp_path / 'health.json'
    target_path = tmp_path / 'target.py'
    target_path.write_text('import time\ntime.sleep(30)\n', encoding='utf-8')
    worker_path = Path(task_worker.__file__).resolve()

    proc = subprocess.Popen([
        sys.executable,
        str(worker_path),
        '--task-name', 'daily_tasks',
        '--lock-path', str(lock_path),
        '--handshake-path', str(handshake_path),
        '--health-path', str(health_path),
        '--owner-token', 'live-worker-handle',
        '--timeout-sec', '60',
        '--parent-pid', str(os.getpid()),
        '--parent-creation-marker', get_process_creation_marker(os.getpid()) or '',
        '--', str(target_path),
    ])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not handshake_path.exists():
            time.sleep(0.05)
        assert json.loads(handshake_path.read_text(encoding='utf-8'))['claimed'] is True
        with pytest.raises(PermissionError):
            lock_path.unlink()
    finally:
        worker_pid = json.loads(handshake_path.read_text(encoding='utf-8'))['worker_pid']
        subprocess.run(
            ['taskkill', '/PID', str(worker_pid), '/F', '/T'],
            capture_output=True,
            timeout=5,
        )
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        lock_path.unlink(missing_ok=True)
