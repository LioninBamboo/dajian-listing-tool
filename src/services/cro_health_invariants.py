"""S127 — Health Invariants 不变量检查.

run_all 聚合多种检查, 写违例 jsonl, 返回 {violations, failed_count, by_check}.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional

from src.services.cro_audit_trail import verify_chain

logger = logging.getLogger(__name__)


def check_no_negative_cohort(rows: Iterable[Dict[str, Any]]
                                ) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        c = r.get('cohort')
        if c is None:
            continue
        try:
            if float(c) < 0:
                out.append({'check': 'no_negative_cohort',
                             'sku': r.get('sku'), 'cohort': c})
        except Exception:
            out.append({'check': 'no_negative_cohort',
                         'sku': r.get('sku'),
                         'detail': 'invalid_cohort_value', 'cohort': c})
    return out


def check_audit_chain(records: List[Dict[str, Any]],
                        secret: str) -> List[Dict[str, Any]]:
    if not records:
        return []
    res = verify_chain(records, secret)
    if res.get('valid'):
        return []
    return [{'check': 'audit_chain',
              'first_invalid_index': res.get('first_invalid_index'),
              'reason': res.get('reason')}]


def check_every_done_has_trace(rows: Iterable[Dict[str, Any]]
                                  ) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        if r.get('status') != 'done':
            continue
        if not r.get('reasoning_trace'):
            out.append({'check': 'done_has_trace',
                         'sku': r.get('sku'),
                         'action_type': r.get('action_type')})
    return out


def _append_violation(path: Optional[str], v: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                **v,
                'ts': datetime.now(UTC).replace(tzinfo=None).isoformat(),
            },
                                ensure_ascii=False, default=str) + '\n')
    except Exception:
        logger.warning('append violation failed', exc_info=True)


def run_all(
    *,
    queue_rows: Optional[List[Dict[str, Any]]] = None,
    audit_records: Optional[List[Dict[str, Any]]] = None,
    secret: Optional[str] = None,
    done_rows: Optional[List[Dict[str, Any]]] = None,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    by_check: Dict[str, int] = {}
    violations: List[Dict[str, Any]] = []
    if queue_rows is not None:
        v = check_no_negative_cohort(queue_rows)
        violations.extend(v)
        by_check['no_negative_cohort'] = len(v)
    if audit_records is not None and secret:
        v = check_audit_chain(audit_records, secret)
        violations.extend(v)
        by_check['audit_chain'] = len(v)
    if done_rows is not None:
        v = check_every_done_has_trace(done_rows)
        violations.extend(v)
        by_check['done_has_trace'] = len(v)
    for v in violations:
        _append_violation(log_path, v)
    return {
        'violations': violations,
        'failed_count': len(violations),
        'by_check': by_check,
    }
