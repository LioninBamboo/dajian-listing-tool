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


def test_run_apply_marks_no_missing_as_skipped(monkeypatch):
    """specifics 已完整 → 终态 skip 应从 pending 出队 (result='skipped'), 停止每日空转."""
    from scripts import cro_fill_specifics as cf

    class FakeClient:
        def get_inventory_item(self, sku):
            return {
                'condition': 'NEW',
                'availability': {'shipToLocationAvailability': {'quantity': 1}},
                'product': {
                    'title': 'Modern Table',
                    'description': 'A modern table for dining room use.',
                    'aspects': {'Brand': ['AquaVerve']},
                    'imageUrls': ['https://img/1.jpg'],
                },
            }

        def create_or_replace_inventory_item(self, sku, payload):
            raise AssertionError('无缺失键时不应 PUT')

    class FakeMatcher:
        def get_category_and_aspects(self, title, aspects, description):
            # completed 与 current 完全一致 → _missing_keys 为空
            return '38204', 'Tables', {'Brand': ['AquaVerve']}

    calls = []
    monkeypatch.setattr(cf, 'load_pending', lambda action_type: [{'sku': 'SKU-DONE'}])
    monkeypatch.setattr(cf, '_default_ebay_client', lambda: FakeClient())
    monkeypatch.setattr(cf, '_default_category_matcher', lambda: FakeMatcher())
    monkeypatch.setattr(
        cf, 'mark_done',
        lambda skus, action, result='done': calls.append(
            (list(skus), action, result)) or len(list(skus)))

    rep = cf.run(apply_changes=True, limit=10)

    assert rep['skipped'] == ['SKU-DONE']
    assert rep['done'] == []
    assert calls == [(['SKU-DONE'], 'fill_specifics', 'skipped')]


def test_fill_one_falls_back_to_local_description_when_ebay_empty(monkeypatch):
    """eBay description 为空时回退本地/标题, 不再 PUT 空 description (eBay 25718)."""
    from scripts import cro_fill_specifics as cf

    class FakeClient:
        def __init__(self):
            self.put_payload = None
            self._filled = False

        def get_inventory_item(self, sku):
            aspects = ({'Brand': ['Acme'], 'Material': ['Wood']}
                       if self._filled else {'Brand': ['Acme']})
            return {
                'condition': 'NEW',
                'availability': {'shipToLocationAvailability': {'quantity': 1}},
                'product': {
                    'title': 'Modern Oak Table',
                    'description': '',  # eBay 侧描述为空
                    'aspects': aspects,
                    'imageUrls': ['https://img/1.jpg'],
                },
            }

        def create_or_replace_inventory_item(self, sku, payload):
            self.put_payload = payload
            self._filled = True

    class FakeMatcher:
        def get_category_and_aspects(self, title, aspects, description):
            return '1', 'Tables', {'Brand': ['Acme'], 'Material': ['Wood']}

    client = FakeClient()
    monkeypatch.setattr(
        cf, '_load_local_product',
        lambda sku: {'title': 'Local Title', 'description': 'Local desc'})

    row = cf._fill_one(client, FakeMatcher(), 'SKU-D')

    assert row['status'] == 'done'
    assert client.put_payload['description'] == 'Local desc'  # 非空回退
