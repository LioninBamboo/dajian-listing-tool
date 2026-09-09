"""S70 — Cost-per-decision 度量.

记录每次 LLM/API/计算开销, 周报里展示决策成本, 帮判断何时降级到规则.
统一价目可被运营覆盖.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_LOG = Path('logs/cro_decision_costs.jsonl')

# 默认单价 (USD), 业务可注入覆盖
DEFAULT_PRICES: Dict[str, float] = {
    'llm_input_per_1k_tokens': 0.0005,    # Qwen Plus 估
    'llm_output_per_1k_tokens': 0.0015,
    'ebay_api_call': 0.0,                  # 自有配额, 不计费
    'compute_second': 0.00002,             # 简化估
}


def cost_for_llm(input_tokens: int, output_tokens: int,
                 prices: Optional[Dict[str, float]] = None) -> float:
    p = prices or DEFAULT_PRICES
    return (input_tokens / 1000.0) * p.get('llm_input_per_1k_tokens', 0.0) \
        + (output_tokens / 1000.0) * p.get('llm_output_per_1k_tokens', 0.0)


def cost_for_compute(seconds: float,
                     prices: Optional[Dict[str, float]] = None) -> float:
    p = prices or DEFAULT_PRICES
    return max(0.0, seconds) * p.get('compute_second', 0.0)


def record_decision_cost(decision_id: str,
                         action: str,
                         *,
                         category: Optional[str] = None,
                         llm_input_tokens: int = 0,
                         llm_output_tokens: int = 0,
                         compute_seconds: float = 0.0,
                         api_calls: int = 0,
                         prices: Optional[Dict[str, float]] = None,
                         log_path: Path = DEFAULT_LOG,
                         ) -> Dict[str, Any]:
    p = prices or DEFAULT_PRICES
    llm_cost = cost_for_llm(llm_input_tokens, llm_output_tokens, p)
    compute_cost = cost_for_compute(compute_seconds, p)
    api_cost = api_calls * p.get('ebay_api_call', 0.0)
    total = round(llm_cost + compute_cost + api_cost, 6)
    rec = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'decision_id': decision_id,
        'action': action,
        'category': category,
        'llm_input_tokens': llm_input_tokens,
        'llm_output_tokens': llm_output_tokens,
        'compute_seconds': compute_seconds,
        'api_calls': api_calls,
        'cost_usd': total,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + '\n')
    return rec


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summarise_costs(rows: Optional[Iterable[Dict[str, Any]]] = None,
                    log_path: Path = DEFAULT_LOG,
                    ) -> Dict[str, Any]:
    rows = list(rows) if rows is not None else _read_jsonl(log_path)
    total = round(sum(r.get('cost_usd', 0.0) for r in rows), 6)
    by_action: Dict[str, Dict[str, float]] = {}
    for r in rows:
        a = r.get('action', 'unknown')
        slot = by_action.setdefault(a, {'count': 0, 'total_cost': 0.0})
        slot['count'] += 1
        slot['total_cost'] += r.get('cost_usd', 0.0)
    for a, slot in by_action.items():
        slot['avg_cost'] = (round(slot['total_cost'] / slot['count'], 6)
                            if slot['count'] else 0.0)
        slot['total_cost'] = round(slot['total_cost'], 6)
    avg = round(total / len(rows), 6) if rows else 0.0
    return {
        'count': len(rows),
        'total_cost': total,
        'avg_cost_per_decision': avg,
        'by_action': by_action,
    }


def downgrade_recommendation(summary: Dict[str, Any],
                             threshold_avg_usd: float = 0.005,
                             ) -> Dict[str, Any]:
    """avg_cost_per_decision 超阈值 → 建议降级到规则模式."""
    avg = summary.get('avg_cost_per_decision', 0.0)
    suggest = avg > threshold_avg_usd
    return {
        'avg_cost_per_decision': avg,
        'threshold': threshold_avg_usd,
        'suggest_downgrade_to_rules': suggest,
        'reason': (f'avg ${avg:.4f} > 阈值 ${threshold_avg_usd:.4f}'
                   if suggest else 'within budget'),
    }
