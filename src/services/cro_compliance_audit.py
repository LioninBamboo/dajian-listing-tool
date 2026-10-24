"""S139 — Compliance Audit (GDPR/CCPA 合规审计日志).

记录 PII 访问 / 数据导出 / 删除请求 → jsonl;
generate_audit_report 按时间窗口聚合 actor / action / resource_type 统计;
search_user_activity 帮助 GDPR Subject Access Request.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    'pii_access', 'data_export', 'data_delete',
    'consent_grant', 'consent_revoke',
    'data_modify', 'data_share',
}

VALID_LEGAL_BASIS = {
    'consent', 'contract', 'legal_obligation',
    'vital_interests', 'public_task', 'legitimate_interests',
}
LAWFUL_BASES = VALID_LEGAL_BASIS


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def verify_lawful_basis(lawful_basis: str) -> str:
    lawful_basis = str(lawful_basis or '').strip()
    if lawful_basis not in LAWFUL_BASES:
        raise ValueError(f'unknown lawful_basis: {lawful_basis}')
    return lawful_basis


def _append_record(log_path: str, record: Dict[str, Any]) -> Dict[str, Any]:
    try:
        os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        return {'ok': True, 'record': record}
    except Exception as e:
        logger.warning('compliance log write failed', exc_info=True)
        return {'ok': False, 'error': repr(e)}


def log_access(user: str, subject_id: str, action: str,
               lawful_basis: str, log_path: str) -> Dict[str, Any]:
    if not user:
        raise ValueError('user required')
    if not subject_id:
        raise ValueError('subject_id required')
    if action not in VALID_ACTIONS:
        raise ValueError(f'unknown action: {action}')
    record = {
        'ts': _utcnow_iso(),
        'user': user,
        'actor': user,
        'subject_id': subject_id,
        'user_id': subject_id,
        'action': action,
        'lawful_basis': verify_lawful_basis(lawful_basis),
        'legal_basis': lawful_basis,
        'resource_type': 'subject',
    }
    return _append_record(log_path, record)


def log_compliance_event(
    *,
    log_path: str,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
    legal_basis: Optional[str] = None,
    purpose: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not actor:
        raise ValueError('actor required')
    if action not in VALID_ACTIONS:
        raise ValueError(f'unknown action: {action}')
    if legal_basis and legal_basis not in VALID_LEGAL_BASIS:
        raise ValueError(f'unknown legal_basis: {legal_basis}')
    rec = {
        'ts': _utcnow_iso(),
        'actor': actor,
        'action': action,
        'resource_type': resource_type,
        'resource_id': resource_id,
        'user_id': user_id,
        'legal_basis': legal_basis,
        'purpose': purpose,
        'extra': extra or {},
    }
    return _append_record(log_path, rec)


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
                    continue
    except Exception:
        return []
    return out


def _within_window(rec_ts: str, since: Optional[datetime],
                    until: Optional[datetime]) -> bool:
    try:
        d = datetime.fromisoformat(str(rec_ts).replace('Z', ''))
    except Exception:
        return False
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def _sort_key(record: Dict[str, Any]) -> str:
    return str(record.get('ts') or '')


def _redact_value(value: Any, redact_keys: Set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: ('***' if key in redact_keys else _redact_value(val, redact_keys))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, redact_keys) for item in value]
    return value


def generate_audit_report(
    log_path: str,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    rows = _read_jsonl(log_path)
    in_window = [r for r in rows
                  if _within_window(r.get('ts', ''), since, until)]
    by_actor: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    by_resource_type: Dict[str, int] = {}
    by_legal_basis: Dict[str, int] = {}
    missing_legal_basis = 0
    for r in in_window:
        a = r.get('actor') or 'unknown'
        by_actor[a] = by_actor.get(a, 0) + 1
        act = r.get('action') or 'unknown'
        by_action[act] = by_action.get(act, 0) + 1
        rt = r.get('resource_type') or 'unknown'
        by_resource_type[rt] = by_resource_type.get(rt, 0) + 1
        lb = r.get('legal_basis')
        if lb:
            by_legal_basis[lb] = by_legal_basis.get(lb, 0) + 1
        elif r.get('action') in ('pii_access', 'data_export', 'data_share',
                                 'data_delete'):
            missing_legal_basis += 1
    return {
        'total_in_window': len(in_window),
        'by_actor': by_actor,
        'by_action': by_action,
        'by_resource_type': by_resource_type,
        'by_legal_basis': by_legal_basis,
        'missing_legal_basis': missing_legal_basis,
    }


def query_subject_history(subject_id: str, log_path: str) -> List[Dict[str, Any]]:
    if not subject_id:
        return []
    rows = []
    for row in _read_jsonl(log_path):
        if row.get('subject_id') == subject_id or row.get('user_id') == subject_id:
            rows.append(row)
    return sorted(rows, key=_sort_key)


def generate_dsar_export(subject_id: str, log_path: str,
                         redact_keys: Optional[Set[str]] = None) -> Dict[str, Any]:
    redact_keys = set(redact_keys or set())
    events = [_redact_value(row, redact_keys)
              for row in query_subject_history(subject_id, log_path)]
    return {
        'subject_id': subject_id,
        'generated_at': _utcnow_iso(),
        'events': events,
        'event_count': len(events),
    }


def search_user_activity(log_path: str, user_id: str
                            ) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    return query_subject_history(user_id, log_path)
