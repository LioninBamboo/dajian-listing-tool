#!/usr/bin/env python3
"""Fitment (车型适用性) audit for published Motors listings.

Read-only by default: for every published auto listing, fetch the live
ItemCompatibilityList (GetItem) and run check_fitment_qc against the title.
Reports MISSING / MISMATCH / SUSPECT issues, prioritising CRITICAL/HIGH.

Writes logs/fitment_audit_YYYYMMDDTHHMMSSZ.json always.

--apply : for vehicle-specific listings that are MISSING fitment, build a
          Year-Make-Model ItemCompatibilityList from the title and push it via
          ReviseFixedPriceItem, then re-GetItem to verify. Never auto-fixes
          mismatch (human review).
--email : send the report (CRITICAL/HIGH focused) with applied / residual /
          mismatch sections.
--limit : cap listings processed (for capped apply jobs).

    python scripts/fitment_audit.py                 # report only
    python scripts/fitment_audit.py --email
    python scripts/fitment_audit.py --apply --limit 20 --email
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()
import requests

from src.services.ebay_auth import EbayOAuthService
from src.services.fitment_qc import (
    build_compatible_products,
    check_fitment_qc,
    classify_fitment,
)
from src.services.motors_trading import format_compatibility_list

SITEID = os.getenv("EBAY_SITE_ID", "100")
API = "https://api.ebay.com/ws/api.dll"
ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


def _hdr(call):
    return {
        "X-EBAY-API-CALL-NAME": call,
        "X-EBAY-API-SITEID": SITEID,
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
        "Content-Type": "text/xml",
    }


def get_live_compat(tok, item_id):
    xml = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>"
        f"<ItemID>{item_id}</ItemID>"
        f"<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>"
        f"</GetItemRequest>"
    )
    r = requests.post(API, data=xml.encode("utf-8"), headers=_hdr("GetItem"), timeout=30)
    entries = []
    for block in re.findall(r"<Compatibility>(.*?)</Compatibility>", r.text, re.S):
        props = [
            {"name": n, "value": v}
            for n, v in re.findall(r"<Name>(.*?)</Name>\s*<Value>(.*?)</Value>", block, re.S)
        ]
        entries.append({"compatibilityProperties": props})
    return entries


def push_compat(tok, item_id, compatible_products):
    compat_xml = format_compatibility_list(compatible_products)
    if "<Compatibility>" not in compat_xml:
        return False, "no compatible products built"
    xml = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>"
        f"<Item><ItemID>{item_id}</ItemID>"
        f"<ProductListingDetails><UPC>Does Not Apply</UPC></ProductListingDetails>"
        f"<ItemSpecifics><NameValueList><Name>Brand</Name><Value>AquaRides</Value></NameValueList>"
        f"<NameValueList><Name>Manufacturer Part Number</Name>"
        f"<Value>Does Not Apply</Value></NameValueList></ItemSpecifics>"
        f"{compat_xml}</Item></ReviseFixedPriceItemRequest>"
    )
    r = requests.post(
        API, data=xml.encode("utf-8"), headers=_hdr("ReviseFixedPriceItem"), timeout=40
    )
    ack = re.search(r"<Ack>(\w+)</Ack>", r.text)
    if ack and ack.group(1) in ("Success", "Warning"):
        return True, ""
    m = re.search(r"<LongMessage>(.*?)</LongMessage>", r.text)
    return False, (m.group(1)[:160] if m else r.text[:160])


def _fitment_process_exit_code(*, apply: bool, apply_fail: int) -> int:
    """Scheduled Task exit code: only apply write failures are non-zero.

    Residual HIGH/mismatch findings stay in the JSON/email report and must not
    mark the Windows job as failed when apply_fail==0.
    """
    if apply and int(apply_fail or 0) > 0:
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="push YMM fitment for MISSING vehicle-specific listings only",
    )
    ap.add_argument("--email", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max listings to process (0=all)")
    ap.add_argument(
        "--sku",
        action="append",
        default=[],
        help="only process selected SKU(s); repeatable",
    )
    args = ap.parse_args()

    db = sqlite3.connect(str(ROOT / "ebay_collection.db"))
    db.row_factory = sqlite3.Row
    selected_skus = [str(value).strip() for value in (args.sku or []) if str(value).strip()]
    if selected_skus:
        placeholders = ",".join("?" for _ in selected_skus)
        rows = db.execute(
            "SELECT sku, listing_id, title FROM collected_products "
            "WHERE status='PUBLISHED' AND listing_id!='' "
            f"AND sku IN ({placeholders})",
            selected_skus,
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT sku, listing_id, title FROM collected_products "
            "WHERE status='PUBLISHED' AND listing_id!=''"
        ).fetchall()
    db.close()
    if args.limit and args.limit > 0:
        rows = rows[: int(args.limit)]

    tok = EbayOAuthService(environment="PRODUCTION").get_valid_token()

    n_spec = n_univ = 0
    findings = []  # dict rows for JSON/email
    applied = 0
    apply_fail = 0
    verified_ok = 0
    verify_failed = 0

    for r in rows:
        title = r["title"] or ""
        if classify_fitment(title) == "vehicle_specific":
            n_spec += 1
        else:
            n_univ += 1
            continue
        live = get_live_compat(tok, r["listing_id"])
        issues = check_fitment_qc(title, live)
        time.sleep(0.25)
        if not issues:
            continue

        row_out = {
            "sku": r["sku"],
            "listing_id": r["listing_id"],
            "title": title,
            "issues": [
                {"code": i.code, "severity": i.severity, "detail": i.detail, "parsed": i.parsed}
                for i in issues
            ],
            "applied": False,
            "apply_error": "",
            "post_apply_issues": None,
        }

        if args.apply and any(i.code == "missing_fitment" for i in issues):
            cps = build_compatible_products(title)
            if cps:
                ok, err = push_compat(tok, r["listing_id"], cps)
                if ok:
                    applied += 1
                    row_out["applied"] = True
                    print(f"  [APPLY] {r['sku']} {r['listing_id']}: +{len(cps)} fitment OK")
                    time.sleep(0.4)
                    live2 = get_live_compat(tok, r["listing_id"])
                    post = check_fitment_qc(title, live2)
                    row_out["post_apply_issues"] = [
                        {
                            "code": i.code,
                            "severity": i.severity,
                            "detail": i.detail,
                            "parsed": i.parsed,
                        }
                        for i in post
                    ]
                    if any(i.code == "missing_fitment" for i in post):
                        verify_failed += 1
                        print(f"  [VERIFY] {r['sku']} still missing_fitment after apply")
                    else:
                        verified_ok += 1
                else:
                    apply_fail += 1
                    row_out["apply_error"] = err
                    print(f"  [APPLY] {r['sku']} FAIL: {err}")
                time.sleep(0.4)

        findings.append(row_out)

    def sev_rank(row):
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
        return min((order.get(i["severity"], 3) for i in row["issues"]), default=3)

    findings.sort(key=sev_rank)
    n_missing = sum(
        1 for row in findings for i in row["issues"] if i["code"] == "missing_fitment"
    )
    n_mismatch = sum(
        1 for row in findings for i in row["issues"] if i["code"] == "fitment_mismatch"
    )
    residual = [
        row
        for row in findings
        if (row.get("post_apply_issues") is None and any(
            i["code"] in ("missing_fitment", "fitment_mismatch") for i in row["issues"]
        ))
        or (
            row.get("post_apply_issues") is not None
            and any(
                i["code"] in ("missing_fitment", "fitment_mismatch")
                for i in row["post_apply_issues"]
            )
        )
    ]
    mismatches = [
        row
        for row in findings
        if any(i["code"] == "fitment_mismatch" for i in row["issues"])
    ]
    applied_rows = [row for row in findings if row.get("applied")]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "audit",
        "vehicle_specific": n_spec,
        "universal": n_univ,
        "listings_with_issues": len(findings),
        "missing": n_missing,
        "mismatch": n_mismatch,
        "applied": applied,
        "apply_fail": apply_fail,
        "verified_ok": verified_ok,
        "verify_failed": verify_failed,
        "findings": findings,
    }
    LOG_DIR.mkdir(exist_ok=True)
    report_path = LOG_DIR / f"fitment_audit_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"\n=== Fitment audit: vehicle_specific={n_spec} universal={n_univ} | "
        f"listings_with_issues={len(findings)} missing={n_missing} mismatch={n_mismatch}"
        + (f" | applied={applied} apply_fail={apply_fail} verified_ok={verified_ok}" if args.apply else "")
        + f" ===\n📄 {report_path}"
    )
    for row in findings[:40]:
        safe = (row["title"] or "").encode("ascii", "ignore").decode()[:55]
        for i in row["issues"]:
            print(f"  [{i['severity']}] {row['sku']} {safe} :: {i['code']} — {i['detail'][:80]}")

    if args.email:
        def _table(rows_data, empty="无"):
            if not rows_data:
                return f"<p>{empty}</p>"
            body = "".join(
                "<tr>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_html.escape(row['sku'])}</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_html.escape(str(row['listing_id']))}</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_html.escape((row['title'] or '')[:60])}</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>"
                + "<br>".join(
                    f"<b>{_html.escape(i['severity'])}</b> {_html.escape(i['detail'][:90])}"
                    for i in (
                        row.get("post_apply_issues")
                        if row.get("post_apply_issues") is not None
                        else row["issues"]
                    )
                )
                + "</td></tr>"
                for row in rows_data
            )
            return (
                "<table style='border-collapse:collapse;width:100%'><thead><tr>"
                "<th align=left style='padding:6px;border-bottom:2px solid #999'>SKU</th>"
                "<th align=left style='padding:6px;border-bottom:2px solid #999'>ItemID</th>"
                "<th align=left style='padding:6px;border-bottom:2px solid #999'>标题</th>"
                "<th align=left style='padding:6px;border-bottom:2px solid #999'>问题</th>"
                "</tr></thead><tbody>"
                f"{body}</tbody></table>"
            )

        banner = "#c0392b" if residual or mismatches else "#27ae60"
        head = (
            f"车型适配需处理 {len(residual)} 条残留（漏挂检出 {n_missing} · 错挂 {n_mismatch}）"
            if residual or mismatches
            else "车型适配全部通过"
        )
        html = (
            f"<h2>AquaRides 车型适用性(Fitment)质检</h2>"
            f"<div style='background:{banner};color:#fff;padding:12px 16px;border-radius:6px;font-weight:700'>{head}</div>"
            f"<p>模式: {'apply' if args.apply else 'audit（只读）'} · "
            f"整车适配件 {n_spec} 条 / 通用件 {n_univ} 条"
            + (f"；本次已挂适配 {applied} 条（回读通过 {verified_ok}）" if args.apply else "")
            + f"</p><p>报告: <code>{_html.escape(str(report_path))}</code></p>"
            f"<h3>已挂适配（{len(applied_rows)}）</h3>{_table(applied_rows, '无本次写入')}"
            f"<h3>仍需处理 / 残留（{len(residual)}）</h3>{_table(residual, '无残留')}"
            f"<h3>疑似错挂 mismatch（{len(mismatches)}，不自动改）</h3>"
            f"{_table(mismatches, '无 mismatch')}"
        )
        from src.utils.email_sender import send_email
        from src.utils.email_policy import should_smtp

        subject = (
            f"AquaRides 车型适配质检 - 残留 {len(residual)} / 错挂 {len(mismatches)}"
            if (residual or mismatches)
            else "AquaRides 车型适配质检 - 通过"
        )
        try:
            has_issues = bool(residual or mismatches)
            send_email(subject, html, send_smtp=should_smtp(has_issues=has_issues))
            print(f"email archived; smtp={'on' if has_issues else 'silenced'}")
        except Exception as e:
            # Email failure must not flip apply_fail==0 into a Task Scheduler fail.
            print(f"email failed: {e}")

    return _fitment_process_exit_code(apply=bool(args.apply), apply_fail=apply_fail)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
