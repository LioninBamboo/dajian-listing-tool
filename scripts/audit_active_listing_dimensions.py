#!/usr/bin/env python3
"""Audit active listings' dimension/weight consistency between item specifics and description."""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "ebay_collection.db"
OUT = ROOT / "logs" / f"active_dims_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

DIM_KEYS = ["Item Length", "Item Width", "Item Height"]
WEIGHT_KEYS = ["Item Weight", "Product Weight", "Product Weight (lbs.)"]
SINGLE_VALUE_KEYS = set(DIM_KEYS + WEIGHT_KEYS + ["Welding Process", "Tabletop Material", "Top Material"])


def parse_json_field(val):
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return json.loads(val)
        except Exception:
            return {}
    return {}


def get_first_value(v):
    if isinstance(v, list):
        if not v:
            return ""
        return str(v[0]).strip()
    return str(v).strip() if v is not None else ""


def has_multi_delimiter(text):
    return bool(re.search(r"\s*[,;/|]\s*", text or ""))


def extract_num(text):
    m = re.search(r"(\d+(?:\.\d+)?)", text or "")
    return m.group(1) if m else ""


def clean_html(html):
    if not html:
        return ""
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.lower()


def is_missing_value(v):
    s = (v or "").strip().lower()
    return (not s) or (s in {"see description", "n/a", "na", "unknown"})


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT sku, title, status, optimization, description
        FROM collected_products
        WHERE status = 'PUBLISHED'
        ORDER BY updated_at DESC
        """
    ).fetchall()

    issues = []
    totals = {
        "published": len(rows),
        "missing_dimension_aspects": 0,
        "missing_weight_aspects": 0,
        "aspect_desc_mismatch": 0,
        "single_field_multi_value": 0,
    }

    for r in rows:
        sku = r["sku"]
        title = r["title"] or ""
        opt = parse_json_field(r["optimization"])
        aspects = opt.get("aspects", {}) if isinstance(opt, dict) else {}
        if not isinstance(aspects, dict):
            aspects = {}

        desc_raw = ""
        if isinstance(opt, dict):
            desc_raw = opt.get("description") or ""
        if not desc_raw:
            desc_raw = r["description"] or ""
        desc_text = clean_html(desc_raw)

        sku_issues = []

        # 1) Missing dimensions/weight in aspects
        missing_dims = []
        for k in DIM_KEYS:
            v = get_first_value(aspects.get(k, ""))
            if is_missing_value(v):
                missing_dims.append(k)
        if missing_dims:
            totals["missing_dimension_aspects"] += 1
            sku_issues.append({"type": "missing_dimension_aspects", "fields": missing_dims})

        missing_w = []
        found_weight = False
        for k in WEIGHT_KEYS:
            v = get_first_value(aspects.get(k, ""))
            if not is_missing_value(v):
                found_weight = True
                break
        if not found_weight:
            totals["missing_weight_aspects"] += 1
            missing_w = WEIGHT_KEYS
            sku_issues.append({"type": "missing_weight_aspects", "fields": missing_w})

        # 2) Single-value fields accidentally carrying multiple values
        bad_multi = []
        for k in SINGLE_VALUE_KEYS:
            if k not in aspects:
                continue
            raw = aspects[k]
            if isinstance(raw, list) and len(raw) > 1:
                bad_multi.append({"field": k, "value": raw})
                continue
            first = get_first_value(raw)
            if has_multi_delimiter(first):
                bad_multi.append({"field": k, "value": first})
        if bad_multi:
            totals["single_field_multi_value"] += 1
            sku_issues.append({"type": "single_field_multi_value", "fields": bad_multi})

        # 3) Basic desc consistency check: numbers from aspects should appear in description text
        mismatch = []
        for k in DIM_KEYS + ["Item Weight"]:
            v = get_first_value(aspects.get(k, ""))
            if is_missing_value(v):
                continue
            num = extract_num(v)
            if num and num not in desc_text:
                mismatch.append({"field": k, "aspect": v})
        if mismatch:
            totals["aspect_desc_mismatch"] += 1
            sku_issues.append({"type": "aspect_desc_mismatch", "fields": mismatch})

        if sku_issues:
            issues.append({
                "sku": sku,
                "title": title[:120],
                "issues": sku_issues,
            })

    report = {
        "generated_at": datetime.now().isoformat(),
        "summary": totals,
        "issue_count": len(issues),
        "issues": issues,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Audit completed")
    print(f"Published checked: {totals['published']}")
    print(f"Listings with issues: {len(issues)}")
    print(f"Missing dimensions: {totals['missing_dimension_aspects']}")
    print(f"Missing weight: {totals['missing_weight_aspects']}")
    print(f"Aspect/desc mismatch: {totals['aspect_desc_mismatch']}")
    print(f"Single-field multi-value: {totals['single_field_multi_value']}")
    print(f"Report: {OUT}")


if __name__ == "__main__":
    main()
