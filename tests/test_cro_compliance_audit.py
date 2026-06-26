"""S139 — compliance audit tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.services.cro_compliance_audit import (
    generate_audit_report, generate_dsar_export, log_access,
    log_compliance_event, query_subject_history, search_user_activity,
    verify_lawful_basis,
)


def test_actor_required(tmp_path):
    with pytest.raises(ValueError):
        log_compliance_event(log_path=str(tmp_path / 'a.jsonl'),
                              actor='', action='pii_access',
                              resource_type='product')


def test_unknown_action_rejected(tmp_path):
    with pytest.raises(ValueError):
        log_compliance_event(log_path=str(tmp_path / 'a.jsonl'),
                              actor='alice', action='hack',
                              resource_type='product')


def test_unknown_legal_basis_rejected(tmp_path):
    with pytest.raises(ValueError):
        log_compliance_event(log_path=str(tmp_path / 'a.jsonl'),
                              actor='alice', action='pii_access',
                              resource_type='product',
                              legal_basis='vibes')


def test_verify_lawful_basis():
    assert verify_lawful_basis('consent') == 'consent'
    with pytest.raises(ValueError):
        verify_lawful_basis('vibes')


def test_log_access_requires_lawful_basis(tmp_path):
    with pytest.raises(ValueError):
        log_access('alice', 'u1', 'pii_access', '', str(tmp_path / 'a.jsonl'))


def test_log_event_appended(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    out = log_compliance_event(log_path=p, actor='alice',
                                 action='pii_access',
                                 resource_type='user',
                                 user_id='u1',
                                 legal_basis='consent')
    assert out['ok'] is True
    assert open(p).read().strip() != ''


def test_audit_report_aggregates(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    log_compliance_event(log_path=p, actor='alice',
                          action='pii_access', resource_type='user',
                          legal_basis='consent')
    log_compliance_event(log_path=p, actor='alice',
                          action='pii_access', resource_type='user',
                          legal_basis='consent')
    log_compliance_event(log_path=p, actor='bob',
                          action='data_export', resource_type='order')
    report = generate_audit_report(p)
    assert report['total_in_window'] == 3
    assert report['by_actor'] == {'alice': 2, 'bob': 1}
    assert report['by_action']['pii_access'] == 2


def test_missing_legal_basis_flagged(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    log_compliance_event(log_path=p, actor='alice',
                          action='pii_access', resource_type='user')
    log_compliance_event(log_path=p, actor='alice',
                          action='consent_grant', resource_type='user')
    rep = generate_audit_report(p)
    assert rep['missing_legal_basis'] == 1


def test_window_filter(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    log_compliance_event(log_path=p, actor='alice',
                          action='pii_access', resource_type='user')
    # 给一个未来 since 应该过滤掉所有
    rep = generate_audit_report(
        p, since=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1))
    assert rep['total_in_window'] == 0


def test_search_user_activity(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    log_compliance_event(log_path=p, actor='alice',
                          action='pii_access', resource_type='user',
                          user_id='u1')
    log_compliance_event(log_path=p, actor='alice',
                          action='data_export', resource_type='user',
                          user_id='u2')
    rows = search_user_activity(p, 'u1')
    assert len(rows) == 1
    assert rows[0]['user_id'] == 'u1'


def test_search_empty_user_id_returns_empty(tmp_path):
    assert search_user_activity(str(tmp_path / 'nope.jsonl'), '') == []


def test_audit_report_no_log_file(tmp_path):
    rep = generate_audit_report(str(tmp_path / 'nope.jsonl'))
    assert rep['total_in_window'] == 0
    assert rep['by_actor'] == {}


def test_query_subject_history_returns_subject_rows(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    log_access('alice', 'u1', 'pii_access', 'consent', p)
    log_access('alice', 'u2', 'pii_access', 'consent', p)
    rows = query_subject_history('u1', p)
    assert len(rows) == 1
    assert rows[0]['subject_id'] == 'u1'


def test_generate_dsar_export_redacts_requested_keys(tmp_path):
    p = str(tmp_path / 'a.jsonl')
    log_access('alice', 'u1', 'data_export', 'contract', p)
    log_compliance_event(log_path=p, actor='alice', action='pii_access',
                          resource_type='user', user_id='u1',
                          legal_basis='consent',
                          extra={'email': 'a@example.com', 'note': 'ok'})
    export = generate_dsar_export('u1', p, redact_keys={'email'})
    assert export['subject_id'] == 'u1'
    assert export['events'][-1]['extra']['email'] == '***'
    assert export['events'][-1]['extra']['note'] == 'ok'
