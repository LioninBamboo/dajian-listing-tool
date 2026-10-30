from __future__ import annotations

from scripts import store_marketing as sm


class _FakeAdService:
    def __init__(self, *, campaigns=None, promoted=None, create_results=None, campaign_id="C1"):
        self.campaigns = list(campaigns or [])
        self.promoted = set(promoted or [])
        self.create_results = dict(create_results or {})
        self.campaign_id = campaign_id
        self.created_campaigns = []
        self.safe_calls = []
        self.batch_calls = []

    def fetch_campaigns(self, status="RUNNING"):
        return self.campaigns

    def create_campaign(self, name, bid_percentage=5.0, marketplace_id="EBAY_US"):
        self.created_campaigns.append((name, bid_percentage))
        return self.campaign_id

    def is_listing_promoted(self, listing_id):
        return str(listing_id) in self.promoted

    def create_ad_safe(self, campaign_id, listing_id, sku=None, bid_percentage=5.0, **_kwargs):
        self.safe_calls.append((campaign_id, str(listing_id), bid_percentage))
        return self.create_results.get(
            str(listing_id),
            {"success": True, "listing_id": str(listing_id)},
        )

    def batch_create_ads(self, campaign_id, listing_ids, bid_percentage=5.0):
        self.batch_calls.append((campaign_id, list(listing_ids), bid_percentage))
        return {"success": True}


def test_lever_promoted_enrolls_via_create_ad_safe(monkeypatch):
    fake = _FakeAdService(
        campaigns=[{"campaignId": "C1"}],
        promoted={"111"},
        create_results={
            "222": {"success": True},
            "333": {"success": False, "reason": "unsafe"},
        },
    )
    monkeypatch.setattr(sm, "_ad_service", lambda: fake)

    msg = sm.lever_promoted("GrovePop", ["111", "222", "333"], 5.0, apply=True)

    assert fake.batch_calls == []
    assert fake.safe_calls == [("C1", "222", 5.0), ("C1", "333", 5.0)]
    assert "enrolled 1" in msg
    assert "unsafe/blacklisted 1" in msg


def test_lever_promoted_dry_run_does_not_write(monkeypatch):
    fake = _FakeAdService(campaigns=[], promoted=set())
    monkeypatch.setattr(sm, "_ad_service", lambda: fake)

    msg = sm.lever_promoted("AquaRides", ["111", "222"], 5.0, apply=False)

    assert fake.created_campaigns == []
    assert fake.safe_calls == []
    assert "DRY" in msg
    assert "create_ad_safe" in msg


def test_lever_promoted_reports_terms_and_conditions(monkeypatch):
    fake = _FakeAdService(campaigns=[], promoted=set())
    fake.campaign_id = None
    fake.last_campaign_error = (
        'HTTP 409: {"errors":[{"errorId":35067,"message":'
        '"The seller must accept the Promoted Listings terms and conditions."}]}'
    )

    def _fail_create(name, bid_percentage=5.0, marketplace_id="EBAY_US"):
        fake.created_campaigns.append((name, bid_percentage))
        return None

    fake.create_campaign = _fail_create
    monkeypatch.setattr(sm, "_ad_service", lambda: fake)

    msg = sm.lever_promoted("GrovePop", ["111"], 5.0, apply=True)

    assert "FAILED" in msg
    assert "terms" in msg.lower()
    assert "useragreement.ebay.com" in msg


def test_lever_promoted_skips_missing_marketing_scope(monkeypatch):
    class Boom(_FakeAdService):
        def fetch_campaigns(self, status="RUNNING"):
            raise RuntimeError("1100 insufficient permission / 403")

    monkeypatch.setattr(sm, "_ad_service", Boom)

    msg = sm.lever_promoted("GrovePop", ["111"], 5.0, apply=True)

    assert "SKIPPED" in msg
    assert "sell.marketing" in msg
