#!/usr/bin/env python3
"""Fitment (车型适用性) audit for published Motors listings.

Read-only by default: for every published auto listing, fetch the live
ItemCompatibilityList (GetItem) and run check_fitment_qc against the title.
Reports MISSING / MISMATCH / SUSPECT issues, prioritising CRITICAL/HIGH.

Writes logs/fitment_audit_YYYYMMDDTHHMMSSZ.json always.

--apply : for vehicle-specific listings that are MISSING fitment, push a
          Year-Make-Model ItemCompatibilityList only when the same make +
          model + year range is also present in the stored GIGA source or the
          stored authoritative optimization.motorsCompatibility.compatibleProducts.
          Never auto-fixes mismatch (human review). Never sends ItemSpecifics.
          In apply mode, --limit counts missing-fitment candidates only
          (universal rows are not read). Vehicle-specific rows rotate by the
          oldest live check in logs/fitment_apply_state.json. --max-reads caps
          pre-write GetItem calls (the post-write verify read is not counted).
--email : send the report (CRITICAL/HIGH focused) with applied / residual /
          mismatch sections.
--limit : audit: max raw rows (0=all). apply: max missing-fitment candidates
          (0=no candidate cap; reads still follow --max-reads).
--max-reads : apply only, default 60. Ignored without --apply.

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
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.fitment_qc import (
    check_fitment_qc,
    classify_fitment,
    resolve_apply_fitment,
)
from src.services.motors_trading import build_revise_fixed_price_item_xml

API = "https://api.ebay.com/ws/api.dll"
LOG_DIR = ROOT / "logs"


def _profile_guard():
    from scripts.trading_content_qc import _profile_guard as _trading_profile_guard

    return _trading_profile_guard()


def _hdr(call: str, site_id: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "X-EBAY-API-CALL-NAME": call,
        "X-EBAY-API-SITEID": str(site_id),
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
        "Content-Type": "text/xml",
    }
    if token:
        headers["X-EBAY-API-IAF-TOKEN"] = token
    return headers


class LiveCompatReadError(RuntimeError):
    """GetItem did not return a usable compatibility snapshot."""


def get_live_compat(tok: str, item_id: str, *, site_id: str, session: Any = None) -> list[dict[str, Any]]:
    import requests

    poster = session.post if session is not None else requests.post
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{item_id}</ItemID>"
        "<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>"
        "</GetItemRequest>"
    )
    r = poster(API, data=xml.encode("utf-8"), headers=_hdr("GetItem", site_id, tok), timeout=30)
    status = getattr(r, "status_code", 200)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 200
    body = r.text if hasattr(r, "text") else str(r)
    ack_match = re.search(r"<Ack>(.*?)</Ack>", body, re.IGNORECASE | re.DOTALL)
    ack = (ack_match.group(1) if ack_match else "").strip()
    err = re.search(r"<LongMessage>(.*?)</LongMessage>", body, re.IGNORECASE | re.DOTALL)
    detail = (err.group(1).strip() if err else body[:160])
    has_error = bool(re.search(r"<SeverityCode>\s*Error\s*</SeverityCode>", body, re.I))
    if status_code >= 400 or ack not in {"Success", "Warning"} or has_error:
        raise LiveCompatReadError(
            f"Trading GetItem failed (Ack={ack or 'missing'} HTTP={status_code}): {detail}"
        )
    entries = []
    for block in re.findall(r"<Compatibility>(.*?)</Compatibility>", body, re.S):
        props = [
            {"name": n, "value": v}
            for n, v in re.findall(r"<Name>(.*?)</Name>\s*<Value>(.*?)</Value>", block, re.S)
        ]
        entries.append({"compatibilityProperties": props})
    return entries


def build_apply_xml(item_id: str, compatible_products: Sequence[Mapping[str, Any]]) -> str:
    """C3: compatibility-only Revise. No ItemSpecifics, UPC, or description."""
    return build_revise_fixed_price_item_xml(
        item_id=item_id,
        compatibility=compatible_products,
        replace_all_compatibility=False,
    )


def push_compat(
    tok: str,
    item_id: str,
    compatible_products: Sequence[Mapping[str, Any]],
    *,
    site_id: str,
    session: Any = None,
) -> tuple[bool, str, str]:
    import requests

    xml = build_apply_xml(item_id, compatible_products)
    if "<Compatibility>" not in xml:
        return False, "no compatible products built", xml
    poster = session.post if session is not None else requests.post
    r = poster(
        API,
        data=xml.encode("utf-8"),
        headers=_hdr("ReviseFixedPriceItem", site_id, tok),
        timeout=40,
    )
    body = r.text if hasattr(r, "text") else str(r)
    ack = re.search(r"<Ack>(\w+)</Ack>", body)
    if ack and ack.group(1) in ("Success", "Warning"):
        return True, "", xml
    m = re.search(r"<LongMessage>(.*?)</LongMessage>", body)
    return False, (m.group(1)[:160] if m else body[:160]), xml


def _fitment_process_exit_code(*, apply: bool, apply_fail: int, read_fail: int = 0) -> int:
    """Scheduled Task exit code for apply writes and live GetItem read failures.

    Residual HIGH/mismatch findings stay in the JSON/email report and must not
    mark the Windows job as failed when apply_fail==0 and every GetItem succeeded.
    A failed live read is a transport failure (same as trading_content_qc):
    non-zero even without ``--apply``.
    """
    if int(read_fail or 0) > 0:
        return 1
    if apply and int(apply_fail or 0) > 0:
        return 2
    return 0


def _parse_mapping(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    return dict(value) if isinstance(value, Mapping) else {}


def _stored_compatible_products(optimization: Mapping[str, Any]) -> list[dict[str, Any]]:
    motors = optimization.get("motorsCompatibility") or {}
    if not isinstance(motors, Mapping):
        return []
    products = motors.get("compatibleProducts") or []
    return [dict(entry) for entry in products if isinstance(entry, Mapping)]


APPLY_STATE_NAME = "fitment_apply_state.json"


def _apply_state_path() -> Path:
    return LOG_DIR / APPLY_STATE_NAME


def _empty_apply_state() -> dict[str, Any]:
    return {"version": 1, "listings": {}}


def _load_apply_state(path: Path) -> dict[str, Any]:
    """Missing or corrupt state means every listing is never checked."""
    if not path.exists():
        print(
            f"WARNING: fitment apply state missing ({path}); "
            "treating every listing as never checked"
        )
        return _empty_apply_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        print(
            f"WARNING: fitment apply state unreadable ({path}): {exc}; "
            "treating every listing as never checked"
        )
        return _empty_apply_state()
    listings = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(listings, dict):
        print(
            f"WARNING: fitment apply state corrupt ({path}); "
            "treating every listing as never checked"
        )
        return _empty_apply_state()
    return {"version": 1, "listings": dict(listings)}


def _save_apply_state(path: Path, state: Mapping[str, Any]) -> None:
    """Atomic replace. A failed write logs a warning and does not raise."""
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "listings": dict(state.get("listings") or {})}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"WARNING: fitment apply state write failed ({path}): {exc}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _row_id(row: Mapping[str, Any]) -> int:
    try:
        return int(row["id"])
    except (KeyError, TypeError, ValueError):
        return 0


def _last_checked(state: Mapping[str, Any], listing_id: str) -> str:
    listings = state.get("listings") or {}
    entry = listings.get(str(listing_id))
    if not isinstance(entry, dict):
        return ""
    value = entry.get("last_checked") or ""
    return str(value).strip() if value else ""


def _apply_sort_key(row: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[int, str, int]:
    """Never-checked first, then oldest last_checked, then collected_products.id."""
    last = _last_checked(state, str(row["listing_id"]))
    return (0 if not last else 1, last, _row_id(row))


def _touch_apply_state(state: dict[str, Any], path: Path, row: Mapping[str, Any], result: str) -> None:
    listings = state.setdefault("listings", {})
    listings[str(row["listing_id"])] = {
        "sku": row["sku"],
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "last_result": result,
    }
    _save_apply_state(path, state)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="push YMM fitment for MISSING vehicle-specific listings only",
    )
    ap.add_argument("--email", action="store_true")
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="audit: max raw rows (0=all); apply: max missing-fitment candidates (0=no candidate cap)",
    )
    ap.add_argument(
        "--max-reads",
        type=int,
        default=60,
        help="apply: max pre-write GetItem reads (default 60; ignored without --apply)",
    )
    ap.add_argument(
        "--sku",
        action="append",
        default=[],
        help="only process selected SKU(s); repeatable",
    )
    ap.add_argument("--db", type=Path, default=ROOT / "ebay_collection.db")
    args = ap.parse_args(list(argv) if argv is not None else None)

    try:
        profile = _profile_guard()
    except RuntimeError as exc:
        print(str(exc))
        return 2

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row
    selected_skus = [str(value).strip() for value in (args.sku or []) if str(value).strip()]
    table_cols = {row[1] for row in db.execute("PRAGMA table_info(collected_products)")}
    id_expr = "id" if "id" in table_cols else "rowid"
    columns = f"{id_expr} AS id, sku, listing_id, title, description, optimization"
    if selected_skus:
        placeholders = ",".join("?" for _ in selected_skus)
        rows = db.execute(
            f"SELECT {columns} FROM collected_products "
            "WHERE status='PUBLISHED' AND listing_id!='' "
            f"AND sku IN ({placeholders})",
            selected_skus,
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT {columns} FROM collected_products "
            "WHERE status='PUBLISHED' AND listing_id!=''"
        ).fetchall()
    db.close()
    # Audit keeps the raw-row cap. Apply counts missing-fitment candidates later.
    if not args.apply and args.limit and args.limit > 0:
        rows = rows[: int(args.limit)]

    from src.services.ebay_auth import EbayOAuthService

    tok = EbayOAuthService(environment="PRODUCTION").get_valid_token()
    site_id = str(getattr(profile, "ebay_site_id", "100"))

    n_spec = n_univ = 0
    findings = []
    applied = 0
    apply_fail = 0
    verified_ok = 0
    verify_failed = 0
    skipped_no_source = 0
    skipped_title_no_year = 0
    reads_used = 0
    n_candidates = 0
    n_has_fitment = 0
    read_cap_hit = False
    max_reads_effective = int(args.max_reads) if int(args.max_reads) >= 1 else 1
    candidate_cap = int(args.limit) if args.limit and args.limit > 0 else None
    state_path = _apply_state_path()
    state = _empty_apply_state()
    work = rows

    if args.apply:
        universal_rows = []
        specific_rows = []
        for r in rows:
            if classify_fitment(r["title"] or "") == "vehicle_specific":
                specific_rows.append(r)
            else:
                universal_rows.append(r)
        n_univ = len(universal_rows)
        n_spec = len(specific_rows)
        state = _load_apply_state(state_path)
        specific_rows.sort(key=lambda row: _apply_sort_key(row, state))
        work = specific_rows

    for r in work:
        if args.apply and reads_used >= max_reads_effective:
            read_cap_hit = True
            print(
                f"read cap {max_reads_effective} reached after {n_candidates} candidates"
            )
            break
        if args.apply and candidate_cap is not None and n_candidates >= candidate_cap:
            break
        title = r["title"] or ""
        if classify_fitment(title) == "vehicle_specific":
            if not args.apply:
                n_spec += 1
        else:
            n_univ += 1
            continue
        if args.apply:
            reads_used += 1
        try:
            live = get_live_compat(tok, r["listing_id"], site_id=site_id)
        except Exception as exc:
            message = re.sub(r"\s+", " ", str(exc)).strip()[:500]
            findings.append(
                {
                    "sku": r["sku"],
                    "listing_id": r["listing_id"],
                    "title": title,
                    "status": "unknown",
                    "issues": [],
                    "applied": False,
                    "apply_error": "",
                    "skip_reason": "",
                    "error": message,
                    "post_apply_issues": None,
                }
            )
            print(f"  [UNKNOWN] {r['sku']} {r['listing_id']}: {message}")
            if args.apply:
                _touch_apply_state(state, state_path, r, "read_failed")
            time.sleep(0.25)
            continue
        issues = check_fitment_qc(title, live)
        time.sleep(0.25)
        if args.apply and not any(i.code == "missing_fitment" for i in issues):
            n_has_fitment += 1
            _touch_apply_state(state, state_path, r, "has_fitment")
        if not issues:
            continue

        row_out = {
            "sku": r["sku"],
            "listing_id": r["listing_id"],
            "title": title,
            "status": "checked",
            "issues": [
                {"code": i.code, "severity": i.severity, "detail": i.detail, "parsed": i.parsed}
                for i in issues
            ],
            "applied": False,
            "apply_error": "",
            "skip_reason": "",
            "post_apply_issues": None,
        }

        if args.apply and any(i.code == "missing_fitment" for i in issues):
            n_candidates += 1
            apply_result = "missing"
            optimization = _parse_mapping(r["optimization"] if "optimization" in r.keys() else {})
            products, skip_reason = resolve_apply_fitment(
                title,
                source_title="",
                source_description=str(r["description"] or "") if "description" in r.keys() else "",
                stored_compatible_products=_stored_compatible_products(optimization),
            )
            if skip_reason or not products:
                row_out["skip_reason"] = skip_reason or "skipped_no_source_fitment"
                apply_result = row_out["skip_reason"]
                if skip_reason == "skipped_title_no_year":
                    skipped_title_no_year += 1
                else:
                    skipped_no_source += 1
            else:
                ok, err, _xml = push_compat(
                    tok, r["listing_id"], products, site_id=site_id
                )
                if ok:
                    applied += 1
                    apply_result = "applied"
                    row_out["applied"] = True
                    print(f"  [APPLY] {r['sku']} {r['listing_id']}: +{len(products)} fitment OK")
                    time.sleep(0.4)
                    try:
                        live2 = get_live_compat(tok, r["listing_id"], site_id=site_id)
                    except Exception as verify_exc:
                        verify_failed += 1
                        row_out["error"] = re.sub(r"\s+", " ", str(verify_exc)).strip()[:500]
                        print(f"  [VERIFY] {r['sku']} GetItem failed after apply (unknown)")
                    else:
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
            _touch_apply_state(state, state_path, r, apply_result)

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
    unknown_rows = [row for row in findings if row.get("status") == "unknown"]
    n_unknown = len(unknown_rows)

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
        "skipped_no_source_fitment": skipped_no_source,
        "skipped_title_no_year": skipped_title_no_year,
        "read_failed": n_unknown,
        "findings": findings,
    }
    if args.apply:
        limit_value = int(args.limit or 0)
        report["limit"] = limit_value
        report["max_reads"] = max_reads_effective
        report["reads_used"] = reads_used
        report["candidates"] = n_candidates
        report["has_fitment"] = n_has_fitment
        report["read_cap_hit"] = bool(read_cap_hit)
        report["state_file"] = str(state_path)
    LOG_DIR.mkdir(exist_ok=True)
    report_path = LOG_DIR / f"fitment_audit_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"\n=== Fitment audit: vehicle_specific={n_spec} universal={n_univ} | "
        f"listings_with_issues={len(findings)} missing={n_missing} mismatch={n_mismatch}"
        + (f" read_failed={n_unknown}" if n_unknown else "")
        + (f" | applied={applied} apply_fail={apply_fail} verified_ok={verified_ok}" if args.apply else "")
        + f" ===\n📄 {report_path}"
    )
    for row in findings[:40]:
        safe = (row["title"] or "").encode("ascii", "ignore").decode()[:55]
        for i in row["issues"]:
            print(f"  [{i['severity']}] {row['sku']} {safe} :: {i['code']} — {i['detail'][:80]}")

    if args.email:
        def _row_issue_html(row):
            issues = (
                row.get("post_apply_issues")
                if row.get("post_apply_issues") is not None
                else row.get("issues") or []
            )
            if issues:
                return "<br>".join(
                    f"<b>{_html.escape(i['severity'])}</b> {_html.escape(i['detail'][:90])}"
                    for i in issues
                )
            return _html.escape(str(row.get("error") or row.get("skip_reason") or "live 读取失败"))

        def _table(rows_data, empty="无"):
            if not rows_data:
                return f"<p>{empty}</p>"
            body = "".join(
                "<tr>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_html.escape(row['sku'])}</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_html.escape(str(row['listing_id']))}</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_html.escape((row['title'] or '')[:60])}</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee'>{_row_issue_html(row)}</td>"
                "</tr>"
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

        has_content_issues = bool(residual or mismatches)
        has_read_failures = n_unknown > 0
        has_issues = has_content_issues or has_read_failures
        banner = "#c0392b" if has_issues else "#27ae60"
        head_bits = []
        if has_content_issues:
            head_bits.append(
                f"车型适配需处理 {len(residual)} 条残留（漏挂检出 {n_missing} · 错挂 {n_mismatch}）"
            )
        if has_read_failures:
            head_bits.append(f"live 读取失败 {n_unknown} 条")
        head = " · ".join(head_bits) if head_bits else "车型适配全部通过"
        if args.apply:
            if args.limit and args.limit > 0:
                cand_label = f"{n_candidates}/{int(args.limit)}"
            else:
                cand_label = f"{n_candidates}/不限"
            cap_bit = "；read_cap_hit: true" if read_cap_hit else ""
            apply_progress = (
                f"<p>本次读取 {reads_used}/{max_reads_effective}，"
                f"缺适配候选 {cand_label}{cap_bit}</p>"
            )
        else:
            apply_progress = ""
        html = (
            f"<h2>AquaRides 车型适用性(Fitment)质检</h2>"
            f"<div style='background:{banner};color:#fff;padding:12px 16px;border-radius:6px;font-weight:700'>{head}</div>"
            f"<p>模式: {'apply' if args.apply else 'audit（只读）'} · "
            f"整车适配件 {n_spec} 条 / 通用件 {n_univ} 条"
            + (f"；本次已挂适配 {applied} 条（回读通过 {verified_ok}）" if args.apply else "")
            + (f"；live 读取失败 {n_unknown} 条" if n_unknown else "")
            + f"</p><p>报告: <code>{_html.escape(str(report_path))}</code></p>"
            f"{apply_progress}"
            f"<h3>已挂适配（{len(applied_rows)}）</h3>{_table(applied_rows, '无本次写入')}"
            f"<h3>仍需处理 / 残留（{len(residual)}）</h3>{_table(residual, '无残留')}"
            f"<h3>疑似错挂 mismatch（{len(mismatches)}，不自动改）</h3>"
            f"{_table(mismatches, '无 mismatch')}"
            f"<h3>live 读取失败（{n_unknown}）</h3>{_table(unknown_rows, '无读取失败')}"
        )
        from src.utils.email_sender import send_email
        from src.utils.email_policy import should_smtp

        subject_bits = []
        if has_content_issues:
            subject_bits.append(f"残留 {len(residual)} / 错挂 {n_mismatch}")
        if has_read_failures:
            subject_bits.append(f"读取失败 {n_unknown}")
        subject = (
            "AquaRides 车型适配质检 - " + " / ".join(subject_bits)
            if subject_bits
            else "AquaRides 车型适配质检 - 通过"
        )
        try:
            send_email(subject, html, send_smtp=should_smtp(has_issues=has_issues))
            print(f"email archived; smtp={'on' if has_issues else 'silenced'}")
        except Exception as e:
            # Email failure must not flip apply_fail==0 into a Task Scheduler fail.
            print(f"email failed: {e}")

    return _fitment_process_exit_code(
        apply=bool(args.apply), apply_fail=apply_fail, read_fail=n_unknown
    )


if __name__ == "__main__":
    raise SystemExit(main() or 0)
