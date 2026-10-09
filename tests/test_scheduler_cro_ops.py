"""Scheduler CRO ops snapshot hook tests."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('scheduler_daemon', ROOT / 'scheduler_daemon.py')
scheduler_daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler_daemon)


class SchedulerCroOpsTests(unittest.TestCase):
    def test_cro_ops_snapshot_runs_cli_with_dr_drill(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_ops_snapshot()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_ops_snapshot')
        self.assertTrue(any(str(arg).endswith('cro_ops_snapshot.py') for arg in cmd_args))
        self.assertIn('--record-capacity-sample', cmd_args)
        self.assertIn('--dr-drill', cmd_args)

    def test_cro_ops_snapshot_skips_when_done_today(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=True),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_cro_ops_snapshot()

        self.assertEqual(result, (True, 'Skipped (already succeeded today)'))
        run_task.assert_not_called()


if __name__ == '__main__':
    unittest.main()