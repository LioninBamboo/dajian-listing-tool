from __future__ import annotations


def test_run_apply_uses_configured_client_factory(monkeypatch):
    from scripts import cro_image_refresh as cir

    class FakeClient:
        def __init__(self):
            self.put_payload = None

        def get_inventory_item(self, sku):
            live_urls = (
                self.put_payload['image_urls']
                if self.put_payload
                else ['https://img/live-old.jpg']
            )
            return {
                'condition': 'NEW',
                'availability': {'shipToLocationAvailability': {'quantity': 1}},
                'product': {
                    'title': 'Listing title',
                    'description': 'Listing description',
                    'aspects': {'Brand': ['AquaVerve']},
                    'imageUrls': live_urls,
                },
            }

        def create_or_replace_inventory_item(self, sku, payload):
            self.put_payload = payload

    marked = {}
    client = FakeClient()
    monkeypatch.setattr(cir, 'load_pending', lambda action_type: [{'sku': 'SKU-IMG'}])
    monkeypatch.setattr(
        cir,
        '_load_local_images',
        lambda sku: ['https://img/local-new.jpg', 'https://img/local-2.jpg'],
    )
    monkeypatch.setattr(cir, '_default_ebay_client', lambda: client)
    monkeypatch.setattr(cir, 'mark_done', lambda skus, action: marked.setdefault('call', (list(skus), action)) or 1)

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-IMG']
    assert rep['failed'] == []
    assert marked['call'] == (['SKU-IMG'], 'image_refresh')
    assert client.put_payload['image_urls'][0] == 'https://img/local-new.jpg'
