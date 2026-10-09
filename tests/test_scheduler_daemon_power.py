import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scheduler_daemon", ROOT / "scheduler_daemon.py")
scheduler_daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler_daemon)


class SchedulerPowerRequestTests(unittest.TestCase):
    def test_awake_request_does_not_keep_display_on(self):
        flags = scheduler_daemon._windows_awake_flags(True)

        self.assertTrue(flags & scheduler_daemon.ES_CONTINUOUS)
        self.assertTrue(flags & scheduler_daemon.ES_SYSTEM_REQUIRED)
        self.assertFalse(flags & scheduler_daemon.ES_DISPLAY_REQUIRED)

    def test_release_request_clears_system_required(self):
        flags = scheduler_daemon._windows_awake_flags(False)

        self.assertEqual(flags, scheduler_daemon.ES_CONTINUOUS)

    def test_windows_request_uses_injected_api(self):
        calls = []

        def fake_set_thread_execution_state(flags):
            calls.append(flags)
            return 1

        with (
            patch.object(scheduler_daemon.sys, "platform", "win32"),
            patch.object(scheduler_daemon, "logger"),
        ):
            ok = scheduler_daemon.request_system_awake(
                True,
                set_thread_execution_state=fake_set_thread_execution_state,
            )

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [scheduler_daemon.ES_CONTINUOUS | scheduler_daemon.ES_SYSTEM_REQUIRED],
        )

    def test_non_windows_request_is_noop(self):
        with patch.object(scheduler_daemon.sys, "platform", "linux"):
            ok = scheduler_daemon.request_system_awake(True)

        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
