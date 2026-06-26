"""S127 — health invariants tests."""
from __future__ import annotations

import json

from src.services.cro_audit_trail import append_to_chain
from src.services.cro_health_invariants import (
    check_audit_chain, check_every_done_has_trace,
    check_no_negative_cohort, run_all,
)

SECRET = 's'


def test_no_negative_cohort_passes():
    assert check_no_negative_cohort([{'sku': 'A', 'cohort': 5}]) == []


def test_no_negative_cohort_flags_negative():
    v = check_no_negative_cohort([{'sku': 'A', 'cohort': -1}])
    assert len(v) == 1
    assert v[0]['cohort'] == -1


def test_no_negative_cohort_invalid_value():
    v = check_no_negative_cohort([{'sku': 'A', 'cohort': 'wrong'}])
    assert v[0]['detail'] == 'invalid_cohort_value'


def test_audit_chain_valid():
    recs = []
    recs.append(append_to_chain(recs, {'event': 'a'}, SECRET))
    recs.append(append_to_chain(recs, {'event': 'b'}, SECRET))
    assert check_audit_chain(recs, SECRET) == []


def test_audit_chain_tampered():
    recs = []
    recs.append(append_to_chain(recs, {'event': 'a'}, SECRET))
    recs.append(append_to_chain(recs, {'event': 'b'}, SECRET))
    # tamper
    recs[0] = {**recs[0], 'event': 'tampered'}
    v = check_audit_chain(recs, SECRET)
    assert len(v) == 1
    assert v[0]['check'] == 'audit_chain'


def test_done_must_have_trace():
    rows = [
        {'sku': 'A', 'status': 'done', 'reasoning_trace': 'why'},
        {'sku': 'B', 'status': 'done'},
        {'sku': 'C', 'status': 'pending'},
    ]
    v = check_every_done_has_trace(rows)
    assert [x['sku'] for x in v] == ['B']


def test_run_all_combines(tmp_path):
    log = str(tmp_path / 'v.jsonl')
    out = run_all(queue_rows=[{'sku': 'A', 'cohort': -1}],
                   done_rows=[{'sku': 'B', 'status': 'done'}],
                   log_path=log)
    assert out['failed_count'] == 2
    assert out['by_check']['no_negative_cohort'] == 1
    assert out['by_check']['done_has_trace'] == 1
    written = open(log).read().strip().splitlines()
    assert len(written) == 2
    for line in written:
        rec = json.loads(line)
        assert 'ts' in rec


def test_run_all_no_audit_section_skipped():
    out = run_all(audit_records=[{'event': 'x'}], secret=None)
    assert 'audit_chain' not in out['by_check']


def test_run_all_empty_returns_zero_failed():
    out = run_all()
    assert out['failed_count'] == 0
    assert out['violations'] == []


def test_run_all_audit_path_passes():
    recs = []
    recs.append(append_to_chain(recs, {'event': 'a'}, SECRET))
    out = run_all(audit_records=recs, secret=SECRET)
    assert out['failed_count'] == 0
    assert out['by_check']['audit_chain'] == 0
