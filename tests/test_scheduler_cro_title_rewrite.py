"""Scheduler CRO title rewrite hook tests."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('scheduler_daemon', ROOT / 'scheduler_daemon.py')
scheduler_daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler_daemon)


class SchedulerCroTitleRewriteTests(unittest.TestCase):
    def test_title_rewrite_runs_apply_by_default(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=True),
            patch.dict(scheduler_daemon.os.environ, {}, clear=False),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_title_rewrite()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_title_rewrite')
        self.assertTrue(any(str(a).endswith('cro_title_rewrite.py') for a in cmd_args))
        self.assertIn('--limit', cmd_args)
        self.assertIn('50', cmd_args)
        self.assertIn('--apply', cmd_args)
        self.assertIn('--yes', cmd_args)

    def test_title_rewrite_waits_for_daily_tasks(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=False),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_cro_title_rewrite()

        self.assertEqual(result, (True, 'Skipped (waiting for daily_tasks)'))
        run_task.assert_not_called()

    def test_title_rewrite_dry_run_can_be_forced_via_env(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=True),
            patch.dict(
                scheduler_daemon.os.environ,
                {
                    'ENABLE_SCHEDULED_TITLE_REWRITE_APPLY': '0',
                    'SCHEDULED_TITLE_REWRITE_DRY_RUN_LIMIT': '25',
                },
                clear=False,
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_title_rewrite()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_title_rewrite')
        self.assertNotIn('--apply', cmd_args)
        self.assertNotIn('--yes', cmd_args)
        limit_index = cmd_args.index('--limit')
        self.assertEqual(cmd_args[limit_index + 1], '25')


if __name__ == '__main__':
    unittest.main()
