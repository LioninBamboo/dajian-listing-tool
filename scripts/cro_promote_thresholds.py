"""S21 — CRO 阈值 promote: pending → 生产, shadow 守门.

用法:
  python scripts/cro_promote_thresholds.py --apply
  python scripts/cro_promote_thresholds.py --apply --email
  python scripts/cro_promote_thresholds.py --force          # 跳过 shadow 守门 (人工已审核)

调度: 周日 02:30 (在 02:00 task_cro_learn_pending 之后跑).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_thresholds import promote_thresholds  # noqa: E402


def _load_products_and_market():
    from src.web.pages.competition_monitor import (
        load_products_from_db, load_performance_data,
        merge_performance_into_products, get_market_data_from_report,
    )
    products = load_products_from_db() or []
    perf = load_performance_data(force_refresh=False)
    merge_performance_into_products(products, perf or {})
    for p in products:
        p['images'] = [p['image_url']] if p.get('image_url') else []
    market_data = get_market_data_from_report(None)
    return products, market_data


def _send_email(rep: dict) -> None:
    try:
        from src.services.email_notifier import send_email
    except Exception:
        return
    n = rep.get('promoted', 0)
    blocked = rep.get('blocked')
    pending = rep.get('pending_count', 0)
    subject = (f"🚫 CRO 阈值 promote 阻断 / 待审核 {pending} 个品类"
               if blocked else f"✅ CRO 阈值 promote {n} 个品类")
    shadow = rep.get('shadow') or {}
    body = (
        f"<h3>{subject}</h3>"
        f"<ul>"
        f"<li>pending: {pending}</li>"
        f"<li>promoted: {n}</li>"
        f"<li>blocked: {blocked}</li>"
        f"<li>p1_delta: {shadow.get('p1_delta')}</li>"
        f"<li>avg_score_delta: {shadow.get('avg_score_delta')}</li>"
        f"<li>explosions: {len(shadow.get('explosions') or [])}</li>"
        f"<li>报告: {rep.get('report_path')}</li>"
        f"</ul>"
    )
    try:
        send_email(subject, body)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='实际写入生产表 (默认 dry-run)')
    ap.add_argument('--force', action='store_true',
                    help='跳过 shadow 守门 (人工已审核)')
    ap.add_argument('--email', action='store_true')
    args = ap.parse_args()

    products, market_data = _load_products_and_market()
    if not args.apply:
        # dry-run: 只跑 shadow + 写报告, 不动生产
        from src.services.cro_thresholds import (
            load_pending_thresholds, load_thresholds,
        )
        from scripts.cro_threshold_shadow import shadow_compare
        pending = load_pending_thresholds()
        current = load_thresholds()
        shadow = shadow_compare(products, market_data or {}, current, pending) if pending else None
        rep = {'promoted': 0, 'blocked': bool(shadow and not shadow.get('safe_to_promote')),
               'pending_count': len(pending), 'shadow': shadow,
               'note': 'dry-run; 加 --apply 真正写库'}
    else:
        rep = promote_thresholds(products, market_data,
                                 safe_only=not args.force)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.apply and args.email:
        _send_email(rep)
    return 0 if not rep.get('blocked') else 2


if __name__ == '__main__':
    raise SystemExit(main())
