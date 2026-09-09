"""S69 — CRO 审批 RBAC.

角色: viewer / operator / admin.
能力矩阵 (action → 最低要求角色):
  view              -> viewer
  approve_threshold -> operator
  approve_promote   -> operator
  approve_pricedrop -> operator
  approve_rollback  -> admin
  approve_delist    -> admin     # delist 永远人工 + admin
  bulk_approve      -> admin
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

ROLE_RANK = {'viewer': 0, 'operator': 1, 'admin': 2}

CAPABILITY_MIN_ROLE: Dict[str, str] = {
    'view': 'viewer',
    'approve_threshold': 'operator',
    'approve_promote': 'operator',
    'approve_pricedrop': 'operator',
    'approve_rollback': 'admin',
    'approve_delist': 'admin',
    'bulk_approve': 'admin',
}


def can(role: Optional[str], capability: str) -> bool:
    if not role or role not in ROLE_RANK:
        return False
    needed = CAPABILITY_MIN_ROLE.get(capability)
    if needed is None:
        return False
    return ROLE_RANK[role] >= ROLE_RANK[needed]


def require(role: Optional[str], capability: str) -> None:
    if not can(role, capability):
        raise PermissionError(
            f'role={role!r} 无法执行 {capability!r} '
            f'(需 {CAPABILITY_MIN_ROLE.get(capability, "?")} 及以上)')


def filter_actionable(items: Iterable[Dict[str, Any]],
                      role: Optional[str],
                      kind_to_capability: Optional[Dict[str, str]] = None,
                      ) -> List[Dict[str, Any]]:
    """给一批 approval items, 返回当前角色能操作的子集."""
    mapping = kind_to_capability or {
        'threshold': 'approve_threshold',
        'promote': 'approve_promote',
        'price_drop': 'approve_pricedrop',
        'rollback': 'approve_rollback',
        'delist': 'approve_delist',
    }
    out = []
    for item in items:
        kind = item.get('kind') or item.get('action')
        cap = mapping.get(kind, 'view')
        if can(role, cap):
            out.append(item)
    return out


def annotate_permissions(items: Iterable[Dict[str, Any]],
                         role: Optional[str],
                         kind_to_capability: Optional[Dict[str, str]] = None,
                         ) -> List[Dict[str, Any]]:
    """给每条 item 加 _can_action 字段, 不过滤."""
    mapping = kind_to_capability or {
        'threshold': 'approve_threshold',
        'promote': 'approve_promote',
        'price_drop': 'approve_pricedrop',
        'rollback': 'approve_rollback',
        'delist': 'approve_delist',
    }
    out = []
    for item in items:
        kind = item.get('kind') or item.get('action')
        cap = mapping.get(kind, 'view')
        out.append({**item, '_can_action': can(role, cap),
                    '_required_capability': cap})
    return out


def role_resolver_factory(role_lookup: Callable[[str], Optional[str]],
                          default: str = 'viewer',
                          ) -> Callable[[str], str]:
    """生产中可注入: 给 user_id → role; 失败/未知 → default."""
    def _resolve(user_id: str) -> str:
        try:
            r = role_lookup(user_id)
            if r in ROLE_RANK:
                return r
        except Exception:
            pass
        return default
    return _resolve
