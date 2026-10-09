"""SKU 广告黑名单 (P4, 2026-05).

`logs/ad_blacklist.json` 结构::

    {
        "version": 1,
        "skus": {
            "SKU-AAA": {
                "added_at": "2026-05-04T12:00:00",
                "reason": "auto_off_streak",       # 或 manual / unsafe_streak
                "off_count": 3,                    # 触发计数
                "last_off_at": "2026-05-04T11:55:00",
                "note": "..."
            }
        }
    }

被 `EbayAdService.create_ad_safe` 在最前一步检查;
被 `repricing_guard._try_disable_ad` 每次成功关广告后调 `bump_off_count` 累计.

3 次以内: 仅记录;  达到 `AUTO_BLACKLIST_THRESHOLD` (=3) → reason 升级为
`auto_off_streak`, 后续 `is_blacklisted` 返回 True.

CLI helper: `python -m src.services.ad_blacklist list|add|remove SKU`
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / 'logs' / 'ad_blacklist.json'

AUTO_BLACKLIST_THRESHOLD = 3  # 累计被关广告 >=3 次, 自动入黑名单
AUTO_REMOVE_SAFE_DAYS = 7     # 连续 7 天现价撑得起 5% → 自动移出 (仅 auto_off_streak)
_LOCK = threading.Lock()


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {'version': 1, 'skus': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if 'skus' not in data:
            data['skus'] = {}
        return data
    except Exception as exc:
        logger.warning(f"读取广告黑名单失败 ({exc}), 使用空表")
        return {'version': 1, 'skus': {}}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def is_blacklisted(sku: str, path: Optional[Path] = None) -> bool:
    """SKU 是否在永不开广告名单 (含自动累计达到阈值)."""
    if not sku:
        return False
    p = Path(path) if path else DEFAULT_PATH
    data = _load(p)
    entry = data['skus'].get(sku)
    if not entry:
        return False
    reason = entry.get('reason')
    if reason in ('manual', 'auto_off_streak', 'unsafe_streak'):
        return True
    # off_count 已达阈值但 reason 未升级 (旧记录) → 视为黑名单
    return int(entry.get('off_count') or 0) >= AUTO_BLACKLIST_THRESHOLD


def get_entry(sku: str, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = Path(path) if path else DEFAULT_PATH
    return _load(p)['skus'].get(sku)


def add_manual(sku: str, note: str = '', path: Optional[Path] = None) -> Dict[str, Any]:
    """手动加入黑名单. 反复调用 idempotent."""
    p = Path(path) if path else DEFAULT_PATH
    with _LOCK:
        data = _load(p)
        entry = data['skus'].get(sku, {})
        entry.update({
            'added_at': entry.get('added_at') or datetime.now().isoformat(),
            'reason': 'manual',
            'note': note,
            'off_count': entry.get('off_count', 0),
            'updated_at': datetime.now().isoformat(),
        })
        data['skus'][sku] = entry
        _save(p, data)
        logger.info(f"📛 SKU {sku} 已加入广告黑名单 (manual): {note}")
        return entry


def remove(sku: str, path: Optional[Path] = None) -> bool:
    p = Path(path) if path else DEFAULT_PATH
    with _LOCK:
        data = _load(p)
        if sku not in data['skus']:
            return False
        del data['skus'][sku]
        _save(p, data)
        logger.info(f"✅ SKU {sku} 已移出广告黑名单")
        return True


def bump_off_count(sku: str, *, reason_hint: str = 'auto_off',
                   path: Optional[Path] = None) -> Dict[str, Any]:
    """守门员每次成功关广告后调用, 累计 off_count.

    达到 AUTO_BLACKLIST_THRESHOLD 时 reason 升级为 auto_off_streak.
    返回更新后的 entry.
    """
    if not sku:
        return {}
    p = Path(path) if path else DEFAULT_PATH
    with _LOCK:
        data = _load(p)
        entry = data['skus'].get(sku) or {
            'added_at': datetime.now().isoformat(),
            'reason': 'tracking',
            'off_count': 0,
        }
        # manual 黑名单不被自动覆盖
        if entry.get('reason') == 'manual':
            entry['off_count'] = int(entry.get('off_count', 0)) + 1
            entry['last_off_at'] = datetime.now().isoformat()
            data['skus'][sku] = entry
            _save(p, data)
            return entry

        entry['off_count'] = int(entry.get('off_count', 0)) + 1
        entry['last_off_at'] = datetime.now().isoformat()
        entry['updated_at'] = datetime.now().isoformat()
        if entry['off_count'] >= AUTO_BLACKLIST_THRESHOLD:
            if entry.get('reason') != 'auto_off_streak':
                logger.warning(
                    f"📛 SKU {sku} 累计被关广告 {entry['off_count']} 次 → "
                    f"自动加入广告黑名单 (auto_off_streak)"
                )
            entry['reason'] = 'auto_off_streak'
        else:
            entry['reason'] = entry.get('reason') or 'tracking'
        data['skus'][sku] = entry
        _save(p, data)
        return entry


def list_all(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    p = Path(path) if path else DEFAULT_PATH
    return dict(_load(p)['skus'])


def mark_safe_check(sku: str, is_safe: bool, *,
                    path: Optional[Path] = None) -> Dict[str, Any]:
    """P7: 累计连续安全天数. 被 auto_blacklist_cleanup 调.

    is_safe=True  → safe_streak += 1
    is_safe=False → safe_streak = 0 (重置)

    达到 AUTO_REMOVE_SAFE_DAYS 时不自动 remove (留给调用方决定),
    但 entry['safe_streak'] 字段反映实时计数.
    manual reason 永远不被影响.
    """
    if not sku:
        return {}
    p = Path(path) if path else DEFAULT_PATH
    with _LOCK:
        data = _load(p)
        entry = data['skus'].get(sku)
        if not entry:
            return {}
        if entry.get('reason') == 'manual':
            return entry  # manual 不参与 auto cleanup
        entry['safe_streak'] = (int(entry.get('safe_streak', 0)) + 1) if is_safe else 0
        entry['last_safe_check_at'] = datetime.now().isoformat()
        data['skus'][sku] = entry
        _save(p, data)
        return entry


def auto_remove_if_recovered(sku: str, *, threshold: int = AUTO_REMOVE_SAFE_DAYS,
                              path: Optional[Path] = None) -> bool:
    """若 SKU 是 auto_off_streak 且 safe_streak >= threshold → 移出.

    Returns: 是否移出.
    """
    if not sku:
        return False
    p = Path(path) if path else DEFAULT_PATH
    with _LOCK:
        data = _load(p)
        entry = data['skus'].get(sku)
        if not entry:
            return False
        if entry.get('reason') != 'auto_off_streak':
            return False  # 仅清理自动入名单的
        if int(entry.get('safe_streak', 0)) < threshold:
            return False
        del data['skus'][sku]
        _save(p, data)
        logger.info(f"♻️ SKU {sku} 连续 {threshold} 天安全 → 自动移出广告黑名单")
        return True


# ─── CLI ────────────────────────────────────────────────────────────
def _cli(argv):
    if len(argv) < 2 or argv[1] in ('-h', '--help'):
        print("用法: python -m src.services.ad_blacklist list|add SKU [note]|remove SKU")
        return 0
    cmd = argv[1]
    if cmd == 'list':
        for sku, e in sorted(list_all().items()):
            print(f"{sku}\treason={e.get('reason')}\toff_count={e.get('off_count')}\t"
                  f"added={e.get('added_at','')[:19]}\t{e.get('note','')}")
        return 0
    if cmd == 'add' and len(argv) >= 3:
        add_manual(argv[2], note=' '.join(argv[3:]))
        return 0
    if cmd == 'remove' and len(argv) >= 3:
        ok = remove(argv[2])
        print('removed' if ok else 'not found')
        return 0 if ok else 1
    print("未知命令")
    return 2


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(_cli(sys.argv))
