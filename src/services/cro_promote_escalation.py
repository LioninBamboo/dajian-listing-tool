"""Promote 空转升级 — at-cap 冷却与升级候选.

背景: promote 执行器对 bid 已达利润感知上限的 SKU 会终态 skip
(reason: 'already at cap (...)'). 诊断器第二天仍会推荐 promote,
每日 30 个入队名额被这些无法再加价的 SKU 反复占用 (2026-07-03
日报: skipped 779 vs done 9), 真正可加价的 SKU 反而排不进队列.

本模块从 promote 执行器已落盘的 logs/cro_promote_*.json 中提取
最近 N 天的 at-cap skip 记录:

1. `at_cap_cooldown_skus()` — 冷却名单, daily runner 在 promote
   入队前排除这些 SKU (默认 7 天), 把名额让给可执行的工作.
2. `escalation_candidates()` — 升级候选清单, 写进 daily report,
   作为运营侧下一级手段 (标题重写 / relist 生命周期) 的输入.

只读日志, 不改队列、不调 eBay; 日志缺失/损坏时静默降级为空集,
绝不让 daily run 失败.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"
AT_CAP_PREFIX = "already at cap"
_LOG_NAME_RE = re.compile(r"cro_promote_(\d{8})_\d{6}\.json$")
# 防御: 目录里日志再多也只回看最近这么多个文件
MAX_LOG_FILES = 200


def _recent_promote_logs(days: int, logs_dir: Optional[Path] = None) -> List[Path]:
    d = Path(logs_dir) if logs_dir else DEFAULT_LOGS_DIR
    if not d.is_dir():
        return []
    cutoff = datetime.now().date() - timedelta(days=days)
    out: List[tuple[str, Path]] = []
    for p in d.glob("cro_promote_*.json"):
        m = _LOG_NAME_RE.search(p.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        out.append((m.group(1) + p.name, p))
    out.sort(reverse=True)
    return [p for _, p in out[:MAX_LOG_FILES]]


def load_at_cap_hits(days: int = 7,
                     logs_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """返回 {sku: {hits, last_reason, last_log}} — 最近 N 天 at-cap skip 汇总."""
    hits: Dict[str, Dict[str, Any]] = {}
    for path in _recent_promote_logs(days, logs_dir):
        try:
            rep = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = rep.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("status") != "skipped":
                continue
            reason = str(row.get("reason") or "")
            if not reason.startswith(AT_CAP_PREFIX):
                continue
            sku = str(row.get("sku") or "").strip()
            if not sku:
                continue
            entry = hits.setdefault(sku, {"hits": 0, "last_reason": "", "last_log": ""})
            entry["hits"] += 1
            # 文件按新→旧遍历, 首次命中即最新一次
            if not entry["last_reason"]:
                entry["last_reason"] = reason
                entry["last_log"] = path.name
    return hits


def at_cap_cooldown_skus(days: int = 7,
                         logs_dir: Optional[Path] = None) -> set[str]:
    """冷却名单: 最近 N 天内被 promote 执行器判定 at-cap 的 SKU."""
    return set(load_at_cap_hits(days=days, logs_dir=logs_dir))


def escalation_candidates(days: int = 7,
                          logs_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """升级候选 (hits 降序): 广告杠杆已打满, 需要非广告手段的 SKU 清单."""
    hits = load_at_cap_hits(days=days, logs_dir=logs_dir)
    out = [{"sku": sku, **info} for sku, info in hits.items()]
    out.sort(key=lambda r: (-int(r.get("hits") or 0), r["sku"]))
    return out
