#!/usr/bin/env python3
"""Sample-verify modular/sectional SKUs still marked Assembly Required=No."""
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.listing_quality_gate import (
    infer_assembly_decision,
    infer_source_assembly_required,
)
from src.utils.publish_validation import first_aspect_text

SKU_FILE = ROOT / "logs" / "modular_sectional_assembly_skus.txt"
OUT_JSON = ROOT / "logs" / "modular_sectional_no_assembly_sample_audit.json"
OUT_CSV = ROOT / "logs" / "modular_sectional_no_assembly_sample_audit.csv"

PATTERN = re.compile(
    r"\b(modular|sectional|sofa set|matching ottoman|individual ottoman)\b",
    re.IGNORECASE,
)


def load_still_no_skus() -> list[str]:
    con = sqlite3.connect(ROOT / "ebay_collection.db")
    skus = []
    for line in SKU_FILE.read_text(encoding="utf-8").splitlines():
        sku = line.strip()
        if not sku or sku.startswith("#"):
            continue
        row = con.execute(
            "SELECT optimization FROM collected_products WHERE sku = ?",
            (sku,),
        ).fetchone()
        if not row:
            continue
        opt = json.loads(row[0] or "{}")
        assembly = (opt.get("aspects") or {}).get("Assembly Required")
        if isinstance(assembly, list):
            assembly = assembly[0] if assembly else ""
        if str(assembly or "").strip().lower() != "no":
            continue
        skus.append(sku)
    return skus


def negative_cue_snippets(text: str, limit: int = 3) -> list[str]:
    cues = []
    patterns = (
        r"\bno\s+assembly(?:\s+is)?\s+(?:required|needed|necessary)\b",
        r"\bno\s+installation\s+required\b",
        r"\bno\s+tools?\s+or\s+assembly\s+required\b",
        r"\bassembly[-\s]?free\b",
        r"\bfully\s+assembled\b",
        r"\b(?:compressed|compression|vacuum[-\s]?packed)\b",
        r"\bcloud\s+couch\b",
        r"\beasy\s+to\s+(?:unpack|unbox|set\s+up)\b",
        r"\bwithout\s+(?:complex|complicated)\s+installation\b",
    )
    plain = re.sub(r"<[^>]+>", " ", text)
    for pattern in patterns:
        for match in re.finditer(pattern, plain, flags=re.IGNORECASE):
            cues.append(match.group(0).strip())
            if len(cues) >= limit:
                return cues
    return cues


def positive_cue_snippets(text: str, limit: int = 3) -> list[str]:
    cues = []
    patterns = (
        r"\bcomponents?\s+must\s+be\s+connected\b",
        r"\bships?\s+as\s+\d+\s+separate\s+(?:carton|box)es?\b",
        r"\bmatching\s+ottoman\b",
        r"\bcombo\s+item\b",
    )
    plain = re.sub(r"<[^>]+>", " ", text)
    for pattern in patterns:
        for match in re.finditer(pattern, plain, flags=re.IGNORECASE):
            cues.append(match.group(0).strip())
            if len(cues) >= limit:
                return cues
    return cues


def main() -> int:
    skus = load_still_no_skus()
    con = sqlite3.connect(ROOT / "ebay_collection.db")
    rows = []
    review_yes = []
    for sku in skus:
        row = con.execute(
            "SELECT title, attributes, specs, description, optimization FROM collected_products WHERE sku = ?",
            (sku,),
        ).fetchone()
        if not row:
            continue
        title, attrs_raw, specs_raw, description, opt_raw = row
        attrs = json.loads(attrs_raw or "{}")
        specs = json.loads(specs_raw or "{}")
        opt = json.loads(opt_raw or "{}")
        source_text = f"{title}\n{description or ''}"
        decision = infer_assembly_decision(
            source_title=title or "",
            source_description=description or "",
            attributes=attrs,
            specs=specs,
            current_assembly="No",
        )
        inferred = infer_source_assembly_required(attrs, specs, description or "")
        required = decision.get("required")
        pkg = decision.get("package") or {}
        neg = negative_cue_snippets(source_text)
        pos = positive_cue_snippets(source_text)
        combo_count = specs.get("Combo Box Count") or attrs.get("Combo Box Count")
        verdict = "keep_no"
        if required == "Yes":
            verdict = "should_be_yes"
            review_yes.append(sku)
        elif required is None and pkg.get("required_family"):
            verdict = "review_family"
            review_yes.append(sku)
        elif inferred == "No" or neg:
            verdict = "keep_no"
        else:
            verdict = "unclear"

        rows.append(
            {
                "sku": sku,
                "title": (opt.get("title") or title or "")[:100],
                "listing_id": opt.get("_quality_gate", {}).get("listing_id") or "",
                "assembly_aspect": "No",
                "infer_source": inferred,
                "decision_required": required,
                "decision_status": decision.get("status"),
                "required_family": pkg.get("required_family"),
                "no_assembly_family": pkg.get("no_assembly_family"),
                "combo_box_count": combo_count,
                "product_type": attrs.get("Product Type"),
                "negative_cues": neg,
                "positive_cues": pos,
                "verdict": verdict,
            }
        )

    report = {
        "generated_at": datetime.now().isoformat(),
        "total_still_no": len(rows),
        "should_be_yes_or_review": len(review_yes),
        "should_be_yes_skus": review_yes,
        "rows": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    import csv

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "sku",
                "verdict",
                "decision_required",
                "infer_source",
                "required_family",
                "no_assembly_family",
                "combo_box_count",
                "negative_cues",
                "positive_cues",
                "title",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sku": row["sku"],
                    "verdict": row["verdict"],
                    "decision_required": row["decision_required"],
                    "infer_source": row["infer_source"],
                    "required_family": row["required_family"],
                    "no_assembly_family": row["no_assembly_family"],
                    "combo_box_count": row["combo_box_count"],
                    "negative_cues": "; ".join(row["negative_cues"]),
                    "positive_cues": "; ".join(row["positive_cues"]),
                    "title": row["title"],
                }
            )

    print(f"still_no={len(rows)} should_review={len(review_yes)}")
    print(f"json={OUT_JSON}")
    print(f"csv={OUT_CSV}")
    if review_yes:
        print("review_skus:", review_yes[:30])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
