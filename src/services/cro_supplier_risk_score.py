"""S115 — 供应商风险评分.

输入: 供应商近 N 天履约记录 + 退货明细.
评分维度: 缺货率/延迟率/退货率/价格波动率.
0-100 (越高越危险), label: low/medium/high/critical.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List

# 权重 (sum=1)
WEIGHTS = {
    'stockout_rate': 0.30,
    'late_rate': 0.25,
    'return_rate': 0.25,
    'price_volatility': 0.20,
}


def _safe_div(a, b):
    return (a / b) if b > 0 else 0.0


def _stdev(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def compute_supplier_metrics(record: Dict[str, Any]) -> Dict[str, float]:
    orders = int(record.get('orders', 0) or 0)
    stockouts = int(record.get('stockouts', 0) or 0)
    late = int(record.get('late_shipments', 0) or 0)
    sold = int(record.get('sold_30d', 0) or 0)
    returned = int(record.get('returned_30d', 0) or 0)
    price_history = [float(p or 0) for p in (record.get('price_history') or [])
                      if (p or 0) > 0]
    avg_price = sum(price_history) / len(price_history) if price_history else 0
    pv = (_stdev(price_history) / avg_price) if avg_price > 0 else 0.0
    return {
        'stockout_rate': min(1.0, _safe_div(stockouts, orders)),
        'late_rate': min(1.0, _safe_div(late, orders)),
        'return_rate': min(1.0, _safe_div(returned, sold)),
        'price_volatility': min(1.0, pv),
    }


def label_for_score(score: float) -> str:
    if score >= 70:
        return 'critical'
    if score >= 50:
        return 'high'
    if score >= 25:
        return 'medium'
    return 'low'


def evaluate_supplier(record: Dict[str, Any]) -> Dict[str, Any]:
    """record: {supplier_id, name, orders, stockouts, late_shipments,
               sold_30d, returned_30d, price_history:[..]}"""
    metrics = compute_supplier_metrics(record)
    score = sum(metrics[k] * WEIGHTS[k] for k in WEIGHTS) * 100
    score = round(score, 2)
    return {
        'supplier_id': record.get('supplier_id') or record.get('name'),
        'name': record.get('name'),
        'risk_score': score,
        'risk_label': label_for_score(score),
        'metrics': {k: round(v, 4) for k, v in metrics.items()},
        'recommendation': _recommendation(label_for_score(score)),
    }


def _recommendation(label: str) -> str:
    return {
        'critical': '立即冻结新单, 启动备选供应商',
        'high': '减少订单分配, 启动备选评估',
        'medium': '加强抽检, 缩短账期',
        'low': '保持合作, 季度复评',
    }.get(label, '继续观察')


def batch_evaluate(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = sorted([evaluate_supplier(r) for r in records],
                    key=lambda x: -x['risk_score'])
    by_label: Dict[str, int] = {}
    for it in items:
        by_label[it['risk_label']] = by_label.get(it['risk_label'], 0) + 1
    return {'items': items, 'count': len(items), 'by_label': by_label,
            'top_risk': items[:5]}
