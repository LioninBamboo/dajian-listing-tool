"""S110 — HMAC + 链式哈希审计日志.

每条记录: HMAC-SHA256 签名 (verify_record).
链式: prev_hash + canonical_json → SHA256, 防回溯篡改 (verify_chain).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional

GENESIS_HASH = '0' * 64


def _canonical(record: Dict[str, Any]) -> str:
    clean = {k: v for k, v in record.items()
             if k not in ('signature', 'chain_hash', 'prev_hash')}
    return json.dumps(clean, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, default=str)


def sign_record(record: Dict[str, Any], secret: str) -> Dict[str, Any]:
    payload = _canonical(record).encode('utf-8')
    sig = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    return {**record, 'signature': sig}


def verify_record(record: Dict[str, Any], secret: str) -> bool:
    sig = record.get('signature')
    if not sig:
        return False
    expected = hmac.new(secret.encode('utf-8'),
                        _canonical(record).encode('utf-8'),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def chain_hash(prev_hash: str, record: Dict[str, Any]) -> str:
    payload = (prev_hash + _canonical(record)).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def append_to_chain(records: List[Dict[str, Any]],
                    record: Dict[str, Any],
                    secret: str) -> Dict[str, Any]:
    prev = records[-1]['chain_hash'] if records else GENESIS_HASH
    signed = sign_record(record, secret)
    h = chain_hash(prev, signed)
    return {**signed, 'prev_hash': prev, 'chain_hash': h}


def verify_chain(records: List[Dict[str, Any]],
                 secret: str) -> Dict[str, Any]:
    prev = GENESIS_HASH
    for i, rec in enumerate(records):
        if not verify_record(rec, secret):
            return {'valid': False, 'first_invalid_index': i,
                    'reason': 'bad_signature'}
        if rec.get('prev_hash') != prev:
            return {'valid': False, 'first_invalid_index': i,
                    'reason': 'broken_prev_hash'}
        expected = chain_hash(prev, {k: v for k, v in rec.items()
                                      if k not in ('chain_hash', 'prev_hash')})
        if rec.get('chain_hash') != expected:
            return {'valid': False, 'first_invalid_index': i,
                    'reason': 'broken_chain_hash'}
        prev = rec['chain_hash']
    return {'valid': True, 'count': len(records),
            'tip_hash': prev if records else GENESIS_HASH}
