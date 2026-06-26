"""S26 — CRO A/B 实验框架.

为提升类动作 (image_refresh / promote) 按 SKU 确定性分组:
  - 20% 样本进 control (跳过执行)
  - 80% 样本进 treatment (正常执行)
  - 同一 (sku, action) 始终落同一组, 周维度切片 (避免长期偏置)

下游消费:
  - cro_action_queue.enqueue: 写入 row['cohort']
  - cro_action_queue.load_pending(include_control=False): 默认隐藏 control
  - cro_effect_audit: 按 cohort 分桶计算 lift% (后续 slice 接入)

价格类动作 (price_drop) 不入 A/B (改价直接影响利润, 不能用控制组).
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Optional

CONTROL_RATIO = 0.20
AB_ENABLED_ACTIONS = frozenset({'image_refresh', 'promote'})


def _week_bucket(d: Optional[str] = None) -> str:
    if d is None:
        d = date.today().isoformat()
    y, m, day = (int(x) for x in d.split('-'))
    today = date(y, m, day)
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def assign_cohort(sku: str, action: str,
                  date_str: Optional[str] = None,
                  control_ratio: float = CONTROL_RATIO) -> str:
    """返回 'control' / 'treatment' / 'na' (后者: 不参与 A/B)."""
    if action not in AB_ENABLED_ACTIONS:
        return 'na'
    bucket = _week_bucket(date_str)
    h = hashlib.sha256(f"{sku}|{action}|{bucket}".encode('utf-8')).hexdigest()
    pct = int(h[:8], 16) % 100
    return 'control' if pct < int(control_ratio * 100) else 'treatment'
