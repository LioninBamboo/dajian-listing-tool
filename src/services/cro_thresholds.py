"""S16 — CRO 阈值按 categoryId 自学习.

不同品类 (家具 vs 工具 vs 服装) 的健康 CTR/CVR 差异巨大. 静态 1.5%/2% 会:
  - 对低 CTR 品类 (家具) 造成误报
  - 对高 CTR 品类 (爆款类目) 不够敏感

策略:
  - 拉过去 30 天 cro_snapshots 与 products 关联 → 按 categoryId 聚合
  - 每个品类取 impressions ≥ 100 的 SKU 的 CTR/CVR/STR **中位数** 作为健康水位
  - 样本 < 10 个品类回退到全局默认 (0.015 / 0.02 / 0.0005)
  - 写入 sqlite `cro_thresholds` 表, 由 cro_daily_runner 每天读取并传给 diagnose_batch
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'

DEFAULT_CTR = 0.015
DEFAULT_CVR = 0.02
DEFAULT_STR = 0.0005
MIN_SAMPLES = 10
LOOKBACK_DAYS = 30
MIN_IMPRESSIONS_PER_SAMPLE = 100

# S28 — 季节性指数衰减权重
WEIGHT_RECENT_DAYS = 7        # 近 7 天权重 = WEIGHT_RECENT_RATIO
WEIGHT_RECENT_RATIO = 0.5     # 近 7 天 + 8-30 天各占一半


def ensure_schema(db_path: Optional[Path] = None) -> None:
    db = Path(db_path) if db_path else DEFAULT_DB
    with sqlite3.connect(str(db)) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS cro_thresholds (
                category_id TEXT PRIMARY KEY,
                healthy_ctr REAL NOT NULL,
                healthy_cvr REAL NOT NULL,
                healthy_str REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                learned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cro_thresholds_pending (
                category_id TEXT PRIMARY KEY,
                healthy_ctr REAL NOT NULL,
                healthy_cvr REAL NOT NULL,
                healthy_str REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                learned_at TEXT NOT NULL
            );
        """)
        c.commit()


def _safe_median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _seasonal_weighted_median(recent: list, older: list,
                              recent_weight: float = WEIGHT_RECENT_RATIO):
    """\u4e24\u6bb5 median \u52a0\u6743\u5e73\u5747. \u4ec5\u4e00\u6bb5\u6709\u6837\u672c \u2192 \u9000\u56de\u8be5\u6bb5 median.
    \u4e24\u6bb5\u90fd\u7a7a \u2192 None."""
    rm = _safe_median(recent)
    om = _safe_median(older)
    if rm is None and om is None:
        return None
    if rm is None:
        return om
    if om is None:
        return rm
    return rm * recent_weight + om * (1 - recent_weight)


