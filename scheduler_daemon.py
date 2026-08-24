#!/usr/bin/env python3
"""
Dajian Listing Tool — 后台调度守护进程

解决的问题:
1. Windows 计划任务 "Interactive Only" → 锁屏就不执行
2. start.bat 每次运行只执行一次 → 无持续调度
3. cmd /k + pause → 窗口堆积不关闭
4. 可在流量高峰前后自动运行关键审计任务

架构:
- 单个 Python 进程 (无窗口, 无 GUI 依赖)
- PID 锁文件防重复启动
- schedule 库内置 cron 调度
- 每个任务独立 subprocess 执行, 超时自动杀死
- 健康状态文件 → Dashboard 可读
- 异常邮件通知

时间表 (可修改):
  09:15  财务同步 (finance_sync_orders)
  09:18  GIGA 推单 (默认 dry-run，显式开关后 apply)
  09:22  GIGA→eBay 运单回写 (默认 dry-run，显式开关后 apply)
  09:30  每日全量任务 (daily_tasks.py，含库存同步+财务再刷+日报)
  11:30  eBay/GIGA live listing 内容审计
  每4h   履约闭环 (push + sync + finance_sync)
  每2h   自动分析 (daily_tasks.py --analyze-only)

使用:
  python scheduler_daemon.py          # 前台运行 (调试)
  pythonw scheduler_daemon.py         # 无窗口后台运行 (推荐)
  python scheduler_daemon.py --once   # 立即执行全部任务一次后退出
"""
import os
import sys
import io
import json
import time
import signal
import logging
import subprocess
import argparse
import atexit
import uuid
from pathlib import Path
from datetime import datetime, timedelta

# Ensure UTF-8 output on Chinese Windows
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if not _stream:
        continue
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
        elif hasattr(_stream, "buffer"):
            setattr(
                sys,
                _stream_name,
                io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
            )
    except Exception:
        pass

import schedule

from src.db.database_safety import validate_runtime_database
from src.utils.process_identity import (
    get_process_creation_marker,
    is_process_identity_alive,
)

# ─── 配置 ───────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
PYTHON_EXE = str(PROJECT_ROOT / '.venv' / 'Scripts' / 'python.exe')
PID_FILE = PROJECT_ROOT / 'logs' / '_scheduler.pid'
HEALTH_FILE = PROJECT_ROOT / 'logs' / '_scheduler_health.json'
MAINTENANCE_FILE = PROJECT_ROOT / 'logs' / '_maintenance.lock'
LOG_DIR = PROJECT_ROOT / 'logs'
TASK_LOCK_DIR = LOG_DIR / '_task_locks'
TASK_WORKER = PROJECT_ROOT / 'src' / 'utils' / 'task_worker.py'
LOG_DIR.mkdir(exist_ok=True)

# 任务超时 (秒)
TASK_TIMEOUT = {
    'title_optimize': 7200,    # 2小时
    'listing_audit': 10800,    # 3小时 (全量 live eBay/GIGA 内容审计)
    'source_aspect_autofix': 7200,   # 2小时 (定向写回,逐 SKU live 调用)
    'daily_tasks': 10800,      # 3小时 (含库存同步 + 智能调价)
    'auto_analyze': 3600,      # 1小时
    'smart_reprice': 7200,     # 2小时
    'ad_restore': 3600,        # 1小时
    'smart_bid': 3600,         # 1小时
    'bid_rollback': 3600,      # 1小时
    'blacklist_cleanup': 1800, # 30分钟
    'guard_anomaly': 600,      # 10分钟
    'health_check': 3600,      # 1小时
    'cro_consume': 5400,
    'cro_image_refresh': 1800,
    'cro_fill_specifics': 1800,       # 90分钟 (CRO 队列消费 + 改价)
    'cro_promote': 1800,
    'cro_send_offer': 900,
    'cro_title_rewrite': 900,
    'cro_ops_snapshot': 1800,
    'cro_lifecycle_detect': 900,
    'cro_lifecycle_evaluate': 900,
    'mi_snapshot': 3600,       # 1小时 (F17 机会发现, 并发查 eBay Browse)
    'listing_status_sync': 1800,  # 30分钟 (ActiveList 全量分页 + 死链协调)
    'auto_publish': 3600,      # 1小时 (READY 草稿刊登; 默认 dry-run)
    'source_refresh': 1800,    # 30分钟 (全量 PUBLISHED 源快照刷新, 批量 detailInfo)
    'order_recheck': 900,      # 15分钟 (出单源复核: GetOrders + 单 SKU 源重抓比对)
    'semantic_rewrite': 3600,  # 1小时 (语义改写管线: 日审增量队列, 默认限 40)
    'finance_sync': 900,       # 15分钟 (eBay 订单 → 财务 PnL 本地表)
    'giga_dropship_push': 1800,  # 30分钟 (PAID 未履约 → GIGA 推单)
    'giga_dropship_sync': 1800,  # 30分钟 (GIGA 运单 → eBay 标发)
}

# Windows execution-state flags. Do not use ES_DISPLAY_REQUIRED: the scheduler
# should keep background tasks alive without keeping the screen visible.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# ─── 日志 ─────────────────────────────────────────────────
log_file = LOG_DIR / 'scheduler_daemon.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8', mode='a'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger('scheduler')

# ─── PID 锁 ──────────────────────────────────────────────

_mutex_handle = None  # 保持 mutex 生命周期
IS_DAEMON_PROCESS = False


def _windows_awake_flags(enable: bool) -> int:
    """Build SetThreadExecutionState flags for background scheduler work."""
    flags = ES_CONTINUOUS
    if enable:
        flags |= ES_SYSTEM_REQUIRED
    return flags


def request_system_awake(enable: bool = True, set_thread_execution_state=None) -> bool:
    """Prevent Windows idle sleep while still allowing the display to turn off."""
    if sys.platform != 'win32':
        return False

    try:
        if set_thread_execution_state is None:
            import ctypes
            set_thread_execution_state = ctypes.windll.kernel32.SetThreadExecutionState

        result = set_thread_execution_state(_windows_awake_flags(enable))
        if result == 0:
            action = "设置" if enable else "释放"
            logger.warning(f"{action} Windows 保持唤醒状态失败 (SetThreadExecutionState returned 0)")
            return False

        if enable:
            logger.info("已请求 Windows 保持系统运行（允许屏幕关闭）")
        else:
            logger.info("已释放 Windows 保持系统运行请求")
        return True
    except Exception as e:
        action = "设置" if enable else "释放"
        logger.warning(f"{action} Windows 保持唤醒状态异常: {e}")
        return False


def ensure_pid_file() -> None:
    """确保 PID 文件存在且与当前进程一致。"""
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        current_pid = str(os.getpid())
        existing_pid = PID_FILE.read_text().strip() if PID_FILE.exists() else ""
        if existing_pid != current_pid:
            PID_FILE.write_text(current_pid, encoding='utf-8')
    except Exception as e:
        logger.warning(f"无法写入 PID 文件: {e}")

def acquire_lock():
    """获取 PID 锁, 防止守护进程重复启动 (Windows Named Mutex)"""
    global _mutex_handle
    import ctypes
    kernel32 = ctypes.windll.kernel32

    # 使用 Windows Named Mutex — 内核级互斥，彻底防止竞争
    MUTEX_NAME = "Global\\DajianSchedulerDaemonMutex"
    ERROR_ALREADY_EXISTS = 183

    _mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    last_err = kernel32.GetLastError()

    if last_err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        logger.error("调度器已在运行 (另一个实例持有 mutex), 退出")
        sys.exit(1)

    if not _mutex_handle:
        logger.error(f"无法创建 mutex (error={last_err}), 退出")
        sys.exit(1)

    # 同时写入 PID 文件 (用于监控/日志)
    ensure_pid_file()
    atexit.register(release_lock)
    logger.info(f"PID 锁已获取: {os.getpid()} (mutex)")


def release_lock():
    """释放 PID 锁和 mutex"""
    global _mutex_handle
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    if _mutex_handle:
        import ctypes
        ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None


def _is_pid_alive(pid):
    """检查 PID 是否存活 (Windows 兼容)"""
    def _tasklist_pid_alive() -> bool:
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=5
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


# ─── 健康状态 ─────────────────────────────────────────────

def update_health(task_name, status, message='', duration_sec=0.0):
    """更新健康状态文件 (Dashboard/监控用)"""
    if IS_DAEMON_PROCESS:
        ensure_pid_file()
    try:
        health = {}
        if HEALTH_FILE.exists():
            health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
    except Exception:
        health = {}

    if IS_DAEMON_PROCESS:
        health['daemon_pid'] = os.getpid()
        health['daemon_alive_at'] = datetime.now().isoformat()
    health.setdefault('tasks', {})
    health['tasks'][task_name] = {
        'status': status,       # 'running', 'success', 'failed', 'timeout'
        'message': message[:500],
        'at': datetime.now().isoformat(),
        'duration_sec': round(duration_sec, 1),
    }

    HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding='utf-8')


def _get_task_health(task_name):
    """读取某任务的最新健康状态。"""
    try:
        if not HEALTH_FILE.exists():
            return {}
        health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
        if task_name == 'mi_self_check':
            info = dict(health.get('mi_self_check') or {})
            if info and 'at' not in info:
                info['at'] = info.get('checked_at')
            return info
        return health.get('tasks', {}).get(task_name, {}) or {}
    except Exception:
        return {}


def _task_succeeded_today(task_name, now=None):
    """判断任务今天是否已经成功执行过（用于防止重复触发）。"""
    now = now or datetime.now()
    info = _get_task_health(task_name)
    if not info:
        return False

    status = str(info.get('status', '')).lower()
    at = info.get('at')
    if status not in {'success', 'ok', 'partial_success', 'completed_with_errors'} or not at:
        return False

    try:
        last_dt = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return False

    return last_dt.date() == now.date()


def _iso_at_or_none(value):
    try:
        if not value:
            return None
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _task_in_progress_or_recovered_today(task_name, scheduled_dt, now=None):
    """Return True when a task is already running or a watchdog recovery was launched today."""
    now = now or datetime.now()
    info = _get_task_health(task_name)
    status = str(info.get('status', '')).lower()
    at_dt = _iso_at_or_none(info.get('at'))
    if (
        status in {'running', 'recovering'}
        and at_dt is not None
        and at_dt.date() == now.date()
        and at_dt >= scheduled_dt
    ):
        return True

    try:
        if not HEALTH_FILE.exists():
            return False
        health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
        recovery = (health.get('watchdog_recovery') or {}).get(task_name) or {}
        recovery_dt = _iso_at_or_none(recovery.get('at'))
        return (
            recovery_dt is not None
            and recovery_dt.date() == now.date()
            and recovery_dt >= scheduled_dt
        )
    except Exception:
        return False


def _task_lock_path(task_name):
    safe_name = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in str(task_name))
    return TASK_LOCK_DIR / f'{safe_name}.lock'


