"""S40 \u2014 \u707e\u96be\u6062\u590d\u6f14\u7ec3 (chaos drill).

\u8fd0\u884c\u4e09\u7c7b\u6ce8\u5165:
  1. fake_ebay_500: ebay \u8c03\u7528\u8fd4\u56de 500
  2. fake_db_locked: sqlite \u6570\u636e\u5e93\u9501\u5b9a 1s
  3. fake_corrupt_queue: cro_action_queue.jsonl \u5199\u5165\u635f\u574f\u884c

\u7136\u540e\u9a8c\u8bc1\u5b88\u95e8\u662f\u5426\u80fd\u5076\u53d1\u73b0+\u9694\u79bb. \u8fd0\u884c\u51e0\u4e2a\u80fd\u53cd\u9988\u7ed3\u679c\u7684\u8d77\u70b9
(safe_invoke_with_retry, validate_queue_lines, db_with_busy_timeout) \u4ee5\u907f\u514d\u4e0e
\u73b0\u6709\u4ee3\u7801\u8026\u5408. \u8c03\u7528\u8005\u53ef\u8fdb\u4e00\u6b65\u5305\u88c5.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def safe_invoke_with_retry(fn: Callable[[], Any], retries: int = 3,
                           backoff_seconds: float = 0.0,
                           ) -> Dict[str, Any]:
    """\u5305\u88c5\u4efb\u610f\u53ef\u80fd\u629b\u5f02\u5e38\u7684\u8c03\u7528. \u6210\u529f\u8fd4 ok=True;
    \u6240\u6709\u91cd\u8bd5\u8017\u5c3d \u2192 ok=False, last_error \u5b58\u5b57\u7b26\u4e32."""
    last_err: Optional[str] = None
    for i in range(retries):
        try:
            v = fn()
            return {'ok': True, 'value': v, 'attempts': i + 1}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if backoff_seconds:
                time.sleep(backoff_seconds * (i + 1))
    return {'ok': False, 'last_error': last_err, 'attempts': retries}


def validate_queue_lines(jsonl_path: Path) -> Dict[str, Any]:
    if not jsonl_path.exists():
        return {'total': 0, 'valid': 0, 'corrupt_lines': []}
    total = 0
    valid = 0
    corrupt: List[int] = []
    with jsonl_path.open('r', encoding='utf-8') as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and 'sku' in obj:
                    valid += 1
                else:
                    corrupt.append(ln)
            except json.JSONDecodeError:
                corrupt.append(ln)
    return {'total': total, 'valid': valid, 'corrupt_lines': corrupt}


def db_with_busy_timeout(db_path: Path, timeout_ms: int = 5000) -> sqlite3.Connection:
    """\u8fd4\u56de\u8bbe\u4e86 busy_timeout \u7684\u8fde\u63a5; \u9047\u5230\u9501\u5b9a\u65f6\u4f1a\u91cd\u8bd5\u3002"""
    conn = sqlite3.connect(str(db_path), timeout=timeout_ms / 1000.0)
    conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    return conn


def run_drill(jsonl_path: Optional[Path] = None,
              flaky_fn: Optional[Callable[[], Any]] = None,
              db_path: Optional[Path] = None,
              ) -> Dict[str, Any]:
    """\u4e00\u4e2a\u6f14\u7ec3 runner: \u6536\u96c6\u4e09\u9879\u68c0\u67e5\u7ed3\u679c."""
    rep: Dict[str, Any] = {'checks': {}}
    if jsonl_path is not None:
        rep['checks']['queue'] = validate_queue_lines(jsonl_path)
    if flaky_fn is not None:
        rep['checks']['retry'] = safe_invoke_with_retry(flaky_fn, retries=3)
    if db_path is not None:
        try:
            with db_with_busy_timeout(db_path) as c:
                c.execute("SELECT 1").fetchone()
            rep['checks']['db'] = {'ok': True}
        except Exception as e:
            rep['checks']['db'] = {'ok': False, 'error': str(e)}
    rep['passed'] = all(
        (v.get('ok') is True if 'ok' in v else v.get('valid', 0) == v.get('total', 0))
        for v in rep['checks'].values()
    )
    return rep
