"""S51 — traffic-light dashboard 数据采集层.

Streamlit 页 (`src/web/pages/cro_status.py`) 只调用本模块, 不直接读 DB/文件,
方便测试与替换实现。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.services.cro_action_queue import DEFAULT_QUEUE


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def collect_approvals(queue_path: Optional[Path] = None) -> Dict[str, Any]:
    rows = _read_jsonl(queue_path or Path(DEFAULT_QUEUE))
    pending = [r for r in rows if r.get('status') == 'pending']
    return {
        'total': len(pending),
        'by_action': _count_by(pending, 'action'),
    }


def collect_alerts(alerts_path: Path) -> Dict[str, Any]:
    rows = _read_jsonl(alerts_path)
    high = [r for r in rows if r.get('priority') == 'high']
    return {
        'high_priority_count': len(high),
        'total': len(rows),
    }


def collect_returns(returns_path: Path) -> Dict[str, Any]:
    rows = _read_jsonl(returns_path)
    high = [r for r in rows if (r.get('return_rate') or 0) >= 0.15]
    return {
        'high_return_count': len(high),
        'total': len(rows),
    }


def collect_inventory(alerts_path: Path) -> Dict[str, Any]:
    rows = _read_jsonl(alerts_path)
    return {
        'throttle_skus': [r.get('sku') for r in rows if r.get('sku')],
    }


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        v = r.get(key) or 'unknown'
        out[v] = out.get(v, 0) + 1
    return out
