from __future__ import annotations

import os
from datetime import datetime

from src.utils import email_sender


def test_send_email_loads_env_before_reading_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv('NOTIFICATION_EMAIL', raising=False)
    monkeypatch.delenv('NOTIFICATION_EMAIL_PASSWORD', raising=False)
    monkeypatch.setattr(email_sender, '_ENV_LOADED', False)

    calls = []

    def fake_load_dotenv(path, override=False):
        calls.append((path, override))
        monkeypatch.setenv('NOTIFICATION_EMAIL', 'sender@example.com')
        monkeypatch.setenv('NOTIFICATION_EMAIL_PASSWORD', 'secret')
        return True

    monkeypatch.setattr(email_sender, 'load_dotenv', fake_load_dotenv)
    monkeypatch.setattr(email_sender, '_save_local_report', lambda subject, body: str(tmp_path / 'report.html'))
    monkeypatch.setattr(email_sender, '_build_mime_message', lambda *args, **kwargs: object())
    monkeypatch.setattr(email_sender, '_send_via_smtp', lambda *args, **kwargs: None)

    ok = email_sender.send_email('subject', '<p>body</p>', retry_delay=0)

    assert ok is True
    assert calls == [(email_sender.PROJECT_ROOT / '.env', False)]


def test_purge_old_email_report_artifacts_keeps_recent_files(tmp_path):
    old = tmp_path / 'daily_report_20260101_010101.html'
    old.write_text('old', encoding='utf-8')
    keep = tmp_path / 'daily_report_20260129_120000.html'
    keep.write_text('keep', encoding='utf-8')
    digest_keep = tmp_path / 'mi_digest_20260129.html'
    digest_keep.write_text('digest', encoding='utf-8')
    report_old = tmp_path / 'reprice_report_20260101_0101.json'
    report_old.write_text('{}', encoding='utf-8')
    unrelated = tmp_path / 'mi_opportunities_20260101_010101.json'
    unrelated.write_text('{}', encoding='utf-8')

    old_ts = datetime(2026, 1, 1, 1, 1, 1).timestamp()
    keep_ts = datetime(2026, 1, 29, 12, 0, 0).timestamp()
    for path, ts in ((old, old_ts), (report_old, old_ts), (keep, keep_ts), (digest_keep, keep_ts), (unrelated, old_ts)):
        path.touch()
        os.utime(path, (ts, ts))

    removed = email_sender._purge_old_email_report_artifacts(
        tmp_path,
        keep_days=3,
        now=datetime(2026, 1, 30, 12, 0, 0),
    )

    assert removed == 2
    assert not old.exists()
    assert not report_old.exists()
    assert keep.exists()
    assert digest_keep.exists()
    assert unrelated.exists()
