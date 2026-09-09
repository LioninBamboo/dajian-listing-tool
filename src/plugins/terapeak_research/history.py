"""F7 — MI 历史趋势加载与对比

读取 reports/mi_opportunities_YYYYMMDD_HHMMSS.json 快照，
计算单 SKU 的 opportunity_score 历史走势。

数据准确性约束：
- 仅使用窗口期内的快照（默认 14 天）。
- 同一 UTC 日期内只保留最新一份快照（避免短时多次扫描偏置）。
- 当历史样本与当前样本的 STR 数据来源不同（real vs 估算），
  在返回值上设置 `mixed_str_basis=True`，UI 必须给出告警，
  以避免把"刚接入实测 STR"误读为机会跃升。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

_FILENAME_RE = re.compile(r"^mi_opportunities_(\d{8})_(\d{6})\.json$")


def _parse_snapshot_timestamp(filename: str) -> Optional[datetime]:
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def load_mi_snapshots(
    window_days: int = 14,
    project_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """读取窗口期内的快照，返回 oldest→newest 排序。

    返回项形如：
        {"generated_at": datetime, "by_sku": {sku: opp_dict, ...}}
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parents[3]
    snap_dir = project_root / "reports"
    if not snap_dir.exists():
        return []
    if now is None:
        now = datetime.now()
    cutoff = now - timedelta(days=window_days)

    candidates: List[tuple[datetime, Path]] = []
    for path in snap_dir.glob("mi_opportunities_*.json"):
        ts = _parse_snapshot_timestamp(path.name)
        if ts is None or ts < cutoff:
            continue
        candidates.append((ts, path))

    # 同日去重：保留最晚的一份
    by_day: Dict[str, tuple[datetime, Path]] = {}
    for ts, path in candidates:
        day_key = ts.strftime("%Y-%m-%d")
        existing = by_day.get(day_key)
        if existing is None or ts > existing[0]:
            by_day[day_key] = (ts, path)

    ordered = sorted(by_day.values(), key=lambda x: x[0])

    snapshots: List[Dict] = []
    for ts, path in ordered:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        opps = data.get("opportunities") or []
        by_sku: Dict[str, Dict] = {}
        for opp in opps:
            sku = opp.get("sku")
            if sku:
                by_sku[str(sku)] = opp
        snapshots.append({
            "generated_at": ts,
            "by_sku": by_sku,
            # F30 — 透传按品类聚合（旧快照无此字段则为空）
            "by_category": data.get("by_category") or [],
        })
    return snapshots


def compute_score_trend(
    snapshots: List[Dict],
    sku: str,
    current_score: Optional[float],
    current_has_real_str: bool = False,
) -> Dict:
    """对比当前分数与历史快照分数。

    返回：
        {
          "prior_score": int|None,        # 上一次出现时的分数
          "delta": int|None,              # current - prior
          "sample_count": int,            # 历史中该 SKU 出现次数
          "first_seen": datetime|None,
          "mixed_str_basis": bool,        # 当前与历史 STR 来源不一致 → 不可比
          "sparkline": list[int|None],    # 历史 + 当前，缺测为 None
        }
    """
    return _compute_field_trend(
        snapshots, sku, current_score,
        field_name="opportunity_score",
        current_has_real_str=current_has_real_str,
        flag_mixed_str=True,
    )


def compute_field_trend(
    snapshots: List[Dict],
    sku: str,
    current_value: Optional[float],
    field_name: str,
) -> Dict:
    """F12 — 通用字段历史趋势（用于 demand_signal_score 等）。

    与 compute_score_trend 同形态，但不做 STR 口径检查。
    """
    return _compute_field_trend(
        snapshots, sku, current_value,
        field_name=field_name,
        current_has_real_str=False,
        flag_mixed_str=False,
    )


