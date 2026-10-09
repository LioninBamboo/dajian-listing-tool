import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "batch_smart_reprice",
    ROOT / "scripts" / "batch_smart_reprice.py",
)
batch_smart_reprice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch_smart_reprice)


class SmartRepriceEmailTests(unittest.TestCase):
    def test_changed_skus_include_thumbnail_and_reason(self):
        captured = {}

        def fake_send_email(subject, body, attachments=None):
            captured["subject"] = subject
            captured["body"] = body
            captured["attachments"] = attachments or []
            return True

        result_row = {
            "sku": "W123",
            "title": "Storage Cabinet",
            "image_url": "https://example.com/cabinet.jpg",
            "current_price": 100.00,
            "new_price": 112.50,
            "price_diff": 12.50,
            "pct_change": 12.5,
            "margin": 0.18,
            "strategy": "MARKET_MATCH",
            "status": "updated",
            "price_reason": "市场均价 $120.00，采用竞争价 $114.00，最终价 $112.50。",
        }

        with (
            patch("src.utils.email_sender.send_email", side_effect=fake_send_email),
            patch.object(
                batch_smart_reprice,
                "build_thumbnail_img_html",
                return_value="<img src='cid-thumb' />",
            ),
            patch.object(batch_smart_reprice, "log"),
        ):
            batch_smart_reprice._send_reprice_email(
                [result_row],
                n_changes=1,
                n_up=1,
                n_down=0,
                n_errors=0,
                n_no_market=0,
                strategies={"MARKET_MATCH": 1},
                total=1,
            )

        body = captured["body"]
        self.assertIn("<img src='cid-thumb' />", body)
        self.assertIn("W123", body)
        self.assertIn("Storage Cabinet", body)
        self.assertIn("市场均价 $120.00", body)
        self.assertIn("<th>Product</th>", body)
        self.assertIn("<th>Reason</th>", body)


if __name__ == "__main__":
    unittest.main()
