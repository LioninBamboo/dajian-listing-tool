"""F19 — Market Intelligence opportunity aggregation utilities.

把 auto_discover_opportunities() 返回的扁平列表按"产品大类"聚合，便于运营按品类决策。
品类来源：opportunity 自身没有结构化 category 字段，所以从 title 中启发式提取。

F22 — 品类规则可配置：从 mi_categories.json 加载，按 mtime 自动重载。
若 JSON 不可用则回退到内置默认表。运营可在不重启 FastAPI 的情况下改规则。
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# F22 — 内置默认（YAML 不可用时回退）
_DEFAULT_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    ("sectional sofa", ("sectional sofa", "modular sofa")),
    ("sofa",          ("sofa", "couch", "loveseat")),
    ("bed",           ("bed", "platform", "headboard", "bedframe")),
    ("table",         ("dining table", "coffee table", "table", "desk", "island")),
    ("chair",         ("recliner", "armchair", "accent chair", "chair", "stool")),
    ("storage",       ("dresser", "wardrobe", "cabinet", "bookcase", "shelf")),
    ("outdoor",       ("patio", "outdoor", "garden", "umbrella")),
    ("mattress",      ("mattress", "topper")),
    ("lighting",      ("lamp", "chandelier", "pendant")),
]
_DEFAULT_FALLBACK = "other"

# 默认配置路径：项目根 / mi_categories.json
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "mi_categories.json"

# mtime 缓存：(mtime, patterns, fallback)
_RULES_CACHE: dict = {"path": None, "mtime": None, "patterns": None, "fallback": None}


def _load_category_rules(config_path: Optional[Path] = None) -> Tuple[List[Tuple[str, Tuple[str, ...]]], str]:
    """按 mtime 缓存加载 JSON 品类规则；失败返回内置默认。"""
    path = config_path or _DEFAULT_CONFIG_PATH
    try:
        if not path.exists():
            return _DEFAULT_PATTERNS, _DEFAULT_FALLBACK
        mtime = path.stat().st_mtime
        if (_RULES_CACHE["path"] == path
                and _RULES_CACHE["mtime"] == mtime
                and _RULES_CACHE["patterns"] is not None):
            return _RULES_CACHE["patterns"], _RULES_CACHE["fallback"]
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        raw_cats = data.get("categories") or []
        patterns: List[Tuple[str, Tuple[str, ...]]] = []
        for entry in raw_cats:
            if not isinstance(entry, dict):
                continue
            label = (entry.get("label") or "").strip().lower()
            kws = entry.get("keywords") or []
            if not label or not isinstance(kws, list):
                continue
            kw_tuple = tuple(str(k).strip().lower() for k in kws if str(k).strip())
            if kw_tuple:
                patterns.append((label, kw_tuple))
        if not patterns:
            return _DEFAULT_PATTERNS, _DEFAULT_FALLBACK
        fallback = (data.get("fallback") or _DEFAULT_FALLBACK).strip().lower() or _DEFAULT_FALLBACK
        _RULES_CACHE.update({"path": path, "mtime": mtime,
                             "patterns": patterns, "fallback": fallback})
        return patterns, fallback
    except Exception:
        # 配置损坏时不能让业务挂掉
        return _DEFAULT_PATTERNS, _DEFAULT_FALLBACK


def derive_product_category(title: Optional[str], config_path: Optional[Path] = None) -> str:
    """从产品标题派生大类标签。
    返回小写英文标签；无法识别返回 fallback（默认 "other"）。
    F22 — 规则从 mi_categories.json 读取（带 mtime 缓存）。
    """
    patterns, fallback = _load_category_rules(config_path)
    if not title:
        return fallback
    t = title.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    for label, keywords in patterns:
        for kw in keywords:
            # 整词匹配
            if re.search(rf"\b{re.escape(kw)}\b", t):
                return label
    return fallback


def aggregate_by_category(opportunities: Iterable[dict]) -> List[Dict]:
    """按品类聚合机会列表。

    返回按 count 倒序的字典列表：
    [{
        "category": "sofa",
        "count": 5,
        "avg_score": 72.0,
        "median_score": 70,
        "median_price": 350.0,
        "avg_margin_rate": 28.5,
        "total_potential_profit": 1200.0,
        "real_str_count": 2,            # 含真实 STR 的 SKU 数
        "top_sku": "DJ-XYZ",            # 该品类下分数最高的 SKU
        "top_score": 88,
    }, ...]
    """
    buckets: Dict[str, List[dict]] = {}
    for opp in opportunities or []:
        if not isinstance(opp, dict):
            continue
        cat = derive_product_category(opp.get("title"))
        buckets.setdefault(cat, []).append(opp)

    out: List[Dict] = []
    for cat, items in buckets.items():
        scores = [_safe_num(o.get("opportunity_score")) for o in items]
        scores = [s for s in scores if s is not None]
        prices = [_safe_num(o.get("suggested_price")) for o in items]
        prices = [p for p in prices if p is not None and p > 0]
        margins = [_safe_num(o.get("margin_rate")) for o in items]
        margins = [m for m in margins if m is not None]
        profits = [_safe_num(o.get("potential_profit")) for o in items]
        profits = [p for p in profits if p is not None]
        real_str_count = sum(
            1 for o in items if _safe_num(o.get("seller_str_pct")) is not None
        )
        # 该品类内分数最高的 SKU
        top_item = max(items, key=lambda o: _safe_num(o.get("opportunity_score")) or 0)

        out.append({
            "category": cat,
            "count": len(items),
            "avg_score": round(statistics.mean(scores), 1) if scores else 0.0,
            "median_score": int(statistics.median(scores)) if scores else 0,
            "median_price": round(statistics.median(prices), 2) if prices else 0.0,
            "avg_margin_rate": round(statistics.mean(margins), 1) if margins else 0.0,
            "total_potential_profit": round(sum(profits), 2),
            "real_str_count": real_str_count,
            "top_sku": top_item.get("sku", ""),
            "top_score": int(_safe_num(top_item.get("opportunity_score")) or 0),
        })

    out.sort(key=lambda r: r["count"], reverse=True)
    return out


def _safe_num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
