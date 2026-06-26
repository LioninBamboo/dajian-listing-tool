"""CRO ops control-plane tests."""
from __future__ import annotations

import json
from datetime import datetime

from src.services.cro_ops_control_plane import (
    build_capacity_components_from_paths,
    build_ops_snapshot,
    render_ops_markdown,
    write_ops_snapshot,
)


def test_capacity_sample_records_once_per_day(tmp_path):
    db = tmp_path / 'ebay_collection.db'
    db.write_bytes(b'12345')
    sample_log = tmp_path / 'capacity.jsonl'
    now = datetime(2026, 3, 30, 9, 0, 0)

    first = build_capacity_components_from_paths(
        {'db': db}, sample_log_path=sample_log, record_sample=True, now=now)
    second = build_capacity_components_from_paths(
        {'db': db}, sample_log_path=sample_log, record_sample=True, now=now)

    assert first['db']['current_value'] == 5.0
    assert second['db']['samples'] == first['db']['samples']
    assert len(sample_log.read_text(encoding='utf-8').strip().splitlines()) == 1


def test_capacity_sample_ignores_corrupt_day_index(tmp_path):
    db = tmp_path / 'ebay_collection.db'
    db.write_bytes(b'12345')
    sample_log = tmp_path / 'capacity.jsonl'
    sample_log.write_text(
        json.dumps({'component': 'db', 'day_index': 'bad', 'value': 1}) + '\n',
        encoding='utf-8',
    )

    out = build_capacity_components_from_paths(
        {'db': db}, sample_log_path=sample_log, record_sample=True,
        now=datetime(2026, 3, 30, 9, 0, 0))

    assert out['db']['current_value'] == 5.0
    assert len(sample_log.read_text(encoding='utf-8').strip().splitlines()) == 2


def test_snapshot_red_when_compliance_lacks_legal_basis(tmp_path):
    log_path = tmp_path / 'compliance.jsonl'
    log_path.write_text(
        json.dumps({'ts': '2026-03-30T09:00:00', 'action': 'pii_access'}) + '\n',
        encoding='utf-8',
    )

    snapshot = build_ops_snapshot(compliance_log_path=log_path)

    assert snapshot['overall_status'] == 'red'
    assert 'compliance missing legal basis' in snapshot['reasons']


def test_snapshot_yellow_for_improvement_suggestions():
    snapshot = build_ops_snapshot(
        traffic_light_fn=lambda: {'red_pillars': ['returns']},
    )

    assert snapshot['overall_status'] == 'yellow'
    section = snapshot['sections']['continuous_improvement']
    assert section['suggestion_count'] == 1
    assert 'Weekly Improvement Brief' in section['weekly_brief']


def test_write_and_render_snapshot(tmp_path):
    output = tmp_path / 'ops.json'
    snapshot = build_ops_snapshot()

    write_ops_snapshot(snapshot, output)
    markdown = render_ops_markdown(snapshot)

    assert output.exists()
    assert json.loads(output.read_text(encoding='utf-8'))['overall_status'] == 'green'
    assert 'Overall: GREEN' in markdown