def _task_lock_is_stale(lock_info, timeout_sec, now=None):
    now = now or datetime.now()
    pid = 0
    try:
        pid = int(lock_info.get('worker_pid') or lock_info.get('pid') or 0)
    except (TypeError, ValueError):
        pid = 0

    started_at = _iso_at_or_none(lock_info.get('started_at'))
    creation_marker = lock_info.get('worker_creation_marker')
    if pid:
        if creation_marker:
            return not is_process_identity_alive(pid, creation_marker)
        return not _is_pid_alive(pid)
    if started_at is None:
        return True

    max_age = timedelta(seconds=max(int(timeout_sec) + 600, 1800))
    if now - started_at > max_age:
        return True
    return False


def _task_run_lock_is_active(task_name, now=None):
    """Return True when a per-task lock still represents a live worker."""
    now = now or datetime.now()
    lock_path = _task_lock_path(task_name)
    if not lock_path.exists():
        return False

    timeout_sec = int(TASK_TIMEOUT.get(task_name, 3600))
    try:
        lock_info = json.loads(lock_path.read_text(encoding='utf-8'))
        timeout_sec = int(lock_info.get('timeout_sec') or timeout_sec)
        return not _task_lock_is_stale(lock_info, timeout_sec, now=now)
    except Exception:
        # The lock is created atomically before its JSON payload is written. A
        # daemon can start in that tiny window, so a recent unreadable lock must
        # fail closed instead of incorrectly interrupting the live task.
        try:
            age_sec = max(0.0, now.timestamp() - lock_path.stat().st_mtime)
        except OSError:
            return False
        max_age_sec = max(timeout_sec + 600, 1800)
        return age_sec <= max_age_sec


def clear_stale_running_tasks():
    """Mark leftover running task states as interrupted after a daemon restart."""
    try:
        if not HEALTH_FILE.exists():
            return
        health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
        tasks = health.get('tasks', {})
        now_iso = datetime.now().isoformat()
        changed = False
        for name, info in tasks.items():
            if name.startswith('_'):
                continue
            if info.get('status') == 'running':
                if _task_run_lock_is_active(name):
                    logger.info(f"保留运行中任务状态: {name} 仍持有有效任务锁")
                    continue
                info['status'] = 'interrupted'
                info['message'] = f"Marked stale after daemon restart at {now_iso}"
                info['at'] = now_iso
                info['duration_sec'] = 0.0
                changed = True
        if changed:
            HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        logger.warning(f"清理陈旧任务状态失败: {e}")


# ─── 任务执行器 ──────────────────────────────────────────

def run_task(task_name, cmd_args, timeout_sec=3600):
    """
    在子进程中执行任务

    - 完全独立进程 (不占用守护进程的内存/状态)
    - 超时自动杀死
    - 返回 (success, message)
    """
    cmd_str = ' '.join(cmd_args)
    logger.info(f"{'='*50}")
    logger.info(f"▶ 启动任务: {task_name}")
    logger.info(f"  命令: {cmd_str}")
    logger.info(f"  超时: {timeout_sec}s")

    start_time = time.time()
    owner_token = uuid.uuid4().hex
    handshake_path = TASK_LOCK_DIR / f'.{_task_lock_path(task_name).stem}.{owner_token}.handshake.json'
    parent_creation_marker = get_process_creation_marker(os.getpid()) or ''
    full_cmd = [
        PYTHON_EXE,
        str(TASK_WORKER),
        '--task-name', str(task_name),
        '--lock-path', str(_task_lock_path(task_name)),
        '--handshake-path', str(handshake_path),
        '--health-path', str(HEALTH_FILE),
        '--owner-token', owner_token,
        '--timeout-sec', str(int(timeout_sec)),
        '--parent-pid', str(os.getpid()),
        '--parent-creation-marker', parent_creation_marker,
        '--',
        *cmd_args,
    ]

    # 任务级日志文件
    task_log = LOG_DIR / f'_task_{task_name}.log'
    proc = None
    worker_pid = 0

    try:
        with open(task_log, 'w', encoding='utf-8') as f:
            proc = subprocess.Popen(
                full_cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )

        # The actual worker atomically owns the lock before it executes target
        # code. This avoids a Windows parent-crash leaving an untracked child.
        handshake = None
        handshake_deadline = time.time() + 15
        while time.time() < handshake_deadline:
            if handshake_path.exists():
                try:
                    handshake = json.loads(handshake_path.read_text(encoding='utf-8'))
                    break
                except Exception:
                    pass
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        if not handshake:
            _terminate_task_worker(proc, proc.pid)
            msg = 'Worker lock handshake timed out'
            logger.error(f"❌ 任务启动失败: {task_name}: {msg}")
            update_health(task_name, 'failed', msg, time.time() - start_time)
            return False, msg

        if not handshake.get('claimed'):
            try:
                returncode = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _terminate_task_worker(proc, proc.pid)
                returncode = proc.returncode
            if returncode == 75:
                logger.info(f"↪ 跳过任务 {task_name}: 已有实际工作进程持有任务锁")
                return True, 'Skipped (already running or recovering)'
            msg = f'Worker lock handshake failed (exit={returncode})'
            logger.error(f"❌ 任务启动失败: {task_name}: {msg}")
            update_health(task_name, 'failed', msg, time.time() - start_time)
            return False, msg

        worker_pid = int(handshake.get('worker_pid') or proc.pid)
        update_health(task_name, 'running', f'Started: {cmd_str}')

        # Poll frequently for short tasks while refreshing the daemon heartbeat
        # at the existing 30-second cadence.
        last_heartbeat_at = 0.0
        while True:
            returncode = proc.poll()
            duration = time.time() - start_time

            if returncode is not None:
                break

            if duration >= timeout_sec:
                logger.error(f"⏰ 任务超时 ({timeout_sec}s): {task_name}")
                _terminate_task_worker(proc, worker_pid)
                update_health(task_name, 'timeout', f'Killed after {timeout_sec}s', duration)
                update_health('_daemon', 'running', f'Timeout while waiting for {task_name}')
                _notify_failure(task_name, f'任务超时 ({timeout_sec}秒), 已强制终止')
                return False, f'Timeout after {timeout_sec}s'

            if duration - last_heartbeat_at >= 30:
                update_health('_daemon', 'running', f'Running {task_name} | elapsed {duration:.0f}s')
                last_heartbeat_at = duration
            time.sleep(1)

        duration = time.time() - start_time

        if returncode == 0:
            logger.info(f"✅ 任务完成: {task_name} ({duration:.0f}s)")
            update_health(task_name, 'success', f'OK in {duration:.0f}s', duration)
            return True, f'Success in {duration:.0f}s'
        if returncode == 2:
            msg = 'Completed with follow-up items; targeted review or retry required'
            logger.warning(f"⚠️ 任务部分完成: {task_name} ({duration:.0f}s)")
            update_health(task_name, 'partial_success', msg, duration)
            return True, msg
        else:
            # 读取最后几行日志
            tail = _tail_file(task_log, 5)
            msg = f'Exit code {returncode}: {tail}'
            logger.error(f"❌ 任务失败: {task_name} (code={returncode}, {duration:.0f}s)")
            logger.error(f"  最后输出: {tail}")
            update_health(task_name, 'failed', msg, duration)
            _notify_failure(task_name, msg)
            return False, msg

    except Exception as e:
        duration = time.time() - start_time
        msg = f'Exception: {e}'
        logger.error(f"❌ 任务异常: {task_name}: {e}")
        if proc is not None and proc.poll() is None:
            _terminate_task_worker(proc, worker_pid or proc.pid)
        update_health(task_name, 'failed', msg, duration)
        _notify_failure(task_name, msg)
        return False, msg
    finally:
        try:
            handshake_path.unlink(missing_ok=True)
        except OSError:
            pass


