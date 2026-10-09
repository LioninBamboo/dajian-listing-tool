import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("daily_tasks", ROOT / "daily_tasks.py")
daily_tasks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_tasks)


class DailySummarySmartRepriceTests(unittest.TestCase):
    def test_daily_summary_uses_smart_reprice_thumbnail_title_and_reason(self):
        captured = {}

        def fake_send_email(subject, body, attachments=None):
            captured["subject"] = subject
            captured["body"] = body
            return True

        results = {
            "analyze": {"success": 0, "failed": 0},
            "inventory": {"checked": 0, "no_change": 0, "price_changed": []},
            "health_check": {},
            "smart_reprice": {
                "status": "ok",
                "timestamp": "2026-04-23T10:00:00",
                "mode": "apply",
                "market_mode": "auto",
                "path": str(ROOT / "reports" / "reprice_report_test.json"),
                "summary": {
                    "total": 1,
                    "price_changes": 1,
                    "price_up": 1,
                    "price_down": 0,
                    "no_change": 0,
                    "errors": 0,
                },
                "changed_rows": [
                    {
                        "sku": "W123",
                        "title": "Storage Cabinet",
                        "image_url": "https://example.com/cabinet.jpg",
                        "old_price": 100.0,
                        "new_price": 112.5,
                        "pct_change": 12.5,
                        "price_reason": "市场均价 $120.00，采用竞争价。",
                    }
                ],
            },
        }

        with (
            patch("src.utils.email_sender.send_email", side_effect=fake_send_email),
            patch.object(
                daily_tasks,
                "build_thumbnail_img_html",
                return_value="<img src='daily-thumb' />",
            ),
        ):
            ok = daily_tasks.send_daily_summary_email(results)

        self.assertTrue(ok)
        body = captured["body"]
        self.assertIn("<img src='daily-thumb' />", body)
        self.assertIn("W123", body)
        self.assertIn("Storage Cabinet", body)
        self.assertIn("市场均价 $120.00，采用竞争价。", body)
        self.assertIn("Product", body)


if __name__ == "__main__":
    unittest.main()
