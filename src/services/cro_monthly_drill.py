"""S64 — 月初自动跑 cro_dryrun_drill 并在表现下滑时告警.

应在 scheduler_daemon 中每天调用 should_run_today; True 则 run_monthly_drill。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.services.cro_dryrun_drill import simulate_drill

DEFAULT_REPORT = Path('logs/cro_monthly_drill.txt')
DEFAULT_HISTORY = Path('logs/cro_monthly_drill_history.jsonl')
DEFAULT_THRESHOLD = 0.10  # avg_reward 月环比下滑超过 10% → 告警


def should_run_today(today: Optional[date] = None) -> bool:
    today = today or date.today()
    return today.day == 1


def _last_avg_reward(history_path: Path) -> Optional[float]:
    if not history_path.exists():
        return None
    last = None
    for line in history_path.read_text(encoding='utf-8').splitlines():
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    if not last:
        return None
    return last.get('avg_reward')


def _append_history(history_path: Path, record: Dict[str, Any]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True)
                + '\n')


def run_monthly_drill(*,
                      alerter: Optional[Callable[[str, str], None]] = None,
                      threshold: float = DEFAULT_THRESHOLD,
                      report_path: Path = DEFAULT_REPORT,
                      history_path: Path = DEFAULT_HISTORY,
                      simulator: Callable[..., Dict[str, Any]] = simulate_drill,
                      seed: int = 42,
                      ) -> Dict[str, Any]:
    """执行 30 天 drill, 记录历史, 必要时告警."""
    result = simulator(days=30, seed=seed)
    avg_reward = (result.get('total_reward', 0.0)
                  / max(1, result.get('days', 30) * 1))
    prev = _last_avg_reward(history_path)
    delta = None
    alerted = False
    if prev is not None and prev > 0:
        delta = (avg_reward - prev) / prev
        if delta < -threshold and alerter:
            try:
                alerter(
                    'CRO drill 月度回归下滑',
                    f'avg_reward {prev:.3f} → {avg_reward:.3f} '
                    f'(Δ {delta * 100:+.1f}%)',
                )
                alerted = True
            except Exception:
                alerted = False
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'avg_reward': round(avg_reward, 4),
        'prev_avg_reward': prev,
        'delta_pct': round(delta * 100, 2) if delta is not None else None,
        'alerted': alerted,
        'best_action': result.get('final_best_action'),
        'traffic_light': result.get('traffic_light'),
    }
    _append_history(history_path, record)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({'result': result, 'record': record},
                   ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return record
