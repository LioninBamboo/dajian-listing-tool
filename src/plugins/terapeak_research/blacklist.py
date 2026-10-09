"""F10 — 共享的 MI 屏蔽名单写入工具

读写格式必须与以下消费者保持一致：
- src/web/pages/market_intelligence.py 的 _load_blacklist_entries / _add_blacklist_entry
- src/plugins/terapeak_research/intelligence_service.py 的 _load_mi_blacklist

文件路径：reports/mi_blacklist.json
形态：{sku: {added_at, expires_at, reason}}（dict 形态，TTL 由 expires_at 控制）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Optional


def _resolve_blacklist_path(project_root: Optional[Path] = None) -> Path:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[3]
    return project_root / "reports" / "mi_blacklist.json"


def load_entries(project_root: Optional[Path] = None) -> Dict[str, Dict]:
    """读取 dict 形态条目（不剥离过期项；保留以便审计）。"""
    path = _resolve_blacklist_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, list):
        return {str(s): {"added_at": None, "expires_at": None, "reason": None}
                for s in data if s}
    if isinstance(data, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in data.items() if k}
    return {}


def save_entries(entries: Dict[str, Dict], project_root: Optional[Path] = None) -> None:
    path = _resolve_blacklist_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def auto_blacklist_audit_failures(
    skus: Iterable[str],
    reason: str = "audit_blocked",
    ttl_days: int = 30,
    project_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """把审计阻塞 SKU 写入屏蔽名单（30 天 TTL）。

    数据安全：
    - 已存在且仍生效的条目不覆盖（保留原有 added_at / 用户手动设置的 reason）。
    - 已存在但已过期的条目会被新一轮 audit 重新激活（TTL 重置）。
    - 永久条目（expires_at=None）不被改动。
    返回：本次实际新增/重新激活的 SKU 数量。
    """
    if now is None:
        now = datetime.now()
    entries = load_entries(project_root)
    changed = 0
    expires_at = (now + timedelta(days=ttl_days)).isoformat()
    for raw in skus:
        sku = str(raw).strip()
        if not sku:
            continue
        existing = entries.get(sku)
        if existing:
            exp = existing.get("expires_at")
            # 永久条目不改
            if exp is None:
                continue
            # 仍生效：跳过（不重置 TTL，避免审计反复执行无限延期）
            try:
                if datetime.fromisoformat(exp) >= now:
                    continue
            except Exception:
                pass
        entries[sku] = {
            "added_at": now.isoformat(),
            "expires_at": expires_at,
            "reason": reason,
        }
        changed += 1
    if changed:
        save_entries(entries, project_root)
    return changed
