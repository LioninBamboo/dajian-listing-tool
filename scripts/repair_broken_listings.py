#!/usr/bin/env python3
"""Repair listings whose LIVE content is broken: raw-source (untemplated)
description and/or truncated title. Rebuilds the store's brand template
from fresh source characteristics + a word-safe title. Source-faithful, zero
LLM generation (same builder as the W3636 repair).

Not a hallucination rewrite — this fixes structurally-broken listings.

Usage:
  python scripts/repair_broken_listings.py --sku W6018P506376            # dry-run
  python scripts/repair_broken_listings.py --sku W6018P506376 --apply
  python scripts/repair_broken_listings.py --sku-file logs/repair_queue.txt --apply
"""
import argparse
import copy
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n)
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
DB = ROOT / "ebay_collection.db"
BACKUP_DIR = ROOT / "logs" / "repair_backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

from scripts.audit_fix_active_listings import (  # noqa: E402
    build_structured_description_from_source,
    compress_html,
    _put_inventory_product_only,
    _select_best_offer,
)
from src.services.source_refresh import build_source_snapshot  # noqa: E402
from src.utils.title_sanitizer import normalize_listing_title_for_ebay  # noqa: E402
from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations  # noqa: E402
from src.utils.dimension_helpers import find_dimension, find_weight  # noqa: E402
from src.utils.store_profile import get_store_profile  # noqa: E402

_PROFILE = get_store_profile()
_BRAND_UPPER = _PROFILE.brand_name.upper()
TEMPLATE_MARKERS = (_BRAND_UPPER, _PROFILE.brand_tagline, "KEY FEATURES",
                    "SPECIFICATIONS", _PROFILE.description_footer_line1.strip("✦ "))


_ACRONYMS = {"mdf": "MDF", "pu": "PU", "pvc": "PVC", "led": "LED", "abs": "ABS",
             "hdpe": "HDPE", "tv": "TV", "usb": "USB", "pe": "PE", "pp": "PP",
             "eva": "EVA", "mgo": "MGO"}


def _claim_tokens(value: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in {"a", "an", "and", "for", "in", "of", "or", "the", "to", "with"}
    }


def _claim_matches_value(claim: object, value: object) -> bool:
    claim_tokens = _claim_tokens(claim)
    value_tokens = _claim_tokens(value)
    return bool(claim_tokens) and claim_tokens <= value_tokens


