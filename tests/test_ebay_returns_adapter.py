"""S56 — eBay returns adapter tests."""
from __future__ import annotations

from src.services.cro_returns_feedback import analyze_returns
from src.services.ebay_returns_adapter import (
    aggregate_by_sku, fetch_returns_raw, returns_fetcher_factory,
)


def _http_with(payload):
    return lambda path, params: payload


def test_fetch_returns_raw_passes_params_and_unwraps():
    captured = {}
    def fake(path, params):
        captured['path'] = path
        captured['params'] = params
        return {'returns': [{'sku': 'A'}]}
    out = fetch_returns_raw(fake, days=14, limit=50)
    assert out == [{'sku': 'A'}]
    assert captured['path'].endswith('/return')
    assert captured['params']['lookback_days'] == 14
    assert captured['params']['limit'] == 50


def test_fetch_returns_raw_handles_missing_key():
    assert fetch_returns_raw(_http_with({})) == []
    assert fetch_returns_raw(_http_with(None)) == []


def test_aggregate_groups_by_sku_and_collects_reasons():
    raw = [
        {'sku': 'A', 'reason': 'defective'},
        {'sku': 'A', 'reasonCode': 'NOT_AS_DESCRIBED'},
        {'sku': 'B', 'reason': 'too_small'},
    ]
    out = {r['sku']: r for r in aggregate_by_sku(raw)}
    assert out['A']['return_count'] == 2
    assert set(out['A']['reason_codes']) == {'DEFECTIVE', 'NOT_AS_DESCRIBED'}
    assert out['B']['return_count'] == 1


def test_aggregate_extracts_sku_from_lineItems():
    raw = [{'lineItems': [{'sku': 'X'}]}]
    out = aggregate_by_sku(raw)
    assert out[0]['sku'] == 'X'


def test_aggregate_drops_records_without_sku():
    raw = [{'foo': 'bar'}, {'sku': 'A'}]
    out = aggregate_by_sku(raw)
    assert len(out) == 1


def test_aggregate_uses_sold_lookup():
    raw = [{'sku': 'A'}]
    out = aggregate_by_sku(raw, sold_lookup=lambda s: 100)
    assert out[0]['sold_count'] == 100


def test_factory_handles_http_exception_returns_empty():
    def boom(path, params):
        raise RuntimeError('eBay 500')
    fetcher = returns_fetcher_factory(boom)
    assert fetcher() == []


def test_factory_chains_into_analyze_returns():
    raw = {'returns': [
        {'sku': 'A', 'reason': 'DEFECTIVE'},
    ] * 12}  # 12 returns of A
    fetcher = returns_fetcher_factory(
        _http_with(raw),
        sold_lookup=lambda s: 50,  # 12/50 = 24% > 15%
    )
    report = analyze_returns(fetcher)
    assert report['high_return_count'] == 1
    assert 'A' in report['blacklist_skus']