def _compute_field_trend(
    snapshots: List[Dict],
    sku: str,
    current_value: Optional[float],
    *,
    field_name: str,
    current_has_real_str: bool,
    flag_mixed_str: bool,
) -> Dict:
    history_scores: List[Optional[int]] = []
    history_real_str_flags: List[bool] = []
    prior_score: Optional[int] = None
    first_seen: Optional[datetime] = None
    sample_count = 0

    for snap in snapshots:
        opp = snap["by_sku"].get(str(sku))
        if opp is None:
            history_scores.append(None)
            continue
        sample_count += 1
        if first_seen is None:
            first_seen = snap["generated_at"]
        raw = opp.get(field_name)
        if raw is None:
            history_scores.append(None)
            history_real_str_flags.append(opp.get("seller_str_pct") is not None)
            continue
        try:
            score_val = int(round(float(raw)))
        except (TypeError, ValueError):
            score_val = 0
        history_scores.append(score_val)
        prior_score = score_val
        history_real_str_flags.append(opp.get("seller_str_pct") is not None)

    # 当前点
    if current_value is None:
        current_int: Optional[int] = None
    else:
        try:
            current_int = int(round(float(current_value)))
        except (TypeError, ValueError):
            current_int = None

    delta: Optional[int] = None
    if prior_score is not None and current_int is not None:
        delta = current_int - prior_score

    any_real = any(history_real_str_flags)
    mixed = False
    if flag_mixed_str and sample_count > 0:
        if current_has_real_str and not any_real:
            mixed = True
        elif (not current_has_real_str) and any_real:
            mixed = True

    sparkline = history_scores + [current_int]

    return {
        "prior_score": prior_score,
        "delta": delta,
        "sample_count": sample_count,
        "first_seen": first_seen,
        "mixed_str_basis": mixed,
        "sparkline": sparkline,
    }


def summarize_recent_history(
    snapshots: List[Dict],
    current_opportunities: List[Dict],
) -> Dict:
    """F11 — 计算 Tab1 顶部 KPI 横幅指标。

    返回：
        {
          "avg_recent_count": float,        # 过去快照平均机会数
          "current_count": int,             # 本次发现数
          "delta_vs_avg": int,              # current_count - avg_recent_count
          "real_str_coverage_pct": float,   # 当前有 real_str 的比例
          "snapshots_analyzed": int,
        }
    """
    snap_count = len(snapshots)
    if snap_count == 0:
        avg_count = 0.0
    else:
        avg_count = sum(len(s["by_sku"]) for s in snapshots) / snap_count

    current_count = len(current_opportunities)
    real_str_n = sum(
        1 for o in current_opportunities
        if o.get("seller_str_pct") is not None
    )
    coverage = (real_str_n / current_count * 100.0) if current_count else 0.0

    return {
        "avg_recent_count": round(avg_count, 1),
        "current_count": current_count,
        "delta_vs_avg": current_count - int(round(avg_count)),
        "real_str_coverage_pct": round(coverage, 1),
        "snapshots_analyzed": snap_count,
    }


def build_kpi_timeseries(snapshots: List[Dict]) -> List[Dict]:
    """F15 — 把每份快照折叠为时间序列点，供折线图使用。

    返回 oldest→newest：
        [{date, opportunity_count, avg_score, real_str_coverage_pct}, ...]
    """
    series: List[Dict] = []
    for snap in snapshots:
        opps = list(snap["by_sku"].values())
        n = len(opps)
        if n == 0:
            avg_score = 0.0
            coverage = 0.0
        else:
            scores: List[float] = []
            for o in opps:
                try:
                    scores.append(float(o.get("opportunity_score") or 0))
                except (TypeError, ValueError):
                    scores.append(0.0)
            avg_score = sum(scores) / n
            real_n = sum(1 for o in opps if o.get("seller_str_pct") is not None)
            coverage = real_n / n * 100.0
        series.append({
            "date": snap["generated_at"].strftime("%Y-%m-%d"),
            "opportunity_count": n,
            "avg_score": round(avg_score, 1),
            "real_str_coverage_pct": round(coverage, 1),
        })
    return series


def list_known_categories(snapshots: List[Dict]) -> List[str]:
    """F30 — 收集历史快照里出现过的品类标签（按出现频次倒序）。"""
    counts: Dict[str, int] = {}
    for snap in snapshots:
        for row in snap.get("by_category") or []:
            cat = (row or {}).get("category")
            if cat:
                counts[cat] = counts.get(cat, 0) + 1
    return sorted(counts.keys(), key=lambda k: (-counts[k], k))