def _learn_payload(db_path: Optional[Path] = None,
                   lookback_days: int = LOOKBACK_DAYS,
                   min_impressions: int = MIN_IMPRESSIONS_PER_SAMPLE,
                   min_samples: int = MIN_SAMPLES,
                   weighted: bool = True,
                   ) -> Dict[str, Dict[str, Any]]:
    """\u53ea\u7b97, \u4e0d\u5199\u5e93. \u8fd4\u56de {category_id: {ctr,cvr,str,samples}}.
    weighted=True: S28 \u8fd1 7d \u4e0e 8-30d \u52a0\u6743\u5404\u534a, \u907f\u514d\u8282\u5047\u65e5\u8001\u6570\u636e\u62d6\u4f4e\u9608\u503c.
    """
    db = Path(db_path) if db_path else DEFAULT_DB
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    recent_cutoff = (date.today() - timedelta(days=WEIGHT_RECENT_DAYS)).isoformat()
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                """
                SELECT p.dajian_category AS cat, s.ctr, s.cvr, s.str_pct,
                       s.impressions, s.snapshot_date
                FROM cro_snapshots s
                JOIN products p ON p.sku = s.sku
                WHERE s.snapshot_date >= ?
                  AND s.impressions >= ?
                  AND p.dajian_category IS NOT NULL
                  AND p.dajian_category != ''
                """,
                (cutoff, min_impressions),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    by_cat: Dict[str, Dict[str, Dict[str, list]]] = {}
    for r in rows:
        cat = r['cat']
        bucket = 'recent' if r['snapshot_date'] >= recent_cutoff else 'older'
        d = by_cat.setdefault(cat, {
            'recent': {'ctr': [], 'cvr': [], 'str': []},
            'older':  {'ctr': [], 'cvr': [], 'str': []},
        })
        if r['ctr'] is not None:
            d[bucket]['ctr'].append(float(r['ctr']))
        if r['cvr'] is not None:
            d[bucket]['cvr'].append(float(r['cvr']))
        if r['str_pct'] is not None:
            d[bucket]['str'].append(float(r['str_pct']))
    out: Dict[str, Dict[str, Any]] = {}
    for cat, d in by_cat.items():
        n = len(d['recent']['ctr']) + len(d['older']['ctr'])
        if n < min_samples:
            continue
        if weighted:
            ctr_med = _seasonal_weighted_median(d['recent']['ctr'], d['older']['ctr']) or DEFAULT_CTR
            cvr_med = _seasonal_weighted_median(d['recent']['cvr'], d['older']['cvr']) or DEFAULT_CVR
            str_med = _seasonal_weighted_median(d['recent']['str'], d['older']['str']) or DEFAULT_STR
        else:
            ctr_med = _safe_median(d['recent']['ctr'] + d['older']['ctr']) or DEFAULT_CTR
            cvr_med = _safe_median(d['recent']['cvr'] + d['older']['cvr']) or DEFAULT_CVR
            str_med = _safe_median(d['recent']['str'] + d['older']['str']) or DEFAULT_STR
        out[cat] = {
            'ctr': max(ctr_med, 0.005),
            'cvr': max(cvr_med, 0.005),
            'str': max(str_med, 0.0001),
            'samples': n,
        }
    return out


def _write_thresholds(db_path: Path, table: str,
                      learned: Dict[str, Dict[str, Any]],
                      replace_all: bool = False) -> None:
    now_iso = date.today().isoformat()
    with sqlite3.connect(str(db_path)) as c:
        if replace_all:
            c.execute(f"DELETE FROM {table}")
        for cat, v in learned.items():
            c.execute(
                f"INSERT OR REPLACE INTO {table} "
                "(category_id, healthy_ctr, healthy_cvr, healthy_str, sample_size, learned_at) "
                "VALUES (?,?,?,?,?,?)",
                (cat, v['ctr'], v['cvr'], v['str'], v['samples'], now_iso),
            )
        c.commit()


def learn_to_pending(db_path: Optional[Path] = None,
                     **kwargs) -> Dict[str, Any]:
    """S21 阶段 1: 计算 + 写到 cro_thresholds_pending. 不影响生产阈值."""
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    learned = _learn_payload(db, **kwargs)
    _write_thresholds(db, 'cro_thresholds_pending', learned, replace_all=True)
    return learned


def load_pending_thresholds(db_path: Optional[Path] = None,
                            ) -> Dict[str, Dict[str, float]]:
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return {}
    out: Dict[str, Dict[str, float]] = {}
    try:
        with sqlite3.connect(str(db)) as c:
            for r in c.execute(
                "SELECT category_id, healthy_ctr, healthy_cvr, healthy_str "
                "FROM cro_thresholds_pending"
            ):
                out[r[0]] = {'ctr': float(r[1]), 'cvr': float(r[2]),
                             'str': float(r[3])}
    except sqlite3.OperationalError:
        return {}
    return out


def promote_pending(db_path: Optional[Path] = None) -> int:
    """S21 阶段 3: pending → 生产 cro_thresholds. 调用方负责 shadow 守门."""
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    n = 0
    with sqlite3.connect(str(db)) as c:
        rows = c.execute(
            "SELECT category_id, healthy_ctr, healthy_cvr, healthy_str, sample_size, learned_at "
            "FROM cro_thresholds_pending"
        ).fetchall()
        for r in rows:
            c.execute(
                "INSERT OR REPLACE INTO cro_thresholds "
                "(category_id, healthy_ctr, healthy_cvr, healthy_str, sample_size, learned_at) "
                "VALUES (?,?,?,?,?,?)",
                r,
            )
            n += 1
        c.commit()
    return n


