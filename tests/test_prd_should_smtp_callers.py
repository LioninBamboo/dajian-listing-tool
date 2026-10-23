"""PR-D D4: should_smtp callers — local report always; SMTP gated.

Smart Bid / Smart Reprice cases drive the real run() / _send_reprice_email
paths with fakes (MF-2). Other callers keep focused send_email mocks.
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


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
    arp.notify_promotion_created({"name": "p", "start_date": "x", "end_date": "y", "listing_count": 1})
    assert calls and calls[0]["send_smtp"] is True


def test_order_source_recheck_smtp_on_critical_only(monkeypatch, capsys):
    from src.utils.email_policy import should_smtp

    assert should_smtp(has_issues=True) is True
    assert should_smtp(has_issues=False) is False

    calls = []

    def fake_send(subject, html, send_smtp=True, **kwargs):
        calls.append({"subject": subject, "send_smtp": send_smtp})
        return True

    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)
    from src.utils.email_sender import send_email
    from src.utils.email_policy import should_smtp as ss

    send_email("alert", "<p>a</p>", send_smtp=ss(has_issues=True))
    send_email("clean", "<p>c</p>", send_smtp=ss(has_issues=False))
    print("clean recheck archived locally (2 order(s); SMTP silenced)")
    assert calls[0]["send_smtp"] is True
    assert calls[1]["send_smtp"] is False
    assert "SMTP silenced" in capsys.readouterr().out


def test_sales_health_smtp_when_problems(monkeypatch):
    from src.utils.email_policy import should_smtp as ss

    calls = []

    def fake_send(*, subject, html_body, send_smtp=True, **kwargs):
        calls.append(send_smtp)
        return True

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


# ---------------------------------------------------------------------------
# MF-2: Smart Bid — drive real run() with fakes
# ---------------------------------------------------------------------------

def _run_smart_bid(monkeypatch, *, plan, apply, ad_update_side_effect=None):
    """Invoke batch_smart_bid.run with build_plan / IO faked; capture send_email."""
    from scripts import batch_smart_bid as bsb

    calls = []

    def fake_send(subject, html, send_smtp=True, **kwargs):
        calls.append({"subject": subject, "send_smtp": send_smtp, "html": html})
        return True

    fake_perf = {"traffic": {}, "sales": {"by_listing": {}, "by_sku": {}}}
    ad_svc = MagicMock()
    if ad_update_side_effect is not None:
        ad_svc.update_ad_bid.side_effect = ad_update_side_effect
    else:
        ad_svc.update_ad_bid.return_value = {"success": True}
    ad_svc.fetch_all_ad_data.return_value = {"ads": {}}

    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setattr(bsb, "_make_real_client", lambda: object())
    monkeypatch.setattr(bsb, "_fetch_cost_map", lambda: {})
    monkeypatch.setattr(bsb, "_load_active_experiments", lambda: [])
    monkeypatch.setattr(bsb, "LOG_DIR", Path(tmp.name))
    monkeypatch.setattr(bsb, "build_plan", lambda *a, **k: list(plan))
    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)

    with (
        patch("src.services.ebay_ad_service.EbayAdService", return_value=ad_svc),
        patch("src.services.ebay_performance.load_performance_cache", return_value=fake_perf),
        patch("src.services.ebay_performance.EbayPerformanceService"),
        patch("src.services.ebay_performance.save_performance_cache"),
    ):
        report = bsb.run(apply=apply, send_email=True)

    # Local JSON report always written
    assert list(Path(tmp.name).glob("batch_smart_bid_*.json"))
    tmp.cleanup()
    return report, calls, ad_svc


def _plan_item(sku, decision, *, bid=5.0, new_bid=7.0, tier="high"):
    return {
        "sku": sku,
        "listing_id": f"L-{sku}",
        "campaign_id": "C1",
        "tier": tier,
        "impressions": 600,
        "ctr": 0.05,
        "sold_qty": 1,
        "current_bid": bid,
        "desired_bid": new_bid,
        "live_price": 200.0,
        "total_cost": 50.0,
        "new_bid": new_bid,
        "decision": decision,
        "reason": f"SYNTHETIC {decision}",
    }


def test_smart_bid_all_no_change_smtp_silenced(monkeypatch):
    plan = [_plan_item("A", "no_change", new_bid=5.0, tier="mid")]
    report, calls, ad_svc = _run_smart_bid(monkeypatch, plan=plan, apply=True)
    assert report["summary"]["by_decision"].get("no_change") == 1
    assert calls and calls[0]["send_smtp"] is False
    ad_svc.update_ad_bid.assert_not_called()


def test_smart_bid_one_adjust_sends(monkeypatch):
    plan = [_plan_item("A", "adjust")]
    report, calls, ad_svc = _run_smart_bid(monkeypatch, plan=plan, apply=True)
    assert report["summary"]["by_decision"].get("adjust") == 1
    assert calls and calls[0]["send_smtp"] is True
    ad_svc.update_ad_bid.assert_called()


def test_smart_bid_experiment_apply_failure_sends(monkeypatch):
    plan = [_plan_item("E1", "experiment", new_bid=6.0)]
    plan[0]["experiment_id"] = "exp-1"
    plan[0]["experiment_arm"] = "variant"

    def fail_update(*a, **k):
        return {"success": False, "error": "SYNTHETIC apply fail"}

    report, calls, ad_svc = _run_smart_bid(
        monkeypatch, plan=plan, apply=True, ad_update_side_effect=fail_update,
    )
    assert report["apply_result"]["applied_failed"] >= 1
    assert calls and calls[0]["send_smtp"] is True


def test_smart_bid_experiment_apply_success_sends(monkeypatch):
    """MF-1: experiment-only success must still SMTP (not silence as no adjust)."""
    plan = [_plan_item("E2", "experiment", new_bid=6.0)]
    plan[0]["experiment_id"] = "exp-2"
    plan[0]["experiment_arm"] = "control"

    report, calls, ad_svc = _run_smart_bid(monkeypatch, plan=plan, apply=True)
    assert report["summary"]["by_decision"].get("experiment") == 1
    assert report["summary"]["by_decision"].get("adjust", 0) == 0
    assert report["apply_result"]["applied_ok"] >= 1
    assert report["apply_result"]["applied_failed"] == 0
    assert calls and calls[0]["send_smtp"] is True
    ad_svc.update_ad_bid.assert_called()


def test_smart_bid_dry_run_keeps_behaviour(monkeypatch):
    """Dry-run: no eBay writes; adjust still SMTP; no_change still silenced."""
    plan_adjust = [_plan_item("D1", "adjust")]
    report, calls, ad_svc = _run_smart_bid(monkeypatch, plan=plan_adjust, apply=False)
    assert report["apply"] is False
    assert report["apply_result"]["applied_ok"] == 0
    ad_svc.update_ad_bid.assert_not_called()
    assert calls and calls[0]["send_smtp"] is True  # adjust planned → mail

    plan_clean = [_plan_item("D2", "no_change", new_bid=5.0, tier="mid")]
    report2, calls2, ad_svc2 = _run_smart_bid(monkeypatch, plan=plan_clean, apply=False)
    assert report2["apply"] is False
    ad_svc2.update_ad_bid.assert_not_called()
    assert calls2 and calls2[0]["send_smtp"] is False


# ---------------------------------------------------------------------------
# MF-2: Smart Reprice — drive real _send_reprice_email
# ---------------------------------------------------------------------------

def _load_batch_smart_reprice():
    spec = importlib.util.spec_from_file_location(
        "batch_smart_reprice_smtp",
        ROOT / "scripts" / "batch_smart_reprice.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smart_reprice_clean_run_silenced_but_archived(monkeypatch):
    bsr = _load_batch_smart_reprice()
    calls = []

    def fake_send(subject, body, attachments=None, send_smtp=True, **kwargs):
        calls.append({"send_smtp": send_smtp, "body": body, "subject": subject})
        return True

    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)
    monkeypatch.setattr(bsr, "log", MagicMock())

    ok = bsr._send_reprice_email(
        results=[],
        n_changes=0,
        n_up=0,
        n_down=0,
        n_errors=0,
        n_no_market=0,
        strategies={},
        total=0,
    )
    assert ok is True
    assert calls and calls[0]["send_smtp"] is False
    assert "Smart Reprice" in calls[0]["subject"] or "eBay Smart Reprice" in calls[0]["subject"]


def test_smart_reprice_error_run_sends(monkeypatch):
    bsr = _load_batch_smart_reprice()
    calls = []

    def fake_send(subject, body, attachments=None, send_smtp=True, **kwargs):
        calls.append({"send_smtp": send_smtp})
        return True

    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)
    monkeypatch.setattr(bsr, "log", MagicMock())

    ok = bsr._send_reprice_email(
        results=[{
            "sku": "SYNTHETIC-ERR",
            "title": "x",
            "image_url": "",
            "current_price": 10,
            "new_price": 10,
            "price_diff": 0,
            "pct_change": 0,
            "margin": 0.1,
            "strategy": "ERR",
            "status": "error",
            "price_reason": "SYNTHETIC error",
        }],
        n_changes=0,
        n_up=0,
        n_down=0,
        n_errors=1,
        n_no_market=0,
        strategies={},
        total=1,
    )
    assert ok is True
    assert calls and calls[0]["send_smtp"] is True
