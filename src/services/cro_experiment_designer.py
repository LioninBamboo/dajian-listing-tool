"""S45 \u2014 A/B \u5b9e\u9a8c\u8bbe\u8ba1\u5668.

\u7ed9\u5b9a\u5019\u9009\u6539\u52a8 (\u5982 "\u6807\u9898\u52a0 Free Shipping"), \u8ba1\u7b97:
  - \u6837\u672c\u91cf (\u57fa\u4e8e baseline CVR + \u671f\u671b\u63d0\u5347 + alpha=0.05 / power=0.80)
  - \u6240\u9700\u5929\u6570 (\u6839\u636e\u65e5\u5e73\u5747 conversion event \u6570)
  - \u63a8\u8350 cohort (\u590d\u7528 S26 control_ratio)
  - \u505c\u673a\u6761\u4ef6 (\u8d1f\u9762\u4f24\u5bb3 \u2192 \u63d0\u524d\u505c\u673a)

\u7eaf\u4f9d\u8d56\u6570\u5b66\u516c\u5f0f, \u4e0d\u5f15\u5165 scipy. \u53cc\u6837\u672c\u6bd4\u4f8b\u68c0\u9a8c.
"""
from __future__ import annotations

import math
from typing import Any, Dict


def _z(power: float = 0.80, alpha: float = 0.05) -> Dict[str, float]:
    # \u5e38\u7528 z \u503c\u67e5\u8868
    Z_ALPHA = {0.10: 1.6449, 0.05: 1.9600, 0.01: 2.5758}
    Z_POWER = {0.80: 0.8416, 0.90: 1.2816}
    return {
        'z_alpha': Z_ALPHA.get(round(alpha, 3), 1.96),
        'z_power': Z_POWER.get(round(power, 2), 0.8416),
    }


def required_sample_per_arm(baseline: float,
                            mde_relative: float,
                            alpha: float = 0.05,
                            power: float = 0.80,
                            ) -> int:
    """\u53cc\u6bd4\u4f8b z \u68c0\u9a8c\u6837\u672c\u91cf.

    baseline: \u73b0\u6709\u8f6c\u5316\u7387 (\u59820.03)
    mde_relative: \u5e0c\u671b\u68c0\u51fa\u7684\u76f8\u5bf9\u63d0\u5347 (\u59820.20\u8868\u793a +20%)
    """
    if baseline <= 0 or baseline >= 1:
        raise ValueError('baseline must be in (0, 1)')
    if mde_relative <= 0:
        raise ValueError('mde_relative must be > 0')
    p1 = baseline
    p2 = baseline * (1 + mde_relative)
    p2 = min(p2, 0.999)
    p_bar = (p1 + p2) / 2
    z = _z(power=power, alpha=alpha)
    se_pool = math.sqrt(2 * p_bar * (1 - p_bar))
    se_diff = math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    n = ((z['z_alpha'] * se_pool + z['z_power'] * se_diff) ** 2
         / ((p2 - p1) ** 2))
    return int(math.ceil(n))


def design_experiment(name: str,
                      baseline_cvr: float,
                      mde_relative: float = 0.20,
                      daily_conversions: float = 50.0,
                      control_ratio: float = 0.20,
                      max_days: int = 28,
                      negative_guard_drop_pct: float = 0.20,
                      ) -> Dict[str, Any]:
    """\u8fd4\u56de\u5b9e\u9a8c\u8bbe\u8ba1\u5305 (\u53ef\u88ab\u4eba/\u8c03\u5ea6\u5668\u5212\u5206\u6cd5)."""
    n_per_arm = required_sample_per_arm(baseline_cvr, mde_relative)
    # \u4e25\u8c28: \u7528 baseline \u4f30\u8ba1\u8fbe\u5230 n \u9700\u8981\u7684\u6837\u672c\u603b\u91cf
    samples_total = n_per_arm * 2
    if daily_conversions <= 0:
        days_needed = max_days
    else:
        # \u8fd9\u91cc daily_conversions \u662f\u4e24\u4e2a arm \u603b\u548c\u7684\u8f6c\u5316\u4e8b\u4ef6
        days_needed = math.ceil(samples_total / daily_conversions)
    runtime_days = min(days_needed, max_days)
    feasible = days_needed <= max_days
    return {
        'name': name,
        'baseline_cvr': baseline_cvr,
        'mde_relative': mde_relative,
        'sample_per_arm_required': n_per_arm,
        'control_ratio': control_ratio,
        'treatment_ratio': round(1 - control_ratio, 2),
        'days_estimated': days_needed,
        'days_committed': runtime_days,
        'feasible': feasible,
        'stop_conditions': {
            'on_negative_drop_pct_below': -negative_guard_drop_pct,
            'on_treatment_n_reached': n_per_arm,
            'on_max_days': max_days,
        },
        'notes': '' if feasible
        else f'\u9700 {days_needed} \u5929 > max_days {max_days}, '
             f'\u8003\u8651\u653e\u5927 mde \u6216\u63d0\u9ad8\u6d41\u91cf',
    }
