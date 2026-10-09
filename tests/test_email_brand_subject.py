from __future__ import annotations

from src.utils import email_sender
from src.utils.store_profile import StoreProfile


def test_brand_email_subject_prefixes_store_name(monkeypatch):
    monkeypatch.setattr(
        email_sender,
        "get_store_profile",
        lambda: StoreProfile(brand_name="GrovePop"),
    )
    assert email_sender.brand_email_subject("源内容刷新报告") == "[GrovePop] 源内容刷新报告"


def test_brand_email_subject_does_not_double_prefix(monkeypatch):
    monkeypatch.setattr(
        email_sender,
        "get_store_profile",
        lambda: StoreProfile(brand_name="GrovePop"),
    )
    assert (
        email_sender.brand_email_subject("[GrovePop] 源内容刷新报告")
        == "[GrovePop] 源内容刷新报告"
    )
    assert (
        email_sender.brand_email_subject("[AquaVerve] already branded")
        == "[AquaVerve] already branded"
    )


def test_brand_email_subject_uses_aquaverve_not_dajian(monkeypatch):
    monkeypatch.setattr(
        email_sender,
        "get_store_profile",
        lambda: StoreProfile(brand_name="AquaVerve"),
    )
    subject = email_sender.brand_email_subject("每日汇总")
    assert subject.startswith("[AquaVerve]")
    assert "Dajian" not in subject


def test_send_email_applies_brand_prefix_to_smtp_and_local(monkeypatch, tmp_path):
    monkeypatch.setattr(
        email_sender,
        "get_store_profile",
        lambda: StoreProfile(brand_name="AquaRides"),
    )
    monkeypatch.setenv("NOTIFICATION_EMAIL", "sender@example.com")
    monkeypatch.setenv("NOTIFICATION_EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(email_sender, "_ENV_LOADED", True)

    saved = {}

    def fake_save(subject, body):
        saved["local_subject"] = subject
        return str(tmp_path / "report.html")

    built = {}

    def fake_build(sender, to_email, subject, html_body, attachments):
        built["subject"] = subject
        return object()

    monkeypatch.setattr(email_sender, "_save_local_report", fake_save)
    monkeypatch.setattr(email_sender, "_build_mime_message", fake_build)
    monkeypatch.setattr(email_sender, "_send_via_smtp", lambda *a, **k: None)
    monkeypatch.setattr(email_sender, "get_smtp_attempts", lambda sender: [
        ("fake", "smtp.example.com", 465, True, False),
    ])

    ok = email_sender.send_email("CRO 标题热词优化日报", "<p>x</p>", retry_delay=0)

    assert ok is True
    assert saved["local_subject"] == "[AquaRides] CRO 标题热词优化日报"
    assert built["subject"] == "[AquaRides] CRO 标题热词优化日报"
