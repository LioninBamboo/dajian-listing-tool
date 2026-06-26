#!/usr/bin/env python3
"""
Dajian Scheduler Watchdog — 守护进程看门狗

每 10 分钟由 Windows Task Scheduler 调用，检查守护进程是否存活。
如果死了，自动重启并记录崩溃日志。

使用: pythonw scheduler_watchdog.py  (无窗口)
"""
import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent
PID_FILE = PROJECT_ROOT / 'logs' / '_scheduler.pid'
HEALTH_FILE = PROJECT_ROOT / 'logs' / '_scheduler_health.json'
WATCHDOG_LOG = PROJECT_ROOT / 'logs' / '_watchdog.log'
PYTHON = str(PROJECT_ROOT / '.venv' / 'Scripts' / 'python.exe')
PYTHONW = str(PROJECT_ROOT / '.venv' / 'Scripts' / 'pythonw.exe')
DAEMON_SCRIPT = str(PROJECT_ROOT / 'scheduler_daemon.py')
STALE_HEARTBEAT_MINUTES = 15

# 关键任务巡检表。过了 daemon 的恢复窗口仍未 success → 主动补跑一次。
# 统一改走 scheduler_daemon.py --task，复用同一套防重和健康状态写入。
CRITICAL_DAILY_TASKS = [
        {'health_name': 'daily_tasks', 'deadline': '09:50', 'daemon_task': 'daily'},
        {'health_name': 'mi_self_check', 'deadline': '10:15', 'daemon_task': 'mi_self_check'},
        {'health_name': 'ad_restore', 'deadline': '09:50', 'daemon_task': 'ad_restore'},
        {'health_name': 'blacklist_cleanup', 'deadline': '09:55', 'daemon_task': 'blacklist_cleanup'},
        {'health_name': 'guard_anomaly', 'deadline': '10:00', 'daemon_task': 'guard_anomaly'},
        {'health_name': 'cro_monthly_report', 'deadline': '10:05', 'daemon_task': 'cro_monthly_report'},
        {'health_name': 'cro_consume', 'deadline': '10:30', 'daemon_task': 'cro_consume', 'depends_on': 'daily_tasks'},
        {'health_name': 'smart_bid', 'deadline': '10:30', 'daemon_task': 'smart_bid', 'weekday': 1},
        {'health_name': 'cro_image_refresh', 'deadline': '10:45', 'daemon_task': 'cro_image_refresh', 'depends_on': 'daily_tasks'},
        {'health_name': 'cro_fill_specifics', 'deadline': '10:50', 'daemon_task': 'cro_fill_specifics', 'depends_on': 'daily_tasks'},
        {'health_name': 'cro_promote', 'deadline': '10:55', 'daemon_task': 'cro_promote', 'depends_on': 'daily_tasks'},
        {'health_name': 'cro_sentinel', 'deadline': '11:00', 'daemon_task': 'cro_sentinel', 'depends_on': 'daily_tasks'},
        {'health_name': 'bid_rollback', 'deadline': '11:20', 'daemon_task': 'bid_rollback', 'weekday': 1},
        {'health_name': 'cro_delist_email', 'deadline': '11:20', 'daemon_task': 'cro_delist_email', 'weekday': 0},
        {'health_name': 'listing_audit', 'deadline': '11:45', 'daemon_task': 'listing_audit'},
        {'health_name': 'health_check', 'deadline': '20:10', 'daemon_task': 'health'},
]


