from __future__ import annotations

import copy

import pytest


_LOCAL_IMAGES = [
    'https://img/local-new.jpg',
    'https://img/local-2.jpg',
]
_SIG = 'sig/part=a%20b'
_URL_UNCHANGED = (
    'https://b2bfiles1.gigab2b.cn/image/a.jpg'
    f'?x-cc=10&x-cu=106133&x-ct=1773558000&x-cs={_SIG}'
)
_URL_OSS_MIDDLE = (
    'https://b2bfiles1.gigab2b.cn/image/b.jpg'
    '?x-oss-process=image/resize,w_800'
    f'&x-cc=10&x-cu=1&x-ct=2&x-cs={_SIG}'
)
_URL_OSS_MIDDLE_EXPECTED = (
    'https://b2bfiles1.gigab2b.cn/image/b.jpg'
    f'?x-cc=10&x-cu=1&x-ct=2&x-cs={_SIG}'
)


class _Resp:
    def __init__(self, status_code=204, text=''):
        self.status_code = status_code
        self.text = text


class _OAuth:
    def get_valid_token(self):
        return 'tok'


class _Session:
    def __init__(self, client):
        self.client = client

    def put(self, url, headers=None, json=None, timeout=None):
        sku = url.rstrip('/').rsplit('/', 1)[-1]
        if sku in self.client.explode:
            raise RuntimeError('put exploded')
        self.client.puts.append({
            'url': url,
            'headers': headers,
            'json': json,
            'timeout': timeout,
        })
        self.client.put_by_sku[sku] = json
        return _Resp()


class FakeClient:
    def __init__(self, items, explode=(), readback=None):
        self.items = items
        self.explode = set(explode)
        self.readback = readback or {}
        self.session = _Session(self)
        self.base_url = 'https://api.ebay.test'
        self.oauth = _OAuth()
        self.puts = []
        self.put_by_sku = {}
        self.partial_writes = []

    def get_inventory_item(self, sku):
        if sku in self.put_by_sku:
            if sku in self.readback:
                item = copy.deepcopy(self.put_by_sku[sku])
                product = item.get('product')
                if not isinstance(product, dict):
                    product = {}
                    item['product'] = product
                product['imageUrls'] = list(self.readback[sku])
                return item
            return self.put_by_sku[sku]
        item = self.items[sku]
        if isinstance(item, Exception):
            raise item
        return item

    def create_or_replace_inventory_item(self, sku, payload):
        self.partial_writes.append((sku, payload))
        raise AssertionError('create_or_replace_inventory_item must not be called')


def _live_item(quantity='__missing__', sku='SKU-IMG'):
    ship = {'allocationByFormat': {'auction': 0, 'fixedPrice': 5}}
    if quantity != '__missing__':
        ship['quantity'] = quantity
    return {
        'sku': sku,
        'href': f'/sell/inventory/v1/inventory_item/{sku}',
        'locale': 'en_US',
        'inventoryItemGroupKeys': ['GRP-1'],
        'image_urls': ['https://alias.example/should-not-send.jpg'],
        'quantity': 1,
        'condition': 'NEW',
        'conditionDescription': 'Brand new',
        'packageWeightAndSize': {
            'dimensions': {
                'height': 10.0,
                'length': 40.0,
                'width': 20.0,
                'unit': 'INCH',
            },
            'weight': {'unit': 'POUND', 'value': 33.4},
        },
        'availability': {
            'pickupAtLocationAvailability': [{
                'availabilityType': 'IN_STOCK',
                'merchantLocationKey': 'wh1',
                'quantity': 5,
            }],
            'shipToLocationAvailability': ship,
        },
        'product': {
            'title': 'Listing title',
            'description': 'Listing description',
            'aspects': {'Brand': ['AquaVerve'], 'Color': ['Blue']},
            'brand': 'AquaVerve',
            'mpn': 'MPN-9',
            'epid': 'EPID-9',
            'upc': ['012345678905'],
            'ean': ['4006381333931'],
            'isbn': ['9780000000002'],
            'imageUrls': ['https://img/live-old.jpg'],
            'videoIds': ['vid-1'],
        },
    }


