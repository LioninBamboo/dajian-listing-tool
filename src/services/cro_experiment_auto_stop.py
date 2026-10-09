"""S78 — 实验自动停机.

输入: 实验当前 metrics + designer 提供的 stop_conditions
若触发, 调用 pause_callable(experiment_id) 并写黑名单.
所有 IO 注入避免 sqlite/streamlit 依赖.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

DEFAULT_LOG = Path('logs/cro_experiment_pause.jsonl')


def evaluate_stop(metrics: Dict[str, Any],
                  stop_conditions: Dict[str, Any]) -> Dict[str, Any]:
    """stop_conditions 例: {max_negative_lift_pct: -0.10,
                              max_returns_pct: 0.20,
                              max_loss_usd: 200}.
    任一条件触发即 stop=True."""
    triggered: List[str] = []

    max_neg = stop_conditions.get('max_negative_lift_pct')
    if max_neg is not None and metrics.get('lift_pct') is not None \
            and metrics['lift_pct'] <= max_neg:
        triggered.append(
            f'lift_pct {metrics["lift_pct"]:.4f} ≤ {max_neg}')

    max_ret = stop_conditions.get('max_returns_pct')
    if max_ret is not None and metrics.get('returns_pct') is not None \
            and metrics['returns_pct'] >= max_ret:
        triggered.append(
            f'returns_pct {metrics["returns_pct"]:.4f} ≥ {max_ret}')

    max_loss = stop_conditions.get('max_loss_usd')
    if max_loss is not None and metrics.get('loss_usd') is not None \
            and metrics['loss_usd'] >= max_loss:
        triggered.append(
            f'loss_usd {metrics["loss_usd"]:.2f} ≥ {max_loss}')

    return {
        'stop': bool(triggered),
        'reasons': triggered,
        'metrics': dict(metrics),
        'stop_conditions': dict(stop_conditions),
    }


def stop_experiment(experiment_id: str,
                    reasons: List[str],
                    *,
                    pause_callable: Optional[Callable[[str], bool]] = None,
                    blacklist_writer: Optional[Callable[[str, str], None]]
                    = None,
                    affected_skus: Optional[List[str]] = None,
                    log_path: Path = DEFAULT_LOG,
                    ) -> Dict[str, Any]:
    paused = False
    if pause_callable is not None:
        try:
            paused = bool(pause_callable(experiment_id))
        except Exception:
            paused = False
    blacklisted: List[str] = []
    if blacklist_writer is not None and affected_skus:
        for sku in affected_skus:
            try:
                blacklist_writer(sku, f'experiment {experiment_id} stopped')
                blacklisted.append(sku)
            except Exception:
                continue
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'experiment_id': experiment_id,
        'reasons': reasons,
        'paused': paused,
        'blacklisted_skus': blacklisted,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return record


def auto_stop_experiments(experiments: Iterable[Dict[str, Any]],
                          *,
                          pause_callable: Optional[Callable[[str], bool]]
                          = None,
                          blacklist_writer: Optional[Callable[[str, str],
                                                              None]] = None,
                          log_path: Path = DEFAULT_LOG,
                          ) -> Dict[str, Any]:
    """experiments: [{experiment_id, metrics, stop_conditions, skus}]"""
    stopped = []
    inspected = 0
    for exp in experiments:
        inspected += 1
        decision = evaluate_stop(exp.get('metrics') or {},
                                 exp.get('stop_conditions') or {})
        if decision['stop']:
            rec = stop_experiment(
                exp.get('experiment_id', 'unknown'),
                decision['reasons'],
                pause_callable=pause_callable,
                blacklist_writer=blacklist_writer,
                affected_skus=exp.get('skus'),
                log_path=log_path,
            )
            stopped.append(rec)
    return {'inspected': inspected, 'stopped_count': len(stopped),
            'stopped': stopped}
