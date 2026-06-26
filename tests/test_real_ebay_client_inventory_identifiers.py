from src.clients.real_ebay_client import RealEbayClient


class _DummyOauth:
    api_base = "https://api.ebay.example"

    def get_valid_token(self):
        return "token"


class _Response:
    status_code = 204
    text = ""

    def json(self):
        return {}


class _Session:
    def __init__(self):
        self.last_json = None

    def put(self, url, headers=None, json=None):
        self.last_json = json
        return _Response()


def test_create_inventory_item_promotes_identifiers_from_aspects(monkeypatch):
    client = object.__new__(RealEbayClient)
    client.base_url = "https://api.ebay.example"
    client.oauth = _DummyOauth()
    client.session = _Session()
    client._prepare_inventory_image_urls = lambda sku, image_urls, max_images=24: image_urls
    client._verify_inventory_image_urls = lambda sku, expected_count: None

    result = client.create_or_replace_inventory_item(
        "SKU123",
        {
            "title": "Glue Gun",
            "description": "<div>ok</div>",
            "image_urls": ["https://example.com/a.jpg"],
            "quantity": 1,
            "condition": "NEW",
            "aspects": {
                "Brand": ["AquaVerve"],
                "MPN": ["GSDPro2-220"],
                "UPC": ["018239357260"],
            },
        },
    )

    assert result["status"] == "success"
    assert client.session.last_json["product"]["brand"] == "AquaVerve"
    assert client.session.last_json["product"]["mpn"] == "GSDPro2-220"
    assert client.session.last_json["product"]["upc"] == ["018239357260"]
