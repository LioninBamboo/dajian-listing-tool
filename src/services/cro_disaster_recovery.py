"""S138 — Disaster Recovery 备份+恢复演练.

create_backup: 把多个 source path 整合到 tar.gz 备份;
verify_backup: 检查备份完整性 (gzip 可解, 包含期望路径, 字节数 >0);
run_dr_drill: 模拟恢复到 tmp 目录 + 文件计数对比.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _sha256_and_size_from_gzip(path: str) -> Dict[str, Any]:
    h = hashlib.sha256()
    size = 0
    with gzip.open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            size += len(chunk)
            h.update(chunk)
    return {'sha256': h.hexdigest(), 'size_bytes': size}


def _read_manifest(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    last_row: Dict[str, Any] = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last_row = json.loads(line)
                except Exception:
                    continue
    except Exception:
        return {}
    return last_row


def _safe_extract_tar(archive_path: str, dest_dir: str) -> None:
    with tarfile.open(archive_path, 'r:gz') as tar:
        for member in tar.getmembers():
            target = os.path.abspath(os.path.join(dest_dir, member.name))
            root = os.path.abspath(dest_dir)
            if os.path.commonpath([root, target]) != root:
                raise ValueError(f'unsafe archive member: {member.name}')
        tar.extractall(dest_dir, filter='data')


def backup_database(src_path: str, dest_dir: str) -> Dict[str, Any]:
    if not src_path:
        raise ValueError('src_path required')
    if not dest_dir:
        raise ValueError('dest_dir required')
    if not os.path.exists(src_path):
        return {'ok': False, 'reason': 'not_found', 'src_path': src_path}

    os.makedirs(dest_dir, exist_ok=True)
    backup_name = f'{os.path.basename(src_path)}.gz'
    backup_path = os.path.join(dest_dir, backup_name)
    manifest_path = os.path.join(dest_dir, 'backup_manifest.jsonl')

    try:
        with open(src_path, 'rb') as src, gzip.open(backup_path, 'wb') as dest:
            shutil.copyfileobj(src, dest)
        size_bytes = os.path.getsize(src_path)
        sha256 = _sha256_of_file(src_path)
        record = {
            'ts': _utcnow_iso(),
            'src_path': src_path,
            'backup_path': backup_path,
            'size_bytes': size_bytes,
            'sha256': sha256,
        }
        with open(manifest_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        return {
            'ok': True,
            'backup_path': backup_path,
            'manifest_path': manifest_path,
            'sha256': sha256,
            'size_bytes': size_bytes,
        }
    except Exception as e:
        return {'ok': False, 'reason': f'backup_failed:{e!r}', 'src_path': src_path}


def create_backup(
    sources: List[str],
    archive_path: str,
) -> Dict[str, Any]:
    if not archive_path:
        raise ValueError('archive_path required')
    os.makedirs(os.path.dirname(archive_path) or '.', exist_ok=True)
    included: List[str] = []
    skipped: List[Dict[str, Any]] = []
    try:
        with tarfile.open(archive_path, 'w:gz', dereference=False) as tar:
            for s in sources or []:
                if not os.path.exists(s):
                    skipped.append({'path': s, 'reason': 'not_found'})
                    continue
                try:
                    tar.add(s, arcname=os.path.basename(s.rstrip(os.sep)))
                    included.append(s)
                except Exception as e:
                    skipped.append({'path': s, 'reason': repr(e)})
    except Exception as e:
        return {'ok': False, 'error': repr(e), 'archive_path': archive_path}
    size = os.path.getsize(archive_path) if os.path.exists(archive_path) else 0
    sha = _sha256_of_file(archive_path) if size > 0 else ''
    return {
        'ok': True,
        'archive_path': archive_path,
        'created_at': _utcnow_iso(),
        'size_bytes': size,
        'sha256': sha,
        'included_count': len(included),
        'skipped': skipped,
    }


def verify_backup(archive_path: str,
                   *, expected_arcnames: Iterable[str] = (),
                   manifest_path: Optional[str] = None,
                   expected_sha: Optional[str] = None) -> Dict[str, Any]:
    if not os.path.exists(archive_path):
        return {'ok': False, 'reason': 'not_found'}
    if os.path.getsize(archive_path) == 0:
        return {'ok': False, 'reason': 'zero_bytes'}
    if expected_arcnames or tarfile.is_tarfile(archive_path):
        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                names = tar.getnames()
        except Exception as e:
            return {'ok': False, 'reason': f'open_failed:{e!r}'}
        missing = [n for n in (expected_arcnames or [])
                    if not any(m == n or m.startswith(n + '/') for m in names)]
        return {
            'ok': not missing,
            'reason': 'missing_entries' if missing else 'verified',
            'entries_count': len(names),
            'missing': missing,
        }

    try:
        stats = _sha256_and_size_from_gzip(archive_path)
    except Exception as e:
        return {'ok': False, 'reason': f'open_failed:{e!r}'}

    manifest = _read_manifest(manifest_path) if manifest_path else {}
    expected_sha = expected_sha or manifest.get('sha256')
    expected_size = manifest.get('size_bytes')
    mismatch = []
    if expected_sha and stats['sha256'] != expected_sha:
        mismatch.append('sha256')
    if expected_size is not None and stats['size_bytes'] != expected_size:
        mismatch.append('size_bytes')
    return {
        'ok': not mismatch,
        'reason': 'mismatch' if mismatch else 'verified',
        'sha256': stats['sha256'],
        'size_bytes': stats['size_bytes'],
        'mismatch': mismatch,
    }


def restore_backup(archive_path: str, dest_dir: str) -> Dict[str, Any]:
    if not os.path.exists(archive_path):
        return {'ok': False, 'reason': 'not_found'}
    os.makedirs(dest_dir, exist_ok=True)
    try:
        _safe_extract_tar(archive_path, dest_dir)
    except Exception as e:
        return {'ok': False, 'reason': f'extract_failed:{e!r}'}
    file_count = sum(len(files) for _, _, files in os.walk(dest_dir))
    return {'ok': True, 'dest': dest_dir, 'file_count': file_count}


def restore_to_temp(backup_path: str, dest_path: str) -> Dict[str, Any]:
    if not os.path.exists(backup_path):
        return {'ok': False, 'reason': 'not_found'}
    os.makedirs(os.path.dirname(dest_path) or '.', exist_ok=True)
    try:
        with gzip.open(backup_path, 'rb') as src, open(dest_path, 'wb') as dest:
            shutil.copyfileobj(src, dest)
    except Exception as e:
        return {'ok': False, 'reason': f'restore_failed:{e!r}'}
    return {
        'ok': True,
        'dest_path': dest_path,
        'size_bytes': os.path.getsize(dest_path),
    }


def _run_tar_drill(sources: List[str], work_dir: str,
                   expected_min_files: int) -> Dict[str, Any]:
    archive = os.path.join(work_dir, 'backup.tar.gz')
    backup = create_backup(sources, archive)
    if not backup.get('ok'):
        return {'success': False, 'stage': 'backup', 'detail': backup}
    verify = verify_backup(archive,
                            expected_arcnames=[os.path.basename(s)
                                                for s in sources or []])
    if not verify.get('ok'):
        return {'success': False, 'stage': 'verify',
                'detail': verify, 'backup': backup}
    dest = os.path.join(work_dir, 'restore')
    restore = restore_backup(archive, dest)
    if not restore.get('ok'):
        return {'success': False, 'stage': 'restore',
                'detail': restore}
    if restore['file_count'] < expected_min_files:
        return {'success': False, 'stage': 'compare',
                'detail': f'file_count {restore["file_count"]}'
                          f'<{expected_min_files}'}
    return {'success': True, 'backup': backup,
            'verify': verify, 'restore': restore}


def run_dr_drill(sources: Any,
                  *, work_dir: Optional[str] = None,
                  expected_min_files: int = 1,
                  smoke_check_callable: Optional[Any] = None) -> Dict[str, Any]:
    """支持旧版 tar 演练和单文件数据库演练两种模式."""
    cleanup = False
    if not work_dir:
        work_dir = tempfile.mkdtemp(prefix='cro_dr_')
        cleanup = True
    try:
        if isinstance(sources, (list, tuple, set)):
            return _run_tar_drill(list(sources), work_dir, expected_min_files)

        backup = backup_database(str(sources), work_dir)
        if not backup.get('ok'):
            return {'success': False, 'stage': 'backup', 'detail': backup}

        verify = verify_backup(backup['backup_path'],
                               manifest_path=backup['manifest_path'])
        if not verify.get('ok'):
            return {'success': False, 'stage': 'verify',
                    'detail': verify, 'backup': backup}

        dest = os.path.join(work_dir, 'restored_db.sqlite')
        restore = restore_to_temp(backup['backup_path'], dest)
        if not restore.get('ok'):
            return {'success': False, 'stage': 'restore',
                    'detail': restore}

        smoke = {'status': 'skipped'}
        if smoke_check_callable is not None:
            try:
                smoke_result = smoke_check_callable(dest)
                if isinstance(smoke_result, dict):
                    smoke = smoke_result
                else:
                    smoke = {'status': 'ok' if smoke_result else 'failed'}
                if smoke.get('status') not in ('ok', 'passed', 'skipped'):
                    return {'success': False, 'stage': 'smoke',
                            'detail': smoke, 'backup': backup,
                            'verify': verify, 'restore': restore}
            except Exception as e:
                return {'success': False, 'stage': 'smoke',
                        'detail': {'status': 'failed', 'error': repr(e)},
                        'backup': backup, 'verify': verify,
                        'restore': restore}

        return {'success': True, 'backup': backup,
                'verify': verify, 'restore': restore, 'smoke': smoke}
    finally:
        if cleanup:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
