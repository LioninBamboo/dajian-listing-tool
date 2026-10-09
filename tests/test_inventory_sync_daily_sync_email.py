from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_get_notification_recipient_reads_only_notification_email(monkeypatch):
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
    from src.plugins.inventory_sync import daily_sync

    monkeypatch.delenv("NOTIFICATION_EMAIL", raising=False)
    monkeypatch.setenv("USER_EMAIL", "xiutingpoon@gmail.com")

    assert daily_sync.get_notification_recipient() == ""

    monkeypatch.setenv("NOTIFICATION_EMAIL", "lioninbamboo@163.com")
    assert daily_sync.get_notification_recipient() == "lioninbamboo@163.com"


def test_maybe_send_email_report_skips_when_notification_email_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
    from src.plugins.inventory_sync import daily_sync

    monkeypatch.delenv("NOTIFICATION_EMAIL", raising=False)
    monkeypatch.setenv("USER_EMAIL", "xiutingpoon@gmail.com")

    called = {}

    def fake_send_email_report(results, to_email):
        called["results"] = results
        called["to_email"] = to_email
        return True

    monkeypatch.setattr(daily_sync, "send_email_report", fake_send_email_report)

    assert daily_sync.maybe_send_email_report(["row"]) is False
    assert called == {}
