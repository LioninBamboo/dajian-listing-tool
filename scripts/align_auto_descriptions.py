#!/usr/bin/env python3
"""Rebuild and (optionally) republish AquaRides Trading descriptions.

The script is deliberately description-only:

* source data is read from ``ebay_collection.db``;
* the deterministic shared template is used (no LLM call);
* Trading ``ReviseFixedPriceItem`` receives only ``Description`` plus the
  Motors-required UPC/Brand/MPN guard fields;
* after a verified live write (or an already-gold skip), the local
  ``optimization.description`` is updated to the same gold HTML;
* every live write is followed by a ``GetItem`` description readback.

Use without ``--apply`` for a read-only preview.  A live run must be explicit:

    python scripts/align_auto_descriptions.py --apply --listing-id 188683585596
    python scripts/align_auto_descriptions.py --apply --skip-listing-id 188683585596

The working directory is not significant; paths are anchored to the checkout
that contains this file.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
DEFAULT_SLEEP_SECONDS = 0.4
MIN_DESCRIPTION_LENGTH = 1500

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ASSEMBLY_RE = re.compile(r"assembly\s+(?:required|instructions)", re.IGNORECASE)
_MOJIBAKE_MARKERS = (
    "\ufffd",  # replacement character
    "Ã",
    "Â",
    "â€",
    "â€™",
    "â€œ",
    "â€�",
    "ï¿½",
)
_RAW_SOURCE_MARKERS = (
    "产品规格",
    "基础信息",
    "产品名称:",
    "产品类型:",
    "产品尺寸",
    "组装长度",
)
_FURNITURE_DECOR_MARKERS = (
    "overwhelming the room",
    "styling the space",
    "pairs easily with modern or classic",
    "clear visual impact",
    "modern or classic décor",
    "modern or classic decor",
)


@dataclass(frozen=True)
class PublishedListing:
    sku: str
    listing_id: str
    title: str
    source_description: str
    attributes: dict[str, Any]
    specs: dict[str, Any]
    aspects: dict[str, Any]


@dataclass(frozen=True)
class Candidate:
    listing: PublishedListing
    description: str
    validation: dict[str, Any]


@dataclass
class RunResult:
    sku: str
    listing_id: str
    status: str
    description_length: int = 0
    ack: str = ""
    error: str = ""
    before_length: int = 0
    after_length: int = 0
    readback_validation: dict[str, Any] | None = None


def _parse_mapping(raw: Any, field_name: str) -> dict[str, Any]:
    """Parse a JSON object field without mutating the database value."""
    if raw in (None, ""):
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} is not a JSON object")
    return dict(value)


def load_published_rows(
    conn: sqlite3.Connection,
    *,
    skus: Sequence[str] | None = None,
    listing_ids: Sequence[str] | None = None,
) -> list[PublishedListing]:
    """Load only PUBLISHED rows with a non-empty listing id.

    Filters are parameterized.  ``optimization`` is read only to obtain its
    source-derived ``aspects``; the generated optimization description/title is
    intentionally ignored.
    """
    clauses = ["status = ?", "listing_id IS NOT NULL", "TRIM(listing_id) != ''"]
    params: list[Any] = ["PUBLISHED"]

    clean_skus = [str(v).strip() for v in (skus or []) if str(v).strip()]
    if clean_skus:
        clauses.append(f"sku IN ({','.join('?' for _ in clean_skus)})")
        params.extend(clean_skus)

    clean_listing_ids = [str(v).strip() for v in (listing_ids or []) if str(v).strip()]
    if clean_listing_ids:
        clauses.append(f"listing_id IN ({','.join('?' for _ in clean_listing_ids)})")
        params.extend(clean_listing_ids)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sku, listing_id, title, description, attributes, specs, optimization "
        "FROM collected_products WHERE "
        + " AND ".join(clauses)
        + " ORDER BY sku",
        params,
    ).fetchall()

    result: list[PublishedListing] = []
    for row in rows:
        optimization = _parse_mapping(row["optimization"], "optimization")
        result.append(
            PublishedListing(
                sku=str(row["sku"] or "").strip(),
                listing_id=str(row["listing_id"] or "").strip(),
                title=str(row["title"] or ""),
                source_description=str(row["description"] or ""),
                attributes=_parse_mapping(row["attributes"], "attributes"),
                specs=_parse_mapping(row["specs"], "specs"),
                aspects=_parse_mapping(optimization.get("aspects"), "optimization.aspects"),
            )
        )
    return result


def looks_like_gold_template(description: str) -> bool:
    """True when the live/local HTML matches the W3611/AquaRides chrome."""
    from src.services.auto_technical_prompt import looks_like_gold_template as _looks_gold

    return _looks_gold(description)


def build_auto_align_description(listing: PublishedListing) -> str:
    """Rebuild a W3611-style AquaRides description without calling an LLM."""
    from src.services.auto_technical_prompt import build_auto_gold_description

    return build_auto_gold_description(
        title=listing.title,
        source_description=listing.source_description,
        attributes=listing.attributes,
        specs=listing.specs,
        aspects=listing.aspects,
    )


def build_candidate(listing: PublishedListing) -> Candidate:
    """Build and validate one deterministic description candidate."""
    description = build_auto_align_description(listing)
    validation = validate_description(description)
    return Candidate(listing=listing, description=description, validation=validation)


def validate_description(description: str, *, min_length: int = MIN_DESCRIPTION_LENGTH) -> dict[str, Any]:
    """Return auditable acceptance checks for a generated/live description."""
    value = str(description or "")
    plain = html.unescape(value)
    lowered = plain.lower()
    errors: list[str] = []

    required_markers = {
        marker: marker.lower() in lowered
        for marker in ("AQUARIDES", "KEY FEATURES", "SPECIFICATIONS")
    }
    for marker, present in required_markers.items():
        if not present:
            errors.append(f"missing_{marker.lower().replace(' ', '_')}")

    if len(value) <= min_length:
        errors.append(f"description_length_le_{min_length}")
    if _CJK_RE.search(plain):
        errors.append("contains_cjk_or_raw_chinese")
    if any(marker in plain for marker in _MOJIBAKE_MARKERS):
        errors.append("contains_mojibake")
    if any(marker in plain for marker in _RAW_SOURCE_MARKERS):
        errors.append("contains_raw_source_marker")
    if _ASSEMBLY_RE.search(plain):
        errors.append("contains_assembly_copy")
    if "]]>" in value:
        # A raw CDATA terminator would make the Trading XML malformed.
        errors.append("contains_cdata_terminator")
    if not re.search(r"</div>\s*$", plain, re.IGNORECASE):
        errors.append("missing_final_closing_div")
    if plain.rstrip().endswith("...") or plain.rstrip().endswith("…"):
        errors.append("ends_with_truncation_marker")
    if "ships from us warehouse" not in lowered:
        errors.append("missing_footer")
    if "#1f2329" not in value or "attribute" not in lowered:
        errors.append("missing_gold_spec_table")
    if any(marker in lowered for marker in _FURNITURE_DECOR_MARKERS):
        errors.append("contains_furniture_decor")

    return {
        "passed": not errors,
        "errors": errors,
        "length": len(value),
        "required_markers": required_markers,
        "has_perfect_for": "perfect for" in lowered,
        "has_vehicle_hint": any(
            token in lowered for token in ("fitment", "vehicle", "compatib", "fit")
        ),
    }


def _profile_guard() -> Any:
    from src.utils.store_profile import get_store_profile

    profile = get_store_profile()
    checks = {
        "store_kind": getattr(profile, "store_kind", ""),
        "listing_channel": getattr(profile, "listing_channel", ""),
        "ebay_site_id": getattr(profile, "ebay_site_id", ""),
        "brand_name": getattr(profile, "brand_name", ""),
    }
    if (
        checks["store_kind"] != "auto"
        or checks["listing_channel"] != "trading"
        or str(checks["ebay_site_id"]) != "100"
        or str(checks["brand_name"]).casefold() != "aquarides"
    ):
        raise RuntimeError(f"refusing run outside AquaRides Trading profile: {checks}")
    return profile


def _client_for_apply() -> Any:
    """Create the production Trading client without touching the collection DB."""
    _profile_guard()
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_auth import EbayOAuthService

    oauth = EbayOAuthService(environment="PRODUCTION")
    # The description-only methods use only oauth + session.  Avoid creating or
    # refreshing the policy cache because this task is read-only on the product DB.
    return RealEbayClient(oauth, policy_manager=None)


def _short_error(exc: BaseException) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text[:500] or type(exc).__name__


def _looks_like_inventory_listing(error: str) -> bool:
    lowered = error.casefold()
    return "inventory-based listing management" in lowered or "inventory based listing" in lowered


def write_local_optimization_description(conn: sqlite3.Connection, sku: str, description: str) -> None:
    """Persist the verified gold HTML onto the local optimization record."""
    row = conn.execute(
        "SELECT optimization FROM collected_products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no collected_products row for sku={sku}")
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["optimization"]
    optimization = _parse_mapping(raw, "optimization")
    optimization["description"] = description
    conn.execute(
        "UPDATE collected_products SET optimization = ? WHERE sku = ?",
        (json.dumps(optimization, ensure_ascii=False), sku),
    )
    conn.commit()


def execute_candidates(
    candidates: Sequence[Candidate],
    *,
    client: Any | None,
    apply: bool,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    conn: sqlite3.Connection | None = None,
) -> list[RunResult]:
    """Preview or execute candidates, with a readback gate for live writes."""
    results: list[RunResult] = []
    for index, candidate in enumerate(candidates, 1):
        listing = candidate.listing
        result = RunResult(
            sku=listing.sku,
            listing_id=listing.listing_id,
            status="ready" if candidate.validation["passed"] else "preflight_failed",
            description_length=len(candidate.description),
        )
        if not candidate.validation["passed"]:
            result.error = "; ".join(candidate.validation["errors"])
            results.append(result)
            continue
        if not apply:
            results.append(result)
            print(
                f"[{index}/{len(candidates)}] DRY {listing.listing_id} "
                f"sku={listing.sku} description={len(candidate.description)}"
            )
            continue

        if client is None:
            raise ValueError("client is required when apply=True")

        try:
            before = client.get_item_description_motors(listing.listing_id) or ""
            result.before_length = len(before)
        except Exception as exc:
            result.status = "read_before_failed"
            result.error = _short_error(exc)
            if _looks_like_inventory_listing(result.error):
                result.status = "skipped_inventory_listing"
            results.append(result)
            print(f"[{index}/{len(candidates)}] {result.status.upper()} {listing.listing_id}: {result.error}")
            time.sleep(sleep_seconds)
            continue

        if looks_like_gold_template(before):
            result.status = "already_gold"
            result.after_length = len(before)
            if conn is not None:
                write_local_optimization_description(conn, listing.sku, candidate.description)
            results.append(result)
            print(
                f"[{index}/{len(candidates)}] ALREADY_GOLD {listing.listing_id} "
                f"sku={listing.sku} local_written={conn is not None}"
            )
            time.sleep(sleep_seconds)
            continue

        try:
            response = client.revise_fixed_price_item_motors(
                listing.listing_id,
                description=candidate.description,
                aspects=listing.aspects,
            )
            result.ack = str((response or {}).get("ack") or "")
            time.sleep(sleep_seconds)
            after = client.get_item_description_motors(listing.listing_id) or ""
            result.after_length = len(after)
            result.readback_validation = validate_description(after)
            if result.readback_validation["passed"]:
                result.status = "success"
                if conn is not None:
                    write_local_optimization_description(conn, listing.sku, candidate.description)
                print(
                    f"[{index}/{len(candidates)}] OK {listing.listing_id} "
                    f"ack={result.ack or '-'} before={result.before_length} after={result.after_length}"
                )
            else:
                result.status = "verify_failed"
                result.error = "; ".join(result.readback_validation["errors"])
                print(f"[{index}/{len(candidates)}] VERIFY_FAILED {listing.listing_id}: {result.error}")
        except Exception as exc:
            result.status = "revise_failed"
            result.error = _short_error(exc)
            if _looks_like_inventory_listing(result.error):
                result.status = "skipped_inventory_listing"
            print(f"[{index}/{len(candidates)}] {result.status.upper()} {listing.listing_id}: {result.error}")
        finally:
            time.sleep(sleep_seconds)
        results.append(result)
    return results


def _write_report(
    *,
    candidates: Sequence[Candidate],
    results: Sequence[RunResult],
    apply: bool,
    path: Path | None = None,
) -> Path:
    path = path or LOG_DIR / (
        "auto_description_alignment_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "root": str(ROOT),
        "candidate_count": len(candidates),
        "counts": counts,
        "verified_success_rate": (
            counts.get("success", 0) / len(candidates) if candidates else 0.0
        ),
        "items": [asdict(result) for result in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform live Trading revisions")
    parser.add_argument("--sku", action="append", default=[], help="limit to SKU; repeatable")
    parser.add_argument("--listing-id", action="append", default=[], help="limit to ItemID; repeatable")
    parser.add_argument("--skip-sku", action="append", default=[], help="exclude SKU; repeatable")
    parser.add_argument("--skip-listing-id", action="append", default=[], help="exclude ItemID; repeatable")
    parser.add_argument("--limit", type=int, default=0, help="process only the first N rows after filters")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS, dest="sleep_seconds")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, default=None, help="JSON report path")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.sleep_seconds < 0.4:
        raise SystemExit("--sleep must be >= 0.4 seconds")
    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")

    # The profile guard also protects against accidentally running this script
    # from the main furniture checkout with the wrong local profile.
    _profile_guard()
    conn = sqlite3.connect(str(args.db))
    try:
        rows = load_published_rows(conn, skus=args.sku, listing_ids=args.listing_id)

        excluded_skus = {str(v).strip() for v in args.skip_sku if str(v).strip()}
        excluded_listing_ids = {str(v).strip() for v in args.skip_listing_id if str(v).strip()}
        rows = [
            row
            for row in rows
            if row.sku not in excluded_skus and row.listing_id not in excluded_listing_ids
        ]
        if args.limit:
            rows = rows[: args.limit]

        print(f"AquaRides PUBLISHED candidates: {len(rows)} (mode={'APPLY' if args.apply else 'DRY-RUN'})")
        candidates = [build_candidate(row) for row in rows]
        invalid = [candidate for candidate in candidates if not candidate.validation["passed"]]
        for candidate in invalid:
            print(
                f"PREFLIGHT_FAILED {candidate.listing.listing_id} "
                f"sku={candidate.listing.sku}: {', '.join(candidate.validation['errors'])}"
            )

        # Fail closed: no live write starts if any selected row is not safe.
        if invalid and args.apply:
            results = [
                RunResult(
                    sku=candidate.listing.sku,
                    listing_id=candidate.listing.listing_id,
                    status="preflight_failed",
                    description_length=len(candidate.description),
                    error="; ".join(candidate.validation["errors"]),
                )
                for candidate in candidates
            ]
            report = _write_report(candidates=candidates, results=results, apply=True, path=args.report)
            print(f"Refusing live writes because preflight failed; report={report}")
            return 2

        client = _client_for_apply() if args.apply and candidates else None
        results = execute_candidates(
            candidates,
            client=client,
            apply=args.apply,
            sleep_seconds=args.sleep_seconds,
            conn=conn if args.apply else None,
        )
        report = _write_report(candidates=candidates, results=results, apply=args.apply, path=args.report)
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        print(f"done: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
        print(f"report={report}")
        ok = counts.get("success", 0) + counts.get("already_gold", 0)
        return 0 if not args.apply or ok == len(candidates) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