def build_category_timeseries(snapshots: List[Dict], category: str) -> List[Dict]:
    """F30 — 抽取某个品类在 snapshots 中的逐日轨迹。

    返回：[{date, count, avg_score, median_price, total_potential_profit}, ...]
    若某天没有该品类则跳过。
    """
    out: List[Dict] = []
    for snap in snapshots:
        rows = snap.get("by_category") or []
        match = next((r for r in rows if (r or {}).get("category") == category), None)
        if not match:
            continue
        out.append({
            "date": snap["generated_at"].strftime("%Y-%m-%d"),
            "count": match.get("count", 0),
            "avg_score": match.get("avg_score", 0.0),
            "median_price": match.get("median_price", 0.0),
            "total_potential_profit": match.get("total_potential_profit", 0.0),
        })
    return out


def summarize_long_window(
    snapshots: List[Dict],
    current_opps: List[Dict],
    short_days: int = 14,
    long_days: int = 30,
    now: Optional[datetime] = None,
) -> Dict:
    """F23 — 长周期透视：对比 short_days vs long_days 窗口内的均值。

    用于识别"机会数最近是否相对长期均值在持续衰退/上升"。

    返回:
        {
            "short_days": 14,
            "long_days": 30,
            "current_count": int,
            "short_avg_count": float,    # short_days 窗口内的平均机会数
            "long_avg_count": float,     # long_days 窗口内的平均机会数
            "drift": float,              # short_avg - long_avg（正=回暖，负=衰退）
            "snapshots_short": int,
            "snapshots_long": int,
            "long_avg_score": float,     # long_days 窗口内的整体平均分
            "trend_label": str,          # "rising" / "falling" / "stable" / "insufficient"
        }
    """
    cutoff = now or datetime.now()
    short_cut = cutoff - timedelta(days=short_days)
    long_cut = cutoff - timedelta(days=long_days)

    # 注意 snapshots 已经是按时间排过序的；不需要再过滤 long_days 上限（load 时已限定）
    short_snaps = [s for s in snapshots if s["generated_at"] >= short_cut]
    long_snaps = [s for s in snapshots if s["generated_at"] >= long_cut]

    def _avg_count(snaps: List[Dict]) -> float:
        if not snaps:
            return 0.0
        return round(sum(len(s["by_sku"]) for s in snaps) / len(snaps), 2)

    short_avg = _avg_count(short_snaps)
    long_avg = _avg_count(long_snaps)
    drift = round(short_avg - long_avg, 2)

    # 长窗口平均分
    all_scores: List[float] = []
    for s in long_snaps:
        for o in s["by_sku"].values():
            try:
                all_scores.append(float(o.get("opportunity_score") or 0))
            except (TypeError, ValueError):
                pass
    long_avg_score = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0.0

    # 趋势标签：长窗口快照不足 3 时不给结论
    if len(long_snaps) < 3:
        trend_label = "insufficient"
    elif drift >= 1.5:
        trend_label = "rising"
    elif drift <= -1.5:
        trend_label = "falling"
    else:
        trend_label = "stable"

    return {
        "short_days": short_days,
        "long_days": long_days,
        "current_count": len(current_opps or []),
        "short_avg_count": short_avg,
        "long_avg_count": long_avg,
        "drift": drift,
        "snapshots_short": len(short_snaps),
        "snapshots_long": len(long_snaps),
        "long_avg_score": long_avg_score,
        "trend_label": trend_label,
    }


