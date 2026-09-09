"""S62 — eBay returns client tests."""
from __future__ import annotations

from src.clients.ebay_returns_client import (
    get_returns, make_authed_get, make_returns_fetcher,
)


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.last_call = None
    def get(self, url, headers=None, params=None, timeout=None):
        self.last_call = {'url': url, 'headers': headers, 'params': params}
        return self.resp


class _FakeOAuth:
    def get_valid_token(self):
        return 'TOK_XYZ'


def _client(resp):
    class _C:
        base_url = 'https://api.ebay.com'
        oauth = _FakeOAuth()
        session = _FakeSession(resp)
    return _C()


def test_make_authed_get_passes_token_and_params():
    c = _client(_FakeResp(200, {'returns': [{'sku': 'A'}]}))
    http = make_authed_get(c)
    out = http('/sell/fulfillment/v1/return', {'limit': 50})
    assert out == {'returns': [{'sku': 'A'}]}
    assert c.session.last_call['headers']['Authorization'] == 'Bearer TOK_XYZ'
    assert c.session.last_call['params']['limit'] == 50


def test_make_authed_get_non_200_returns_empty():
    c = _client(_FakeResp(401, {'error': 'unauthorized'}))
    out = make_authed_get(c)('/x', {})
    assert out == {}


def test_make_authed_get_swallows_exception():
    class _BadSess:
        def get(self, *a, **k):
            raise RuntimeError('boom')
    class _C:
        base_url = 'https://x'
        oauth = _FakeOAuth()
        session = _BadSess()
    out = make_authed_get(_C())('/x', {})
    assert out == {}


def test_get_returns_unwraps_returns_key():
    c = _client(_FakeResp(200, {'returns': [{'sku': 'A'}, {'sku': 'B'}]}))
    out = get_returns(c, days=14)
    assert len(out) == 2
    assert c.session.last_call['params']['lookback_days'] == 14


def test_get_returns_empty_when_missing_key():
    c = _client(_FakeResp(200, {}))
    assert get_returns(c) == []


def test_make_returns_fetcher_chains_with_adapter():
    c = _client(_FakeResp(200, {'returns': [
        {'sku': 'A', 'reason': 'DEFECTIVE'},
        {'sku': 'A', 'reason': 'DEFECTIVE'},
    ]}))
    fetcher = make_returns_fetcher(c, sold_lookup=lambda s: 100)
    rows = fetcher()
    assert len(rows) == 1
    assert rows[0]['sku'] == 'A'
    assert rows[0]['return_count'] == 2
    assert rows[0]['sold_count'] == 100
