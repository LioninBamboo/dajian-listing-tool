"""S124 — Publish Gate tests."""
from __future__ import annotations

import json
import os
import pytest

from src.services.cro_audit_trail import GENESIS_HASH, verify_chain
from src.services.cro_publish_gate import gate_publish

SECRET = 'test-secret'


def _empty_log(tmp_path):
    return str(tmp_path / 'velocity.jsonl')


def test_secret_required(tmp_path):
    with pytest.raises(ValueError):
        gate_publish(['A'], log_path=_empty_log(tmp_path), secret='')


def test_default_audit_passes_all_within_limits(tmp_path):
    out = gate_publish(['A', 'B'], log_path=_empty_log(tmp_path),
                        secret=SECRET)
    assert out['approved'] == ['A', 'B']
    assert out['audit_failed'] == []
    assert out['deferred'] == []


def test_audit_failure_excluded(tmp_path):
    def audit(sku):
        return {'ok': sku != 'B', 'issues': ['bad'] if sku == 'B' else []}
    out = gate_publish(['A', 'B'], log_path=_empty_log(tmp_path),
                        audit_check=audit, secret=SECRET)
    assert out['approved'] == ['A']
    assert out['audit_failed'][0]['sku'] == 'B'


def test_audit_exception_marks_fail(tmp_path):
    def boom(sku):
        raise RuntimeError('audit down')
    out = gate_publish(['A'], log_path=_empty_log(tmp_path),
                        audit_check=boom, secret=SECRET)
    assert out['approved'] == []
    assert 'audit_check_error' in out['audit_failed'][0]['issues'][0]


def test_chain_is_valid(tmp_path):
    out = gate_publish(['A', 'B', 'C'], log_path=_empty_log(tmp_path),
                        secret=SECRET)
    assert verify_chain(out['signed_records'], SECRET)['valid'] is True
    assert out['chain_tip_hash'] != GENESIS_HASH


def test_empty_approved_returns_genesis_tip(tmp_path):
    def audit(sku):
        return {'ok': False, 'issues': ['nope']}
    out = gate_publish(['A'], log_path=_empty_log(tmp_path),
                        audit_check=audit, secret=SECRET)
    assert out['signed_records'] == []
    assert out['chain_tip_hash'] == GENESIS_HASH


def test_velocity_limit_defers(tmp_path):
    out = gate_publish(['A', 'B', 'C'], log_path=_empty_log(tmp_path),
                        secret=SECRET,
                        limits={'per_day': 1, 'per_week': 10,
                                 'per_month': 100})
    assert out['approved'] == ['A']
    assert out['deferred'] == ['B', 'C']


def test_velocity_remaining_after_reported(tmp_path):
    out = gate_publish(['A'], log_path=_empty_log(tmp_path),
                        secret=SECRET,
                        limits={'per_day': 5, 'per_week': 5,
                                 'per_month': 5})
    rem = out['velocity_remaining_after']
    assert rem['per_day'] == 4
    assert rem['per_week'] == 4
    assert rem['per_month'] == 4


def test_signed_records_contain_event_field(tmp_path):
    out = gate_publish(['A'], log_path=_empty_log(tmp_path), secret=SECRET)
    rec = out['signed_records'][0]
    assert rec['sku'] == 'A'
    assert rec['event'] == 'publish_approved'
    assert 'signature' in rec
    assert 'chain_hash' in rec
