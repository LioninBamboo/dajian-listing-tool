"""S74 — Alert clustering RCA hint.

补充 cro_alert_clustering 的输出, 把每个 cluster 的特征翻译成根因假说.
RULES 是 (匹配条件, 假说, 建议动作) 的元组; 第一个匹配生效.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# (predicate, hypothesis, suggested_action)
RcaRule = Tuple[Callable[[Dict[str, Any]], bool], str, str]


def _ge(field: str, threshold: float) -> Callable[[Dict[str, Any]], bool]:
    def _f(f: Dict[str, Any]) -> bool:
        if field not in f or f.get(field) is None:
            return False
        return float(f.get(field, 0) or 0) >= threshold
    return _f


def _le(field: str, threshold: float) -> Callable[[Dict[str, Any]], bool]:
    def _f(f: Dict[str, Any]) -> bool:
        if field not in f or f.get(field) is None:
            return False
        return float(f.get(field, 0) or 0) <= threshold
    return _f


DEFAULT_RULES: List[RcaRule] = [
    # 退货高 + 主图老 → 主图问题
    (lambda f: _ge('returns_pct', 0.15)(f) and _ge('image_age_days', 30)(f),
     '主图与商品差异大, 引发高退货率',
     '触发主图轮换 + 描述补充'),
    # 退货高 + 尺寸投诉 → 尺寸描述
    (lambda f: _ge('returns_pct', 0.15)(f) and _ge('size_complaint_pct', 0.30)(f),
     '尺寸描述/详情页测量信息不准',
     '审核 dimensions/itemSpecifics, 补充实测尺寸图'),
    # 流量正常但 CTR 低 → 主图/标题
    (lambda f: _ge('impressions', 100)(f) and _le('ctr', 0.005)(f),
     '展现量正常但 CTR 低, 主图或标题缺乏吸引力',
     '触发主图 A/B + 标题再优化'),
    # CTR 正常但 CVR 低 → 价格/竞品
    (lambda f: _ge('ctr', 0.01)(f) and _le('cvr', 0.005)(f),
     '点击进店但不买, 大概率价格/运费/对比竞品劣势',
     '比价 + 触发 price_drop 或运费下调'),
    # 库存断货
    (_le('stock', 1),
     '库存为 0, 流量浪费',
     '紧急补货, 暂停推广'),
    # 高广告花费但低 ROI
    (lambda f: _ge('ad_spend', 50)(f) and _le('roi', 0.5)(f),
     '广告花费高但 ROI 低, 出价档位过高',
     '触发 promote bid bandit 降档或暂停推广'),
]


def diagnose_features(features: Dict[str, Any],
                      rules: Optional[List[RcaRule]] = None,
                      ) -> Dict[str, Any]:
    rules = rules or DEFAULT_RULES
    for predicate, hypothesis, action in rules:
        try:
            if predicate(features):
                return {
                    'root_cause_hypothesis': hypothesis,
                    'suggested_action': action,
                    'matched': True,
                }
        except (TypeError, ValueError):
            continue
    return {
        'root_cause_hypothesis': '无明显模式, 建议人工复核',
        'suggested_action': 'manual_review',
        'matched': False,
    }


def annotate_clusters(clusters: List[Dict[str, Any]],
                      rules: Optional[List[RcaRule]] = None,
                      feature_key: str = 'features',
                      ) -> List[Dict[str, Any]]:
    out = []
    for cluster in clusters:
        features = cluster.get(feature_key) or {}
        rca = diagnose_features(features, rules)
        out.append({**cluster, **rca})
    return out


def summarise_rca(annotated: List[Dict[str, Any]]) -> Dict[str, Any]:
    matched = [c for c in annotated if c.get('matched')]
    by_hypo: Dict[str, int] = {}
    for c in matched:
        h = c['root_cause_hypothesis']
        by_hypo[h] = by_hypo.get(h, 0) + 1
    return {
        'total': len(annotated),
        'matched_count': len(matched),
        'unmatched_count': len(annotated) - len(matched),
        'by_hypothesis': by_hypo,
    }
