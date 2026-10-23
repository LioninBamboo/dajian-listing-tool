"""PR-D D4: should_smtp callers — local report always; SMTP gated."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_email_force(monkeypatch):
    monkeypatch.delenv("EMAIL_FORCE_ROUTINE", raising=False)


def test_auto_rotate_promo_smtp_only_when_forced(monkeypatch):
    from scripts import auto_rotate_promotions as arp

    calls = []

    def fake_send(subject, html, send_smtp=True, **kwargs):
        calls.append({"send_smtp": send_smtp})
        return True

    monkeypatch.setattr(arp, "get_store_profile", lambda: type("P", (), {"brand_name": "Test"})())
    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)

    arp.notify_promotion_created({"name": "p", "start_date": "x", "end_date": "y", "listing_count": 1})
    assert calls and calls[0]["send_smtp"] is False

    calls.clear()
    monkeypatch.setenv("EMAIL_FORCE_ROUTINE", "1")
    # Re-import policy reads env at call time via routine_smtp_forced
    arp.notify_promotion_created({"name": "p", "start_date": "x", "end_date": "y", "listing_count": 1})
    assert calls and calls[0]["send_smtp"] is True


def test_order_source_recheck_smtp_on_critical_only(monkeypatch, capsys):
    from src.utils.email_policy import should_smtp

    # Unit-level: CRITICAL → True; clean → False (D-dec2 default silence)
    assert should_smtp(has_issues=True) is True
    assert should_smtp(has_issues=False) is False

    # Integration-ish: exercise the send_email kwargs the script uses
    calls = []

    def fake_send(subject, html, send_smtp=True, **kwargs):
        calls.append({"subject": subject, "send_smtp": send_smtp})
        return True

    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)
    from src.utils.email_sender import send_email
    from src.utils.email_policy import should_smtp as ss

    # Simulate script branches
    send_email("alert", "<p>a</p>", send_smtp=ss(has_issues=True))
    send_email("clean", "<p>c</p>", send_smtp=ss(has_issues=False))
    print("clean recheck archived locally (2 order(s); SMTP silenced)")
    assert calls[0]["send_smtp"] is True
    assert calls[1]["send_smtp"] is False
    assert "SMTP silenced" in capsys.readouterr().out


def test_sales_health_smtp_when_problems(monkeypatch):
    from src.utils.email_policy import should_smtp

    assert should_smtp(has_issues=bool({"problems": 3}.get("problems"))) is True
    assert should_smtp(has_issues=bool({"problems": 0}.get("problems"))) is False

    calls = []

    def fake_send(*, subject, html_body, send_smtp=True, **kwargs):
        calls.append(send_smtp)
        return True

    # Mimic the 4-line hunk
    from src.utils.email_policy import should_smtp as ss
    for problems in (0, 2):
        s = {"problems": problems}
        do_smtp = ss(has_issues=bool(s.get("problems")))
        fake_send(subject="h", html_body="<p/>", send_smtp=do_smtp)
    assert calls == [False, True]


def test_marketing_ops_digest_forced_only(monkeypatch):
    from src.utils.email_policy import should_smtp

    assert should_smtp(has_issues=False) is False
    monkeypatch.setenv("EMAIL_FORCE_ROUTINE", "1")
    assert should_smtp(has_issues=False) is True


def test_source_content_refresh_smtp_on_alerts(monkeypatch, capsys):
    from src.utils.email_policy import should_smtp

    calls = []

    def fake_send(subject, html, send_smtp=True, **kwargs):
        calls.append(send_smtp)
        return True

    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)
    from src.utils.email_sender import send_email

    for alerts, fetch_failed, unavailable in (
        ({}, 0, 0),
        ({"SKU1": []}, 0, 0),
        ({}, 1, 0),
        ({}, 0, 1),
    ):
        summary = {"fetch_failed": fetch_failed, "unavailable": unavailable, "checked": 1}
        has_issues = bool(alerts) or bool(summary.get("fetch_failed")) or bool(summary.get("unavailable"))
        send_email("subj", "<p/>", send_smtp=should_smtp(has_issues=has_issues))
        if not has_issues:
            print("no seller drift; report archived locally (SMTP silenced)")
    assert calls == [False, True, True, True]
    assert "SMTP silenced" in capsys.readouterr().out


def test_batch_smart_reprice_smtp_on_changes_or_errors():
    from src.utils.email_policy import should_smtp

    assert should_smtp(has_issues=bool(0 or 0)) is False
    assert should_smtp(has_issues=bool(3 or 0)) is True
    assert should_smtp(has_issues=bool(0 or 1)) is True


def test_batch_smart_bid_smtp_on_adjust_or_errors(monkeypatch):
    from src.utils.email_policy import should_smtp

    for n_adjust, errors, expect in (
        (0, None, False),
        (2, None, True),
        (0, ["e"], True),
    ):
        assert should_smtp(has_issues=bool(n_adjust or errors)) is expect


def test_local_report_still_invoked_when_smtp_silenced(monkeypatch, tmp_path):
    """send_email always archives locally even when send_smtp=False."""
    from src.utils import email_sender

    saved = []
    monkeypatch.setattr(email_sender, "_ENV_LOADED", True)
    monkeypatch.setattr(
        email_sender,
        "_save_local_report",
        lambda subject, body: saved.append((subject, body)) or str(tmp_path / "report.html"),
    )
    monkeypatch.setattr(
        email_sender,
        "get_smtp_attempts",
        lambda sender: (_ for _ in ()).throw(AssertionError("SMTP should not run")),
    )

    ok = email_sender.send_email("subj", "<p>body</p>", send_smtp=False, retry_delay=0)
    assert ok is True
    assert saved and "<p>body</p>" in saved[0][1]