def _daemon_task_command(task_alias):
        return [PYTHON, DAEMON_SCRIPT, '--task', task_alias]


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} {msg}\n"
    try:
        with open(WATCHDOG_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def is_pid_alive(pid):
    """Check if PID is alive on Windows."""
    def _tasklist_pid_alive() -> bool:
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return _tasklist_pid_alive()
    except Exception:
        return _tasklist_pid_alive()


def read_health():
    try:
        if HEALTH_FILE.exists():
            return json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        log(f"[WATCHDOG] Failed to read health file: {e}")
    return {}


def get_stale_heartbeat_info(health):
    """Return stale heartbeat diagnostics when daemon health is too old."""
    if not health:
        return None

    alive_at = health.get('daemon_alive_at')
    if not alive_at:
        return None

    try:
        alive_dt = datetime.fromisoformat(alive_at)
    except Exception:
        return None

    age = datetime.now() - alive_dt
    if age <= timedelta(minutes=STALE_HEARTBEAT_MINUTES):
        return None

    daemon_pid = int(health.get('daemon_pid') or 0)
    daemon_task = health.get('tasks', {}).get('_daemon', {})
    daemon_status = daemon_task.get('status') or 'unknown'
    daemon_message = daemon_task.get('message') or ''
    return {
        'pid': daemon_pid,
        'age_minutes': age.total_seconds() / 60.0,
        'status': daemon_status,
        'message': daemon_message[:200],
    }


def stop_pid(pid, reason):
    if not pid or not is_pid_alive(pid):
        return
    try:
        subprocess.run(
            ['taskkill', '/PID', str(pid), '/F', '/T'],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        log(f"[WATCHDOG] Stopped stale daemon PID={pid} ({reason})")
    except Exception as e:
        log(f"[WATCHDOG] Failed to stop stale daemon PID={pid}: {e}")


def check_and_restart():
    daemon_alive = False
    old_pid = None
    health_pid = None
    health = read_health()

    # Check PID file
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            daemon_alive = is_pid_alive(old_pid)
        except Exception:
            pass

    stale = get_stale_heartbeat_info(health)
    if daemon_alive and not stale:
        return  # All good, daemon is running

    if stale:
        stale_pid = stale.get('pid') or old_pid
        log(
            "[WATCHDOG] Detected stale daemon heartbeat "
            f"(PID={stale_pid}, age={stale['age_minutes']:.1f}m, status={stale['status']}, "
            f"message={stale['message']})"
        )
        stop_pid(stale_pid, f"stale heartbeat > {STALE_HEARTBEAT_MINUTES}m")
        daemon_alive = False

    # Fallback: PID file may be missing, but the daemon recorded in health is still alive.
    if not stale:
        try:
            if health:
                health_pid = int(health.get('daemon_pid') or 0)
                if health_pid and is_pid_alive(health_pid):
                    PID_FILE.write_text(str(health_pid), encoding='utf-8')
                    log(f"[WATCHDOG] Restored missing PID file from health record (PID={health_pid})")
                    return
        except Exception as e:
            log(f"[WATCHDOG] Failed to restore PID from health record: {e}")

    # Daemon is dead — log crash
    crash_info = f"PID={old_pid}" if old_pid else ("health PID=" + str(health_pid) if health_pid else "no PID file")
    log(f"[WATCHDOG] Daemon NOT running ({crash_info}), restarting...")

    # Record crash in health file
    try:
        health = {}
        if HEALTH_FILE.exists():
            health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
        crashes = health.get('crashes', [])
        crashes.append({
            'at': datetime.now().isoformat(),
            'old_pid': old_pid,
        })
        # Keep last 20 crash records
        health['crashes'] = crashes[-20:]
        HEALTH_FILE.write_text(
            json.dumps(health, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    except Exception:
        pass

    # Clean up stale PID file
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    # Restart daemon (no window)
    try:
        proc = subprocess.Popen(
            [PYTHONW, DAEMON_SCRIPT],
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
        log(f"[WATCHDOG] Daemon restarted with new PID={proc.pid}")
    except Exception as e:
        log(f"[WATCHDOG] Failed to restart daemon: {e}")


def _task_succeeded_today(health, task_name, today):
    info = _get_task_info(health, task_name)
    if str(info.get('status', '')).lower() not in {'success', 'ok'}:
        return False
    at = info.get('at')
    if not at:
        return False
    try:
        return datetime.fromisoformat(at).date() == today
    except Exception:
        return False


def _task_in_progress_today(health, task_name, today):
    info = _get_task_info(health, task_name)
    if str(info.get('status', '')).lower() not in {'running', 'recovering'}:
        return False
    at = info.get('at')
    if not at:
        return False
    try:
        return datetime.fromisoformat(at).date() == today
    except Exception:
        return False


def _get_task_info(health, task_name):
    if task_name == 'mi_self_check':
        info = dict((health.get('mi_self_check') or {}))
        if info:
            if info.get('checked_at') and not info.get('at'):
                info['at'] = info.get('checked_at')
            return info
    return (health.get('tasks') or {}).get(task_name) or {}


def _record_recovery_in_progress(health, task_name, deadline_hhmm):
    now_iso = datetime.now().isoformat()
    health = health or {}
    health.setdefault('watchdog_recovery', {})[task_name] = {
        'at': now_iso,
        'reason': f'overdue past {deadline_hhmm}',
    }
    health.setdefault('tasks', {})[task_name] = {
        'status': 'recovering',
        'message': f'Watchdog launched recovery after {deadline_hhmm}',
        'at': now_iso,
        'duration_sec': 0.0,
    }
    HEALTH_FILE.write_text(
        json.dumps(health, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return health


def check_overdue_critical_tasks():
    """巡检关键日任务: 过了截止时间还没成功 → 主动补跑一次.

    避免守护进程偶发崩溃 / 单条任务异常导致主线 (CRO) 整天没跑.
    每次 watchdog tick (10 分钟) 至多触发一次补跑, 由 _task_succeeded_today 自防重.
    """
    health = read_health()
    today = datetime.now().date()
    now = datetime.now()
    for task in CRITICAL_DAILY_TASKS:
        name = str(task['health_name'])
        task_alias = str(task['daemon_task'])
        weekday = task.get('weekday')
        if weekday is not None and now.weekday() != int(weekday):
            continue

        deadline_hhmm = str(task['deadline'])
        try:
            hh, mm = (int(x) for x in deadline_hhmm.split(':'))
            deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except Exception:
            continue
        if now < deadline:
            continue
        depends_on = task.get('depends_on')
        if depends_on and not _task_succeeded_today(health, str(depends_on), today):
            continue
        if _task_succeeded_today(health, name, today):
            continue
        if _task_in_progress_today(health, name, today):
            continue
        # 防短时间反复触发: 看 last_recovery_at
        recovery = (health.get('watchdog_recovery') or {}).get(name) or {}
        last = recovery.get('at')
        if last:
            try:
                if datetime.fromisoformat(last).date() == today:
                    continue  # 今天已尝试过补跑
            except Exception:
                pass
        log(f"[WATCHDOG] OVERDUE: {name} 应在 {deadline_hhmm} 前完成, 主动补跑")
        try:
            health = read_health() or {}
            health = _record_recovery_in_progress(health, name, deadline_hhmm)
        except Exception as e:
            log(f"[WATCHDOG] Failed to record recovery state for {name}: {e}")
        try:
            subprocess.Popen(
                _daemon_task_command(task_alias),
                cwd=str(PROJECT_ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        except Exception as e:
            log(f"[WATCHDOG] Failed to launch recovery for {name}: {e}")
            continue


if __name__ == '__main__':
    check_and_restart()
    check_overdue_critical_tasks()
