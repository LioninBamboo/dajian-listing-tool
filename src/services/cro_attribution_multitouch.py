"""S92 — 多触点归因.

每条 conversion 输入: {conversion_id, value, touchpoints:[{channel, ts}]}.
输出: 各 channel 的归因贡献 (按模型).
模型: last/first/linear/time_decay (半衰期 7 天).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_HALF_LIFE_DAYS = 7.0


def _parse_ts(ts: Any) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            # tolerate trailing Z
            return datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


def _weights_last(touches: List[Dict[str, Any]]) -> List[float]:
    if not touches:
        return []
    w = [0.0] * len(touches)
    w[-1] = 1.0
    return w


def _weights_first(touches: List[Dict[str, Any]]) -> List[float]:
    if not touches:
        return []
    w = [0.0] * len(touches)
    w[0] = 1.0
    return w


def _weights_linear(touches: List[Dict[str, Any]]) -> List[float]:
    n = len(touches)
    return [1.0 / n] * n if n else []


def _weights_time_decay(touches: List[Dict[str, Any]],
                        half_life_days: float) -> List[float]:
    if not touches:
        return []
    last = _parse_ts(touches[-1].get('ts'))
    if last is None:
        return _weights_linear(touches)
    raw: List[float] = []
    for t in touches:
        cur = _parse_ts(t.get('ts'))
        if cur is None:
            raw.append(0.0)
            continue
        days_back = max(0.0, (last - cur).total_seconds() / 86400.0)
        raw.append(0.5 ** (days_back / half_life_days))
    total = sum(raw)
    if total <= 0:
        return _weights_linear(touches)
    return [r / total for r in raw]


WEIGHT_FNS = {
    'last': lambda touches, hl: _weights_last(touches),
    'first': lambda touches, hl: _weights_first(touches),
    'linear': lambda touches, hl: _weights_linear(touches),
    'time_decay': _weights_time_decay,
}


def attribute(conversions: Iterable[Dict[str, Any]],
              *,
              model: str = 'last',
              half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
              ) -> Dict[str, Any]:
    if model not in WEIGHT_FNS:
        raise ValueError(f'unknown model: {model}')
    by_channel: Dict[str, float] = {}
    total_value = 0.0
    n_conv = 0
    for c in conversions:
        touches = list(c.get('touchpoints') or [])
        if not touches:
            continue
        n_conv += 1
        value = float(c.get('value', 0) or 0)
        total_value += value
        weights = WEIGHT_FNS[model](touches, half_life_days)
        for t, w in zip(touches, weights):
            channel = t.get('channel') or 'unknown'
            by_channel[channel] = by_channel.get(channel, 0.0) + value * w
    by_channel = {k: round(v, 6) for k, v in by_channel.items()}
    return {
        'model': model,
        'conversions_attributed': n_conv,
        'total_value': round(total_value, 6),
        'by_channel': by_channel,
        'top_channel': (max(by_channel, key=by_channel.get)
                        if by_channel else None),
    }


def compare_models(conversions: Iterable[Dict[str, Any]],
                   *,
                   models: Iterable[str] = ('last', 'first', 'linear',
                                            'time_decay'),
                   half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
                   ) -> Dict[str, Any]:
    convs = list(conversions)
    return {m: attribute(convs, model=m, half_life_days=half_life_days)
            for m in models}
