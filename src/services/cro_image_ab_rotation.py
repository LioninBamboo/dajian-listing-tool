"""S47 — 主图 A/B 自动轮换调度.

为每个 SKU 在 N 张候选主图之间轮换 (默认 7 天一轮), 复用 S26 cohort 桶。
不直接调 eBay revise — 只决定 "本周该用哪张图"; 实际 revise 由 cro_image_refresh.py 完成。

存储格式 (logs/cro_image_rotation.jsonl, 每行一条):
  {sku, started_at, candidates: [url1, url2, ...], history: [{idx, week, sold, views}], current_idx}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

ROTATION_DAYS = 7
MIN_VIEWS_TO_JUDGE = 100


def _load_state(path: Path) -> Dict[str, Dict[str, Any]]:
    state: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return state
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            state[row['sku']] = row
        except (json.JSONDecodeError, KeyError):
            continue
    return state


def _save_state(path: Path, state: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(v, ensure_ascii=False, sort_keys=True)
             for v in state.values()]
    path.write_text('\n'.join(lines) + ('\n' if lines else ''),
                    encoding='utf-8')


def init_rotation(sku: str, candidates: List[str], state: Dict[str, Any],
                  week_now: int) -> Dict[str, Any]:
    if sku not in state:
        state[sku] = {
            'sku': sku,
            'candidates': candidates,
            'history': [],
            'current_idx': 0,
            'started_week': week_now,
        }
    return state[sku]


def record_week_result(sku: str, week: int, idx: int, sold: int, views: int,
                       state: Dict[str, Any]) -> None:
    if sku not in state:
        return
    state[sku].setdefault('history', []).append({
        'week': week, 'idx': idx, 'sold': sold, 'views': views,
    })


def pick_next_idx(rec: Dict[str, Any]) -> int:
    """全部候选都跑过一轮 → 选 conversion 最高的那张作为常驻; 否则继续轮换。"""
    n = len(rec['candidates'])
    history = rec.get('history', [])
    tested_idxs = {h['idx'] for h in history
                   if h.get('views', 0) >= MIN_VIEWS_TO_JUDGE}
    if len(tested_idxs) < n:
        # 还有图没测够样本 → 继续选下一张未测够的
        for i in range(n):
            if i not in tested_idxs:
                return i
        return rec['current_idx']
    # 全部测过 → 按 sold/views 排
    by_idx: Dict[int, Dict[str, int]] = {}
    for h in history:
        d = by_idx.setdefault(h['idx'], {'sold': 0, 'views': 0})
        d['sold'] += h.get('sold', 0)
        d['views'] += h.get('views', 0)
    best = max(by_idx.items(),
               key=lambda kv: (kv[1]['sold'] / max(kv[1]['views'], 1)))
    return best[0]


def schedule_rotation(sku: str, candidates: List[str], week_now: int,
                      last_week_metrics: Optional[Dict[str, int]] = None,
                      path: Optional[Path] = None,
                      ) -> Dict[str, Any]:
    """主入口: 喂入候选图 + 上周指标, 返回本周该用的 url + idx."""
    if path is None:
        path = Path('logs/cro_image_rotation.jsonl')
    state = _load_state(path)
    rec = init_rotation(sku, candidates, state, week_now)
    if last_week_metrics is not None:
        record_week_result(sku, week_now - 1, rec['current_idx'],
                           last_week_metrics.get('sold', 0),
                           last_week_metrics.get('views', 0),
                           state)
    next_idx = pick_next_idx(rec)
    rec['current_idx'] = next_idx
    _save_state(path, state)
    return {
        'sku': sku,
        'idx': next_idx,
        'image_url': candidates[next_idx],
        'history_len': len(rec.get('history', [])),
    }
