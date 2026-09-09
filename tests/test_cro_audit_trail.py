"""S110 — audit trail tests."""
from __future__ import annotations

from src.services.cro_audit_trail import (
    GENESIS_HASH, append_to_chain, chain_hash, sign_record, verify_chain,
    verify_record,
)


SECRET = 'top-secret-key'


def test_sign_then_verify():
    rec = sign_record({'sku': 'A', 'price': 9.99}, SECRET)
    assert 'signature' in rec
    assert verify_record(rec, SECRET) is True


def test_verify_fails_on_tamper():
    rec = sign_record({'sku': 'A', 'price': 9.99}, SECRET)
    rec['price'] = 99.99
    assert verify_record(rec, SECRET) is False


def test_verify_fails_with_wrong_secret():
    rec = sign_record({'sku': 'A'}, SECRET)
    assert verify_record(rec, 'other') is False


def test_verify_no_signature_returns_false():
    assert verify_record({'sku': 'A'}, SECRET) is False


def test_chain_hash_changes_when_prev_changes():
    rec = {'sku': 'A'}
    h1 = chain_hash(GENESIS_HASH, rec)
    h2 = chain_hash('a' * 64, rec)
    assert h1 != h2


def test_append_builds_chain():
    chain = []
    chain.append(append_to_chain(chain, {'sku': 'A'}, SECRET))
    chain.append(append_to_chain(chain, {'sku': 'B'}, SECRET))
    assert chain[0]['prev_hash'] == GENESIS_HASH
    assert chain[1]['prev_hash'] == chain[0]['chain_hash']
    assert chain[0]['chain_hash'] != chain[1]['chain_hash']


def test_verify_chain_ok():
    chain = []
    for sku in ('A', 'B', 'C'):
        chain.append(append_to_chain(chain, {'sku': sku}, SECRET))
    out = verify_chain(chain, SECRET)
    assert out['valid'] is True
    assert out['count'] == 3
    assert out['tip_hash'] == chain[-1]['chain_hash']


def test_verify_chain_detects_signature_tamper():
    chain = []
    for sku in ('A', 'B', 'C'):
        chain.append(append_to_chain(chain, {'sku': sku}, SECRET))
    chain[1]['sku'] = 'TAMPERED'
    out = verify_chain(chain, SECRET)
    assert out['valid'] is False
    assert out['first_invalid_index'] == 1


def test_verify_chain_detects_broken_link():
    chain = []
    for sku in ('A', 'B', 'C'):
        chain.append(append_to_chain(chain, {'sku': sku}, SECRET))
    chain[2]['prev_hash'] = '0' * 64
    out = verify_chain(chain, SECRET)
    assert out['valid'] is False
    assert out['first_invalid_index'] == 2
