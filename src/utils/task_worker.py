"""Run one scheduled Python script while the actual worker owns its task lock."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.process_identity import (  # noqa: E402
    get_process_creation_marker,
    is_process_identity_alive,
)


ALREADY_RUNNING_EXIT_CODE = 75


def _write_json_atomic(path: Path, payload: dict, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{token}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def _lock_is_stale(lock_info: dict, timeout_sec: int, now: datetime) -> bool:
    pid = int(lock_info.get("worker_pid") or lock_info.get("pid") or 0)
    marker = lock_info.get("worker_creation_marker")
    if pid:
        return not is_process_identity_alive(pid, marker)

    try:
        started_at = datetime.fromisoformat(str(lock_info.get("started_at") or ""))
    except ValueError:
        started_at = None
    if started_at is None:
        return not pid

    recorded_timeout = int(lock_info.get("timeout_sec") or timeout_sec)
    max_age = timedelta(seconds=max(recorded_timeout + 600, 1800))
    return now - started_at > max_age


def _acquire_worker_lock(
    lock_path: Path,
    *,
    task_name: str,
    timeout_sec: int,
    owner_token: str,
) -> int | None:
    lock_info = {
        "task_name": task_name,
        "pid": os.getpid(),
        "worker_pid": os.getpid(),
        "worker_creation_marker": get_process_creation_marker(os.getpid()),
        "owner_token": owner_token,
        "started_at": datetime.now().isoformat(),
        "timeout_sec": int(timeout_sec),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(3):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                payload = json.dumps(lock_info, ensure_ascii=False, indent=2).encode("utf-8")
                os.write(fd, payload)
                os.fsync(fd)
                if sys.platform != "win32":
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Keep this descriptor open for the complete target lifecycle.
                # On Windows the live handle denies deletion; on POSIX flock
                # provides the equivalent ownership barrier for reapers.
                return fd
            except Exception:
                os.close(fd)
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                raise
        except FileExistsError:
            observed_text = None
            try:
                observed_text = lock_path.read_text(encoding="utf-8")
                existing = json.loads(observed_text)
            except Exception:
                # Another worker may be between O_EXCL creation and its fsynced
                # JSON payload. Treat a recent unreadable lock as active; only
                # reap an abandoned unreadable file after a bounded age.
                try:
                    age_sec = max(0.0, time.time() - lock_path.stat().st_mtime)
                except OSError:
                    continue
                if age_sec <= max(int(timeout_sec) + 600, 1800):
                    return None
                try:
                    observed_text = lock_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                existing = {}
            if not _lock_is_stale(existing, timeout_sec, datetime.now()):
                return None
            if _try_reap_observed_stale_lock(
                lock_path,
                observed_text=observed_text or "",
                timeout_sec=timeout_sec,
            ):
                continue
            return None
    return None


def _try_reap_observed_stale_lock(
    lock_path: Path,
    *,
    observed_text: str,
    timeout_sec: int,
) -> bool:
    """Remove exactly the stale file observed by the caller, never its replacement."""
    if sys.platform == "win32":
        try:
            if lock_path.read_text(encoding="utf-8") != observed_text:
                return False
            # A replacement live worker keeps its descriptor open without
            # delete sharing, so this unlink fails instead of deleting it.
            lock_path.unlink()
            return True
        except OSError:
            return False

    fd = None
    try:
        import fcntl

        fd = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.lseek(fd, 0, os.SEEK_SET)
        current_text = os.read(fd, max(1, len(observed_text.encode("utf-8")) + 4096)).decode(
            "utf-8", errors="replace"
        )
        if current_text != observed_text:
            return False
        try:
            current_info = json.loads(current_text)
        except Exception:
            current_info = {}
        if not _lock_is_stale(current_info, timeout_sec, datetime.now()):
            return False
        lock_path.unlink()
        return True
    except (BlockingIOError, FileNotFoundError, OSError):
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _release_worker_lock(lock_path: Path, owner_token: str, lock_fd: int | None) -> None:
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
        if existing.get("owner_token") == owner_token:
            lock_path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def _exit_code_from_system_exit(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    print(exc.code, file=sys.stderr)
    return 1


def _run_python_command(command: list[str]) -> int:
    if not command:
        print("task worker received an empty command", file=sys.stderr)
        return 2

    old_argv = sys.argv[:]
    old_path = sys.path[:]
    try:
        if command[0] == "-m" and len(command) >= 2:
            sys.argv = [command[1], *command[2:]]
            try:
                runpy.run_module(command[1], run_name="__main__", alter_sys=True)
            except SystemExit as exc:
                return _exit_code_from_system_exit(exc)
            return 0

        target = Path(command[0])
        if not target.is_absolute():
            target = (PROJECT_ROOT / target).resolve()
        if not target.is_file():
            print(f"task worker target not found: {target}", file=sys.stderr)
            return 2

        sys.argv = [str(target), *command[1:]]
        sys.path.insert(0, str(target.parent))
        try:
            runpy.run_path(str(target), run_name="__main__")
        except SystemExit as exc:
            return _exit_code_from_system_exit(exc)
        return 0
    finally:
        sys.argv = old_argv
        sys.path[:] = old_path


def _update_health_if_parent_died(
    health_path: Path,
    *,
    task_name: str,
    status: str,
    message: str,
    duration_sec: float,
    parent_pid: int,
    parent_creation_marker: str | None,
    owner_token: str,
) -> None:
    if is_process_identity_alive(parent_pid, parent_creation_marker):
        return
    try:
        health = json.loads(health_path.read_text(encoding="utf-8")) if health_path.exists() else {}
    except Exception:
        health = {}
    health.setdefault("tasks", {})[task_name] = {
        "status": status,
        "message": message[:500],
        "at": datetime.now().isoformat(),
        "duration_sec": round(duration_sec, 1),
    }
    try:
        _write_json_atomic(health_path, health, owner_token)
    except OSError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--lock-path", required=True)
    parser.add_argument("--handshake-path", required=True)
    parser.add_argument("--health-path", required=True)
    parser.add_argument("--owner-token", required=True)
    parser.add_argument("--timeout-sec", required=True, type=int)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--parent-creation-marker")
    args, command = parser.parse_known_args(argv)
    if command and command[0] == "--":
        command = command[1:]

    lock_path = Path(args.lock_path)
    handshake_path = Path(args.handshake_path)
    health_path = Path(args.health_path)
    lock_fd = _acquire_worker_lock(
        lock_path,
        task_name=args.task_name,
        timeout_sec=args.timeout_sec,
        owner_token=args.owner_token,
    )
    acquired = lock_fd is not None
    _write_json_atomic(
        handshake_path,
        {"claimed": acquired, "worker_pid": os.getpid()},
        args.owner_token,
    )
    if not acquired:
        return ALREADY_RUNNING_EXIT_CODE

    started = time.time()
    returncode = 1
    try:
        returncode = _run_python_command(command)
        return returncode
    finally:
        duration = time.time() - started
        status = "success" if returncode == 0 else "failed"
        _update_health_if_parent_died(
            health_path,
            task_name=args.task_name,
            status=status,
            message=f"Worker exit code {returncode} after parent exit",
            duration_sec=duration,
            parent_pid=args.parent_pid,
            parent_creation_marker=args.parent_creation_marker,
            owner_token=args.owner_token,
        )
        _release_worker_lock(lock_path, args.owner_token, lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
