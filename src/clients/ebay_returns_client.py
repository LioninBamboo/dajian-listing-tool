"""S62 — eBay Returns 真客户端调用层.

不修改 real_ebay_client.py (大文件), 而是用一个薄层包它的 oauth+session,
对外暴露 get_returns(days) → list[raw return].
组合 ebay_returns_adapter.aggregate_by_sku 即可喂给 cro_returns_feedback.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

RETURNS_URL = '/sell/fulfillment/v1/return'
DEFAULT_LIMIT = 200


def make_authed_get(client: Any) -> Callable[[str, Dict[str, Any]],
                                              Dict[str, Any]]:
    """从 RealEbayClient 实例生成 http_get(path, params)."""
    def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f'{client.base_url}{path}'
        token = client.oauth.get_valid_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
        }
        try:
            r = client.session.get(url, headers=headers, params=params,
                                   timeout=60)
            if r.status_code == 200:
                return r.json() or {}
            logging.warning('eBay returns API non-200: %s', r.status_code)
            return {}
        except Exception as e:
            logging.error('eBay returns fetch failed: %s', e)
            return {}
    return _get


def get_returns(client: Any, days: int = 30,
                limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """便捷封装: 直接拉一页 returns."""
    http_get = make_authed_get(client)
    data = http_get(RETURNS_URL, {'limit': limit, 'lookback_days': days})
    return data.get('returns') or []


def make_returns_fetcher(client: Any,
                         sold_lookup: Optional[Callable[[str], int]] = None,
                         days: int = 30,
                         ) -> Callable[[], List[Dict[str, Any]]]:
    """组合: 返回 cro_returns_feedback.analyze_returns 直接可吞的 fetcher."""
    from src.services.ebay_returns_adapter import returns_fetcher_factory
    return returns_fetcher_factory(make_authed_get(client),
                                   sold_lookup=sold_lookup, days=days)
