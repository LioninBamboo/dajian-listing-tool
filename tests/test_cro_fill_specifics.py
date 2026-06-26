"""S17 — fill_specifics merge / missing 逻辑."""
from __future__ import annotations


def test_merge_only_fills_missing_keys():
    from scripts.cro_fill_specifics import _merge_missing
    cur = {'Brand': ['Acme'], 'Color': ['Black']}
    completed = {'Brand': ['Bogus'], 'Color': ['White'], 'Material': ['Wood']}
    out = _merge_missing(cur, completed)
    assert out['Brand'] == ['Acme']  # 不覆盖
    assert out['Color'] == ['Black']  # 不覆盖
    assert out['Material'] == ['Wood']  # 补


def test_merge_skips_protected_keys():
    from scripts.cro_fill_specifics import _merge_missing
    cur = {}
    completed = {
        'Item Length': ['72'],  # protected — 必须真实源数据
        'Item Width': ['30'],
        'Material': ['Wood'],
    }
    out = _merge_missing(cur, completed)
    assert 'Item Length' not in out
    assert 'Item Width' not in out
    assert out['Material'] == ['Wood']


def test_merge_treats_meaningless_as_missing():
    from scripts.cro_fill_specifics import _merge_missing
    cur = {'Brand': ['N/A'], 'Color': [''], 'Type': ['unknown']}
    completed = {'Brand': ['Acme'], 'Color': ['Black'], 'Type': ['Sofa']}
    out = _merge_missing(cur, completed)
    assert out['Brand'] == ['Acme']
    assert out['Color'] == ['Black']
    assert out['Type'] == ['Sofa']


def test_missing_keys_excludes_protected_and_meaningless():
    from scripts.cro_fill_specifics import _missing_keys
    cur = {'Brand': ['Acme'], 'Color': ['']}
    completed = {
        'Brand': ['Bogus'],
        'Color': ['Black'],
        'Material': ['Wood'],
        'Item Length': ['72'],
    }
    miss = _missing_keys(cur, completed)
    assert miss == {'Color', 'Material'}


def test_run_apply_uses_configured_factories(monkeypatch):
    from scripts import cro_fill_specifics as cf

    class FakeClient:
        def __init__(self):
            self.put_payload = None
            self.reads = 0

        def get_inventory_item(self, sku):
            self.reads += 1
            aspects = {'Brand': ['AquaVerve']} if self.put_payload else {}
            return {
                'condition': 'NEW',
                'availability': {'shipToLocationAvailability': {'quantity': 1}},
                'product': {
                    'title': 'Modern Table',
                    'description': 'A modern table for dining room use.',
                    'aspects': aspects,
                    'imageUrls': ['https://img/1.jpg'],
                },
            }

        def create_or_replace_inventory_item(self, sku, payload):
            self.put_payload = payload

    class FakeMatcher:
        def get_category_and_aspects(self, title, aspects, description):
            return '38204', 'Tables', {'Brand': ['AquaVerve']}

    marked = {}
    client = FakeClient()
    monkeypatch.setattr(cf, 'load_pending', lambda action_type: [{'sku': 'SKU-1'}])
    monkeypatch.setattr(cf, '_default_ebay_client', lambda: client)
    monkeypatch.setattr(cf, '_default_category_matcher', lambda: FakeMatcher())
    monkeypatch.setattr(cf, 'mark_done', lambda skus, action: marked.setdefault('call', (list(skus), action)) or 1)

    rep = cf.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-1']
    assert rep['failed'] == []
    assert marked['call'] == (['SKU-1'], 'fill_specifics')
    assert client.put_payload['aspects']['Brand'] == ['AquaVerve']