def _install(monkeypatch, client, pending, images=_LOCAL_IMAGES, marked=None):
    from scripts import cro_image_refresh as cir

    monkeypatch.setattr(cir, 'load_pending', lambda action_type: list(pending))

    def _local(sku):
        pics = images(sku) if callable(images) else list(images)
        return {
            'images': pics,
            'title': 'Local title',
            'description': 'Local description',
        }

    monkeypatch.setattr(cir, '_load_local_product', _local)
    monkeypatch.setattr(cir, '_default_ebay_client', lambda: client)
    sink = marked if marked is not None else []

    def _fake_mark_done(skus, action, result='done'):
        sink.append((list(skus), action, result))
        return len(list(skus))

    monkeypatch.setattr(cir, 'mark_done', _fake_mark_done)
    return cir


def test_run_apply_uses_configured_client_factory(monkeypatch):
    marked = []
    client = FakeClient({'SKU-IMG': _live_item(quantity=1, sku='SKU-IMG')})
    cir = _install(
        monkeypatch,
        client,
        [{'sku': 'SKU-IMG'}],
        marked=marked,
    )

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-IMG']
    assert rep['failed'] == []
    assert marked == [(['SKU-IMG'], 'image_refresh', 'done')]
    body = client.puts[0]['json']
    assert body['product']['imageUrls'][0] == 'https://img/local-new.jpg'
    assert client.partial_writes == []


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


@pytest.mark.parametrize('quantity,reason', [
    ('__missing__', 'live quantity missing'),
    (None, 'live quantity unknown'),
    ('n/a', 'live quantity unknown'),
    (True, 'live quantity unknown'),
    (0, 'live quantity is 0'),
    ('0', 'live quantity is 0'),
])
def test_missing_or_zero_quantity_skips_without_put(monkeypatch, quantity, reason):
    marked = []
    client = FakeClient({'SKU-IMG': _live_item(quantity=quantity)})
    cir = _install(monkeypatch, client, [{'sku': 'SKU-IMG'}], marked=marked)

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == []
    assert rep['failed'] == []
    assert rep['skipped'] == ['SKU-IMG']
    assert rep['rows'][0]['reason'] == reason
    assert client.puts == []
    assert client.partial_writes == []
    assert marked == []


def test_quantity_five_puts_full_live_snapshot(monkeypatch):
    marked = []
    live = _live_item(quantity=5, sku='SKU-Q5')
    original = copy.deepcopy(live)
    client = FakeClient({'SKU-Q5': live})
    cir = _install(monkeypatch, client, [{'sku': 'SKU-Q5'}], marked=marked)

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-Q5']
    assert rep['failed'] == []
    assert rep['skipped'] == []
    assert len(client.puts) == 1
    assert client.partial_writes == []
    body = client.puts[0]['json']
    expected = copy.deepcopy(original)
    for key in ('sku', 'href', 'inventoryItemGroupKeys', 'image_urls', 'quantity'):
        expected.pop(key, None)
    expected['product']['imageUrls'] = list(_LOCAL_IMAGES)
    assert body == expected
    assert body['availability']['shipToLocationAvailability']['quantity'] == 5
    assert body['packageWeightAndSize'] == original['packageWeightAndSize']
    assert body['locale'] == 'en_US'
    assert body['condition'] == 'NEW'
    assert body['product']['aspects'] == original['product']['aspects']
    assert body['product']['upc'] == ['012345678905']
    assert body['product']['ean'] == ['4006381333931']
    assert body['product']['mpn'] == 'MPN-9'
    assert body['product']['brand'] == 'AquaVerve'
    assert body['product']['epid'] == 'EPID-9'
    assert body['product']['title'] == 'Listing title'
    assert 'sku' not in body
    assert 'quantity' not in body
    assert client.puts[0]['headers']['Content-Language'] == 'en-US'
    assert live['product']['imageUrls'] == ['https://img/live-old.jpg']
    assert marked == [(['SKU-Q5'], 'image_refresh', 'done')]


