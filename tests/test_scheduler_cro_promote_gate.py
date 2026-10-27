"""scheduler_enable_cro_promote gates task_cro_promote (default off)."""
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from datetime import datetime
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
        self.assertEqual(health.call_args.args[1], 'skipped')
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


def _main_profile(**overrides):
    from src.utils.store_profile import StoreProfile

    fields = {'brand_name': 'AquaVerve', 'scheduler_profile': 'full'}
    fields.update(overrides)
    return StoreProfile(**fields)


def _aqua_profile():
    """AquaRides ops profile: ads on, no cro_promote key (dataclass default)."""
    from src.utils.store_profile import StoreProfile

    return StoreProfile(
        brand_name='AquaRides',
        scheduler_profile='ops',
        scheduler_enable_ads=True,
    )


class SchedulerAdCreateGateTests(unittest.TestCase):
    """ad_restore / ads_enroll are blocked on Main AquaVerve only."""

    def test_main_skips_ad_restore_and_ads_enroll(self):
        profile = _main_profile()
        self.assertTrue(profile.scheduled_ad_create_blocked)
        self.assertFalse(profile.scheduler_enable_cro_promote)
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, 'get_store_profile', return_value=profile),
            patch.object(scheduler_daemon, 'update_health') as health,
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            restore = scheduler_daemon.task_ad_restore()
            enroll = scheduler_daemon.task_ads_enroll()

        self.assertEqual(restore, (True, scheduler_daemon.MAIN_AD_CREATE_SKIP_MESSAGE))
        self.assertEqual(enroll, (True, scheduler_daemon.MAIN_AD_CREATE_SKIP_MESSAGE))
        run_task.assert_not_called()
        statuses = [call.args[1] for call in health.call_args_list]
        names = [call.args[0] for call in health.call_args_list]
        self.assertEqual(names, ['ad_restore', 'ads_enroll'])
        self.assertEqual(statuses, ['skipped', 'skipped'])
        for call in health.call_args_list:
            self.assertIn('scheduler_enable_cro_promote off', call.args[2])

    def test_main_runs_ad_jobs_when_promote_flag_explicitly_on(self):
        profile = _main_profile(scheduler_enable_cro_promote=True)
        self.assertFalse(profile.scheduled_ad_create_blocked)
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, 'get_store_profile', return_value=profile),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_ad_restore()
            scheduler_daemon.task_ads_enroll()

        launched = [call.args[0] for call in run_task.call_args_list]
        self.assertEqual(launched, ['ad_restore', 'ads_enroll'])

    def test_aqua_profile_without_cro_promote_key_still_runs_ads(self):
        profile = _aqua_profile()
        self.assertTrue(profile.scheduler_enable_ads)
        self.assertTrue(profile.ops_ads_enabled)
        self.assertFalse(profile.scheduler_enable_cro_promote)
        self.assertFalse(profile.scheduled_ad_create_blocked)
        with (
            patch.object(scheduler_daemon, '_task_succeeded_today', return_value=False),
            patch.object(scheduler_daemon, 'get_store_profile', return_value=profile),
            patch.object(scheduler_daemon, 'run_task') as run_task,
        ):
            scheduler_daemon.task_ad_restore()
            scheduler_daemon.task_ads_enroll()

        launched = [call.args[0] for call in run_task.call_args_list]
        self.assertEqual(launched, ['ad_restore', 'ads_enroll'])
        enroll_cmd = run_task.call_args_list[1].args[1]
        self.assertTrue(any(str(a).endswith('store_marketing.py') for a in enroll_cmd))
        self.assertIn('--apply', enroll_cmd)

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
        self.assertTrue(profile.scheduled_ad_create_blocked)

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
            self.assertEqual(profile.brand_name, 'AquaVerve')
            self.assertFalse(profile.scheduled_ad_create_blocked)

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

    def test_tracked_main_yaml_blocks_ad_create_without_promote_key(self):
        from src.utils.store_profile import load_store_profile

        profile = load_store_profile(ROOT / 'config' / 'store_profile.yaml')
        text = (ROOT / 'config' / 'store_profile.yaml').read_text(encoding='utf-8')
        self.assertNotIn('scheduler_enable_cro_promote', text)
        self.assertEqual(profile.brand_name, 'AquaVerve')
        self.assertTrue(profile.scheduled_ad_create_blocked)

    def test_aqua_yaml_without_promote_key_keeps_ads(self):
        from src.utils.store_profile import load_store_profile

        with tempfile.TemporaryDirectory() as td:
            yaml_file = Path(td) / 'store.yaml'
            yaml_file.write_text(
                textwrap.dedent(
                    """
                    store:
                      brand_name: AquaRides
                    server:
                      scheduler_profile: ops
                      scheduler_enable_ads: true
                    """
                ),
                encoding='utf-8',
            )
            profile = load_store_profile(yaml_file)
        self.assertTrue(profile.scheduler_enable_ads)
        self.assertFalse(profile.scheduler_enable_cro_promote)
        self.assertFalse(profile.scheduled_ad_create_blocked)
        self.assertTrue(profile.ops_ads_enabled)


