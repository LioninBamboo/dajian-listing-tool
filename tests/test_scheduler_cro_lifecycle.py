"""Scheduler relist-lifecycle hook tests — detect/evaluate only, never execute."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('scheduler_daemon', ROOT / 'scheduler_daemon.py')
scheduler_daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler_daemon)


class SchedulerCroLifecycleTests(unittest.TestCase):
    def test_detect_runs_cli_with_detect_apply_only(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=True),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_lifecycle_detect()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_lifecycle_detect')
        self.assertTrue(any(str(a).endswith('cro_relist_lifecycle.py') for a in cmd_args))
        self.assertIn('--detect', cmd_args)
        self.assertIn('--apply', cmd_args)
        # 调度器绝不自动执行 withdraw/republish
        self.assertNotIn('--execute', cmd_args)
        self.assertNotIn('--precheck', cmd_args)

    def test_evaluate_runs_cli_with_evaluate_apply_only(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=True),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_lifecycle_evaluate()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_lifecycle_evaluate')
        self.assertIn('--evaluate', cmd_args)
        self.assertIn('--apply', cmd_args)
        self.assertNotIn('--execute', cmd_args)

    def test_detect_skips_when_done_today(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=True),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_cro_lifecycle_detect()

        self.assertEqual(result, (True, 'Skipped (already succeeded today)'))
        run_task.assert_not_called()

    def test_evaluate_waits_for_daily_tasks(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=False),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_cro_lifecycle_evaluate()

        self.assertEqual(result, (True, 'Skipped (waiting for daily_tasks)'))
        run_task.assert_not_called()


if __name__ == '__main__':
    unittest.main()