def test_giga_signature_query_is_not_reencoded(monkeypatch):
    from scripts import cro_image_refresh as cir

    unchanged = _URL_UNCHANGED
    with_oss = (
        'https://b2bfiles1.gigab2b.cn/image/c.jpg'
        f'?x-cc=10&x-cu=2&x-ct=3&x-cs={_SIG}'
        '&x-oss-process=image/resize,w_800,h_800,m_pad'
    )
    assert cir._image_url_drop_oss_process(unchanged) is unchanged
    assert cir._image_url_drop_oss_process(_URL_OSS_MIDDLE) == _URL_OSS_MIDDLE_EXPECTED
    only_oss = 'https://cdn.example/a.jpg?x-oss-process=image/resize,w_800'
    assert cir._image_url_drop_oss_process(only_oss) == 'https://cdn.example/a.jpg'

    images = [unchanged, _URL_OSS_MIDDLE, with_oss]
    client = FakeClient({'SKU-SIG': _live_item(quantity=5, sku='SKU-SIG')})
    cir = _install(monkeypatch, client, [{'sku': 'SKU-SIG'}], images=images)

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-SIG']
    urls = client.puts[0]['json']['product']['imageUrls']
    assert urls[0].encode('utf-8') == unchanged.encode('utf-8')
    assert urls[1].encode('utf-8') == _URL_OSS_MIDDLE_EXPECTED.encode('utf-8')
    assert urls[2].encode('utf-8') == (
        'https://b2bfiles1.gigab2b.cn/image/c.jpg'
        f'?x-cc=10&x-cu=2&x-ct=3&x-cs={_SIG}'
    ).encode('utf-8')
    for url in urls:
        assert f'x-cs={_SIG}'.encode('utf-8') in url.encode('utf-8')
        assert b'x-oss-process' not in url.encode('utf-8')
    assert client.partial_writes == []


def test_one_sku_failure_does_not_abort_the_batch(monkeypatch):
    marked = []

    def images(sku):
        return list(_LOCAL_IMAGES)

    client = FakeClient(
        {
            'SKU-BOOM': _live_item(quantity=5, sku='SKU-BOOM'),
            'SKU-OK': _live_item(quantity=5, sku='SKU-OK'),
            'SKU-ZERO': _live_item(quantity=0, sku='SKU-ZERO'),
        },
        explode={'SKU-BOOM'},
    )
    cir = _install(
        monkeypatch,
        client,
        [{'sku': 'SKU-BOOM'}, {'sku': 'SKU-OK'}, {'sku': 'SKU-ZERO'}],
        images=images,
        marked=marked,
    )

    rep = cir.run(apply_changes=True, limit=10)

    assert [row['sku'] for row in rep['rows']] == ['SKU-BOOM', 'SKU-OK', 'SKU-ZERO']
    assert rep['failed'] == ['SKU-BOOM']
    assert rep['done'] == ['SKU-OK']
    assert rep['skipped'] == ['SKU-ZERO']
    assert len(rep['done']) == 1
    assert len(rep['failed']) == 1
    assert len(rep['skipped']) == 1
    assert rep['rows'][0]['reason'].startswith('PUT error:')
    assert rep['rows'][2]['reason'] == 'live quantity is 0'
    assert list(client.put_by_sku) == ['SKU-OK']
    assert client.put_by_sku['SKU-OK']['availability']['shipToLocationAvailability']['quantity'] == 5
    assert client.partial_writes == []
    assert marked == [(['SKU-OK'], 'image_refresh', 'done')]


