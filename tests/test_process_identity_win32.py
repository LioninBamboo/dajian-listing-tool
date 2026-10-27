"""Windows PID liveness must never use os.kill(pid, 0)."""

from __future__ import annotations

import os
import types

from src.utils import process_identity
import scheduler_daemon


def test_is_process_identity_alive_win32_never_calls_os_kill(monkeypatch):
    calls = []

    def _forbidden_kill(pid, sig):
        calls.append((pid, sig))
        raise AssertionError('os.kill must not be used on win32')

    monkeypatch.setattr(process_identity.sys, 'platform', 'win32')
    monkeypatch.setattr(process_identity.os, 'kill', _forbidden_kill)
    # OpenProcess unavailable here — marker None; win32 branch must return False
    # without falling through to os.kill.
    monkeypatch.setattr(process_identity, 'get_process_creation_marker', lambda pid: None)

    assert process_identity.is_process_identity_alive(4242) is False
    assert calls == []


def test_scheduler_is_pid_alive_win32_never_calls_os_kill(monkeypatch):
    calls = []

    def _forbidden_kill(pid, sig):
        calls.append((pid, sig))
        raise AssertionError('os.kill must not be used on win32')

    monkeypatch.setattr(scheduler_daemon.sys, 'platform', 'win32')
    monkeypatch.setattr(os, 'kill', _forbidden_kill)
    monkeypatch.setattr(scheduler_daemon.os, 'kill', _forbidden_kill)

    def _fake_run(*_a, **_k):
        return types.SimpleNamespace(stdout='')

    monkeypatch.setattr(scheduler_daemon.subprocess, 'run', _fake_run)
    if not hasattr(scheduler_daemon.subprocess, 'CREATE_NO_WINDOW'):
        monkeypatch.setattr(scheduler_daemon.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)

    # On Linux, ctypes.windll is missing → win32 branch falls to tasklist mock.
    assert scheduler_daemon._is_pid_alive(4242) is False
    assert calls == []
