#!/usr/bin/env python3
"""Combined three-store daily ops digest (one SMTP email ~11:45).

Pulls scheduler health + light local stats from Main / GrovePop / AquaRides,
renders one HTML email, and sends it through Main's email_sender.

Routine per-task success mails are silenced elsewhere; this is the inbox
summary. Urgent alerts (failure / CRITICAL / login drop / order mismatch /
CRO plunge / approval needed) still send immediately on their own.

    python scripts/daily_ops_digest.py
    python scripts/daily_ops_digest.py --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

STORES = [
    {
        "key": "main",
        "label": "AquaVerve (Main)",
        "root": Path(r"C:\Users\poonx\Dajian_Listing_Tool"),
    },
    {
        "key": "gp",
        "label": "GrovePop",
        "root": Path(r"C:\Users\poonx\GrovePop_Listing_Tool"),
    },
    {
        "key": "aqua",
        "label": "AquaRides",
        "root": Path(r"C:\Users\poonx\AutoParts_Listing_Tool"),
    },
]

# Task names worth surfacing when not clean success.
WATCH_STATUSES = {"failed", "error", "partial_success", "timeout", "running"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _health(root: Path) -> dict:
    path = root / "logs" / "_scheduler_health.json"
    data = _read_json(path) if path.exists() else None
    if not isinstance(data, dict):
        return {"ok": False, "error": f"missing health: {path}", "tasks": {}, "path": str(path)}
    return {
        "ok": True,
        "path": str(path),
        "daemon_pid": data.get("daemon_pid"),
        "daemon_alive_at": data.get("daemon_alive_at"),
        "tasks": data.get("tasks") or {},
        "crashes": data.get("crashes") or [],
        "mi_self_check": data.get("mi_self_check"),
    }


def _today_report_count(root: Path) -> int:
    reports = root / "reports"
    if not reports.exists():
        return 0
    day = datetime.now().strftime("%Y%m%d")
    return len(list(reports.glob(f"daily_report_{day}*.html")))


def _finance_snapshot(root: Path) -> dict:
    """Best-effort today GMV / order count from local finance DB if present."""
    db = root / "ebay_collection.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db))
        try:
            # Table may not exist on every store.
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(finance_orders)").fetchall()
            }
            if not cols:
                return {}
            day = datetime.now().strftime("%Y-%m-%d")
            # Prefer paid/created date columns when available.
            date_col = next(
                (c for c in ("order_date", "paid_date", "creation_date", "created_at") if c in cols),
                None,
            )
            if not date_col:
                return {}
            amt_col = next(
                (c for c in ("total", "order_total", "gmv", "amount") if c in cols),
                None,
            )
            sql = f"SELECT COUNT(*) FROM finance_orders WHERE substr(CAST({date_col} AS TEXT),1,10)=?"
            n = conn.execute(sql, (day,)).fetchone()[0]
            gmv = None
            if amt_col:
                gmv = conn.execute(
                    f"SELECT COALESCE(SUM({amt_col}),0) FROM finance_orders WHERE substr(CAST({date_col} AS TEXT),1,10)=?",
                    (day,),
                ).fetchone()[0]
            return {"orders_today": int(n or 0), "gmv_today": float(gmv or 0) if gmv is not None else None}
        finally:
            conn.close()
    except Exception as exc:
        return {"error": str(exc)}


def _task_rows(tasks: dict) -> list[tuple[str, str, str]]:
    rows = []
    for name, info in sorted(tasks.items()):
        if name.startswith("_"):
            continue
        if not isinstance(info, dict):
            continue
        status = str(info.get("status") or "").lower() or "unknown"
        msg = str(info.get("message") or info.get("error") or "")[:120]
        if status in WATCH_STATUSES or status not in {"success", "skipped", "ok"}:
            rows.append((name, status, msg))
    # Always show a few key tasks even if success for orientation.
    for key in ("daily_tasks", "ops_daily", "listing_audit", "order_recheck", "guard_anomaly", "cro_sentinel"):
        info = tasks.get(key)
        if isinstance(info, dict) and not any(r[0] == key for r in rows):
            rows.append((key, str(info.get("status") or ""), str(info.get("message") or "")[:120]))
    return rows[:18]


def collect_store(store: dict) -> dict:
    root: Path = store["root"]
    health = _health(root)
    return {
        "label": store["label"],
        "root": str(root),
        "exists": root.exists(),
        "health": health,
        "reports_today": _today_report_count(root),
        "finance": _finance_snapshot(root) if root.exists() else {},
        "attention": _task_rows(health.get("tasks") or {}),
    }


def render_html(stores: list[dict], now: datetime) -> str:
    parts = [
        "<html><body style='font-family:Arial,sans-serif;padding:20px;max-width:900px;'>",
        f"<h2>三店运营日报 — {now.strftime('%Y-%m-%d %H:%M')}</h2>",
        "<p style='color:#666;'>例行成功邮件已静默；细节在各店 reports/。本邮件为合并摘要。"
        "紧急项（失败 / CRITICAL / 登录掉线 / 出单源不符 / CRO 跳水 / 需确认 End）仍即时另发。</p>",
    ]
    for s in stores:
        parts.append(f"<h3 style='margin-top:24px;border-bottom:2px solid #1a73e8;padding-bottom:4px;'>{html.escape(s['label'])}</h3>")
        if not s["exists"]:
            parts.append("<p style='color:red;'>店铺目录不存在</p>")
            continue
        h = s["health"]
        if not h.get("ok"):
            parts.append(f"<p style='color:red;'>健康文件不可读: {html.escape(str(h.get('error')))}</p>")
        else:
            parts.append(
                f"<p>daemon pid <b>{html.escape(str(h.get('daemon_pid')))}</b> · "
                f"alive <code>{html.escape(str(h.get('daemon_alive_at') or ''))}</code> · "
                f"本地报告今日 <b>{s['reports_today']}</b> 份</p>"
            )
        fin = s.get("finance") or {}
        if fin.get("orders_today") is not None:
            gmv = fin.get("gmv_today")
            gmv_s = f" · GMV ~${gmv:,.2f}" if isinstance(gmv, (int, float)) else ""
            parts.append(f"<p>今日订单(本地财务表): <b>{fin['orders_today']}</b>{gmv_s}</p>")
        elif fin.get("error"):
            parts.append(f"<p style='color:#999;'>财务快照跳过: {html.escape(str(fin['error'])[:120])}</p>")

        attn = s.get("attention") or []
        if not attn:
            parts.append("<p style='color:#2e7d32;'>关键任务无异常标记</p>")
        else:
            parts.append(
                "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
                "<tr style='background:#f5f5f5;'>"
                "<th style='text-align:left;padding:6px;border:1px solid #ddd;'>任务</th>"
                "<th style='text-align:left;padding:6px;border:1px solid #ddd;'>状态</th>"
                "<th style='text-align:left;padding:6px;border:1px solid #ddd;'>说明</th>"
                "</tr>"
            )
            for name, status, msg in attn:
                color = "#c0392b" if status in {"failed", "error", "timeout"} else (
                    "#e67e22" if status == "partial_success" else "#333"
                )
                parts.append(
                    "<tr>"
                    f"<td style='padding:6px;border:1px solid #ddd;'>{html.escape(name)}</td>"
                    f"<td style='padding:6px;border:1px solid #ddd;color:{color};font-weight:bold;'>{html.escape(status)}</td>"
                    f"<td style='padding:6px;border:1px solid #ddd;color:#555;'>{html.escape(msg)}</td>"
                    "</tr>"
                )
            parts.append("</table>")

    parts.append(
        "<hr style='margin-top:28px;'>"
        "<p style='color:#999;font-size:12px;'>由 Main scripts/daily_ops_digest.py 生成 · "
        "EMAIL_FORCE_ROUTINE=1 可恢复旧版逐任务成功邮件</p>"
        "</body></html>"
    )
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print summary, do not SMTP")
    args = ap.parse_args()

    now = datetime.now()
    stores = [collect_store(s) for s in STORES]
    html_body = render_html(stores, now)

    # Subject: flag if any failed/partial
    bad = []
    for s in stores:
        for name, status, _msg in s.get("attention") or []:
            if status in {"failed", "error", "timeout", "partial_success"}:
                bad.append(f"{s['label']}:{name}={status}")
    if bad:
        subject = f"三店运营日报 {now.strftime('%m-%d')} · 需关注 {len(bad)} 项"
    else:
        subject = f"三店运营日报 {now.strftime('%m-%d')} · 正常"

    out = ROOT / "reports" / f"daily_ops_digest_{now.strftime('%Y%m%d_%H%M%S')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_body, encoding="utf-8")
    print(f"wrote {out}")
    for s in stores:
        print(f"- {s['label']}: reports_today={s['reports_today']} attention={len(s.get('attention') or [])}")

    if args.dry_run:
        print("dry-run: SMTP skipped")
        return 0

    from src.utils.email_sender import send_email

    # Digest itself always SMTPs (this is the intentional daily inbox mail).
    ok = send_email(subject, html_body, send_smtp=True)
    print("smtp", "ok" if ok else "failed-or-local")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
