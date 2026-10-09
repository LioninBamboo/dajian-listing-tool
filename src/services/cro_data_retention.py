"""S136 — Data Retention 历史数据归档/清理.

按 jsonl 行的 'ts' 字段判断保留窗口, 过期行写入归档目录后从源文件移除.
不会删除无 ts 字段或 ts 不可解析的行 (安全 fallback).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_ts(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).replace('Z', ''))
    except Exception:
        return None


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    out.append({'_corrupt_line': line})
    except Exception:
        logger.warning('read jsonl failed: %s', path, exc_info=True)
        return []
    return out


def _write_jsonl(path: str, rows: List[Dict[str, Any]]) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for r in rows:
                if '_corrupt_line' in r:
                    f.write(r['_corrupt_line'] + '\n')
                else:
                    f.write(json.dumps(r, ensure_ascii=False,
                                        default=str) + '\n')
        return True
    except Exception:
        logger.warning('write jsonl failed', exc_info=True)
        return False


def _append_archive(path: str, rows: List[Dict[str, Any]],
                     gzip_archive: bool) -> bool:
    if not rows:
        return True
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        if gzip_archive:
            with gzip.open(path, 'at', encoding='utf-8') as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False,
                                        default=str) + '\n')
        else:
            with open(path, 'a', encoding='utf-8') as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False,
                                        default=str) + '\n')
        return True
    except Exception:
        logger.warning('archive write failed', exc_info=True)
        return False


def apply_retention(
    source_path: str,
    archive_path: str,
    *,
    retention_days: int,
    now: Optional[datetime] = None,
    ts_field: str = 'ts',
    gzip_archive: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if retention_days < 0:
        raise ValueError('retention_days must be >=0')
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=retention_days)

    rows = _read_jsonl(source_path)
    keep: List[Dict[str, Any]] = []
    archive: List[Dict[str, Any]] = []
    unparseable_kept = 0

    for r in rows:
        if '_corrupt_line' in r:
            keep.append(r)
            unparseable_kept += 1
            continue
        ts = _parse_ts(r.get(ts_field))
        if ts is None:
            keep.append(r)
            unparseable_kept += 1
            continue
        if ts < cutoff:
            archive.append(r)
        else:
            keep.append(r)

    summary = {
        'source': source_path,
        'archive': archive_path,
        'cutoff': cutoff.isoformat(),
        'total_in': len(rows),
        'kept': len(keep),
        'archived': len(archive),
        'unparseable_kept': unparseable_kept,
        'dry_run': dry_run,
    }

    if dry_run or not archive:
        return summary

    if not _append_archive(archive_path, archive, gzip_archive):
        summary['error'] = 'archive_write_failed'
        return summary
    if not _write_jsonl(source_path, keep):
        summary['error'] = 'source_rewrite_failed'
    return summary
