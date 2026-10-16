"""S41 \u2014 \u5468\u73af\u6bd4\u5f02\u5e38\u805a\u7c7b.

\u4e0d\u5f15\u5165 sklearn (\u907f\u514d\u91cd\u4f9d\u8d56), \u7528\u7b80\u5355 group-by (category, root_cause)
\u63ed\u793a "\u67d0\u54c1\u7c7b\u6574\u4f53\u6076\u5316" \u8d8b\u52bf, \u800c\u4e0d\u53ea\u770b\u5355 SKU\u3002

\u8f93\u5165: alerts = [{sku, category, root_cause, severity, ...}]
\u8f93\u51fa: clusters: [{key, n, severity_avg, sample_skus}], top_k_categories
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List


CLUSTER_MIN_SIZE = 3  # \u540c\u4e00 (category, root_cause) \u51fa\u73b0 >=3 \u624d\u4f5c\u4e3a\u96c6\u7fa4


def _severity(alert: Dict[str, Any]) -> float:
    s = alert.get('severity')
    if isinstance(s, (int, float)):
        return float(s)
    # fallback \u4ece drop_pct / worsened_pct \u63a8
    for k in ('drop_pct', 'worsened_pct', 'drift'):
        v = alert.get(k)
        if isinstance(v, (int, float)):
            return float(abs(v))
    return 1.0


def cluster_alerts(alerts: Iterable[Dict[str, Any]],
                   min_size: int = CLUSTER_MIN_SIZE,
                   ) -> Dict[str, Any]:
    by_key: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for a in alerts:
        cat = a.get('category') or 'unknown'
        rc = a.get('root_cause') or a.get('funnel_stage') or 'unknown'
        by_key[(cat, rc)].append(a)
    clusters: List[Dict[str, Any]] = []
    singletons: List[Dict[str, Any]] = []
    for (cat, rc), items in by_key.items():
        if len(items) >= min_size:
            sev_sum = sum(_severity(x) for x in items)
            clusters.append({
                'category': cat,
                'root_cause': rc,
                'n': len(items),
                'severity_avg': round(sev_sum / len(items), 3),
                'severity_total': round(sev_sum, 3),
                'sample_skus': [x.get('sku') for x in items[:5]],
            })
        else:
            singletons.extend(items)
    clusters.sort(key=lambda c: (-c['severity_total'], -c['n']))

    by_cat: Dict[str, int] = defaultdict(int)
    for c in clusters:
        by_cat[c['category']] += c['n']
    top_categories = sorted(by_cat.items(), key=lambda x: -x[1])[:5]
    return {
        'total_alerts': sum(len(v) for v in by_key.values()),
        'clusters': clusters,
        'top_categories': [{'category': k, 'n': n} for k, n in top_categories],
        'singleton_count': len(singletons),
    }


def render_cluster_text(report: Dict[str, Any]) -> str:
    lines = [f"\u5f02\u5e38\u805a\u7c7b: \u603b {report['total_alerts']} "
             f"\u00b7 {len(report['clusters'])} \u4e2a\u96c6\u7fa4 "
             f"\u00b7 {report['singleton_count']} \u4e2a\u72ec\u7acb"]
    for c in report['clusters'][:5]:
        lines.append(
            f"  [{c['category']}/{c['root_cause']}] n={c['n']} "
            f"sev={c['severity_avg']} skus={c['sample_skus']}"
        )
    return '\n'.join(lines)
