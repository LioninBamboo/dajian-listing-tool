"""价格守门员当日活动汇总测试 (P2, 2026-05)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CollectGuardActivityTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def _write_log(self, name: str, text: str):
        (self.tmpdir / name).write_text(text, encoding='utf-8')

    def _write_restore(self, totals: dict):
        from datetime import datetime
        d = datetime.now().strftime('%Y%m%d')
        path = self.tmpdir / f'ad_restore_audit_{d}_120000.json'
        path.write_text(json.dumps({'totals': totals}), encoding='utf-8')

    def test_empty_returns_zeros(self):
        from src.services import guard_activity as ga
        with patch.object(ga, 'LOG_DIR', self.tmpdir):
            act = ga.collect_guard_activity()
        self.assertEqual(act['rejects'], [])
        self.assertEqual(act['ad_offs'], [])
        self.assertEqual(act['restore_ok'], 0)
        self.assertFalse(act['has_restore_report'])

    def test_parses_rejects_and_ad_offs(self):
        from datetime import datetime
        from src.services import guard_activity as ga
        today = datetime.now().strftime('%Y-%m-%d')
        self._write_log('scheduler.log', f"""\
{today} 10:00:01 INFO doing stuff
{today} 10:00:02 WARNING 🚫 [PRICE GUARD] SKU-AAA: cost=100 floor=121
{today} 10:00:03 WARNING 🛑 [AD AUTO-OFF] SKU-BBB campaign=C1
{today} 10:00:04 WARNING 🚫 [PRICE GUARD] SKU-AAA: duplicate
2025-01-01 10:00:00 WARNING 🚫 [PRICE GUARD] SKU-OLD: should be ignored
""")
        with patch.object(ga, 'LOG_DIR', self.tmpdir):
            act = ga.collect_guard_activity()
        self.assertEqual(act['rejects'], ['SKU-AAA'])  # 去重
        self.assertEqual(act['ad_offs'], ['SKU-BBB'])
        self.assertNotIn('SKU-OLD', act['rejects'])

    def test_includes_restore_totals(self):
        from src.services import guard_activity as ga
        self._write_restore({
            'restored_ok': 3, 'restore_failed': 1,
            'skip_low_margin': 5, 'skip_unsafe': 2, 'evaluated_not_promoted': 50,
        })
        with patch.object(ga, 'LOG_DIR', self.tmpdir):
            act = ga.collect_guard_activity()
        self.assertEqual(act['restore_ok'], 3)
        self.assertEqual(act['restore_failed'], 1)
        self.assertEqual(act['restore_skip_low_margin'], 5)
        self.assertEqual(act['restore_skip_unsafe'], 2)
        self.assertTrue(act['has_restore_report'])


class RenderGuardActivityHtmlTests(unittest.TestCase):

    def test_empty_returns_blank(self):
        from src.services import guard_activity as ga
        html = ga.render_guard_activity_html({
            'rejects': [], 'ad_offs': [], 'restore_ok': 0,
            'restore_failed': 0, 'restore_skip_low_margin': 0,
            'restore_skip_unsafe': 0, 'has_restore_report': False,
        })
        self.assertEqual(html, '')

    def test_renders_section_when_activity_exists(self):
        from src.services import guard_activity as ga
        html = ga.render_guard_activity_html({
            'rejects': ['SKU-1'], 'ad_offs': ['SKU-2'],
            'restore_ok': 3, 'restore_failed': 0,
            'restore_skip_low_margin': 1, 'restore_skip_unsafe': 0,
            'restore_evaluated': 100, 'has_restore_report': True,
        })
        self.assertIn('价格守门员', html)
        self.assertIn('SKU-1', html)
        self.assertIn('SKU-2', html)


if __name__ == '__main__':
    unittest.main()
