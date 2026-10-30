"""Bid A/B 实验框架 (P12, 2026-05).

让 bid 决策从 "规则驱动" 渐进到 "数据驱动":
  1. 创建实验: 把候选 SKU 按哈希分流到 control / variant
     control = 当前 bid; variant = 测试 bid (e.g. control + 2pct)
  2. 实验持续 N 天 (默认 14 天)
  3. 结束时对比两组 CTR / 销量, 用简单 z-test 判断是否显著
  4. 显著 lift → 写建议: 全量调整到 variant bid

只读模式 — 不直接调 bid. 决策由 batch_smart_bid 或人工二次确认.

数据存储: SQLite 表 `bid_experiments` + `bid_experiment_assignments`
   experiment_id  | name | created_at | end_at | status | control_bid | variant_bid | tier_filter
   experiment_id  | sku  | arm

API:
  - create_experiment(name, control_bid, variant_bid, sku_pool, duration_days=14, tier=None)
  - assign_arm(experiment_id, sku) -> 'control' | 'variant'  (确定性哈希)
  - record_result(experiment_id, metrics_by_sku) — 由调用方注入 perf 数据
  - evaluate(experiment_id) -> {control_avg, variant_avg, lift, p_value, recommendation}
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)
DEFAULT_DB = Path(__file__).resolve().parents[2] / 'ebay_collection.db'

_SCHEMA_EXP = """
CREATE TABLE IF NOT EXISTS bid_experiments (
    experiment_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    control_bid REAL NOT NULL,
    variant_bid REAL NOT NULL,
    tier_filter TEXT,
    created_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    result_json TEXT
)
"""
_SCHEMA_ASSIGN = """
CREATE TABLE IF NOT EXISTS bid_experiment_assignments (
    experiment_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    arm TEXT NOT NULL,
    PRIMARY KEY (experiment_id, sku)
)
"""


def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.execute(_SCHEMA_EXP)
    c.execute(_SCHEMA_ASSIGN)
    c.commit()
    return c


def _hash_arm(experiment_id: str, sku: str) -> str:
    """50/50 确定性分流."""
    h = hashlib.sha256(f"{experiment_id}::{sku}".encode()).hexdigest()
    return 'variant' if int(h[:8], 16) % 2 == 0 else 'control'


def _balanced_assignments(experiment_id: str, sku_pool: Iterable[str]) -> Dict[str, str]:
    """Deterministically split a pool into both arms when possible."""
    unique_skus = sorted({str(sku).strip() for sku in sku_pool if str(sku).strip()})
    if not unique_skus:
        return {}
    ranked = sorted(
        unique_skus,
        key=lambda sku: hashlib.sha256(f"{experiment_id}::{sku}".encode()).hexdigest(),
    )
    variant_cutoff = max(1, len(ranked) // 2)
    variant_skus = set(ranked[:variant_cutoff])
    if len(ranked) == 1:
        only_sku = ranked[0]
        return {only_sku: _hash_arm(experiment_id, only_sku)}
    return {
        sku: ('variant' if sku in variant_skus else 'control')
        for sku in unique_skus
    }


def create_experiment(name: str, control_bid: float, variant_bid: float,
                       sku_pool: Iterable[str], *,
                       duration_days: int = 14, tier_filter: Optional[str] = None,
                       db_path: Path = DEFAULT_DB) -> str:
    """创建实验. 返回 experiment_id."""
    if control_bid == variant_bid:
        raise ValueError("control_bid 与 variant_bid 必须不同")
    eid = f"exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(name.encode()).hexdigest()[:6]}"
    now = datetime.now()
    end = now + timedelta(days=duration_days)
    assignments = _balanced_assignments(eid, sku_pool)
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO bid_experiments (experiment_id, name, control_bid, variant_bid, "
            "tier_filter, created_at, end_at, status) VALUES (?,?,?,?,?,?,?, 'running')",
            (eid, name, float(control_bid), float(variant_bid), tier_filter,
             now.isoformat(), end.isoformat()),
        )
        for sku, arm in assignments.items():
            c.execute(
                "INSERT OR IGNORE INTO bid_experiment_assignments (experiment_id, sku, arm) "
                "VALUES (?,?,?)",
                (eid, sku, arm),
            )
        c.commit()
        logger.info(f"🧪 实验 {eid} 已创建 ({name}), 共 {len(assignments)} SKU, 持续 {duration_days} 天")
        return eid
    finally:
        c.close()


def assign_arm(experiment_id: str, sku: str, *, db_path: Path = DEFAULT_DB) -> Optional[str]:
    c = _conn(db_path)
    try:
        row = c.execute(
            "SELECT arm FROM bid_experiment_assignments WHERE experiment_id=? AND sku=?",
            (experiment_id, sku),
        ).fetchone()
        return row['arm'] if row else None
    finally:
        c.close()


def get_assignments(experiment_id: str, *, db_path: Path = DEFAULT_DB) -> Dict[str, str]:
    c = _conn(db_path)
    try:
        rows = c.execute(
            "SELECT sku, arm FROM bid_experiment_assignments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchall()
        return {r['sku']: r['arm'] for r in rows}
    finally:
        c.close()


def get_experiment(experiment_id: str, *, db_path: Path = DEFAULT_DB) -> Optional[Dict[str, Any]]:
    c = _conn(db_path)
    try:
        row = c.execute(
            "SELECT * FROM bid_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def list_experiments(status: Optional[str] = None, *,
                      db_path: Path = DEFAULT_DB) -> List[Dict[str, Any]]:
    c = _conn(db_path)
    try:
        if status:
            rows = c.execute(
                "SELECT * FROM bid_experiments WHERE status=? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM bid_experiments ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


@dataclass
class EvaluationResult:
    experiment_id: str
    control_n: int
    variant_n: int
    control_avg_ctr: float
    variant_avg_ctr: float
    control_avg_sold: float
    variant_avg_sold: float
    lift_ctr_pct: float       # (variant-control)/control × 100
    lift_sold_pct: float
    z_score_ctr: float        # 简化双比例 z
    is_significant: bool      # |z| > 1.96 (95%)
    recommendation: str       # 'adopt_variant' | 'keep_control' | 'inconclusive'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate(experiment_id: str, metrics_by_sku: Dict[str, Dict[str, float]],
              *, db_path: Path = DEFAULT_DB,
              min_total_impressions: int = 1000) -> EvaluationResult:
    """评估实验.

    metrics_by_sku: {sku: {'impressions': N, 'clicks': N, 'sold_qty': N}}
    """
    assignments = get_assignments(experiment_id, db_path=db_path)
    if not assignments:
        raise ValueError(f"实验 {experiment_id} 不存在或无 SKU")

    groups = {'control': {'imp': 0, 'clk': 0, 'sold': 0, 'n': 0},
              'variant': {'imp': 0, 'clk': 0, 'sold': 0, 'n': 0}}
    for sku, arm in assignments.items():
        m = metrics_by_sku.get(sku, {})
        groups[arm]['imp'] += int(m.get('impressions', 0))
        groups[arm]['clk'] += int(m.get('clicks', 0))
        groups[arm]['sold'] += int(m.get('sold_qty', 0))
        groups[arm]['n'] += 1

    def _safe_div(a, b):
        return a / b if b else 0.0

    c_ctr = _safe_div(groups['control']['clk'], groups['control']['imp'])
    v_ctr = _safe_div(groups['variant']['clk'], groups['variant']['imp'])
    c_sold = _safe_div(groups['control']['sold'], max(groups['control']['n'], 1))
    v_sold = _safe_div(groups['variant']['sold'], max(groups['variant']['n'], 1))

    # 双比例 z-test (CTR)
    n1 = groups['control']['imp']; x1 = groups['control']['clk']
    n2 = groups['variant']['imp']; x2 = groups['variant']['clk']
    if n1 > 0 and n2 > 0:
        p_pool = (x1 + x2) / (n1 + n2)
        se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2)) or 1e-9
        z = (v_ctr - c_ctr) / se
    else:
        z = 0.0

    total_imp = n1 + n2
    is_sig = abs(z) > 1.96 and total_imp >= min_total_impressions

    if is_sig and v_ctr > c_ctr:
        rec = 'adopt_variant'
    elif is_sig and v_ctr < c_ctr:
        rec = 'keep_control'
    else:
        rec = 'inconclusive'

    res = EvaluationResult(
        experiment_id=experiment_id,
        control_n=groups['control']['n'], variant_n=groups['variant']['n'],
        control_avg_ctr=round(c_ctr, 5), variant_avg_ctr=round(v_ctr, 5),
        control_avg_sold=round(c_sold, 3), variant_avg_sold=round(v_sold, 3),
        lift_ctr_pct=round((v_ctr - c_ctr) / c_ctr * 100, 2) if c_ctr else 0.0,
        lift_sold_pct=round((v_sold - c_sold) / c_sold * 100, 2) if c_sold else 0.0,
        z_score_ctr=round(z, 3), is_significant=is_sig, recommendation=rec,
    )
    # 落库
    c = _conn(db_path)
    try:
        c.execute(
            "UPDATE bid_experiments SET result_json=?, status=? WHERE experiment_id=?",
            (json.dumps(res.to_dict()), 'completed' if datetime.now().isoformat() >=
             (get_experiment(experiment_id) or {}).get('end_at', '') else 'running',
             experiment_id),
        )
        c.commit()
    finally:
        c.close()
    return res
