#!/usr/bin/env python3
"""Run one W714 batch end-to-end: collect → analyze → scrub → audit → dry-run → live.

  python scripts/w714_run_batch.py --batch B1
  python scripts/w714_run_batch.py --batch B1 --skip-collect --skip-live
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
W714 = ROOT / "tools" / "w714_listing"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

BANNED = [
    r"engineered\s+wood",
    r"engineered\s+frame",
    r"solid\s+wood",
    r"\bwood\b",
    r"high-density\s+foam",
    r"\bfoam\b",
    r"\bmetal\b",
    r"reclining",
    r"recliner",
    r"thoughtful\s+dimensions",
    r"premium\s+cushioning",
    r"space-smart(?:\s+combo)?",
    r"complete\s+styling\s+package",
    r"effortless\s+assembly",
    r"sturdy\s+(?:construction|build|2-seat design)",
    r"stable\s+build",
    r"club-style\s+silhouette",
    r"single-box\s+(?:assembly|delivery)",
    r"corner\s+placement",
    r"\bcushioning\b",
    r"\bframe\b",
]


def run(cmd: list[str], check: bool = True) -> int:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if check and p.returncode != 0:
        raise SystemExit(p.returncode)
    return p.returncode


def scrub_batch(skus: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct
    from sqlalchemy.orm.attributes import flag_modified

    def scrub(text: str) -> str:
        if not text:
            return text
        out = text
        for pat in BANNED:
            out = re.sub(pat, "", out, flags=re.I)
        out = re.sub(r"\s{2,}", " ", out)
        return out.strip(" ,;-")

    db = SessionLocal()
    try:
        for sku in skus:
            r = db.query(CollectedProduct).filter_by(sku=sku).first()
            if not r or r.status not in {"READY", "ERROR", "COLLECTED"}:
                continue
            attrs = dict(r.attributes or {})
            material = attrs.get("Material") or attrs.get("Main Material") or "Chenille"
            color = attrs.get("Color") or attrs.get("Main Color") or ""
            if "Upholstery Material:" not in (r.description or ""):
                r.description = (
                    f"<div><p>Upholstery Material: {material}.</p>"
                    f"<p>Main Color: {color}.</p>"
                    f"<p>Assembled dimensions are taken from the supplier dimension diagram.</p>"
                    f"{r.description or ''}</div>"
                )
                flag_modified(r, "description")
            opt = dict(r.optimization or {})
            if not opt:
                continue
            for key, val in list(opt.items()):
                if isinstance(val, str):
                    opt[key] = scrub(val)
            aspects = opt.get("aspects") or {}
            if isinstance(aspects, dict):
                cleaned = {}
                for k, v in aspects.items():
                    if "frame" in k.lower() or "fill" in k.lower():
                        continue
                    if isinstance(v, list):
                        nv = [scrub(x) if isinstance(x, str) else x for x in v]
                        nv = [x for x in nv if x]
                        if nv:
                            cleaned[k] = nv
                    elif isinstance(v, str):
                        nv = scrub(v)
                        if nv:
                            cleaned[k] = nv
                    else:
                        cleaned[k] = v
                cleaned["Material"] = ["Chenille"]
                cleaned["Upholstery Material"] = ["Chenille"]
                opt["aspects"] = cleaned
            r.optimization = opt
            flag_modified(r, "optimization")
            print(f"  scrubbed {sku}", flush=True)
        db.commit()
    finally:
        db.close()


def reset_error_to_collected(skus: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct
    from sqlalchemy.orm.attributes import flag_modified

    db = SessionLocal()
    try:
        for sku in skus:
            r = db.query(CollectedProduct).filter_by(sku=sku).first()
            if r and r.status == "ERROR":
                r.status = "COLLECTED"
                r.optimization = None
                flag_modified(r, "optimization")
                print(f"  reset ERROR→COLLECTED {sku}", flush=True)
        db.commit()
    finally:
        db.close()


def batch_statuses(skus: list[str]) -> dict:
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct
    from collections import Counter

    db = SessionLocal()
    try:
        rows = db.query(CollectedProduct).filter(CollectedProduct.sku.in_(skus)).all()
        return dict(Counter(r.status for r in rows))
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--skip-collect", action="store_true")
    ap.add_argument("--skip-analyze", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--dry-run-only", action="store_true")
    ap.add_argument(
        "--require-favorite-api",
        action="store_true",
        help="Hard-gate: all SKUs must be DaJian API-readable (favorited) before collect/publish",
    )
    args = ap.parse_args()
    batch = args.batch
    sku_file = W714 / f"{batch}_skus.txt"
    skus = [l.strip() for l in sku_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    sku_list = ",".join(skus)
    report = {"batch": batch, "skus": skus, "steps": []}

    if args.require_favorite_api:
        gate_out = W714 / f"{batch}_favorite_gate.json"
        run(
            [
                str(PY),
                "scripts/supplier_favorite_api_gate.py",
                "--sku-list",
                str(sku_file),
                "--require-all",
                "--json-out",
                str(gate_out),
            ]
        )
        report["steps"].append("favorite_api_gate")
        report["favorite_gate"] = str(gate_out)

    if not args.skip_collect:
        run([str(PY), "scripts/w714_page_collect.py", "--batch", batch])
        report["steps"].append("collect")

    if not args.skip_analyze:
        reset_error_to_collected(skus)
        # analyze only processes COLLECTED; ensure batch SKUs that are COLLECTED get analyzed
        run([str(PY), "batch_analyze.py"])
        # second pass for any remaining ERROR
        reset_error_to_collected(skus)
        st = batch_statuses(skus)
        if st.get("COLLECTED") or st.get("ERROR"):
            run([str(PY), "batch_analyze.py"])
        report["steps"].append("analyze")
        report["status_after_analyze"] = batch_statuses(skus)

    scrub_batch(skus)
    report["steps"].append("scrub")

    for sku in skus:
        run([str(PY), "scripts/audit_fix_ready_drafts.py", "--sku", sku], check=False)
    report["steps"].append("audit")

    run([str(PY), "batch_publish.py", "--dry-run", "--sku-list", sku_list], check=False)
    report["steps"].append("dry_run")

    if not args.skip_live and not args.dry_run_only:
        run([str(PY), "batch_publish.py", "--sku-list", sku_list], check=False)
        report["steps"].append("live")

    report["status_final"] = batch_statuses(skus)
    out = W714 / f"{batch}_pipeline_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("pipeline report", out, report["status_final"], flush=True)


if __name__ == "__main__":
    main()