def _aspect_values(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _claim_matches_key(claim: object, key: object) -> bool:
    """Return whether a FactSheet claim names an aspect key directly."""
    return _claim_matches_value(claim, key)


def _capacity_target(source_evidence: object) -> str | None:
    """Extract a source-backed capacity range in eBay-friendly casing."""
    text = str(source_evidence or "").strip()
    if not text or text.upper() == "NOT_FOUND":
        return None
    match = re.search(
        r"\b(\d+)\s*[-–]\s*(\d+)\s*(person|people|pair|pairs|seat|seats)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    unit = match.group(3).lower()
    if unit.startswith("person") or unit.startswith("people"):
        unit = "Person"
    elif unit.startswith("pair"):
        unit = "Pairs"
    else:
        unit = "Seats"
    return f"{match.group(1)}-{match.group(2)} {unit}"


def _strip_claim_from_value(value: str, claim: str) -> str:
    """Remove an unsupported material/feature token without dropping the field."""
    pattern = re.escape(str(claim or "").strip()).replace(r"\ ", r"\s+")
    cleaned = re.sub(pattern, "", str(value), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:/|-–")
    return cleaned


def remove_unsupported_capacity_claims(description: str, violations: list[dict] | None) -> str:
    """Generalize numeric capacity phrases when the FactSheet has no source."""
    cleaned = str(description or "")
    for violation in violations or []:
        if violation.get("claim_type") != "semantic_capacity":
            continue
        if str(violation.get("source_evidence") or "").strip().upper() != "NOT_FOUND":
            continue
        claim = str(violation.get("claim_text") or "")
        match = re.search(
            r"\b\d+\s*[-–]\s*\d+\s*(person|people|pair|pairs|seat|seats)\b"
            r"|\b\d+\s*(person|people|pair|pairs|seat|seats)\b",
            claim,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        phrase = match.group(0)
        unit = (match.group(1) or match.group(2) or "").lower()
        replacement = "multiple pairs" if unit.startswith("pair") else "multiple people"
        pattern = re.escape(phrase).replace(r"\ ", r"\s*")
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    return cleaned


def reconcile_semantic_aspects(
    aspects: dict,
    snap,
    violations: list[dict] | None = None,
) -> dict:
    """Remove stale semantic claims before rebuilding source-faithful copy.

    The live optimizer can retain old aspects after a source refresh. A
    description rebuilt from GIGA is not enough if those aspects are then
    reused to generate the package/specification sections. This helper only
    changes fields implicated by the current FactSheet violations and uses an
    explicit source upholstery value when one is available.
    """
    out = copy.deepcopy(aspects or {})
    violations = violations or []
    material_claims = [
        str(v.get("claim_text") or "").strip()
        for v in violations
        if v.get("claim_type") == "semantic_material"
    ]
    feature_claims = [
        str(v.get("claim_text") or "").strip()
        for v in violations
        if v.get("claim_type") == "semantic_feature"
    ]
    capacity_violations = [
        v for v in violations if v.get("claim_type") == "semantic_capacity"
    ]
    count_violations = [
        v for v in violations if v.get("claim_type") == "semantic_count"
    ]
    source_attrs = dict(getattr(snap, "attributes", {}) or {})
    source_upholstery = str(
        source_attrs.get("Upholstery Material")
        or source_attrs.get("Upholstery Fabric")
        or ""
    ).strip()
    material_key_fragments = (
        "material",
        "fabric",
        "filler",
        "filling",
        "foam",
        "wood type",
    )

    for key in list(out):
        key_text = str(key)
        key_lower = key_text.lower()
        values = _aspect_values(out.get(key))

        for violation in capacity_violations:
            claim = str(violation.get("claim_text") or "").strip()
            source_evidence = violation.get("source_evidence")
            target = _capacity_target(source_evidence)
            claim_without_source = re.sub(
                r"\s*\(source:\s*[^)]*\)", "", claim, flags=re.IGNORECASE
            ).strip()
            claim_matches_value = any(
                _claim_matches_value(claim_without_source, value)
                for value in values
            )
            capacity_key = any(
                token in key_lower
                for token in ("capacity", "sleeper size", "number of seats", "accommodates")
            )
            unknown_source_key = (
                not str(source_evidence or "").strip()
                or str(source_evidence or "").strip().upper() == "NOT_FOUND"
            ) and capacity_key and (
                any(
                    unit in " ".join(values).lower()
                    for unit in ("person", "people", "pair", "pairs", "seat", "seats")
                )
                or any(
                    token in key_lower
                    for token in ("seating capacity", "number of seats", "accommodates")
                )
            )
            if not (claim_matches_value or unknown_source_key):
                continue
            if target:
                out[key] = [target]
                values = [target]
            else:
                out.pop(key, None)
                values = []
            break
        if key not in out:
            continue

        for violation in count_violations:
            claim = str(violation.get("claim_text") or "").strip()
            source_match = re.search(
                r"\(source:\s*([^)]+)\)", claim, flags=re.IGNORECASE
            )
            source_count = str(source_match.group(1)).strip() if source_match else ""
            claim_without_source = re.sub(
                r"\s*\(source:\s*[^)]*\)", "", claim, flags=re.IGNORECASE
            ).strip()
            key_matches = any(
                token in key_lower
                for token in re.findall(r"[a-z]+", claim_without_source.lower())
                if token not in {"a", "an", "the"}
            )
            value_matches = any(
                _claim_matches_value(claim_without_source, value)
                for value in values
            )
            if not (key_matches or value_matches):
                continue
            if source_count and re.fullmatch(r"\d+(?:\.\d+)?", source_count):
                out[key] = [source_count]
                values = [source_count]
            else:
                out.pop(key, None)
                values = []
            break
        if key not in out:
            continue

        if feature_claims and any(
            _claim_matches_key(claim, key_text)
            or _claim_matches_value(claim, value)
            for claim in feature_claims
            for value in values or [""]
        ):
            kept = [
                value
                for value in values
                if not any(
                    _claim_matches_value(claim, value)
                    for claim in feature_claims
                )
            ]
            if kept and not any(
                _claim_matches_key(claim, key_text) for claim in feature_claims
            ):
                out[key] = kept
            else:
                out.pop(key, None)
            continue

        matched_material = any(
            _claim_matches_value(claim, value)
            for claim in material_claims
            for value in values
        )
        matched_material_key = any(
            _claim_matches_key(claim, key_text) for claim in material_claims
        )
        is_material_field = any(fragment in key_lower for fragment in material_key_fragments)
        is_wrong_type = key_lower == "type" and (matched_material or matched_material_key)
        if not matched_material and not matched_material_key and not is_wrong_type:
            continue

        if (
            source_upholstery
            and "upholstery" in key_lower
            and ("fabric" in key_lower or "material" in key_lower)
        ):
            out[key] = [source_upholstery]
        elif is_material_field or is_wrong_type:
            out.pop(key, None)
        elif matched_material:
            kept = [
                _strip_claim_from_value(value, claim)
                for value in values
                for claim in material_claims
                if _claim_matches_value(claim, value)
            ]
            kept = [value for value in kept if value]
            if kept:
                out[key] = kept
            else:
                out.pop(key, None)

    return out


def _material_case(raw: str) -> str:
    """Title-case a material but keep acronyms upper (MDF not Mdf)."""
    parts = re.split(r"([,+/&]| and | with )", str(raw or ""))
    out = []
    for part in parts:
        toks = [(_ACRONYMS.get(w.lower(), w.title())) for w in part.split()]
        out.append(" ".join(toks) if toks else part)
    return "".join(out).strip()


def _dajian():
    from src.clients.dajian_client import DaJianClient
    cid, sec = os.getenv("DAJIAN_API_KEY"), os.getenv("DAJIAN_API_SECRET")
    return DaJianClient(cid, sec) if cid and sec else None


def _merge_missing_source_measurements(snap, stored_attributes):
    """Retain source measurements when a fresh GIGA snapshot is sparse.

    Source refreshes can return current material/characteristic fields while
    omitting assembled dimensions that were present in the stored source
    snapshot. Only measurement values are eligible for this fallback; stale
    material or marketing claims are intentionally never copied back.
    """
    fresh_attributes = dict(getattr(snap, "attributes", {}) or {})
    stored_attributes = dict(stored_attributes or {})
    merged = dict(fresh_attributes)

    def _raw_value(keys):
        for key in keys:
            value = stored_attributes.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    for axis in ("Length", "Width", "Height"):
        if find_dimension(fresh_attributes, axis) is not None:
            continue
        value = find_dimension(stored_attributes, axis)
        if value is not None:
            merged[f"Assembled {axis} (in.)"] = _raw_value(
                (
                    f"Assembled {axis} (in.)",
                    f"Overall {axis}",
                    f"Product {axis}",
                    axis,
                )
            ) or str(value)

    if find_weight(fresh_attributes) is None:
        value = find_weight(stored_attributes)
        if value is not None:
            merged["Product Weight (lbs.)"] = _raw_value(
                (
                    "Product Weight (lbs.)",
                    "Product Weight",
                    "Weight of Overrall Product",
                    "Weight of Overall Product",
                    "Overall Product Weight",
                    "Overall Product Weight (with cushion)",
                )
            ) or str(value)

    if merged == fresh_attributes:
        return snap
    merged_snap = copy.copy(snap)
    merged_snap.attributes = merged
    return merged_snap


def build_description_from_snapshot(title, snap, aspects):
    """Build the shared store template from a source snapshot and aspects."""
    features_html = (
        "<div><h3>Product Features</h3><ul>"
        + "".join(f"<li>{c}</li>" for c in snap.characteristics)
        + "</ul></div>"
    )
    # Some GIGA snapshots omit one assembled axis from ``attributes`` while
    # the already stored, source-derived aspects still carry it (W1586135449
    # omitted width). Feed that trusted aspect into the shared table builder.
    description_attrs = dict(snap.attributes or {})
    for axis in ("Length", "Width", "Height"):
        if any(
            description_attrs.get(key)
            for key in (
                f"Assembled {axis} (in.)",
                f"Overall {axis}",
                f"Product {axis}",
                axis,
            )
        ):
            continue
        values = (aspects or {}).get(f"Item {axis}")
        value = values[0] if isinstance(values, list) and values else values
        if value:
            description_attrs[f"Product {axis}"] = value
    return build_structured_description_from_source(
        title, features_html, description_attrs, snap.specs, aspects
    )


def build_repair(conn, dj, sku):
    """Return (new_title, new_description, new_aspects, snapshot) or (None, reason)."""
    try:
        row = conn.execute(
            "SELECT title, optimization, attributes FROM collected_products WHERE sku=?", (sku,)
        ).fetchone()
        stored_attributes_raw = row[2] if row else None
    except sqlite3.OperationalError:
        # Small unit-test/legacy databases may not have the source snapshot
        # column. The repair remains usable; it simply has no fallback data.
        row = conn.execute(
            "SELECT title, optimization FROM collected_products WHERE sku=?", (sku,)
        ).fetchone()
        stored_attributes_raw = None
    if not row:
        return None, "not_in_db"
    opt = json.loads(row[1] or "{}")
    aspects = dict(opt.get("aspects") or {})
    try:
        stored_attributes = json.loads(stored_attributes_raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_attributes = {}
    if not isinstance(stored_attributes, dict):
        stored_attributes = {}

    detail = dj.get_product_detail_by_sku(sku) if dj else None
    snap = build_source_snapshot(detail) if detail else None
    if snap is None:
        return None, "no_source"
    if not snap.characteristics:
        return None, "thin_source_characteristics"
    snap = _merge_missing_source_measurements(snap, stored_attributes)

    # Title: rebuild word-safe from source product name. A source name too thin
    # to be a title (GIGA sometimes stores just "chicken coop") must NOT abandon
    # the repair — the description is the serious defect here (a live listing
    # showing raw Chinese supplier data), and a short title is the lesser evil.
    # Keep the current live title in that case and still fix the description.
    # W3166P455683 sat broken on live because this bailed out (2026-07-27).
    new_title, _ = normalize_listing_title_for_ebay(snap.title, source_title=snap.title)
    # The word-safe truncator can legally cut immediately before the next
    # source fragment, leaving a dangling connector (W1580S00647: ``...,with``).
    # Trim only the incomplete tail; do not invent a replacement title.
    new_title = re.sub(r"\s*(?:with|for|and|a|of|&)\s*$", "", new_title, flags=re.IGNORECASE).strip(" ,-/|")
    if not new_title or len(new_title) < 15:
        current_title, _ = normalize_listing_title_for_ebay(
            re.sub(r"\s+", " ", str(row[0] or "")).strip(), source_title=snap.title
        )
        new_title = current_title or new_title
        if not new_title:
            return None, "title_rebuild_failed"

    # Material from source (fixes Wood→MDF style mismatches)
    if snap.attributes.get("Main Material"):
        aspects["Material"] = [_material_case(snap.attributes["Main Material"])]

    # Description: store template rebuilt from source characteristics.
    new_desc = build_description_from_snapshot(new_title, snap, aspects)
    if not new_desc or not all(m in new_desc for m in (_BRAND_UPPER, "KEY FEATURES")):
        return None, "template_build_failed"
    return (new_title, new_desc, aspects, snap), None


def verify(new_title, new_desc, new_aspects, snap):
    cons = build_source_constraints(
        attrs=snap.attributes, specs=snap.specs,
        source_description=snap.description_html or "", source_title=snap.title,
    )
    cv = [v for v in detect_claim_violations(
        source_constraints=cons, generated_title=new_title,
        generated_description=new_desc, generated_aspects=new_aspects) if v.severity == "CRITICAL"]
    markers_ok = all(m in new_desc for m in TEMPLATE_MARKERS)
    lwh = bool(re.search(r"\d", new_desc))
    title_ok = len(new_title) <= 80 and not re.search(r"\b(with|for|and|a|of|&)\s*$", new_title, re.I)
    return {"claim_critical": len(cv), "markers_ok": markers_ok,
            "has_digits": lwh, "title_ok": title_ok,
            "passed": not cv and markers_ok and title_ok}


def repair_one(conn, dj, ebay, sku, apply):
    built, reason = build_repair(conn, dj, sku)
    if built is None:
        return {"sku": sku, "result": "SKIP", "reason": reason}
    new_title, new_desc, new_aspects, snap = built
    v = verify(new_title, new_desc, new_aspects, snap)
    rec = {"sku": sku, "new_title": new_title, "title_len": len(new_title),
           "desc_len": len(new_desc), "verify": v}
    if not v["passed"]:
        rec["result"] = "VERIFY_FAILED"
        return rec
    if not apply:
        rec["result"] = "DRY_OK"
        return rec

    # backup live before touching
    inv = ebay.get_inventory_item(sku) or {}
    p = inv.get("product") or {}
    offers = ebay.get_offers_by_sku(sku)
    best = _select_best_offer(offers)
    if not best or not best.get("offerId"):
        rec["result"] = "NO_OFFER"
        return rec
    (BACKUP_DIR / f"{sku}.json").write_text(json.dumps({
        "title": p.get("title"), "description": (best.get("listingDescription") or p.get("description")),
        "aspects": p.get("aspects"), "offer_id": best.get("offerId"),
        "listing_id": (best.get("listing") or {}).get("listingId"),
        "category_id": best.get("categoryId") or (best.get("category") or {}).get("categoryId"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    offer_id = best["offerId"]
    cat = best.get("categoryId") or (best.get("category") or {}).get("categoryId")
    _put_inventory_product_only(ebay, sku, new_title, new_desc, new_aspects)
    if not ebay.update_offer_category(offer_id, cat, listing_description=compress_html(new_desc)):
        rec["result"] = "OFFER_UPDATE_FAILED"
        return rec
    pub = ebay.publish_offer(offer_id)
    lid = (pub or {}).get("listingId")
    if not lid:
        rec["result"] = "PUBLISH_FAILED"
        return rec

    # live verify
    live = ebay.get_inventory_item(sku) or {}
    lp = live.get("product") or {}
    off2 = ebay.get_offers_by_sku(sku)
    ld = (off2[0].get("listingDescription") if off2 else "") or lp.get("description") or ""
    live_ok = (_BRAND_UPPER in ld and "产品规格" not in ld
               and not re.search(r"\b(with|for|and|a|of|&)\s*$", lp.get("title", ""), re.I))
    rec["result"] = "DONE" if live_ok else "LIVE_VERIFY_FAILED"
    rec["listing_id"] = lid

    if live_ok:
        r = conn.execute("SELECT optimization, logs FROM collected_products WHERE sku=?", (sku,)).fetchone()
        opt = json.loads(r[0] or "{}")
        opt["title"] = new_title
        opt["description"] = compress_html(new_desc)
        opt["aspects"] = new_aspects
        logs = json.loads(r[1] or "[]")
        logs.append(f"Broken-listing repair at {datetime.now().isoformat()}: "
                    f"rebuilt template + word-safe title from source (was raw-source/truncated).")
        conn.execute(
            "UPDATE collected_products SET optimization=?, logs=?, updated_at=CURRENT_TIMESTAMP WHERE sku=?",
            (json.dumps(opt, ensure_ascii=False), json.dumps(logs, ensure_ascii=False), sku))
        conn.commit()
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", action="append")
    ap.add_argument("--sku-file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    skus = list(args.sku or [])
    if args.sku_file:
        skus += [l.strip() for l in open(args.sku_file, encoding="utf-8") if l.strip()]
    skus = list(dict.fromkeys(skus))
    if args.limit:
        skus = skus[: args.limit]

    dj = _dajian()
    if dj is None:
        print("[ERROR] no Dajian creds"); return 1
    from src.clients.real_ebay_client import create_real_ebay_client
    ebay = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    conn = sqlite3.connect(DB)

    from collections import Counter
    tally = Counter()
    out = []
    for sku in skus:
        try:
            rec = repair_one(conn, dj, ebay, sku, args.apply)
        except Exception as e:
            rec = {"sku": sku, "result": "ERROR", "reason": str(e)[:80]}
        tally[rec["result"]] += 1
        out.append(rec)
        v = rec.get("verify", {})
        print(f"{sku}: {rec['result']}"
              + (f" title_len={rec.get('title_len')} claim={v.get('claim_critical')} tpl={v.get('markers_ok')}"
                 if "verify" in rec else f" ({rec.get('reason','')})"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (ROOT / "logs" / f"repair_run_{ts}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nTALLY:", dict(tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())
