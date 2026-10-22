#!/usr/bin/env python3
"""Non-ad store-marketing runner + single digest email.

Runs the non-advertising marketing automations for the ambient store (smart
reprice, MI snapshot, title optimization) one after another, captures each
outcome, and sends ONE consolidated email saying whether each ran — so the
operator can see at a glance if the marketing cron fired, without hunting
through per-task mails. Ads are intentionally excluded.

    python scripts/marketing_ops_digest.py            # run all + email digest
    python scripts/marketing_ops_digest.py --dry-run  # list what would run
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.utils.smart_reprice_schedule import (
    should_run_smart_reprice,
    smart_reprice_skip_reason,
)

PY = sys.executable

# (label, argv, enabled). Ads deliberately omitted.
# Title work is cro_title_rewrite (ADR-002), not the paused daily_optimize path.
JOBS = [
    ("Smart Price 智能改价", [str(ROOT / "scripts" / "batch_smart_reprice.py"), "--apply", "--email"], True),
    ("财务订单同步", [str(ROOT / "scripts" / "finance_sync_orders.py"), "--days", "30"], True),
    ("MI 市场情报快照", [str(ROOT / "daily_tasks.py"), "--mi-only"], True),
]


def latest_mi_ready_skus(reports_dir: Path, limit: int = 10) -> list[str]:
    from src.utils.mi_opportunity_flow import lookup_supplier_stock, select_mi_auto_publish_skus
    from src.utils.store_profile import get_store_profile

    today = datetime.now().strftime("%Y%m%d")
    snapshots = sorted(
        Path(reports_dir).glob(f"mi_opportunities_{today}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not snapshots:
        return []
    try:
        payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    opportunities = payload if isinstance(payload, list) else (payload.get("opportunities") or [])
    store_kind = getattr(get_store_profile(), "store_kind", "furniture")
    return select_mi_auto_publish_skus(
        opportunities,
        limit=limit,
        store_kind=store_kind,
        stock_lookup=lookup_supplier_stock,
    )


def build_title_rewrite_argv(root: Path | None = None) -> tuple[list[str] | None, str]:
    """Same CRO title path as the main-store 10:45 scheduler job."""
    project_root = Path(root) if root is not None else ROOT
    apply_enabled = os.getenv("ENABLE_SCHEDULED_TITLE_REWRITE_APPLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    limit_env = (
        os.getenv("SCHEDULED_TITLE_REWRITE_APPLY_LIMIT", "50")
        if apply_enabled
        else os.getenv("SCHEDULED_TITLE_REWRITE_DRY_RUN_LIMIT", "50")
    )
    try:
        limit = max(1, min(50, int(limit_env)))
    except (TypeError, ValueError):
        limit = 50
    argv = [
        str(project_root / "scripts" / "cro_title_rewrite.py"),
        "--limit",
        str(limit),
        "--email",
    ]
    if apply_enabled:
        argv.extend(["--apply", "--yes"])
    return argv, ""


def build_mi_auto_publish_argv(root: Path | None = None) -> tuple[list[str] | None, str]:
    project_root = Path(root) if root is not None else ROOT
    enable = os.getenv("ENABLE_MI_AUTO_PUBLISH", "").strip().lower() in ("1", "true", "yes", "on")
    try:
        limit = max(1, int(os.getenv("MI_AUTO_PUBLISH_LIMIT", "10")))
    except ValueError:
        limit = 10
    skus = latest_mi_ready_skus(project_root / "reports", limit)
    if not skus:
        return None, "没有过门槛的 READY MI SKU（推荐分/店定位/库存）"
    argv = [
        str(project_root / "batch_publish.py"),
        "--limit",
        str(limit),
        "--sku-list",
        ",".join(skus),
    ]
    if not enable:
        argv.append("--dry-run")
    return argv, ""


def _run(argv, timeout):
    t0 = time.monotonic()
    try:
        p = subprocess.run([PY, *argv], cwd=str(ROOT), capture_output=True,
                            text=True, timeout=timeout, encoding="utf-8", errors="replace")
        ok = p.returncode == 0
        tail = (p.stdout or "")[-400:]
        return ok, tail, round(time.monotonic() - t0, 1)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT", round(time.monotonic() - t0, 1)
    except Exception as e:  # noqa: BLE001
        return False, f"EXC: {e}", round(time.monotonic() - t0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    from src.utils.store_profile import get_store_profile
    brand = getattr(get_store_profile(), "brand_name", "Store")

    results = []
    reprice_skip = smart_reprice_skip_reason()
    for label, argv, enabled in JOBS:
        if label.startswith("Smart Price") and not should_run_smart_reprice():
            results.append((label, "SKIPPED", reprice_skip, 0.0))
            continue
        if not enabled:
            results.append((label, "PAUSED", "已停用（待幻觉审计）", 0.0))
            continue
        if args.dry_run:
            results.append((label, "DRY", " ".join(argv), 0.0))
            continue
        ok, tail, dur = _run(argv, args.timeout)
        results.append((label, "OK" if ok else "FAIL", tail, dur))

    title_argv, title_skip = build_title_rewrite_argv()
    if title_skip:
        results.append(("CRO 标题热词优化", "SKIPPED", title_skip, 0.0))
    elif args.dry_run:
        results.append(("CRO 标题热词优化", "DRY", " ".join(title_argv or []), 0.0))
    else:
        ok, tail, dur = _run(title_argv or [], args.timeout)
        results.append(("CRO 标题热词优化", "OK" if ok else "FAIL", tail, dur))

    pub_argv, pub_skip = build_mi_auto_publish_argv()
    if pub_skip:
        results.append(("MI 自动刊登", "SKIPPED", pub_skip, 0.0))
    elif args.dry_run:
        results.append(("MI 自动刊登", "DRY", " ".join(pub_argv or []), 0.0))
    else:
        ok, tail, dur = _run(pub_argv or [], args.timeout)
        results.append(("MI 自动刊登", "OK" if ok else "FAIL", tail, dur))

    icon = {"OK": "✅", "FAIL": "❌", "PAUSED": "⏸️", "SKIPPED": "⏭️", "DRY": "🔎"}
    n_ok = sum(1 for r in results if r[1] == "OK")
    n_fail = sum(1 for r in results if r[1] == "FAIL")
    rows = "".join(
        f'<tr><td style="padding:8px;border-bottom:1px solid #eee">{icon.get(st,"")} {label}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;font-weight:600">{st}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;color:#888">{dur}s</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;color:#888;font-size:12px">'
        f'<code>{(tail or "")[-160:]}</code></td></tr>'
        for label, st, tail, dur in results
    )
    banner = "#c0392b" if n_fail else "#27ae60"
    head = f"{brand} 营销任务日报：{n_ok} 成功 · {n_fail} 失败"
    html = f"""
    <h2>{brand} 店铺营销定时任务执行情况（不含广告）</h2>
    <div style="background:{banner};color:#fff;padding:12px 16px;border-radius:6px;font-weight:700;font-size:16px;margin:8px 0 12px">{head}</div>
    <table style="border-collapse:collapse;width:100%">
      <thead><tr>
        <th align="left" style="padding:8px;border-bottom:2px solid #999">任务</th>
        <th align="left" style="padding:8px;border-bottom:2px solid #999">状态</th>
        <th align="left" style="padding:8px;border-bottom:2px solid #999">耗时</th>
        <th align="left" style="padding:8px;border-bottom:2px solid #999">输出末尾</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="color:#999;font-size:12px">来自 marketing_ops_digest.py</p>
    """
    try:
        from src.services.finance_orders import render_finance_email_html

        html += render_finance_email_html(range_key="30d")
    except Exception as exc:  # noqa: BLE001
        html += f'<h3>💰 财务摘要</h3><p style="color:red;">⚠️ 渲染失败: {exc}</p>'
    if args.dry_run:
        print("\n".join(f"{st} {label}: {tail}" for label, st, tail, _ in results))
        return
    from src.utils.email_sender import send_email
    subject = f"📣 {brand} 营销任务日报 - {n_ok} 成功" + (f" / {n_fail} 失败" if n_fail else "")
    send_email(subject, html)
    print(f"digest sent: {n_ok} ok, {n_fail} fail")


if __name__ == "__main__":
    main()
