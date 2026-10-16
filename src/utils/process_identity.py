"""Small cross-process identity helpers used by the Windows scheduler."""

from __future__ import annotations

import os
import sys


def get_process_creation_marker(pid: int) -> str | None:
    """Return a stable creation marker so PID reuse is not mistaken for liveness."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created = FILETIME()
                exited = FILETIME()
                kernel = FILETIME()
                user = FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return f"win-filetime:{ticks}"
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None

    try:
        fields = (f"/proc/{pid}/stat")
        with open(fields, "r", encoding="utf-8") as stream:
            # Field 22 is the process start time in clock ticks. Splitting from
            # the final ')' avoids spaces in the process name corrupting fields.
            tail = stream.read().rsplit(")", 1)[1].strip().split()
        return f"proc-start-ticks:{tail[19]}"
    except Exception:
        return None


def is_process_identity_alive(pid: int, creation_marker: str | None = None) -> bool:
    """Check both process liveness and, when available, its creation identity."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    current_marker = get_process_creation_marker(pid)
    if current_marker is None:
        if sys.platform == "win32":
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    return creation_marker in (None, "") or current_marker == creation_marker
