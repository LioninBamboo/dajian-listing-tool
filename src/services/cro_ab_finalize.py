"""S133 — A/B 实验收口仪式.

输入: 实验 cohort_health 结果 + 显著性检验; 输出 finalize_decision:
  - 'promote_winner' (winner→100%, control→archive)
  - 'rollback_to_control'
  - 'continue' (尚未显著)
  - 'inconclusive' (样本不够 + 接近 max_runtime → 强制收口为 control)
"""
from __future__ import annotations

import json
import logging
import math
import os
from statistics import NormalDist
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MIN_SAMPLE_PER_ARM = 200
DEFAULT_ALPHA = 0.05
Z_95 = 1.96


def _z_threshold(alpha: float) -> float:
    if alpha <= 0 or alpha >= 1:
        return Z_95
    return NormalDist().inv_cdf(1 - alpha / 2)


def _two_proportion_z(n1: int, x1: int, n2: int, x2: int
                       ) -> Optional[float]:
    if n1 <= 0 or n2 <= 0:
        return None
    p1 = x1 / n1
    p2 = x2 / n2
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    return (p2 - p1) / se


def evaluate_significance(control: Dict[str, int],
                            treatment: Dict[str, int],
                            *, alpha: float = DEFAULT_ALPHA
                            ) -> Dict[str, Any]:
    n1 = int(control.get('exposures', 0) or 0)
    x1 = int(control.get('conversions', 0) or 0)
    n2 = int(treatment.get('exposures', 0) or 0)
    x2 = int(treatment.get('conversions', 0) or 0)
    z = _two_proportion_z(n1, x1, n2, x2)
    if z is None:
        return {'significant': False, 'z': None, 'lift': None,
                'reason': 'insufficient_data'}
    p1 = x1 / n1
    p2 = x2 / n2
    if p1 == 0:
        lift = float('inf') if p2 > 0 else 0.0
    else:
        lift = (p2 - p1) / p1
    z_threshold = _z_threshold(alpha)
    return {'significant': abs(z) >= z_threshold,
             'z': z, 'lift': lift,
             'p_control': p1, 'p_treatment': p2,
             'alpha': alpha,
             'z_threshold': z_threshold,
             'direction': 'positive' if z > 0 else 'negative'}


def finalize_experiment(
    *,
    experiment_id: str,
    control: Dict[str, int],
    treatment: Dict[str, int],
    runtime_days: int,
    max_runtime_days: int = 28,
    min_sample_per_arm: int = DEFAULT_MIN_SAMPLE_PER_ARM,
    alpha: float = DEFAULT_ALPHA,
) -> Dict[str, Any]:
    n1 = int(control.get('exposures', 0) or 0)
    n2 = int(treatment.get('exposures', 0) or 0)
    enough = n1 >= min_sample_per_arm and n2 >= min_sample_per_arm

    sig = evaluate_significance(control, treatment, alpha=alpha)

    if enough and sig['significant']:
        if sig['direction'] == 'positive':
            decision = 'promote_winner'
        else:
            decision = 'rollback_to_control'
    elif runtime_days >= max_runtime_days:
        # 超过最大运行时长仍不显著 → 安全 fallback control
        decision = 'inconclusive_fallback_control'
    else:
        decision = 'continue'

    return {
        'experiment_id': experiment_id,
        'decision': decision,
        'sample_ok': enough,
        'min_sample_per_arm': min_sample_per_arm,
        'runtime_days': runtime_days,
        'significance': sig,
    }


def archive_experiment(experiment_id: str,
                        decision: Dict[str, Any],
                        log_path: str) -> bool:
    if not log_path:
        return False
    try:
        os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({'ts': datetime.now(UTC).replace(tzinfo=None).isoformat(),
                                  'experiment_id': experiment_id,
                                  **decision},
                                ensure_ascii=False, default=str) + '\n')
        return True
    except Exception:
        logger.warning('archive failed', exc_info=True)
        return False