def test_refuses_partial_rebuild_without_live_session(monkeypatch):
    class PartialOnly:
        def __init__(self):
            self.calls = []

        def get_inventory_item(self, sku):
            return _live_item(quantity=5, sku=sku)

        def create_or_replace_inventory_item(self, sku, payload):
            self.calls.append((sku, payload))
            raise AssertionError('create_or_replace_inventory_item must not be called')

    marked = []
    client = PartialOnly()
    cir = _install(monkeypatch, client, [{'sku': 'SKU-IMG'}], marked=marked)

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['failed'] == ['SKU-IMG']
    assert rep['done'] == []
    assert client.calls == []
    assert 'refusing partial inventory write' in rep['rows'][0]['reason']
    assert marked == []


def test_live_get_failure_skips_without_writing_local_fields(monkeypatch):
    marked = []
    client = FakeClient({
        'SKU-GET': RuntimeError('inventory GET down'),
        'SKU-OK': _live_item(quantity=5, sku='SKU-OK'),
    })
    cir = _install(
        monkeypatch,
        client,
        [{'sku': 'SKU-GET'}, {'sku': 'SKU-OK'}],
        marked=marked,
    )

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['skipped'] == ['SKU-GET']
    assert rep['done'] == ['SKU-OK']
    assert rep['failed'] == []
    assert rep['rows'][0]['reason'].startswith('live GET failed:')
    assert 'Local title' not in str(client.puts)
    assert 'Local description' not in str(client.puts)
    assert list(client.put_by_sku) == ['SKU-OK']
    assert client.partial_writes == []
    assert marked == [(['SKU-OK'], 'image_refresh', 'done')]


def test_readback_image_count_must_match_or_stay_queued(monkeypatch):
    """2 张写出、读回 1 张不算 done; 3 张写出、读回 3 张才 done."""
    marked = []
    two = ['https://img/a.jpg', 'https://img/b.jpg']
    three = [
        'https://img/1.jpg?x-cc=10&x-cu=1&x-ct=2&x-cs=sig/part=a%20b',
        'https://img/2.jpg?x-cc=10&x-cu=1&x-ct=2&x-cs=sig/part=a%20b',
        'https://img/3.jpg?x-cc=10&x-cu=1&x-ct=2&x-cs=sig/part=a%20b',
    ]
    client = FakeClient(
        {
            'SKU-COLLAPSE': _live_item(quantity=5, sku='SKU-COLLAPSE'),
            'SKU-EXACT': _live_item(quantity=5, sku='SKU-EXACT'),
        },
        readback={
            'SKU-COLLAPSE': two[:1],
            'SKU-EXACT': list(three),
        },
    )

    def images(sku):
        return list(two if sku == 'SKU-COLLAPSE' else three)

    cir = _install(
        monkeypatch,
        client,
        [{'sku': 'SKU-COLLAPSE'}, {'sku': 'SKU-EXACT'}],
        images=images,
        marked=marked,
    )

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['failed'] == ['SKU-COLLAPSE']
    assert rep['done'] == ['SKU-EXACT']
    assert rep['rows'][0]['reason'] == 'verification image count mismatch'
    assert rep['rows'][0]['expected_count'] == 2
    assert rep['rows'][0]['live_n'] == 1
    assert rep['rows'][1]['live_n'] == 3
    assert marked == [(['SKU-EXACT'], 'image_refresh', 'done')]
    sent = client.put_by_sku['SKU-EXACT']['product']['imageUrls']
    assert [url.encode('utf-8') for url in sent] == [url.encode('utf-8') for url in three]
    assert client.partial_writes == []