def _terminate_task_worker(proc, worker_pid):
    """Terminate the actual lock-owning worker, not a venv launcher shim."""
    if sys.platform == 'win32':
        try:
            subprocess.run(
                ['taskkill', '/PID', str(int(worker_pid)), '/F', '/T'],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            logger.warning(f"无法终止实际任务进程 PID {worker_pid}: {exc}")
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


def _tail_file(path, n=5):
    """读取文件最后 n 行"""
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').strip().split('\n')
        return '\n'.join(lines[-n:])
    except Exception:
        return '(无法读取日志)'


def _notify_failure(task_name, error_msg):
    """任务失败时发送简短通知邮件"""
    try:
        from src.utils.email_sender import send_email
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        html = f"""
        <h3 style="color:red;">⚠️ Dajian 任务失败: {task_name}</h3>
        <p>时间: {now}</p>
        <pre style="background:#f5f5f5;padding:10px;">{error_msg[:1000]}</pre>
        <p style="color:#999;font-size:12px;">来自 scheduler_daemon.py</p>
        """
        send_email(f"⚠️ {task_name} 失败 - {now}", html)
    except Exception as e:
        logger.warning(f"失败通知邮件发送失败: {e}")


# ─── 预定任务函数 ─────────────────────────────────────────

def task_title_optimize():
    """标题优化任务已硬停，避免继续向 live listing 写入未经审计的新事实。"""
    message = 'Disabled: title optimization is stopped pending hallucination audit'
    logger.warning("↪ 跳过标题优化: %s", message)
    update_health('title_optimize', 'success', message)
    return True, message


def task_listing_audit():
    """只读 eBay/GIGA live listing 内容审计。"""
    if _task_succeeded_today('listing_audit'):
        logger.info("↪ 跳过刊登内容审计: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'listing_audit',
        [
            str(PROJECT_ROOT / 'scripts' / 'audit_fix_active_listings.py'),
            '--live',
            '--email',
            '--record-clean-state',
            '--exit-zero-on-issues',
        ],
        timeout_sec=TASK_TIMEOUT['listing_audit'],
    )


# 12:10 自动修白名单。新增类型/key 必须单独决定;categoryId 永远不在其中。
SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES = (
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
)
SOURCE_ASPECT_AUTOFIX_FIX_KEYS = (
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
)


def build_source_aspect_autofix_cmd(report_path) -> list[str]:
    """Build the scoped live-fix command for the 12:10 source-aspect job."""
    cmd = [
        str(PROJECT_ROOT / "scripts" / "audit_fix_active_listings.py"),
        "--live",
        "--ignore-clean-freeze",
        "--exit-zero-on-issues",
        "--source-report",
        str(report_path),
    ]
    for issue_type in SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES:
        cmd.extend(["--issue-type", issue_type])
    for fix_key in SOURCE_ASPECT_AUTOFIX_FIX_KEYS:
        cmd.extend(["--fix-key", fix_key])
    cmd.append("--fix")
    return cmd


def task_source_aspect_autofix():
    """把 GIGA 源参数写回 live 属性 — 严格限定 fix key。

    12:10,在 11:30 审计之后、12:30 语义改写之前。

    为什么单独一个任务而不是给 listing_audit 加 --fix:审计不传 --fix-key 时
    会应用该 SKU 的【全部】待修项,2026-07-27 就是这样把两条室内软包储物凳
    改判进 Outdoor Daybeds 并 republish 上线的。这里只放确定性、源直供的
    key,categoryId 永远不在其中。

    第一批扩展(2026-08-13):描述尺寸/重量不一致、错误 Item L/W/H、
    缺 KEY FEATURES。
    第二批扩展:错误/缺失 Item Weight、占位尺寸、中文源描述倾倒。
    第三批扩展:描述缺组装句、无来源 Assembly Status、源/几何明确的 Assembly Required、
    不适用属性、截断标题。类目、折叠、package_conflict、semantic_feature 仍不自动修。
    """
    if _task_succeeded_today('source_aspect_autofix'):
        logger.info("↪ 跳过源参数自动修复: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from scripts.semantic_rewrite import pick_latest_full_corpus_audit
        picked = pick_latest_full_corpus_audit(list((PROJECT_ROOT / 'logs').glob('listing_audit_fix_*.json')))
    except Exception as exc:
        logger.warning("↪ 跳过源参数自动修复: 无法定位审计报告 (%s)", exc)
        return True, f'Skipped (no audit report: {exc})'
    if not picked:
        logger.info("↪ 跳过源参数自动修复: 尚无全量审计报告")
        return True, 'Skipped (no full-corpus audit report)'

    report_path, _scores = picked
    run_task(
        'source_aspect_autofix',
        build_source_aspect_autofix_cmd(report_path),
        timeout_sec=TASK_TIMEOUT['source_aspect_autofix'],
    )


def task_source_refresh():
    """源内容刷新: 重抓 GIGA/大建源详情, 检测卖家侧内容漂移并修复本地快照。

    排在 11:30 listing_audit 之前, 让当日审计对比的是供应商当前真实源数据,
    而不是采集时的冻结快照。
    """
    if _task_succeeded_today('source_refresh'):
        logger.info("↪ 跳过源内容刷新: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'source_refresh',
        [
            str(PROJECT_ROOT / 'scripts' / 'source_content_refresh.py'),
            '--email',
        ],
        timeout_sec=TASK_TIMEOUT['source_refresh'],
    )


def task_semantic_rewrite():
    """语义改写闭环: 读当日最新 audit 推导增量队列, 限量 apply 并邮件摘要。

    排在 11:30 listing_audit 之后 (12:30)。双闸仍要求 .env 中
    SEMANTIC_REWRITE_APPLY_ENABLED=1, 否则脚本会拒绝 --apply 并 exit 3。
    """
    if _task_succeeded_today('semantic_rewrite'):
        logger.info("↪ 跳过语义改写: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'semantic_rewrite',
        [
            str(PROJECT_ROOT / 'scripts' / 'semantic_rewrite.py'),
            '--from-daily-audit',
            '--limit', '40',
            '--apply',
            '--email',
        ],
        timeout_sec=TASK_TIMEOUT['semantic_rewrite'],
    )


def task_order_recheck():
    """出单源复核: 新订单 SKU 立即重抓源并比对 live listing 声明, 发货前拦退款。"""
    run_task(
        'order_recheck',
        [
            str(PROJECT_ROOT / 'scripts' / 'order_source_recheck.py'),
            # Wider than the 6h cadence on purpose. A skipped run (2026-07-27
            # 21:xx) and a DNS failure (2026-07-28 09:26) each left a ~12h gap,
            # and an 8h lookback simply never saw the orders inside it. Rechecks
            # are deduped by (order_id, sku), so a wide window costs one extra
            # GetOrders page and re-examines nothing.
            '--hours-back', '48',
            '--email',
        ],
        timeout_sec=TASK_TIMEOUT['order_recheck'],
    )


def _env_flag_enabled(name: str, default: bool = True) -> bool:
    """Parse env feature flags. Empty/unset → default; 0/false/no/off → False."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def task_finance_sync():
    """财务数字刷新: eBay 已付款订单 → finance_orders PnL + GIGA 关联.

    只写本地库, 不改 eBay/GIGA. 日报邮件与 Streamlit「💰 财务」依赖此表.
    """
    run_task(
        'finance_sync',
        [
            str(PROJECT_ROOT / 'scripts' / 'finance_sync_orders.py'),
            '--days', '30',
        ],
        timeout_sec=TASK_TIMEOUT['finance_sync'],
    )


def task_giga_dropship_push():
    """已成交单推 GIGA 一件代发.

    默认 dry-run. 开启: ENABLE_GIGA_DROPSHIP_PUSH=1 → live apply.
    推单后 GIGA 侧可能仍为 Unpaid, 需在 GIGA 付款后由 sync 回写运单.
    """
    cmd = [
        str(PROJECT_ROOT / 'scripts' / 'giga_dropship_push.py'),
        '--days', '14',
    ]
    if _env_flag_enabled('ENABLE_GIGA_DROPSHIP_PUSH', default=False):
        cmd.append('--apply')
        logger.info("[GIGA PUSH] live apply 已显式启用")
    else:
        logger.info("[GIGA PUSH] dry-run only (设置 ENABLE_GIGA_DROPSHIP_PUSH=1 才写入)")
    run_task('giga_dropship_push', cmd, timeout_sec=TASK_TIMEOUT['giga_dropship_push'])


def task_giga_dropship_sync():
    """GIGA 状态/运单 → eBay createShippingFulfillment.

    默认 dry-run. 开启: ENABLE_GIGA_EBAY_FULFILL=1 → 写入 eBay.
    """
    cmd = [str(PROJECT_ROOT / 'scripts' / 'giga_dropship_sync.py')]
    if _env_flag_enabled('ENABLE_GIGA_EBAY_FULFILL', default=False):
        cmd.append('--apply')
        logger.info("[GIGA SYNC] eBay 标发 apply 已显式启用")
    else:
        logger.info("[GIGA SYNC] dry-run only (设置 ENABLE_GIGA_EBAY_FULFILL=1 才写入)")
    run_task('giga_dropship_sync', cmd, timeout_sec=TASK_TIMEOUT['giga_dropship_sync'])


def task_daily_full():
    """每日全量任务 (分析 + 库存同步 + 报告；智能调价仅周一/周四执行)"""
    if _task_succeeded_today('daily_tasks'):
        logger.info("↪ 跳过每日全量任务: 今日已成功执行，避免重复发送每日汇总")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'daily_tasks',
        [str(PROJECT_ROOT / 'daily_tasks.py')],
        timeout_sec=TASK_TIMEOUT['daily_tasks'],
    )


# task_inventory_sync() 已移除 — daily_tasks.py (09:30) 已包含库存同步+邮件报告
# 推荐单独入口: python daily_tasks.py --sync-only
# 仅调试时再用: python src/plugins/inventory_sync/daily_sync.py --email


def task_auto_analyze():
    """自动分析采集产品"""
    run_task(
        'auto_analyze',
        [str(PROJECT_ROOT / 'daily_tasks.py'), '--analyze-only'],
        timeout_sec=TASK_TIMEOUT['auto_analyze'],
    )


def task_smart_reprice():
    """手动智能重新定价（保留给人工触发，不再定时调度）"""
    run_task(
        'smart_reprice',
        [str(PROJECT_ROOT / 'scripts' / 'batch_smart_reprice.py'), '--apply', '--email'],
        timeout_sec=TASK_TIMEOUT['smart_reprice'],
    )


def task_health_check():
    """每日销售健康诊断 (含自动修复定价偏高/低转化)"""
    if _task_succeeded_today('health_check'):
        logger.info("↪ 跳过销售健康诊断: 今日已成功执行，避免重复发送健康报告")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'health_check',
        [str(PROJECT_ROOT / 'scripts' / 'sales_health_check.py'), '--auto-fix', '--email'],
        timeout_sec=TASK_TIMEOUT['health_check'],
    )


def task_promotion_rotate():
    """自动轮转 5% 店铺促销 (2天一期)"""
    run_task(
        'promotion_rotate',
        [str(PROJECT_ROOT / 'scripts' / 'auto_rotate_promotions.py'), '--email'],
        timeout_sec=1800,
    )


def _daily_tasks_ready_for_cro(task_label):
    if _task_succeeded_today('daily_tasks'):
        return True
    logger.info(f"↪ 跳过 {task_label}: daily_tasks 尚未成功完成，等待 CRO 队列生成")
    return False


def task_cro_consume():
    """CRO P1 改价消费 — 把 daily_tasks 09:30 入队的 P1 price_drop 真的执行掉.

    依赖 09:30 daily_tasks 完成 (run_cro_diagnose 写队列), 故安排 10:00 触发.
    成功改价的 SKU 会被自动 mark_done.
    """
    if _task_succeeded_today('cro_consume'):
        logger.info("↪ 跳过 CRO 队列消费: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 队列消费'):
        return True, 'Skipped (waiting for daily_tasks)'

    run_task(
        'cro_consume',
        [str(PROJECT_ROOT / 'scripts' / 'batch_smart_reprice.py'),
         '--apply', '--from-cro-queue', '--email'],
        timeout_sec=TASK_TIMEOUT['cro_consume'],
    )


def task_listing_status_sync():
    """每日死链协调 — 把 eBay 已结束的 listing 在本地标 ENDED / 清 READY 草稿的
    死链 listing_id, 让 MI 重识别的"曾刊登→未出单→死链"候选自动变回可重发草稿.

    内含抓取完整性闸: ActiveList 分页不全时整批跳过, 绝不把在售误判死链.
    排在 mi_snapshot / auto_publish 之前, 保证发布用的是干净状态.
    """
    if _task_succeeded_today('listing_status_sync'):
        logger.info("↪ 跳过死链协调: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    run_task(
        'listing_status_sync',
        [str(PROJECT_ROOT / 'src' / 'services' / 'listing_status_sync.py')],
        timeout_sec=TASK_TIMEOUT['listing_status_sync'],
    )


def _latest_mi_ready_skus(limit: int, reports_dir: Path | None = None) -> list[str]:
    """Return READY SKUs from today's latest MI opportunity snapshot."""
    reports = Path(reports_dir) if reports_dir else PROJECT_ROOT / 'reports'
    if not reports.is_dir():
        return []
    today = datetime.now().strftime('%Y%m%d')
    snapshots = sorted(
        reports.glob(f'mi_opportunities_{today}_*.json'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not snapshots:
        return []
    try:
        data = json.loads(snapshots[0].read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        opportunities = data
    elif isinstance(data, dict):
        opportunities = data.get('opportunities') or []
    else:
        opportunities = []

    skus: list[str] = []
    seen: set[str] = set()
    for opportunity in opportunities:
        if not isinstance(opportunity, dict):
            continue
        status = str(opportunity.get('status') or '').strip().upper()
        sku = str(opportunity.get('sku') or '').strip()
        if status not in {'READY', 'READY_TO_PUBLISH'} or not sku or sku in seen:
            continue
        seen.add(sku)
        skus.append(sku)
        if len(skus) >= limit:
            break
    return skus


def task_auto_publish():
    """MI 机会自动刊登 — 把 READY 草稿发布到 eBay (质检硬门已强制在 batch_publish 内).

    安全默认 dry-run: 只有 ENABLE_MI_AUTO_PUBLISH=1 才真正上架 (不可逆对外操作).
    每次只允许发布最新 MI 快照中的 READY SKU; --limit 由 MI_AUTO_PUBLISH_LIMIT 控制.
    依赖当日 listing_status_sync 先跑完 (清掉死链 listing_id), 故安排在其后.
    """
    if _task_succeeded_today('auto_publish'):
        logger.info("↪ 跳过 MI 自动刊登: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    enable = os.getenv('ENABLE_MI_AUTO_PUBLISH', '').strip().lower() in ('1', 'true', 'yes', 'on')
    try:
        limit = max(1, int(os.getenv('MI_AUTO_PUBLISH_LIMIT', '10')))
    except ValueError:
        limit = 10

    mi_skus = _latest_mi_ready_skus(limit)
    if not mi_skus:
        msg = 'Skipped (no READY MI opportunities in latest snapshot)'
        update_health('auto_publish', 'success', msg)
        logger.info(f"[AUTO-PUBLISH] {msg}")
        return True, msg

    cmd = [
        str(PROJECT_ROOT / 'batch_publish.py'),
        '--limit', str(limit),
        '--sku-list', ','.join(mi_skus),
    ]
    if not enable:
        cmd.append('--dry-run')
        logger.info(
            f"[AUTO-PUBLISH] dry-run 预览 (ENABLE_MI_AUTO_PUBLISH 未开启), "
            f"MI SKUs={len(mi_skus)}, limit={limit}"
        )
    else:
        logger.info(f"[AUTO-PUBLISH] 真实刊登已启用, MI SKUs={len(mi_skus)}, limit={limit}")
    run_task('auto_publish', cmd, timeout_sec=TASK_TIMEOUT['auto_publish'])


def task_mi_snapshot():
    """F17 解耦 — MI 自动机会发现 + 快照 (从 45 分钟 daily_tasks 拆出独立定时).

    独立 subprocess + 自带 1h 超时, daily_tasks 卡顿/失败不再拖累 MI 每日产出.
    不依赖 daily_tasks 完成 (MI 从本地采集库存 + eBay Browse 发现, 无需当日改价队列).
    """
    if _task_succeeded_today('mi_snapshot'):
        logger.info("↪ 跳过 MI 快照: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    run_task(
        'mi_snapshot',
        [str(PROJECT_ROOT / 'daily_tasks.py'), '--mi-only'],
        timeout_sec=TASK_TIMEOUT['mi_snapshot'],
    )


def task_cro_image_refresh():
    """CRO P1 image_refresh 消费 — 把 low_ctr 的产品主图重拼为本地完整多图."""
    if _task_succeeded_today('cro_image_refresh'):
        logger.info("↪ 跳过 CRO 图片刷新: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 图片刷新'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_image_refresh',
        [str(PROJECT_ROOT / 'scripts' / 'cro_image_refresh.py'),
         '--apply', '--limit', '80', '--email'],
        timeout_sec=TASK_TIMEOUT['cro_image_refresh'],
    )


def task_cro_fill_specifics():
    """CRO P1 fill_specifics 消费 — 给 low_cvr 产品补品类必填 aspects."""
    if _task_succeeded_today('cro_fill_specifics'):
        logger.info("↪ 跳过 CRO 补 specifics: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 补 specifics'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_fill_specifics',
        [str(PROJECT_ROOT / 'scripts' / 'cro_fill_specifics.py'),
         '--apply', '--limit', '80', '--email'],
        timeout_sec=TASK_TIMEOUT['cro_fill_specifics'],
    )


def task_cro_ops_snapshot():
    """Weekly CRO ops/governance snapshot with safe DB restore drill."""
    if _task_succeeded_today('cro_ops_snapshot'):
        logger.info("↪ 跳过 CRO ops 快照: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    run_task(
        'cro_ops_snapshot',
        [str(PROJECT_ROOT / 'scripts' / 'cro_ops_snapshot.py'),
         '--output', str(PROJECT_ROOT / 'logs' / 'cro_ops_snapshot.json'),
         '--brief', str(PROJECT_ROOT / 'logs' / 'cro_weekly_improvement.md'),
         '--capacity-sample-log', str(PROJECT_ROOT / 'logs' / 'cro_capacity_samples.jsonl'),
         '--record-capacity-sample',
         '--dr-drill'],
        timeout_sec=TASK_TIMEOUT['cro_ops_snapshot'],
    )


def task_cro_send_offer():
    """CRO send_offer 消费 — 给 low_cvr SKU 的 interested buyers 发保本 offer.

    10:35; offer 价由 cro_offer_pricing 的地板价守门 (最低净利率 5%),
    折扣封顶 10%, 同 SKU 30 天频控, A/B 对照组不执行.
    """
    if _task_succeeded_today('cro_send_offer'):
        logger.info("↪ 跳过 CRO offer: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO offer'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_send_offer',
        [str(PROJECT_ROOT / 'scripts' / 'cro_send_offer.py'),
         '--apply', '--limit', '20', '--email'],
        timeout_sec=TASK_TIMEOUT['cro_send_offer'],
    )


def task_cro_lifecycle_detect():
    """CRO relist 生命周期候选检测 — 只写本地 candidate 行, 不动 eBay.

    10:40 (spec Scheduler Contract), 在 CRO 诊断与安全动作之后.
    withdraw/republish 执行不进调度器: revive_relist 保持人工批准,
    运营走 scripts/cro_relist_lifecycle.py --precheck/--execute.
    """
    if _task_succeeded_today('cro_lifecycle_detect'):
        logger.info("↪ 跳过 CRO 生命周期检测: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 生命周期检测'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_lifecycle_detect',
        [str(PROJECT_ROOT / 'scripts' / 'cro_relist_lifecycle.py'),
         '--detect', '--apply', '--limit', '200'],
        timeout_sec=TASK_TIMEOUT['cro_lifecycle_detect'],
    )


def task_cro_title_rewrite():
    """CRO title keyword enrichment — scheduled dry-run by default.

    Live title writes require an explicit environment opt-in:
    ENABLE_SCHEDULED_TITLE_REWRITE_APPLY=1.  This keeps routine schedule runs
    observable without reintroducing unreviewed factual changes.
    """
    if _task_succeeded_today('cro_title_rewrite'):
        logger.info("↪ 跳过 CRO 标题热词优化: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 标题热词优化'):
        return True, 'Skipped (waiting for daily_tasks)'

    raw_apply = os.getenv('ENABLE_SCHEDULED_TITLE_REWRITE_APPLY', '').strip().lower()
    apply_enabled = raw_apply in {'1', 'true', 'yes', 'on'}
    limit_env = (
        os.getenv('SCHEDULED_TITLE_REWRITE_APPLY_LIMIT', '50')
        if apply_enabled else
        os.getenv('SCHEDULED_TITLE_REWRITE_DRY_RUN_LIMIT', '50')
    )
    try:
        limit = max(1, min(50, int(limit_env)))
    except (TypeError, ValueError):
        limit = 50

    cmd = [
        str(PROJECT_ROOT / 'scripts' / 'cro_title_rewrite.py'),
        '--limit', str(limit),
        '--email',
    ]
    if apply_enabled:
        cmd.extend(['--apply', '--yes'])

    run_task(
        'cro_title_rewrite',
        cmd,
        timeout_sec=TASK_TIMEOUT['cro_title_rewrite'],
    )


def task_cro_lifecycle_evaluate():
    """CRO relist 生命周期观察评估 — 状态转移 + 软手段入队, 仅本地写入.

    10:50; evaluate 只做 published_new→observing→终态 的本地转移,
    needs_conversion_help 经 cro_action_queue 走软手段, 绝不 withdraw/republish.
    """
    if _task_succeeded_today('cro_lifecycle_evaluate'):
        logger.info("↪ 跳过 CRO 生命周期评估: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 生命周期评估'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_lifecycle_evaluate',
        [str(PROJECT_ROOT / 'scripts' / 'cro_relist_lifecycle.py'),
         '--evaluate', '--apply'],
        timeout_sec=TASK_TIMEOUT['cro_lifecycle_evaluate'],
    )


def task_cro_sentinel():
    """CRO 北极星指标告警 — 7d avg 跌 ≥ 5 分 或 当日恶化占比 ≥ 20% 时邮件.

    安排 10:30 (在 cro_consume 之后), 避开高峰邮件.
    """
    if _task_succeeded_today('cro_sentinel'):
        logger.info("↪ 跳过 CRO 北极星告警: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 北极星告警'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_sentinel',
        [str(PROJECT_ROOT / 'scripts' / 'cro_sentinel.py'), '--email'],
        timeout_sec=600,
    )


def task_cro_learn_thresholds():
    """周日 02:00 — S21 阶段 1: 按 categoryId 学到 cro_thresholds_pending (不动生产)."""
    run_task(
        'cro_learn_thresholds',
        [str(PROJECT_ROOT / 'src' / 'services' / 'cro_thresholds.py'), '--pending'],
        timeout_sec=900,
    )


def task_cro_promote_thresholds():
    """周日 02:30 — S21 阶段 2+3: shadow 守门 + 安全则把 pending 推到生产."""
    if _task_succeeded_today('cro_promote_thresholds'):
        logger.info("↪ 跳过阈值 promote: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    run_task(
        'cro_promote_thresholds',
        [str(PROJECT_ROOT / 'scripts' / 'cro_promote_thresholds.py'),
         '--apply', '--email'],
        timeout_sec=900,
    )


def task_cro_monthly_report():
    """S22 — 每月 1 日 09:45 跑 effect_audit + 阈值反哺融合月报."""
    if datetime.now().date().day != 1:
        update_health('cro_monthly_report', 'success', 'Skipped (not month start)')
        return True, 'Skipped (not month start)'
    if _task_succeeded_today('cro_monthly_report'):
        return True, 'Skipped (already succeeded today)'
    run_task(
        'cro_monthly_report',
        [str(PROJECT_ROOT / 'scripts' / 'cro_effect_audit.py'),
         '--monthly', '--email'],
        timeout_sec=1200,
    )


def task_cro_promote():
    """S23 — 每日执行 CRO promote 队列: 已推广则提 bid, 未推广则安全开广告."""
    if _task_succeeded_today('cro_promote'):
        return True, 'Skipped (already succeeded today)'
    if not _daily_tasks_ready_for_cro('CRO 推广执行'):
        return True, 'Skipped (waiting for daily_tasks)'
    run_task(
        'cro_promote',
        [str(PROJECT_ROOT / 'scripts' / 'cro_promote.py'),
         '--apply', '--limit', '200', '--email'],
        timeout_sec=TASK_TIMEOUT['cro_promote'],
    )


def task_cro_delist_email():
    """S25 — 周一 11:00: 给运营发死链下架候选 + magic-link (人工确认才下架)."""
    if _task_succeeded_today('cro_delist_email'):
        return True, 'Skipped (already succeeded today)'
    from src.utils.store_profile import get_store_profile
    base = os.environ.get('CRO_DELIST_BASE_URL', get_store_profile().server_base_url)
    run_task(
        'cro_delist_email',
        [str(PROJECT_ROOT / 'scripts' / 'cro_delist.py'),
         '--base-url', base, '--limit', '50', '--email'],
        timeout_sec=900,
    )


def task_ad_restore():
    """广告自动恢复审计 — 把因触底被关掉的广告在条件改善后重新打开 (Phase 3 闭环).

    依赖 09:30 daily_tasks 的跟价完成, 故安排在 09:40.
    """
    if _task_succeeded_today('ad_restore'):
        logger.info("↪ 跳过广告恢复审计: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'ad_restore',
        [str(PROJECT_ROOT / 'scripts' / 'ad_restore_audit.py'), '--apply', '--email'],
        timeout_sec=TASK_TIMEOUT['ad_restore'],
    )


def task_smart_bid():
    """分级动态 bid 优化 (P3) — 高 CTR/转化 ↑ bid, 低 CTR/零销 ↓ bid.

    每条调整都被 PricingEngine 守门员复核 — 现价不支持则取可支持的最大 bid.
    每周二 10:00 运行, 给性能数据足够一周的样本量.
    """
    if _task_succeeded_today('smart_bid'):
        logger.info("↪ 跳过智能 Bid: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'

    run_task(
        'smart_bid',
        [str(PROJECT_ROOT / 'scripts' / 'batch_smart_bid.py'), '--apply', '--email'],
        timeout_sec=TASK_TIMEOUT['smart_bid'],
    )


def task_bid_rollback():
    """P5: smart_bid 7 天后回溯 — 提升 bid 未见效果则回退到 5%."""
    if _task_succeeded_today('bid_rollback'):
        logger.info("↪ 跳过 Bid 回溯: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    run_task(
        'bid_rollback',
        [str(PROJECT_ROOT / 'scripts' / 'bid_rollback_audit.py'), '--apply', '--email'],
        timeout_sec=TASK_TIMEOUT['bid_rollback'],
    )


def task_blacklist_cleanup():
    """P7: 广告黑名单自动清理 — 连续 7 天现价支持 5% 广告则自动移出 (仅 auto)."""
    if _task_succeeded_today('blacklist_cleanup'):
        logger.info("↪ 跳过黑名单清理: 今日已成功执行")
        return True, 'Skipped (already succeeded today)'
    run_task(
        'blacklist_cleanup',
        [str(PROJECT_ROOT / 'scripts' / 'ad_blacklist_cleanup.py'), '--apply', '--email'],
        timeout_sec=TASK_TIMEOUT['blacklist_cleanup'],
    )


def task_guard_anomaly():
    """P8: 守门员异常告警 — 当日 reject/关广告数远高于 7 日均值则立即邮件."""
    # 不查 _task_succeeded_today: 脚本内部用 logs/guard_anomaly_<date>.json 自防重
    run_task(
        'guard_anomaly',
        [str(PROJECT_ROOT / 'scripts' / 'guard_anomaly_alert.py')],
        timeout_sec=TASK_TIMEOUT['guard_anomaly'],
    )


# ─── F32: MI 流水线自检 ──────────────────────────────────

def check_mi_pipeline_health(reports_dir: Path = None,
                              now: datetime = None,
                              today_only: bool = True) -> dict:
    """F32 — 体检 MI 流水线四件套：
    1) 当日是否有新快照 mi_opportunities_*.json
    2) 当日是否有日报归档 mi_digest_YYYYMMDD.html
    3) 长周期 trend 历史 mi_long_window_history.json 最新一条是否为今日
    4) 屏蔽名单文件是否可读

    返回 {ok: bool, status: str, severity: str, issues: [..], details: {...}}。
    """
    rdir = reports_dir or (PROJECT_ROOT / 'reports')
    ts = now or datetime.now()
    today = ts.strftime('%Y%m%d')
    issues: list = []
    details: dict = {}

    if not rdir.exists():
        return {
            "ok": False,
            "status": "fail",
            "severity": "high",
            "issues": ["reports/ 目录不存在"],
            "details": {},
        }

    # 1) 当日快照
    snaps_today = list(rdir.glob(f"mi_opportunities_{today}_*.json"))
    details["snapshots_today"] = len(snaps_today)
    if today_only and not snaps_today:
        issues.append(f"当日 ({today}) 无 MI 快照，run_mi_snapshot 可能未运行")

    # 2) 当日日报归档
    digest = rdir / f"mi_digest_{today}.html"
    details["digest_today_exists"] = digest.exists()
    if today_only and not digest.exists():
        issues.append(f"当日日报归档缺失: mi_digest_{today}.html (F25)")
    elif digest.exists():
        details["digest_references_today_snapshot"] = None
        details["digest_snapshot_name"] = None
        details["digest_count_matches_snapshot"] = None
        try:
            import re

            digest_html = digest.read_text(encoding='utf-8')
            matched_snapshot = None
            for snap in sorted(snaps_today, key=lambda path: path.stat().st_mtime, reverse=True):
                if snap.name in digest_html:
                    matched_snapshot = snap
                    break

            details["digest_references_today_snapshot"] = matched_snapshot is not None
            if today_only and snaps_today and not matched_snapshot:
                issues.append("当日日报归档内容未引用任何当日快照，可能被覆盖、过期或被测试样例污染")

            if matched_snapshot is not None:
                details["digest_snapshot_name"] = matched_snapshot.name
                try:
                    snap_data = json.loads(matched_snapshot.read_text(encoding='utf-8'))
                    expected_count = len(snap_data.get("opportunities") or [])
                    details["digest_snapshot_opportunity_count"] = expected_count
                    count_pattern = re.compile(
                        rf"共发现\s*<b>\s*{expected_count}\s*</b>\s*条机会"
                    )
                    details["digest_count_matches_snapshot"] = bool(
                        count_pattern.search(digest_html)
                    )
                    if today_only and not details["digest_count_matches_snapshot"]:
                        issues.append(
                            f"当日日报归档机会数与快照 {matched_snapshot.name} 不一致"
                        )
                except Exception as e:
                    details["digest_snapshot_opportunity_count"] = None
                    details["digest_count_matches_snapshot"] = None
                    issues.append(f"当日快照不可读，无法校验 digest 内容: {e}")
        except Exception as e:
            details["digest_references_today_snapshot"] = None
            details["digest_snapshot_name"] = None
            details["digest_count_matches_snapshot"] = None
            issues.append(f"日报归档不可读: {e}")

    # 3) 长周期 trend 历史
    trend_path = rdir / "mi_long_window_history.json"
    details["trend_history_exists"] = trend_path.exists()
    if trend_path.exists():
        try:
            data = json.loads(trend_path.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                issues.append("trend 文件结构异常: 期望为 list")
                details["trend_entry_count"] = None
                details["trend_latest_date"] = None
            elif not data:
                details["trend_entry_count"] = 0
                details["trend_latest_date"] = None
                if today_only:
                    issues.append("长周期 trend 历史为空 (F27)")
            else:
                details["trend_entry_count"] = len(data)
                latest = data[-1].get("date")
                details["trend_latest_date"] = latest
                if today_only and latest != ts.strftime('%Y-%m-%d'):
                    issues.append(
                        f"长周期 trend 最新日期为 {latest}，非今日 (F27)"
                    )
        except Exception as e:
            issues.append(f"trend 文件不可读: {e}")
            details["trend_entry_count"] = None
            details["trend_latest_date"] = None
    else:
        details["trend_entry_count"] = 0
        details["trend_latest_date"] = None
        if today_only:
            issues.append("长周期 trend 历史缺失: mi_long_window_history.json (F27)")

    # 4) 屏蔽名单
    bl_path = rdir / "mi_blacklist.json"
    if bl_path.exists():
        try:
            json.loads(bl_path.read_text(encoding='utf-8'))
            details["blacklist_readable"] = True
        except Exception as e:
            details["blacklist_readable"] = False
            issues.append(f"mi_blacklist.json 不可读: {e}")
    else:
        details["blacklist_readable"] = None

    ok = len(issues) == 0
    return {
        "ok": ok,
        "status": "ok" if ok else "fail",
        "severity": "info" if ok else "high",
        "issues": issues,
        "details": details,
    }


def _daily_tasks_active_today(now: datetime = None) -> dict | None:
    """Return daily_tasks health when today's full task is still running."""
    ts = now or datetime.now()
    info = _get_task_health('daily_tasks')
    status = str(info.get('status', '')).lower()
    at_dt = _iso_at_or_none(info.get('at'))
    if (
        status in {'running', 'recovering'}
        and at_dt is not None
        and at_dt.date() == ts.date()
    ):
        return info
    return None


def _write_mi_self_check_health(result: dict, checked_at: datetime = None) -> None:
    """Persist MI self-check result without touching daily task success records."""
    ts = checked_at or datetime.now()
    health_payload = {}
    if HEALTH_FILE.exists():
        health_payload = json.loads(HEALTH_FILE.read_text(encoding='utf-8') or '{}')
    health_payload.setdefault('mi_self_check', {})
    health_payload['mi_self_check'] = {
        "checked_at": ts.isoformat(),
        **result,
    }
    status = str(result.get("status") or "").lower()
    if result.get("ok") and status == "ok":
        task_status = "success"
        task_message = "OK"
    elif status == "pending":
        task_status = "pending"
        task_message = result.get("details", {}).get("reason", "Pending")
    else:
        task_status = "failed"
        task_message = "; ".join(result.get("issues") or []) or status or "failed"
    health_payload.setdefault('tasks', {})['mi_self_check'] = {
        "status": task_status,
        "message": str(task_message)[:500],
        "at": ts.isoformat(),
        "duration_sec": 0.0,
    }
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(
        json.dumps(health_payload, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def task_mi_self_check():
    """F32 — MI 流水线自检（与 09:30 daily_tasks 解耦）。

    任一缺失 → 写入 _scheduler_health.json + 发一封中文邮件提醒。
    """
    try:
        checked_at = datetime.now()
        active_daily = _daily_tasks_active_today(now=checked_at)
        if active_daily:
            result = {
                "ok": True,
                "status": "pending",
                "severity": "info",
                "issues": [],
                "details": {
                    "reason": "daily_tasks still running; MI snapshot/digest may not be final yet",
                    "daily_tasks_status": active_daily.get("status"),
                    "daily_tasks_at": active_daily.get("at"),
                },
            }
            try:
                _write_mi_self_check_health(result, checked_at=checked_at)
            except Exception as we:
                logger.warning(f"MI self-check 写入 health 文件失败: {we}")
            logger.info("[MI SELF-CHECK] daily_tasks 仍在运行，等待后续检查")
            return True, "Pending: daily_tasks still running"

        result = check_mi_pipeline_health(now=checked_at)
        # 落地到 health 文件（不污染 daily 任务的成功记录）
        try:
            _write_mi_self_check_health(result, checked_at=checked_at)
        except Exception as we:
            logger.warning(f"MI self-check 写入 health 文件失败: {we}")

        if result["ok"]:
            logger.info("[MI SELF-CHECK] 流水线健康")
            return True, "OK"

        # 发邮件提醒
        try:
            from src.utils.email_sender import send_email
            issues_html = "".join(f"<li>{i}</li>" for i in result["issues"])
            details_html = "<br>".join(f"{k} = {v}" for k, v in result["details"].items())
            html = f"""
            <html><body style="font-family:'Microsoft YaHei',Arial,sans-serif;">
            <h2>🚨 MI 流水线自检发现 {len(result["issues"])} 项异常</h2>
            <p>检测时间: {datetime.now():%Y-%m-%d %H:%M}</p>
            <h3>异常列表</h3>
            <ul>{issues_html}</ul>
            <h3>诊断详情</h3>
            <p>{details_html}</p>
            <hr>
            <p style="color:#999;font-size:12px;">
              由 scheduler_daemon.task_mi_self_check 触发 ·
              排查：python scripts/mi_diagnose.py
            </p>
            </body></html>
            """
            send_email(
                f"🚨 MI 流水线自检异常 - {datetime.now():%Y-%m-%d}",
                html,
            )
            logger.warning(f"[MI SELF-CHECK] 已发送告警邮件: {result['issues']}")
        except Exception as ee:
            logger.error(f"MI self-check 邮件发送失败: {ee}")
        return False, "; ".join(result["issues"])
    except Exception as e:
        logger.error(f"task_mi_self_check 异常: {e}")
        return False, str(e)


# ─── 调度配置 ─────────────────────────────────────────────

def setup_schedule():
    """配置任务调度时间表"""
    # 09:15 — 财务订单同步 (eBay → finance_orders), 赶在 09:30 日报前刷新数字
    schedule.every().day.at("09:15").do(task_finance_sync).tag('daily', 'finance_sync')

    # 09:18 — 已成交未履约 → GIGA 推单 (默认 dry-run; 显式设 1 才 apply)
    schedule.every().day.at("09:18").do(task_giga_dropship_push).tag('daily', 'giga_push')

    # 09:22 — GIGA 运单回写 eBay (默认 dry-run; 显式设 1 才 apply)
    schedule.every().day.at("09:22").do(task_giga_dropship_sync).tag('daily', 'giga_sync')

    # 每天 09:30 — 全量任务 (分析+同步+报告)
    #   注意: daily_tasks.py 已包含库存同步；智能调价在同一流程里仅周一/周四执行
    schedule.every().day.at("09:30").do(task_daily_full).tag('daily', 'full')

    # 09:58 — 死链协调 (标 ENDED / 清 READY 死链 listing_id), 需在 MI 发布链路之前
    schedule.every().day.at("09:58").do(task_listing_status_sync).tag('daily', 'listing_sync')

    # F17 解耦 — 10:00 MI 机会发现快照 (独立于 45 分钟 daily_tasks, 自带超时)
    schedule.every().day.at("10:00").do(task_mi_snapshot).tag('daily', 'mi_snapshot')

    # F32 — 10:05 MI 流水线自检（在 mi_snapshot 之后, 验证当日快照/digest 已生成）
    schedule.every().day.at("10:05").do(task_mi_self_check).tag('daily', 'mi_check')

    # 10:10 — MI 机会自动刊登 (READY 草稿 → eBay); 默认 dry-run, 需 env 开关才真发布
    schedule.every().day.at("10:10").do(task_auto_publish).tag('daily', 'auto_publish')

    # 09:40 — 广告恢复审计 (Phase 3 闭环：跟价后重新评估被关广告的 SKU)
    schedule.every().day.at("09:40").do(task_ad_restore).tag('daily', 'ad_restore')
    # S25: 周一 11:00 给运营发死链下架候选 (magic-link 人工确认)
    schedule.every().monday.at("11:00").do(task_cro_delist_email).tag('weekly', 'cro_delist_email')

    # 每周二 10:00 分级 bid 优化 (需足量近 7 天性能数据)
    schedule.every().tuesday.at("10:00").do(task_smart_bid).tag('weekly', 'smart_bid')
    schedule.every().tuesday.at("11:00").do(task_bid_rollback).tag('weekly', 'bid_rollback')

    # 周日 02:00 学到 pending 表; 02:30 shadow 守门后 promote 到生产 (S21)
    schedule.every().sunday.at("02:00").do(task_cro_learn_thresholds).tag('weekly', 'cro_learn_thresholds')
    schedule.every().sunday.at("02:30").do(task_cro_promote_thresholds).tag('weekly', 'cro_promote_thresholds')
    schedule.every().sunday.at("03:00").do(task_cro_ops_snapshot).tag('weekly', 'cro_ops_snapshot')

    # S22 — 每天 09:55 检查是否月初 (脚本内自门控), 是则跑融合月报
    schedule.every().day.at("09:55").do(task_cro_monthly_report).tag('daily', 'cro_monthly_report')

    # 09:45 — 广告黑名单自动清理 (P7 — 连续7天安全则移出 auto_off_streak)
    schedule.every().day.at("09:45").do(task_blacklist_cleanup).tag('daily', 'bl_cleanup')

    # 09:50 — 守门员异常告警 (P8 — 当日数据 vs 7日均值)
    schedule.every().day.at("09:50").do(task_guard_anomaly).tag('daily', 'guard_alert')

    # 10:00 — CRO P1 改价消费 (主线: 把 daily_tasks 09:30 入队的转化率优化建议落地)
    schedule.every().day.at("10:00").do(task_cro_consume).tag('daily', 'cro_consume')

    # 10:15 — CRO P1 图片刷新 (本地多图 → 远端 inventory)
    schedule.every().day.at("10:15").do(task_cro_image_refresh).tag('daily', 'cro_image_refresh')

    # 10:20 — CRO P1 补 specifics (品类必填 aspects)
    schedule.every().day.at("10:20").do(task_cro_fill_specifics).tag('daily', 'cro_fill_specifics')

    # 10:25 — CRO P1 推广执行 (安全开广告/提 bid)
    schedule.every().day.at("10:25").do(task_cro_promote).tag('daily', 'cro_promote')

    # 10:30 — CRO 北极星告警 (7d avg 跌 ≥ 5 分 / 恶化占比 ≥ 20%)
    schedule.every().day.at("10:30").do(task_cro_sentinel).tag('daily', 'cro_sentinel')

    # 10:35 — CRO send_offer (low_cvr → interested buyers 保本限时 offer)
    schedule.every().day.at("10:35").do(task_cro_send_offer).tag('daily', 'cro_send_offer')

    # 10:40 — CRO relist 生命周期候选检测 (本地 candidate 行, 不动 eBay)
    schedule.every().day.at("10:40").do(task_cro_lifecycle_detect).tag('daily', 'cro_lifecycle_detect')

    # 10:45 — CRO 标题热词优化 (默认 dry-run 生成报告; 显式 env 才 apply)
    schedule.every().day.at("10:45").do(task_cro_title_rewrite).tag('daily', 'cro_title_rewrite')

    # 10:50 — CRO relist 生命周期观察评估 (本地状态转移 + 软手段入队)
    schedule.every().day.at("10:50").do(task_cro_lifecycle_evaluate).tag('daily', 'cro_lifecycle_evaluate')

    # 10:55 — 源内容刷新 (重抓 GIGA 源详情, 检测卖家漂移, 修复本地快照; 供 11:30 审计使用)
    schedule.every().day.at("10:55").do(task_source_refresh).tag('daily', 'source_refresh')

    # 11:30 — 只读审计 live eBay 刊登内容 vs GIGA 原文，发现 AI 幻觉/事实偏差后发邮件
    schedule.every().day.at("11:30").do(task_listing_audit).tag('daily', 'listing_audit')

    # 12:30 — 语义改写闭环 (读当日 audit 增量队列, 限 40; 需 SEMANTIC_REWRITE_APPLY_ENABLED=1)
    schedule.every().day.at("12:10").do(task_source_aspect_autofix).tag('daily', 'source_aspect_autofix')
    schedule.every().day.at("12:30").do(task_semantic_rewrite).tag('daily', 'semantic_rewrite')

    # 每 6 小时 — 出单源复核 (新订单 SKU 源重抓 + live 声明比对, 发货前拦退款)
    # 单量不高, 6h 一轮足够; lookback 8h 留重叠, order_recheck_log 去重防重复告警
    schedule.every(6).hours.do(task_order_recheck).tag('recurring', 'order_recheck')

    # 每 4 小时 — 履约闭环: 推未推单 + 轮询运单回写 eBay + 刷新财务关联
    # 与 09:18/09:22 日班叠加; 推单/标发幂等 (本地 giga_fulfillment_orders 去重)
    schedule.every(4).hours.do(task_giga_dropship_push).tag('recurring', 'giga_push')
    schedule.every(4).hours.do(task_giga_dropship_sync).tag('recurring', 'giga_sync')
    schedule.every(4).hours.do(task_finance_sync).tag('recurring', 'finance_sync')

    # ⚠️ (旧) 10:00 库存同步已移除 — daily_tasks.py (09:30) 已包含库存同步
    # 之前 10:00 的 task_inventory_sync 会导致重复发送库存报告邮件 (内容不同)
    # 如果需要单独测试库存同步: python scheduler_daemon.py --task inventory

    # 每 2 小时 — 自动分析
    schedule.every(2).hours.do(task_auto_analyze).tag('recurring', 'analyze')

    # 每天 20:00 — 销售健康诊断 (含自动降价修复)
    schedule.every().day.at("20:00").do(task_health_check).tag('daily', 'health')

    # 每 6 小时 — 促销自动轮转 (2天一期 5% off)
    schedule.every(6).hours.do(task_promotion_rotate).tag('recurring', 'promotion')

    logger.info("调度表已配置:")
    logger.info("  标题优化: 已停用 (不会定时/补跑/手动执行优化脚本)")
    logger.info("  09:15  财务同步 (finance_sync_orders --days 30)")
    logger.info("  09:18  GIGA 推单 (默认 dry-run; ENABLE_GIGA_DROPSHIP_PUSH=1 才 apply)")
    logger.info("  09:22  GIGA→eBay 运单回写 (默认 dry-run; ENABLE_GIGA_EBAY_FULFILL=1 才 apply)")
    logger.info("  09:30  每日全量任务 (daily_tasks.py — 含库存同步+报告；智能调价仅周一/周四)")
    logger.info("  10:05  MI 流水线自检 (F32 — 异常发邮件)")
    logger.info("  09:40  广告恢复审计 (ad_restore_audit --apply --email)")
    logger.info("  周二 10:00  分级 Bid 优化 (batch_smart_bid --apply --email)")
    logger.info("  周二 11:00  Bid 7 天回溯 (bid_rollback_audit --apply --email)")
    logger.info("  周日 03:00  CRO ops 快照 + DB DR 演练 (cro_ops_snapshot)")
    logger.info("  09:45  广告黑名单自动清理 (P7 — ad_blacklist_cleanup)")
    logger.info("  09:50  守门员异常告警 (P8 — guard_anomaly_alert)")
    logger.info("  10:00-10:25  CRO 队列自动执行 (改价/图/specifics/推广)")
    logger.info("  10:45  CRO 标题热词优化 dry-run (显式 env 才 live apply)")
    logger.info("  10:55  源内容刷新 (source_content_refresh --email — 卖家漂移检测)")
    logger.info("  11:30  eBay/GIGA live listing 内容审计 (audit_fix_active_listings --live --email)")
    logger.info("  12:10  源参数写回 live (audit --source-report --issue-type source_aspect_mismatch --fix,限定 Color/Material/描述重建)")
    logger.info("  12:30  语义改写闭环 (semantic_rewrite --from-daily-audit --limit 40 --apply --email)")
    logger.info("  每 6h  出单源复核 (order_source_recheck --hours-back 48 --email)")
    logger.info("  每 4h  履约闭环 (giga push + sync + finance_sync)")
    logger.info("  20:00  销售健康诊断 (health_check --auto-fix --email)")
    logger.info("  每 2h  自动分析 (daily_tasks.py --analyze-only)")
    logger.info("  每 6h  促销轮转 (auto_rotate_promotions.py)")


# ─── 清理旧进程 ──────────────────────────────────────────

def cleanup_stale_processes():
    """清理可能残留的旧任务进程 (端口占用等)"""
    logger.info("清理残留进程...")
    killed = 0

    # 清理占用 8000 端口的旧 FastAPI 进程 — 仅在不是由 start.bat 管理时才清
    # 这里不清理 8000/8501 端口, 因为那是 start.bat 管理的服务进程
    # 仅清理可能残留的旧 daily_tasks / daily_optimize 窗口
    for title in ['Daily Tasks', 'Title Optimizer']:
        try:
            result = subprocess.run(
                ['taskkill', '/F', '/FI', f'WINDOWTITLE eq {title}*'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
            if 'SUCCESS' in result.stdout.upper():
                killed += 1
                logger.info(f"  已清理: {title} 窗口")
        except Exception:
            pass

    if killed:
        logger.info(f"  共清理 {killed} 个残留窗口")
    else:
        logger.info("  无残留进程")


# ─── 主循环 ──────────────────────────────────────────────

def run_once():
    """立即执行所有任务一次 (调试/手动触发)"""
    logger.info("=" * 60)
    logger.info("立即执行全部任务 (--once 模式)")
    logger.info("=" * 60)

    task_listing_audit()
    task_daily_full()  # 已包含库存同步 + 智能调价

    logger.info("全部任务执行完毕")


def recover_missed_tasks(log_when_clean=True):
    """检查并恢复错过的定时任务（例如电脑关机/休眠导致的）"""
    logger.info("检查是否有遗漏的定时任务...")
    now = datetime.now()
    recovered = []
    
    try:
        health = {}
        if HEALTH_FILE.exists():
            health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
        tasks = health.get('tasks', {})
        
        # 定义需要在 daemon 重启后补跑的关键定时任务。
        # 判断标准是: 已经过了今日的恢复窗口，且自今日计划时间之后还没有成功过。
        critical_tasks = [
            {
                'name': 'finance_sync',
                'scheduled_time': '09:15',
                'recovery_grace_minutes': 15,
                'func': task_finance_sync,
                'label': '财务订单同步',
                'priority': 12,
            },
            {
                'name': 'giga_dropship_push',
                'scheduled_time': '09:18',
                'recovery_grace_minutes': 15,
                'func': task_giga_dropship_push,
                'label': 'GIGA 推单',
                'priority': 14,
            },
            {
                'name': 'giga_dropship_sync',
                'scheduled_time': '09:22',
                'recovery_grace_minutes': 15,
                'func': task_giga_dropship_sync,
                'label': 'GIGA 运单回写 eBay',
                'priority': 16,
            },
            {
                'name': 'daily_tasks',
                'scheduled_time': '09:30',
                'recovery_grace_minutes': 20,
                'func': task_daily_full,
                'label': '每日全量任务',
                'priority': 20,
            },
            {
                'name': 'listing_status_sync',
                'scheduled_time': '09:58',
                'recovery_grace_minutes': 15,
                'func': task_listing_status_sync,
                'label': '死链协调',
                'priority': 23,   # 先于 mi_snapshot(25)/auto_publish, 清干净死链再发布
            },
            {
                'name': 'mi_snapshot',
                'scheduled_time': '10:00',
                'recovery_grace_minutes': 15,
                'func': task_mi_snapshot,
                'label': 'MI 机会发现快照',
                'priority': 25,   # 先于 mi_self_check(30), 让自检验证到当日快照
            },
            {
                'name': 'auto_publish',
                'scheduled_time': '10:10',
                'recovery_grace_minutes': 20,
                'func': task_auto_publish,
                'label': 'MI 自动刊登',
                'priority': 35,   # 在 mi_snapshot/self_check 之后
            },
            {
                'name': 'mi_self_check',
                'scheduled_time': '10:05',
                'recovery_grace_minutes': 10,
                'func': task_mi_self_check,
                'label': 'MI 流水线自检',
                'priority': 30,
            },
            {
                'name': 'ad_restore',
                'scheduled_time': '09:40',
                'recovery_grace_minutes': 10,
                'func': task_ad_restore,
                'label': '广告恢复审计',
                'priority': 40,
            },
            {
                'name': 'blacklist_cleanup',
                'scheduled_time': '09:45',
                'recovery_grace_minutes': 10,
                'func': task_blacklist_cleanup,
                'label': '广告黑名单清理',
                'priority': 50,
            },
            {
                'name': 'guard_anomaly',
                'scheduled_time': '09:50',
                'recovery_grace_minutes': 10,
                'func': task_guard_anomaly,
                'label': '守门员异常告警',
                'priority': 60,
            },
            {
                'name': 'cro_monthly_report',
                'scheduled_time': '09:55',
                'recovery_grace_minutes': 10,
                'func': task_cro_monthly_report,
                'label': 'CRO 月报门控',
                'priority': 70,
            },
            {
                'name': 'cro_consume',
                'scheduled_time': '10:00',
                'recovery_grace_minutes': 30,
                'func': task_cro_consume,
                'label': 'CRO 改价消费',
                'priority': 80,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'smart_bid',
                'scheduled_time': '10:00',
                'recovery_grace_minutes': 30,
                'func': task_smart_bid,
                'label': '智能 Bid',
                'priority': 85,
                'weekday': 1,
            },
            {
                'name': 'cro_image_refresh',
                'scheduled_time': '10:15',
                'recovery_grace_minutes': 30,
                'func': task_cro_image_refresh,
                'label': 'CRO 图片刷新',
                'priority': 90,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_fill_specifics',
                'scheduled_time': '10:20',
                'recovery_grace_minutes': 30,
                'func': task_cro_fill_specifics,
                'label': 'CRO 补 specifics',
                'priority': 100,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_promote',
                'scheduled_time': '10:25',
                'recovery_grace_minutes': 30,
                'func': task_cro_promote,
                'label': 'CRO 推广执行',
                'priority': 110,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_sentinel',
                'scheduled_time': '10:30',
                'recovery_grace_minutes': 30,
                'func': task_cro_sentinel,
                'label': 'CRO 北极星告警',
                'priority': 120,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_send_offer',
                'scheduled_time': '10:35',
                'recovery_grace_minutes': 30,
                'func': task_cro_send_offer,
                'label': 'CRO 保本 Offer',
                'priority': 121,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_lifecycle_detect',
                'scheduled_time': '10:40',
                'recovery_grace_minutes': 30,
                'func': task_cro_lifecycle_detect,
                'label': 'CRO 生命周期检测',
                'priority': 122,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_title_rewrite',
                'scheduled_time': '10:45',
                'recovery_grace_minutes': 20,
                'func': task_cro_title_rewrite,
                'label': 'CRO 标题热词优化',
                'priority': 123,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'cro_lifecycle_evaluate',
                'scheduled_time': '10:50',
                'recovery_grace_minutes': 30,
                'func': task_cro_lifecycle_evaluate,
                'label': 'CRO 生命周期评估',
                'priority': 124,
                'depends_on_success': 'daily_tasks',
            },
            {
                'name': 'bid_rollback',
                'scheduled_time': '11:00',
                'recovery_grace_minutes': 20,
                'func': task_bid_rollback,
                'label': 'Bid 7 天回溯',
                'priority': 125,
                'weekday': 1,
            },
            {
                'name': 'cro_delist_email',
                'scheduled_time': '11:00',
                'recovery_grace_minutes': 20,
                'func': task_cro_delist_email,
                'label': 'CRO 下架候选邮件',
                'priority': 130,
                'weekday': 0,
            },
            {
                'name': 'source_refresh',
                'scheduled_time': '10:55',
                'recovery_grace_minutes': 20,
                'func': task_source_refresh,
                'label': '源内容刷新',
                'priority': 135,   # 必须在 listing_audit(140) 之前, 审计要用新鲜源快照
            },
            {
                'name': 'listing_audit',
                'scheduled_time': '11:30',
                'recovery_grace_minutes': 15,
                'func': task_listing_audit,
                'label': '刊登内容审计',
                'priority': 140,
            },
            {
                'name': 'semantic_rewrite',
                'scheduled_time': '12:30',
                'recovery_grace_minutes': 20,
                'func': task_semantic_rewrite,
                'label': '语义改写闭环',
                'priority': 145,  # listing_audit(140) 之后
            },
            {
                'name': 'health_check',
                'scheduled_time': '20:00',
                'recovery_grace_minutes': 10,
                'func': task_health_check,
                'label': '销售健康诊断',
                'priority': 200,
            },
            {
                'name': 'cro_learn_thresholds',
                'scheduled_time': '02:00',
                'recovery_grace_minutes': 20,
                'func': task_cro_learn_thresholds,
                'label': 'CRO 阈值学习',
                'priority': 300,
                'weekday': 6,
            },
            {
                'name': 'cro_promote_thresholds',
                'scheduled_time': '02:30',
                'recovery_grace_minutes': 20,
                'func': task_cro_promote_thresholds,
                'label': 'CRO 阈值推广',
                'priority': 310,
                'weekday': 6,
            },
            {
                'name': 'cro_ops_snapshot',
                'scheduled_time': '03:00',
                'recovery_grace_minutes': 20,
                'func': task_cro_ops_snapshot,
                'label': 'CRO ops 快照',
                'priority': 320,
                'weekday': 6,
            },
        ]

        ordered_tasks = sorted(
            critical_tasks,
            key=lambda item: (item.get('priority', 99), item['scheduled_time'])
        )

        for config in ordered_tasks:
            task_name = str(config['name'])
            weekday = config.get('weekday')
            if weekday is not None and now.weekday() != int(weekday):
                continue

            scheduled_time = str(config.get('scheduled_time', '00:00'))
            try:
                scheduled_hour, scheduled_minute = [int(x) for x in scheduled_time.split(':', 1)]
            except Exception:
                logger.warning(f"  ⚠️ 任务 {task_name} scheduled_time 配置非法: {scheduled_time}")
                continue

            scheduled_dt = now.replace(
                hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0
            )
            grace_minutes = int(config.get('recovery_grace_minutes', 10))
            recovery_window_start = scheduled_dt + timedelta(minutes=grace_minutes)

            # 今天已经成功执行过，不再补跑
            if _task_succeeded_today(task_name, now=now):
                continue
            
            # 未到补跑窗口（给定时任务留出执行时间）
            if now < recovery_window_start:
                continue

            depends_on = config.get('depends_on_success')
            if depends_on and not _task_succeeded_today(str(depends_on), now=now):
                continue

            if _task_in_progress_or_recovered_today(task_name, scheduled_dt, now=now):
                continue

            last_run = _get_task_health(task_name).get('at', '')
            if last_run:
                try:
                    last_dt = datetime.fromisoformat(last_run)
                except (ValueError, TypeError):
                    continue
                if last_dt >= scheduled_dt:
                    continue
                logger.info(
                    f"  ⚠️ 发现遗漏任务: {config['label']} "
                    f"(上次运行: {last_run}, 今日计划: {scheduled_time})"
                )
            else:
                logger.info(
                    f"  ⚠️ 发现遗漏任务: {config['label']} "
                    f"(无历史记录, 今日计划: {scheduled_time})"
                )

            recovered.append(task_name)
            config['func']()
        
        if recovered:
            logger.info(f"  已恢复 {len(recovered)} 个遗漏任务: {recovered}")
        elif log_when_clean:
            logger.info("  ✅ 没有遗漏的任务")
    except Exception as e:
        logger.error(f"  遗漏任务检查异常: {e}")


def run_daemon():
    """启动守护进程主循环"""
    global IS_DAEMON_PROCESS
    acquire_lock()
    IS_DAEMON_PROCESS = True
    if request_system_awake(True):
        atexit.register(request_system_awake, False)

    logger.info("=" * 60)
    logger.info(f"Dajian Listing Tool 调度守护进程启动")
    logger.info(f"PID: {os.getpid()}")
    logger.info(f"Python: {sys.executable}")
    logger.info(f"Project: {PROJECT_ROOT}")
    logger.info("=" * 60)

    cleanup_stale_processes()
    setup_schedule()
    clear_stale_running_tasks()
    
    # 恢复错过的任务
    recover_missed_tasks()

    # 更新初始健康状态
    update_health('_daemon', 'running', f'Started at {datetime.now().isoformat()}')

    # 优雅退出
    running = True

    def signal_handler(signum, frame):
        nonlocal running
        logger.info(f"收到信号 {signum}, 准备退出...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if sys.platform == 'win32':
        signal.signal(signal.SIGBREAK, signal_handler)

    # 主循环
    heartbeat_interval = 300  # 5分钟心跳
    last_heartbeat = time.time()

    logger.info("进入调度循环 (Ctrl+C 退出)...")
    logger.info(f"下一个任务: {schedule.next_run()}")

    while running:
        try:
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                recover_missed_tasks(log_when_clean=False)
                next_run = schedule.next_run()
                update_health('_daemon', 'running',
                            f'Heartbeat OK | Next: {next_run}')
                last_heartbeat = now

            schedule.run_pending()

            # 短睡眠, 便于响应 Ctrl+C
            time.sleep(30)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"调度循环异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            time.sleep(60)  # 异常后等待 1 分钟再重试

    logger.info("调度守护进程已停止")
    update_health('_daemon', 'stopped', f'Stopped at {datetime.now().isoformat()}')


def main():
    parser = argparse.ArgumentParser(description='Dajian Listing Tool 后台调度守护进程')
    parser.add_argument('--once', action='store_true',
                       help='立即执行全部任务一次后退出')
    parser.add_argument('--task', type=str,
                       choices=['title', 'listing_audit', 'daily', 'analyze', 'reprice', 'health', 'promotion', 'mi_snapshot', 'mi_self_check', 'listing_status_sync', 'auto_publish', 'ad_restore', 'blacklist_cleanup', 'guard_anomaly', 'cro_monthly_report', 'cro_consume', 'smart_bid', 'cro_image_refresh', 'cro_fill_specifics', 'cro_promote', 'cro_sentinel', 'bid_rollback', 'cro_delist_email', 'cro_ops', 'cro_lifecycle_detect', 'cro_title_rewrite', 'cro_lifecycle_evaluate', 'cro_send_offer', 'source_refresh', 'order_recheck', 'semantic_rewrite', 'finance_sync', 'giga_dropship_push', 'giga_dropship_sync'],
                       help='立即执行指定单个任务后退出')
    parser.add_argument('--status', action='store_true',
                       help='显示守护进程状态')
    args = parser.parse_args()

    # 让调度进程本身读取 .env, 使任务级开关 (ENABLE_MI_AUTO_PUBLISH 等) 在守护进程
    # 里可见 — task_* 在启动子进程前用 os.getenv 决定 dry-run/apply, 故必须在父进程。
    # 仅在真正启动 (main) 时加载, 不影响模块导入/测试。
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
    except Exception:
        pass

    if args.status:
        show_status()
        return

    if MAINTENANCE_FILE.exists():
        logger.critical(f"维护锁存在，拒绝启动调度器: {MAINTENANCE_FILE}")
        raise SystemExit(2)

    try:
        database_report = validate_runtime_database(PROJECT_ROOT / 'ebay_collection.db')
    except Exception as exc:
        logger.critical(f"数据库启动检查失败，拒绝运行任何任务: {exc}")
        raise SystemExit(2) from exc
    logger.info(
        "数据库启动检查通过: integrity=%s, links=%s, pages=%s",
        database_report.integrity_check,
        database_report.link_count,
        database_report.page_count,
    )

    if args.task:
        task_map = {
            'title': task_title_optimize,
            'listing_audit': task_listing_audit,
            'source_aspect_autofix': task_source_aspect_autofix,
            'daily': task_daily_full,
            'analyze': task_auto_analyze,
            'reprice': task_smart_reprice,
            'health': task_health_check,
            'promotion': task_promotion_rotate,
            'mi_snapshot': task_mi_snapshot,
            'mi_self_check': task_mi_self_check,
            'listing_status_sync': task_listing_status_sync,
            'auto_publish': task_auto_publish,
            'ad_restore': task_ad_restore,
            'blacklist_cleanup': task_blacklist_cleanup,
            'guard_anomaly': task_guard_anomaly,
            'cro_monthly_report': task_cro_monthly_report,
            'cro_consume': task_cro_consume,
            'smart_bid': task_smart_bid,
            'cro_image_refresh': task_cro_image_refresh,
            'cro_fill_specifics': task_cro_fill_specifics,
            'cro_promote': task_cro_promote,
            'cro_sentinel': task_cro_sentinel,
            'bid_rollback': task_bid_rollback,
            'cro_delist_email': task_cro_delist_email,
            'cro_ops': task_cro_ops_snapshot,
            'cro_lifecycle_detect': task_cro_lifecycle_detect,
            'cro_title_rewrite': task_cro_title_rewrite,
            'cro_lifecycle_evaluate': task_cro_lifecycle_evaluate,
            'cro_send_offer': task_cro_send_offer,
            'source_refresh': task_source_refresh,
            'order_recheck': task_order_recheck,
            'semantic_rewrite': task_semantic_rewrite,
            'finance_sync': task_finance_sync,
            'giga_dropship_push': task_giga_dropship_push,
            'giga_dropship_sync': task_giga_dropship_sync,
        }
        task_map[args.task]()
        return

    if args.once:
        run_once()
        return

    # 默认: 守护进程模式
    run_daemon()


def show_status():
    """显示守护进程和任务状态"""
    print("=" * 55)
    print("  Dajian Listing Tool — 调度器状态")
    print("=" * 55)

    # PID 检查
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            alive = _is_pid_alive(pid)
            status = "🟢 运行中" if alive else "🔴 已停止 (残留PID)"
            print(f"\n守护进程: {status} (PID={pid})")
        except Exception:
            print("\n守护进程: ⚪ 未知")
    else:
        fallback_printed = False
        if HEALTH_FILE.exists():
            try:
                health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
                health_pid = int(health.get('daemon_pid') or 0)
                if health_pid and _is_pid_alive(health_pid):
                    print(f"\n守护进程: 🟡 运行中但 PID 文件缺失 (PID={health_pid})")
                    fallback_printed = True
            except Exception:
                pass
        if not fallback_printed:
            print("\n守护进程: ⚪ 未启动")

    # 健康状态
    if HEALTH_FILE.exists():
        try:
            health = json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
            alive_at = health.get('daemon_alive_at', 'N/A')
            print(f"最后心跳: {alive_at}")
            print(f"\n任务状态:")
            display_tasks = dict(health.get('tasks', {}) or {})
            if isinstance(health.get('mi_self_check'), dict):
                mi_info = dict(health['mi_self_check'])
                if mi_info.get('checked_at') and not mi_info.get('at'):
                    mi_info['at'] = mi_info['checked_at']
                if mi_info.get('status') == 'ok':
                    mi_info['status'] = 'success'
                    mi_info['message'] = 'OK'
                display_tasks['mi_self_check'] = mi_info
            for name, info in display_tasks.items():
                if name.startswith('_'):
                    continue
                st = info.get('status', '?')
                at = info.get('at', '?')[:19]
                dur = info.get('duration_sec', 0)
                icon = {'success': '✅', 'failed': '❌', 'timeout': '⏰',
                        'running': '🔄', 'pending': '⏳', 'recovering': '🔄',
                        'ok': '✅'}.get(st, '⚪')
                print(f"  {icon} {name:20s} | {st:8s} | {at} | {dur:.0f}s")
        except Exception:
            print("  (无法读取健康状态)")
    else:
        print("\n  (无健康状态文件)")
    print()


if __name__ == '__main__':
    main()
