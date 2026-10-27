"""scheduler_enable_cro_promote gates task_cro_promote (default off)."""
from __future__ import annotations

import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'scheduler_daemon', ROOT / 'scheduler_daemon.py'
)
scheduler_daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler_daemon)


class SchedulerCroPromoteGateTests(unittest.TestCase):
    def test_promote_skips_when_flag_absent(self):
        """Absent / false profile flag => no cro_promote.py live write."""
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=False
            ),
            patch.object(scheduler_daemon, 'update_health') as health,
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_cro_promote()

        self.assertEqual(
            result, (True, 'Skipped (scheduler_enable_cro_promote off)')
        )
        run_task.assert_not_called()
        health.assert_called_once()
        self.assertEqual(health.call_args.args[0], 'cro_promote')
        self.assertEqual(health.call_args.args[1], 'success')
        self.assertIn('scheduler_enable_cro_promote off', health.call_args.args[2])

    def test_promote_runs_when_flag_enabled(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=True
            ),
            patch.object(
                scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=True
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_promote()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_promote')
        self.assertTrue(any(str(a).endswith('cro_promote.py') for a in cmd_args))
        self.assertIn('--apply', cmd_args)
        self.assertIn('--limit', cmd_args)
        self.assertIn('200', cmd_args)
        self.assertIn('--email', cmd_args)

    def test_helper_fail_closed_when_profile_unreadable(self):
        with patch.object(
            scheduler_daemon,
            'get_store_profile',
            side_effect=RuntimeError('no profile'),
        ):
            self.assertFalse(scheduler_daemon._cro_promote_scheduler_enabled())


class SchedulerSmartBidRaiseGateTests(unittest.TestCase):
    """smart_bid may lower without the flag; raises need the same opt-in."""

    def test_smart_bid_passes_no_raises_when_flag_off(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=False
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_smart_bid()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'smart_bid')
        self.assertTrue(any(str(a).endswith('batch_smart_bid.py') for a in cmd_args))
        self.assertIn('--apply', cmd_args)
        self.assertIn('--email', cmd_args)
        self.assertIn('--no-raises', cmd_args)

    def test_smart_bid_allows_raises_when_flag_on(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=True
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_smart_bid()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'smart_bid')
        self.assertIn('--apply', cmd_args)
        self.assertNotIn('--no-raises', cmd_args)


class SchedulerAdCreateGateTests(unittest.TestCase):
    """create_ad_safe jobs stay off unless scheduler_enable_cro_promote is on."""

    def test_ad_restore_skips_when_flag_off(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=False
            ),
            patch.object(scheduler_daemon, 'update_health') as health,
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_ad_restore()

        self.assertEqual(
            result, (True, 'Skipped (scheduler_enable_cro_promote off)')
        )
        run_task.assert_not_called()
        self.assertEqual(health.call_args.args[0], 'ad_restore')

    def test_ad_restore_runs_when_flag_on(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=True
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_ad_restore()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'ad_restore')
        self.assertTrue(any(str(a).endswith('ad_restore_audit.py') for a in cmd_args))
        self.assertIn('--apply', cmd_args)

    def test_ads_enroll_runs_when_flag_on(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=True
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_ads_enroll()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'ads_enroll')
        self.assertTrue(any(str(a).endswith('store_marketing.py') for a in cmd_args))
        self.assertIn('--apply', cmd_args)

    def test_ads_enroll_skips_when_flag_off(self):
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=False
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            result = scheduler_daemon.task_ads_enroll()

        self.assertEqual(
            result, (True, 'Skipped (scheduler_enable_cro_promote off)')
        )
        run_task.assert_not_called()

    def test_cro_consume_does_not_launch_ad_writers(self):
        """10:00 queue consume is price_drop only, even if promote is the topic."""
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(
                scheduler_daemon, '_daily_tasks_ready_for_cro', return_value=True
            ),
            patch.object(
                scheduler_daemon, '_cro_promote_scheduler_enabled', return_value=False
            ),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_cro_consume()

        task_name, cmd_args = run_task.call_args.args[:2]
        self.assertEqual(task_name, 'cro_consume')
        joined = ' '.join(str(a) for a in cmd_args)
        self.assertIn('batch_smart_reprice.py', joined)
        self.assertIn('--from-cro-queue', joined)
        self.assertNotIn('cro_promote.py', joined)
        self.assertNotIn('ad_restore_audit.py', joined)
        self.assertNotIn('store_marketing.py', joined)


class StoreProfileCroPromoteFlagTests(unittest.TestCase):
    def test_default_flag_absent_is_false(self):
        from src.utils.store_profile import StoreProfile

        profile = StoreProfile()
        self.assertFalse(profile.scheduler_enable_cro_promote)
        self.assertFalse(profile.cro_promote_scheduler_enabled)

    def test_yaml_true_enables_property(self):
        from src.utils.store_profile import load_store_profile

        with tempfile.TemporaryDirectory() as td:
            yaml_file = Path(td) / 'store.yaml'
            yaml_file.write_text(
                textwrap.dedent(
                    """
                    server:
                      scheduler_profile: full
                      scheduler_enable_cro_promote: true
                    """
                ),
                encoding='utf-8',
            )
            profile = load_store_profile(yaml_file)
            self.assertTrue(profile.scheduler_enable_cro_promote)
            self.assertTrue(profile.cro_promote_scheduler_enabled)

    def test_relist_flag_defaults_false_and_yaml_can_enable(self):
        from src.utils.store_profile import StoreProfile, load_store_profile

        profile = StoreProfile()
        self.assertFalse(profile.scheduler_enable_cro_relist)
        self.assertFalse(profile.cro_relist_scheduler_enabled)

        with tempfile.TemporaryDirectory() as td:
            yaml_file = Path(td) / 'store.yaml'
            yaml_file.write_text(
                textwrap.dedent(
                    """
                    server:
                      scheduler_enable_cro_relist: true
                    """
                ),
                encoding='utf-8',
            )
            loaded = load_store_profile(yaml_file)
            self.assertTrue(loaded.scheduler_enable_cro_relist)
            self.assertTrue(loaded.cro_relist_scheduler_enabled)


if __name__ == '__main__':
    unittest.main()