def learn_all(db_path: Optional[Path] = None,
              lookback_days: int = LOOKBACK_DAYS,
              min_impressions: int = MIN_IMPRESSIONS_PER_SAMPLE,
              min_samples: int = MIN_SAMPLES,
              ) -> Dict[str, Any]:
    """兼容旧 API: 一步到位写生产表. S21 调度走两阶段版本."""
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    learned = _learn_payload(db, lookback_days=lookback_days,
                              min_impressions=min_impressions,
                              min_samples=min_samples)
    _write_thresholds(db, 'cro_thresholds', learned, replace_all=False)
    return learned


def load_thresholds(db_path: Optional[Path] = None,
                    ) -> Dict[str, Dict[str, float]]:
    """读 cro_thresholds 表, 返回 {category_id: {ctr,cvr,str}}.

    给 conversion_diagnoser.diagnose_batch 的 thresholds_by_category 用.
    """
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return {}
    out: Dict[str, Dict[str, float]] = {}
    try:
        with sqlite3.connect(str(db)) as c:
            for r in c.execute(
                "SELECT category_id, healthy_ctr, healthy_cvr, healthy_str "
                "FROM cro_thresholds"
            ):
                cat, ctr, cvr, strv = r
                out[cat] = {'ctr': float(ctr), 'cvr': float(cvr),
                            'str': float(strv)}
    except sqlite3.OperationalError:
        return {}
    return out


# ─── S19: effect_audit 反哺 ─────────────────────────────────────────
SUGGEST_LOW_IMPROVED_RATE = 0.30   # < 30% 改善率 → 阈值偏激进, 建议放宽
SUGGEST_HIGH_IMPROVED_RATE = 0.65  # > 65% 改善率 → 阈值偏保守, 建议收紧
SUGGEST_MIN_SAMPLES = 5
ADJUST_RATIO = 0.10                # ±10% 微调


def suggest_threshold_adjustments(effect_report: Dict[str, Any],
                                  current_thresholds: Dict[str, Dict[str, float]],
                                  ) -> List[Dict[str, Any]]:
    """根据 effect_audit 的 by_action × per-sku 表现, 提出阈值调整建议.

    规则:
      - price_drop 改善率 < 30% (样本 ≥ 5) → 说明很多 low_ctr 诊断是噪声
        ⇒ 把对应 categoryId 的 ctr 阈值下调 10% (更难触发 low_ctr)
      - price_drop 改善率 > 65% → 阈值偏保守, 漏诊机会 ⇒ ctr 上调 10%
      - fill_specifics 同理影响 cvr 阈值

    输入 effect_report 形如 cro_effect_audit.evaluate_actions() 的输出, 必须每行带 'category' (S19 调用前需要 join).
    本函数容忍缺 'category' 时按 '_global' 聚合.

    返回提议列表, 不写库; 由调用方决定是否 apply.
    """
    rows = effect_report.get('rows') or []
    # 按 (category, action) 聚合
    bucket: Dict[tuple, Dict[str, int]] = {}
    for r in rows:
        cat = r.get('category') or '_global'
        key = (cat, r.get('action'))
        d = bucket.setdefault(key, {'total': 0, 'improved': 0, 'worsened': 0})
        d['total'] += 1
        v = r.get('verdict')
        if v in d:
            d[v] += 1

    suggestions: List[Dict[str, Any]] = []
    for (cat, action), s in bucket.items():
        if s['total'] < SUGGEST_MIN_SAMPLES:
            continue
        rate = s['improved'] / s['total']
        # 仅 price_drop / image_refresh 影响 ctr; fill_specifics 影响 cvr
        metric = ('cvr' if action == 'fill_specifics' else 'ctr')
        cur = (current_thresholds.get(cat) or {}).get(metric)
        if cur is None:
            continue  # 该品类还没学到阈值, 跳
        direction = None
        if rate < SUGGEST_LOW_IMPROVED_RATE:
            direction = 'relax'  # 阈值下调, 减少 false positive
            new_val = round(cur * (1 - ADJUST_RATIO), 5)
        elif rate > SUGGEST_HIGH_IMPROVED_RATE:
            direction = 'tighten'
            new_val = round(cur * (1 + ADJUST_RATIO), 5)
        else:
            continue
        suggestions.append({
            'category_id': cat,
            'action': action,
            'metric': metric,
            'current': cur,
            'suggested': new_val,
            'direction': direction,
            'improved_rate': round(rate, 3),
            'samples': s['total'],
        })
    return suggestions


