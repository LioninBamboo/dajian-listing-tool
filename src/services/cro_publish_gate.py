"""S124 — Publish Gate.

发布前的最后闸门:
  1. S116 速率上限切分 (allowed / deferred)
  2. audit_check 注入回调, 返回 {ok, issues}; 不通过 → audit_failed
  3. S110 对每条放行记录签名 + chain_hash
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from src.services.cro_audit_trail import GENESIS_HASH, append_to_chain
from src.services.cro_listing_velocity import filter_publishable

logger = logging.getLogger(__name__)

AuditCheck = Callable[[str], Dict[str, Any]]


def _default_audit(_sku: str) -> Dict[str, Any]:
    return {'ok': True, 'issues': []}


def gate_publish(
    skus: Iterable[str],
    *,
    log_path: str,
    audit_check: Optional[AuditCheck] = None,
    secret: str,
    limits: Dict[str, int] = None,
) -> Dict[str, Any]:
    if not secret:
        raise ValueError('secret must be non-empty for audit signing')

    audit_check = audit_check or _default_audit

    velocity = filter_publishable(skus, log_path, limits=limits)
    candidates: List[str] = list(velocity.get('allowed') or [])
    deferred: List[str] = list(velocity.get('deferred') or [])

    approved: List[str] = []
    audit_failed: List[Dict[str, Any]] = []
    for sku in candidates:
        try:
            res = audit_check(sku) or {}
        except Exception as e:
            res = {'ok': False, 'issues': [f'audit_check_error:{e!r}']}
        if res.get('ok'):
            approved.append(sku)
        else:
            audit_failed.append({'sku': sku,
                                  'issues': list(res.get('issues') or [])})

    signed_records: List[Dict[str, Any]] = []
    for sku in approved:
        rec = {'sku': sku, 'event': 'publish_approved',
               'gate': 'cro_publish_gate'}
        signed = append_to_chain(signed_records, rec, secret)
        signed_records.append(signed)

    chain_tip = signed_records[-1]['chain_hash'] if signed_records \
        else GENESIS_HASH

    return {
        'approved': approved,
        'deferred': deferred,
        'audit_failed': audit_failed,
        'signed_records': signed_records,
        'chain_tip_hash': chain_tip,
        'velocity_remaining_after': velocity.get('remaining_after', {}),
    }
