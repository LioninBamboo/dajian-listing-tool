"""S136 — data retention tests."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta

import pytest

from src.services.cro_data_retention import apply_retention


def _write(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_retention_negative_raises(tmp_path):
    with pytest.raises(ValueError):
        apply_retention(str(tmp_path / 'a'), str(tmp_path / 'b'),
                         retention_days=-1)


def test_archives_old_keeps_recent(tmp_path):
    src = tmp_path / 'src.jsonl'
    arc = tmp_path / 'arc.jsonl.gz'
    now = datetime(2026, 5, 1)
    _write(src, [
        {'ts': '2026-04-01T00:00:00', 'event': 'old'},
        {'ts': '2026-04-30T00:00:00', 'event': 'recent'},
    ])
    out = apply_retention(str(src), str(arc), retention_days=10, now=now)
    assert out['archived'] == 1
    assert out['kept'] == 1
    # gzip archive
    with gzip.open(str(arc), 'rt', encoding='utf-8') as f:
        archived = [json.loads(l) for l in f if l.strip()]
    assert archived[0]['event'] == 'old'
    # source 只剩 recent
    remaining = [json.loads(l) for l in open(str(src)) if l.strip()]
    assert remaining[0]['event'] == 'recent'


def test_dry_run_does_not_modify(tmp_path):
    src = tmp_path / 'src.jsonl'
    arc = tmp_path / 'arc.jsonl.gz'
    _write(src, [{'ts': '2026-01-01T00:00:00', 'event': 'old'}])
    before = open(str(src)).read()
    out = apply_retention(str(src), str(arc), retention_days=10,
                            now=datetime(2026, 5, 1), dry_run=True)
    assert out['archived'] == 1
    assert out['dry_run'] is True
    assert open(str(src)).read() == before
    assert not arc.exists()


def test_unparseable_ts_kept(tmp_path):
    src = tmp_path / 'src.jsonl'
    arc = tmp_path / 'arc.jsonl.gz'
    _write(src, [{'event': 'no_ts'},
                  {'ts': 'not a date', 'event': 'bad_ts'}])
    out = apply_retention(str(src), str(arc), retention_days=10,
                            now=datetime(2026, 5, 1))
    assert out['archived'] == 0
    assert out['unparseable_kept'] == 2
    assert out['kept'] == 2


def test_corrupt_line_kept(tmp_path):
    src = tmp_path / 'src.jsonl'
    arc = tmp_path / 'arc.jsonl.gz'
    src.write_text('{"ts":"2026-04-01T00:00:00"}\nNOT JSON\n')
    out = apply_retention(str(src), str(arc), retention_days=10,
                            now=datetime(2026, 5, 1))
    assert out['unparseable_kept'] == 1
    # corrupt 行原样保留
    assert 'NOT JSON' in open(str(src)).read()


def test_no_source_returns_zero(tmp_path):
    out = apply_retention(str(tmp_path / 'nope.jsonl'),
                           str(tmp_path / 'arc.jsonl.gz'),
                           retention_days=10,
                           now=datetime(2026, 5, 1))
    assert out['total_in'] == 0
    assert out['archived'] == 0


def test_no_archive_when_nothing_old(tmp_path):
    src = tmp_path / 'src.jsonl'
    arc = tmp_path / 'arc.jsonl.gz'
    _write(src, [{'ts': '2026-04-30T00:00:00', 'event': 'recent'}])
    out = apply_retention(str(src), str(arc), retention_days=10,
                            now=datetime(2026, 5, 1))
    assert out['archived'] == 0
    assert not arc.exists()


def test_uncompressed_archive(tmp_path):
    src = tmp_path / 'src.jsonl'
    arc = tmp_path / 'arc.jsonl'
    _write(src, [{'ts': '2026-01-01T00:00:00', 'event': 'x'}])
    apply_retention(str(src), str(arc), retention_days=10,
                     now=datetime(2026, 5, 1), gzip_archive=False)
    txt = open(str(arc)).read()
    assert '"event": "x"' in txt or '"event":"x"' in txt
