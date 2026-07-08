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
        '_load_local_product',
        lambda sku: {
            'images': ['https://img/local-new.jpg', 'https://img/local-2.jpg'],
            'title': 'Local title',
            'description': 'Local description',
        },
    )
    monkeypatch.setattr(cir, '_default_ebay_client', lambda: client)

    def _fake_mark_done(skus, action, result='done'):
        marked[result] = (list(skus), action)
        return len(marked[result][0])
    monkeypatch.setattr(cir, 'mark_done', _fake_mark_done)

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-IMG']
    assert rep['failed'] == []
    assert marked['done'] == (['SKU-IMG'], 'image_refresh')
    assert client.put_payload['image_urls'][0] == 'https://img/local-new.jpg'


def test_run_apply_marks_terminal_skip_as_skipped(monkeypatch):
    """本地无图 → 终态 skip 应从 pending 出队 (mark_done result='skipped'), 停止空转."""
    from scripts import cro_image_refresh as cir

    calls = []
    monkeypatch.setattr(cir, 'load_pending', lambda action_type: [{'sku': 'SKU-NOIMG'}])
    monkeypatch.setattr(
        cir, '_load_local_product',
        lambda sku: {'images': [], 'title': '', 'description': ''})
    monkeypatch.setattr(cir, '_default_ebay_client', lambda: object())
    monkeypatch.setattr(
        cir, 'mark_done',
        lambda skus, action, result='done': calls.append(
            (list(skus), action, result)) or len(list(skus)))

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['skipped'] == ['SKU-NOIMG']
    assert rep['done'] == []
    assert calls == [(['SKU-NOIMG'], 'image_refresh', 'skipped')]