def apply_suggestions_to_pending(suggestions: List[Dict[str, Any]],
                                 db_path: Optional[Path] = None,
                                 ) -> int:
    """S32 \u2014 \u628a effect_audit \u53cd\u54fa\u5efa\u8bae\u5199\u5230 cro_thresholds_pending,
    \u540e\u7eed\u7531 promote_thresholds \u8d70 shadow \u5b88\u95e8 + \u4eba\u5de5\u786e\u8ba4 (S21).

    \u5408\u5e76\u7b56\u7565: \u53d6 (cur \u00d7 suggested) \u8c03\u6574\u540e\u7684 \u9608\u503c\u5199\u5165 pending.
    \u540c\u4e00 category \u591a\u4e2a metric \u4f1a\u5408\u5e76\u4e3a\u540c\u4e00\u884c.
    \u8fd4\u56de\u5199\u5165\u7684 category \u6570.
    """
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    if not suggestions:
        return 0
    cur_full = load_thresholds(db)
    by_cat: Dict[str, Dict[str, Any]] = {}
    for s in suggestions:
        cat = s.get('category_id')
        if not cat:
            continue
        cur_row = cur_full.get(cat) or {}
        merged = by_cat.setdefault(cat, {
            'ctr': cur_row.get('ctr', DEFAULT_CTR),
            'cvr': cur_row.get('cvr', DEFAULT_CVR),
            'str': cur_row.get('str', DEFAULT_STR),
            'samples': s.get('samples', SUGGEST_MIN_SAMPLES),
        })
        metric = s.get('metric')
        if metric in {'ctr', 'cvr', 'str'} and s.get('suggested') is not None:
            merged[metric] = float(s['suggested'])
        merged['samples'] = max(merged['samples'], s.get('samples', 0))
    _write_thresholds(db, 'cro_thresholds_pending', by_cat, replace_all=False)
    return len(by_cat)


def promote_thresholds(products: List[Dict[str, Any]],
                       market_data: Optional[Dict[str, Any]] = None,
                       db_path: Optional[Path] = None,
                       safe_only: bool = True,
                       report_dir: Optional[Path] = None,
                       ) -> Dict[str, Any]:
    """S21 阶段 2+3: 跑 shadow 守门 → 安全则 promote pending → 生产.

    返回 {promoted, blocked, shadow, pending_count, report_path}.
    """
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    pending = load_pending_thresholds(db)
    if not pending:
        return {'promoted': 0, 'blocked': False, 'shadow': None,
                'pending_count': 0, 'report_path': None,
                'reason': 'no_pending'}
    current = load_thresholds(db)

    from scripts.cro_threshold_shadow import shadow_compare
    shadow = shadow_compare(products, market_data or {}, current, pending)

    rep_dir = Path(report_dir) if report_dir else (PROJECT_ROOT / 'reports')
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep_path = rep_dir / f"cro_threshold_shadow_{date.today().isoformat()}.json"
    rep_path.write_text(
        __import__('json').dumps({
            'pending': pending,
            'current': current,
            'shadow': shadow,
        }, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    if safe_only and not shadow.get('safe_to_promote', False):
        return {'promoted': 0, 'blocked': True, 'shadow': shadow,
                'pending_count': len(pending),
                'report_path': str(rep_path),
                'reason': 'shadow_explosion'}
    n = promote_pending(db)
    return {'promoted': n, 'blocked': False, 'shadow': shadow,
            'pending_count': len(pending),
            'report_path': str(rep_path)}


def main():
    import argparse, json
    p = argparse.ArgumentParser(description='Learn per-category CRO thresholds')
    p.add_argument('--lookback-days', type=int, default=LOOKBACK_DAYS)
    p.add_argument('--min-impressions', type=int, default=MIN_IMPRESSIONS_PER_SAMPLE)
    p.add_argument('--min-samples', type=int, default=MIN_SAMPLES)
    p.add_argument('--pending', action='store_true',
                   help='S21: 写到 cro_thresholds_pending 而非生产表')
    args = p.parse_args()
    fn = learn_to_pending if args.pending else learn_all
    rep = fn(lookback_days=args.lookback_days,
             min_impressions=args.min_impressions,
             min_samples=args.min_samples)
    print(json.dumps({
        'categories_learned': len(rep),
        'pending': args.pending,
        'samples': {k: v['samples'] for k, v in rep.items()},
        'thresholds': rep,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