def compare_kpi_day_over_day(
    snapshots: List[Dict],
    current_opps: List[Dict],
) -> Dict:
    """F28 — 把"今日 vs 上一天"做成一份对比结果。

    入参：
      - snapshots：oldest→newest 的历史快照（不含今日；通常来自 load_mi_snapshots）。
      - current_opps：本次刚生成的机会列表。

    返回：
        {
          "has_yesterday": bool,
          "yesterday_date": "YYYY-MM-DD" | None,
          "today_count": int,
          "yesterday_count": int,
          "count_delta": int,
          "today_avg_score": float,
          "yesterday_avg_score": float,
          "score_delta": float,
          "today_real_str_pct": float,
          "yesterday_real_str_pct": float,
          "real_str_pct_delta": float,
          "new_skus": [sku, ...],         # 今日新增（≤ 20 条）
          "exited_skus": [sku, ...],      # 今日掉出（≤ 20 条）
        }
    """
    today_skus = {str(o.get("sku")) for o in (current_opps or []) if o.get("sku")}
    today_count = len(current_opps or [])

    def _avg_score(items: Iterable) -> float:
        scores: List[float] = []
        for it in items:
            try:
                v = it.get("opportunity_score") if isinstance(it, dict) else None
                if v is not None:
                    scores.append(float(v))
            except (TypeError, ValueError):
                pass
        return round(sum(scores) / len(scores), 1) if scores else 0.0

    def _real_str_pct(items: Iterable, n: int) -> float:
        if n == 0:
            return 0.0
        real = sum(1 for it in items
                   if isinstance(it, dict) and it.get("seller_str_pct") is not None)
        return round(real / n * 100.0, 1)

    today_avg = _avg_score(current_opps or [])
    today_real = _real_str_pct(current_opps or [], today_count)

    if not snapshots:
        return {
            "has_yesterday": False,
            "yesterday_date": None,
            "today_count": today_count,
            "yesterday_count": 0,
            "count_delta": today_count,
            "today_avg_score": today_avg,
            "yesterday_avg_score": 0.0,
            "score_delta": today_avg,
            "today_real_str_pct": today_real,
            "yesterday_real_str_pct": 0.0,
            "real_str_pct_delta": today_real,
            "new_skus": sorted(today_skus)[:20],
            "exited_skus": [],
        }

    last = snapshots[-1]
    last_by_sku = last.get("by_sku") or {}
    last_items = list(last_by_sku.values())
    last_count = len(last_items)
    last_avg = _avg_score(last_items)
    last_real = _real_str_pct(last_items, last_count)
    last_skus = set(last_by_sku.keys())

    new_skus = sorted(today_skus - last_skus)[:20]
    exited_skus = sorted(last_skus - today_skus)[:20]

    return {
        "has_yesterday": True,
        "yesterday_date": last["generated_at"].strftime("%Y-%m-%d"),
        "today_count": today_count,
        "yesterday_count": last_count,
        "count_delta": today_count - last_count,
        "today_avg_score": today_avg,
        "yesterday_avg_score": last_avg,
        "score_delta": round(today_avg - last_avg, 1),
        "today_real_str_pct": today_real,
        "yesterday_real_str_pct": last_real,
        "real_str_pct_delta": round(today_real - last_real, 1),
        "new_skus": new_skus,
        "exited_skus": exited_skus,
    }


# ---------------------------------------------------------------------------
# F9 — 快照保留策略 + sparkline 渲染
# ---------------------------------------------------------------------------

def cleanup_old_snapshots(
    keep_days: int = 30,
    project_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """删除超过 keep_days 的 mi_opportunities_*.json 快照，返回删除数量。

    数据安全：统一委托运行期产物保留模块，沿用相同的文件名日期、白名单和
    单文件失败继续策略，避免 MI 页面、日报任务各自维护一套删除语义。
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parents[3]
    from src.utils.report_retention import purge_named_artifacts

    return purge_named_artifacts(
        project_root / "reports",
        ("mi_opportunities_*.json",),
        keep_days,
        now=now or datetime.now(),
    )


_SPARK_BARS = "▁▂▃▄▅▆▇█"


def render_sparkline(values: List[Optional[int]]) -> str:
    """将一组 0–100 分数渲染为 8 级 unicode sparkline。

    None 表示缺测，渲染为空格保留位次（保留时间轴对齐）。
    所有有效值相同时，统一渲染为中位高度，避免误暗示波动。
    """
    if not values:
        return ""
    nums = [v for v in values if v is not None]
    if not nums:
        return " " * len(values)
    lo, hi = min(nums), max(nums)
    span = hi - lo
    out = []
    for v in values:
        if v is None:
            out.append(" ")
        elif span == 0:
            out.append(_SPARK_BARS[len(_SPARK_BARS) // 2])
        else:
            idx = int(round((v - lo) / span * (len(_SPARK_BARS) - 1)))
            idx = max(0, min(len(_SPARK_BARS) - 1, idx))
            out.append(_SPARK_BARS[idx])
    return "".join(out)
