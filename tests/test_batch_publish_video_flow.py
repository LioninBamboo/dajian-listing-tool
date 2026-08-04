import batch_publish


def test_publish_image_limit_defaults_and_honors_sku_override(monkeypatch):
    monkeypatch.delenv("PUBLISH_IMAGE_LIMIT", raising=False)
    monkeypatch.delenv("PUBLISH_IMAGE_LIMIT_SKU1", raising=False)
    assert batch_publish._publish_image_limit("SKU1") == 24

    monkeypatch.setenv("PUBLISH_IMAGE_LIMIT", "12")
    monkeypatch.setenv("PUBLISH_IMAGE_LIMIT_SKU1", "8")
    assert batch_publish._publish_image_limit("SKU1") == 8


def test_publish_image_limit_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("PUBLISH_IMAGE_LIMIT_SKU1", "0")
    assert batch_publish._publish_image_limit("SKU1") == 24

    monkeypatch.setenv("PUBLISH_IMAGE_LIMIT_SKU1", "not-a-number")
    assert batch_publish._publish_image_limit("SKU1") == 24


def test_publish_uploads_source_video_before_inventory_put(monkeypatch):
    captured = {}

    class _FakeOAuth:
        api_base = "https://api.ebay.example"

        def __init__(self, environment):
            self.environment = environment

        def is_authorized(self):
            return True

        def get_application_token(self):
            return "app-token"

        def get_valid_token(self):
            return "token"

    class _FakeCategoryMatcher:
        def __init__(self, oauth):
            pass

        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def get_category_and_aspects(self, title, aspects, description):
            return "38204", "Coffee Tables", aspects

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

        def _get_category_aspects(self, category_id):
            return [], []

    class _FakePolicyManager:
        def __init__(self, oauth):
            pass

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            self.published = False

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return [
                "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg",
                "https://i.ebayimg.com/images/g/BBB/s-l1600.jpg",
            ]

        def create_or_replace_inventory_item(self, sku, product):
            captured["inventory_product"] = product
            return {"status": "success"}

        def get_inventory_item(self, sku):
            product = captured["inventory_product"]
            return {
                "condition": product["condition"],
                "availability": {"shipToLocationAvailability": {"quantity": product["quantity"]}},
                "product": {
                    "title": product["title"],
                    "description": product["description"],
                    "imageUrls": product["image_urls"],
                    "videoIds": product.get("video_urls", []),
                    "packageWeightAndSize": product.get("packageWeightAndSize"),
                },
            }

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            if not self.published:
                return {"status": "DRAFT"}
            return {
                "offerId": "offer-1",
                "categoryId": "38204",
                "status": "PUBLISHED",
                "listing": {"listingId": "listing-1", "listingStatus": "ACTIVE"},
            }

        def publish_offer(self, offer_id):
            self.published = True
            return {"listingId": "listing-1"}

    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", lambda product, market_p: 99.0)
    monkeypatch.setattr(batch_publish, "_is_sellable_leaf_category", lambda oauth, category_id: True)
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda product, oauth, sku, title: "video-1")
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "update_product_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(
        batch_publish,
        "run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": [],
            "warnings": [],
            "source_fingerprint": "source",
            "candidate_fingerprint": "candidate",
            "ruleset_version": "listing-qc-v1",
            "fact_sheet_version": 4,
        },
    )
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _FakeOAuth)
    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", _FakeCategoryMatcher)
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", _FakePolicyManager)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _FakeEbayClient)

    result = batch_publish.publish_single_product(
        {
            "sku": "SKU-VIDEO",
            "title": "Coffee Table",
            "description": "<div>Assembly Required Yes</div>",
            "price": 50.0,
            "suggested_price": 99.0,
            "shipping": 0,
            "images": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
            "videos": ["https://example.com/source.mp4"],
            "attributes": {
                "Assembled Length (in.)": "40",
                "Assembled Width (in.)": "20",
                "Assembled Height (in.)": "18",
            },
            "specs": {},
            "cost_breakdown": {"total_dajian_cost": 40},
            "optimization": {
                "title": "Coffee Table",
                "description": (
                    "<div><h3>KEY FEATURES</h3><ul>"
                    "<li>Storage shelf keeps living room essentials organized.</li>"
                    "<li>Sturdy frame supports everyday coffee table use.</li>"
                    "<li>Compact profile fits apartments and family rooms.</li>"
                    "</ul></div>"
                ),
                "categoryId": "38204",
                "categoryName": "Coffee Tables",
                "aspects": {
                    "Brand": ["AquaVerve"],
                    "Type": ["Coffee Table"],
                    "Assembly Required": ["Yes"],
                    "Item Length": ["40 in"],
                    "Item Width": ["20 in"],
                    "Item Height": ["18 in"],
                },
            },
        }
    )

    assert result["status"] == "success"
    assert captured["inventory_product"]["video_urls"] == ["video-1"]
