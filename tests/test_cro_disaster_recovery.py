"""S138 — disaster recovery tests."""
from __future__ import annotations

import os

import pytest

from src.services.cro_disaster_recovery import (
    backup_database, create_backup, restore_backup, restore_to_temp,
    run_dr_drill, verify_backup,
)


def _make_data(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    (d / 'a.txt').write_text('hello')
    (d / 'b.txt').write_text('world')
    return d


def test_archive_path_required(tmp_path):
    with pytest.raises(ValueError):
        create_backup([str(tmp_path)], '')


def test_create_backup_includes_existing(tmp_path):
    d = _make_data(tmp_path)
    arc = str(tmp_path / 'b.tar.gz')
    out = create_backup([str(d), str(tmp_path / 'nope')], arc)
    assert out['ok'] is True
    assert out['included_count'] == 1
    assert out['skipped'][0]['reason'] == 'not_found'
    assert out['size_bytes'] > 0
    assert len(out['sha256']) == 64


def test_verify_backup_ok(tmp_path):
    d = _make_data(tmp_path)
    arc = str(tmp_path / 'b.tar.gz')
    create_backup([str(d)], arc)
    out = verify_backup(arc, expected_arcnames=['data'])
    assert out['ok'] is True
    assert out['entries_count'] >= 1


def test_verify_backup_missing(tmp_path):
    d = _make_data(tmp_path)
    arc = str(tmp_path / 'b.tar.gz')
    create_backup([str(d)], arc)
    out = verify_backup(arc, expected_arcnames=['no_such'])
    assert out['ok'] is False
    assert out['reason'] == 'missing_entries'


def test_verify_not_found(tmp_path):
    out = verify_backup(str(tmp_path / 'nope.tar.gz'))
    assert out['ok'] is False
    assert out['reason'] == 'not_found'


def test_backup_database_writes_manifest(tmp_path):
    src = tmp_path / 'db.sqlite'
    src.write_bytes(b'sqlite-bytes')
    out = backup_database(str(src), str(tmp_path / 'backups'))
    assert out['ok'] is True
    assert out['size_bytes'] == len(b'sqlite-bytes')
    assert len(out['sha256']) == 64
    verify = verify_backup(out['backup_path'], manifest_path=out['manifest_path'])
    assert verify['ok'] is True


def test_restore_to_temp_round_trips_bytes(tmp_path):
    src = tmp_path / 'db.sqlite'
    src.write_bytes(b'sqlite-bytes')
    out = backup_database(str(src), str(tmp_path / 'backups'))
    restored = tmp_path / 'restored.sqlite'
    restore = restore_to_temp(out['backup_path'], str(restored))
    assert restore['ok'] is True
    assert restored.read_bytes() == b'sqlite-bytes'


def test_restore_backup_extracts(tmp_path):
    d = _make_data(tmp_path)
    arc = str(tmp_path / 'b.tar.gz')
    create_backup([str(d)], arc)
    dest = str(tmp_path / 'restored')
    out = restore_backup(arc, dest)
    assert out['ok'] is True
    assert out['file_count'] == 2


def test_restore_not_found(tmp_path):
    out = restore_backup(str(tmp_path / 'nope.tar.gz'),
                          str(tmp_path / 'r'))
    assert out['ok'] is False
    assert out['reason'] == 'not_found'


def test_run_dr_drill_success(tmp_path):
    d = _make_data(tmp_path)
    out = run_dr_drill([str(d)], work_dir=str(tmp_path / 'work'),
                        expected_min_files=2)
    assert out['success'] is True
    assert out['restore']['file_count'] == 2


def test_run_dr_drill_min_files_too_high(tmp_path):
    d = _make_data(tmp_path)
    out = run_dr_drill([str(d)], work_dir=str(tmp_path / 'work'),
                        expected_min_files=999)
    assert out['success'] is False
    assert out['stage'] == 'compare'


def test_run_dr_drill_no_sources(tmp_path):
    out = run_dr_drill([], work_dir=str(tmp_path / 'work'),
                        expected_min_files=1)
    # 备份生成但没有内容 → 比较阶段失败
    assert out['success'] is False


def test_run_dr_drill_calls_smoke_check(tmp_path):
    src = tmp_path / 'db.sqlite'
    src.write_bytes(b'sqlite-bytes')
    seen = {}

    def smoke(path):
        seen['path'] = path
        return {'status': 'ok'}

    out = run_dr_drill(str(src), work_dir=str(tmp_path / 'drill'),
                        smoke_check_callable=smoke)
    assert out['success'] is True
    assert seen['path'].endswith('restored_db.sqlite')
    assert out['smoke']['status'] == 'ok'