def test_nonpositive_package_weight_dropped_from_put_body(monkeypatch):
    marked = []
    zero_only = _live_item(quantity=5, sku='SKU-W0')
    zero_only['packageWeightAndSize'] = {'weight': {'value': 0, 'unit': 'POUND'}}
    zero_with_dims = _live_item(quantity=5, sku='SKU-WD')
    zero_with_dims['packageWeightAndSize'] = {
        'weight': {'value': 0, 'unit': 'POUND'},
        'dimensions': {
            'height': 10.0, 'length': 40.0, 'width': 20.0, 'unit': 'INCH',
        },
    }
    client = FakeClient({
        'SKU-W0': zero_only,
        'SKU-WD': zero_with_dims,
    })
    cir = _install(
        monkeypatch,
        client,
        [{'sku': 'SKU-W0'}, {'sku': 'SKU-WD'}],
        marked=marked,
    )

    rep = cir.run(apply_changes=True, limit=10)

    assert rep['done'] == ['SKU-W0', 'SKU-WD']
    assert rep['failed'] == []
    body_zero = client.put_by_sku['SKU-W0']
    body_dims = client.put_by_sku['SKU-WD']
    assert 'packageWeightAndSize' not in body_zero
    assert 'weight' not in body_dims['packageWeightAndSize']
    assert body_dims['packageWeightAndSize']['dimensions'] == {
        'height': 10.0, 'length': 40.0, 'width': 20.0, 'unit': 'INCH',
    }
    assert body_zero['availability']['shipToLocationAvailability']['quantity'] == 5
    assert body_dims['availability']['shipToLocationAvailability']['quantity'] == 5
    assert zero_only['packageWeightAndSize']['weight']['value'] == 0
    assert marked == [
        (['SKU-W0'], 'image_refresh', 'done'),
        (['SKU-WD'], 'image_refresh', 'done'),
    ]


def test_should_refresh_normalizes_oss_on_both_sides():
    from scripts.cro_image_refresh import (
        _normalize_urls_for_compare,
        _should_refresh,
        strip_oss_keep_giga_sig,
    )

    local = [_URL_OSS_MIDDLE, _URL_UNCHANGED]
    live = [strip_oss_keep_giga_sig(u) for u in local]
    assert 'x-oss-process' in local[0]
    assert 'x-oss-process' not in live[0]
    assert _normalize_urls_for_compare(local) == _normalize_urls_for_compare(live)
    assert _should_refresh(local, live) is None
    assert _should_refresh(local, ['https://img/other.jpg', live[1]]) == (
        'local lead image differs from live'
    )


def test_refresh_one_second_run_healthy_after_oss_strip(monkeypatch):
    local_images = [_URL_OSS_MIDDLE, _URL_UNCHANGED]
    client = FakeClient({'SKU-OSS': _live_item(quantity=5, sku='SKU-OSS')})
    cir = _install(monkeypatch, client, [{'sku': 'SKU-OSS'}], images=local_images)

    first = cir._refresh_one(client, 'SKU-OSS', {'images': local_images})
    assert first['status'] == 'done'
    assert len(client.puts) == 1
    written = client.puts[0]['json']['product']['imageUrls']
    assert 'x-oss-process' not in written[0]
    assert written[0] == _URL_OSS_MIDDLE_EXPECTED

    second = cir._refresh_one(client, 'SKU-OSS', {'images': local_images})
    assert second['status'] == 'skipped'
    assert second['reason'] == 'live already healthy'
    assert len(client.puts) == 1


def test_prepare_refresh_truncates_without_failing_sigs():
    from scripts.cro_image_refresh import (
        _INVENTORY_IMAGE_LIMIT,
        prepare_refresh_image_urls,
        urls_keep_giga_sigs,
    )

    local = [
        (
            f'https://b2bfiles1.gigab2b.cn/img/{i:02d}.jpg'
            f'?x-oss-process=image/resize,w_800&x-cc=c{i}&x-cu=u{i}&x-ct=t{i}&x-cs=s{i}'
        )
        for i in range(25)
    ]
    prepared, reason = prepare_refresh_image_urls(local)
    assert reason is None
    assert len(prepared) == _INVENTORY_IMAGE_LIMIT == 24
    assert urls_keep_giga_sigs(local, prepared) is True
    assert all('x-oss-process' not in url for url in prepared)
    assert all(f'x-cs=s{i}' in prepared[i] for i in range(24))