class GateSkipVisibilityTests(unittest.TestCase):
    def test_show_status_prints_skipped_reason(self):
        health = Path(tempfile.mkdtemp()) / 'health.json'
        health.write_text(
            json.dumps({
                'tasks': {
                    'ad_restore': {
                        'status': 'skipped',
                        'message': scheduler_daemon.MAIN_AD_CREATE_SKIP_MESSAGE,
                        'at': datetime.now().isoformat(),
                        'duration_sec': 0,
                    }
                }
            }),
            encoding='utf-8',
        )
        buf = io.StringIO()
        with (
            patch.object(scheduler_daemon, 'HEALTH_FILE', health),
            patch.object(scheduler_daemon, 'PID_FILE', health.parent / 'missing.pid'),
            redirect_stdout(buf),
        ):
            scheduler_daemon.show_status()
        text = buf.getvalue()
        self.assertIn('skipped', text)
        self.assertIn('scheduler_enable_cro_promote off', text)

    def test_gate_skip_counts_as_done_today(self):
        now = datetime.now().replace(microsecond=0)
        health = Path(tempfile.mkdtemp()) / 'health.json'
        health.write_text(
            json.dumps({
                'tasks': {
                    'ad_restore': {
                        'status': 'skipped',
                        'message': scheduler_daemon.MAIN_AD_CREATE_SKIP_MESSAGE,
                        'at': now.isoformat(),
                        'duration_sec': 0,
                    },
                    'health_check': {
                        'status': 'skipped',
                        'message': 'Skipped (something else)',
                        'at': now.isoformat(),
                        'duration_sec': 0,
                    },
                }
            }),
            encoding='utf-8',
        )
        with patch.object(scheduler_daemon, 'HEALTH_FILE', health):
            self.assertTrue(scheduler_daemon._task_succeeded_today('ad_restore', now=now))
            self.assertFalse(scheduler_daemon._task_succeeded_today('health_check', now=now))

    def test_watchdog_gate_skip_counts_as_done_today(self):
        import scheduler_watchdog as watchdog

        today = datetime.now().date()
        at = datetime.now().isoformat()
        health = {
            'tasks': {
                'ads_enroll': {
                    'status': 'skipped',
                    'at': at,
                    'message': scheduler_daemon.MAIN_AD_CREATE_SKIP_MESSAGE,
                },
                'health_check': {
                    'status': 'skipped',
                    'at': at,
                    'message': 'Skipped (something else)',
                },
            }
        }
        self.assertTrue(watchdog._task_succeeded_today(health, 'ads_enroll', today))
        self.assertFalse(watchdog._task_succeeded_today(health, 'health_check', today))


if __name__ == '__main__':
    unittest.main()
