"""Audit email SMTP gate: CRITICAL/HIGH -> SMTP; clean runs stay local."""
from __future__ import annotations

from unittest.mock import patch

from scripts import audit_fix_active_listings


def _minimal_report(*, critical: int = 0, high: int = 0) -> dict:
    issues = []
    if critical:
        issues.append(
            {
                "sku": "SKU-CRIT",
                "listing_id": "1",
                "title": "Critical item",
                "issues": [
                    {
                        "severity": "CRITICAL",
                        "type": "category_mismatch",
                        "detail": "wrong category",
                    }
                ],
            }
        )
    return {
        "mode": "live_audit",
        "total_published": 1,
        "total_with_issues": len(issues),
        "total_transport_failures": 0,
        "skipped_clean_frozen": 0,
        "clean_state_recorded": 0,
        "severity_counts": {
            "CRITICAL": critical,
            "HIGH": high,
            "MEDIUM": 0,
            "LOW": 0,
        },
        "fixed_count": 0,
        "issues": issues,
        "transport_issues": [],
    }


def test_send_audit_email_send_smtp_true_when_critical(tmp_path, monkeypatch):
    monkeypatch.delenv("EMAIL_FORCE_ROUTINE", raising=False)
    report_path = tmp_path / "audit.json"
    report_path.write_text("{}", encoding="utf-8")

    with patch("src.utils.email_sender.send_email", return_value=True) as mock_send:
        ok = audit_fix_active_listings._send_audit_email(
            _minimal_report(critical=1),
            report_path,
        )

    assert ok is True
    assert mock_send.call_args.kwargs["send_smtp"] is True


def test_send_audit_email_send_smtp_false_when_clean(tmp_path, monkeypatch):
    monkeypatch.delenv("EMAIL_FORCE_ROUTINE", raising=False)
    report_path = tmp_path / "audit.json"
    report_path.write_text("{}", encoding="utf-8")

    with patch("src.utils.email_sender.send_email", return_value=True) as mock_send:
        ok = audit_fix_active_listings._send_audit_email(
            _minimal_report(critical=0, high=0),
            report_path,
        )

    assert ok is True
    assert mock_send.call_args.kwargs["send_smtp"] is False
