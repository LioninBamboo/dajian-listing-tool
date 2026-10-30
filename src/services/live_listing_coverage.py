"""Map eBay live listings onto local collected_products rows.

Inventory sync and the qty=0 stock gate must cover what is actually live on
eBay, not only DB rows marked PUBLISHED. A live listing whose local status
drifted to ENDED / PENDING / READY still needs to be zeroed when GIGA is OOS.

Matching is by SKU first, then by listing / item id. Live listings that cannot
be mapped are orphans: they are logged, never invented as DB rows.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from src.utils.approved_category import is_category_approved

logger = logging.getLogger(__name__)

LIVE_UNMAPPED_ORPHAN = "live_unmapped_orphan"
PUBLISHED_NOT_LIVE = "db_published_not_live"
DRIFT_STATUS = "status_drift"
DRIFT_LISTING_ID = "listing_id_drift"


@dataclass(frozen=True)
class LiveListing:
    """One ActiveList / Inventory live listing."""

    item_id: str
    sku: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", str(self.item_id or "").strip())
        object.__setattr__(self, "sku", str(self.sku or "").strip())


@dataclass
class DbListingRow:
    sku: str
    listing_id: str = ""
    status: str = ""
    title: str = ""
    source_url: str = ""
    cost_breakdown: Any = None
    optimization: Any = None
    category_lock: bool = False

    def product_dict(self, live_item_id: str = "") -> dict:
        breakdown = self.cost_breakdown
        if isinstance(breakdown, str) and breakdown:
            try:
                breakdown = json.loads(breakdown)
            except json.JSONDecodeError:
                breakdown = {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        listing_id = live_item_id or self.listing_id
        return {
            "sku": self.sku,
            "title": self.title,
            "source_url": self.source_url,
            "cost_breakdown": breakdown,
            "ebay_listing_id": listing_id,
            "listing_id": listing_id,
            "db_status": self.status,
            "db_listing_id": self.listing_id,
            "category_lock": self.category_lock,
        }


@dataclass
class CoverageMatch:
    sku: str
    live_item_id: str
    db_status: str
    db_listing_id: str
    match_by: str
    product: dict
    category_lock: bool = False

    @property
    def status_drifted(self) -> bool:
        return str(self.db_status or "").upper() != "PUBLISHED"

    @property
    def listing_id_drifted(self) -> bool:
        live = str(self.live_item_id or "").strip()
        stored = str(self.db_listing_id or "").strip()
        return bool(live) and live != stored


@dataclass
class CoverageReport:
    fetch_complete: bool
    live_count: int
    covered: list[CoverageMatch] = field(default_factory=list)
    orphans: list[LiveListing] = field(default_factory=list)
    published_not_live: list[DbListingRow] = field(default_factory=list)

    @property
    def drifted(self) -> list[CoverageMatch]:
        return [
            match for match in self.covered
            if match.status_drifted or match.listing_id_drifted
        ]

    @property
    def covered_products(self) -> list[dict]:
        return [match.product for match in self.covered]


def _parse_optimization(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _has_category_lock(optimization: Any) -> bool:
    opt = _parse_optimization(optimization)
    if not opt:
        return False
    return is_category_approved(opt, str(opt.get("categoryId") or "").strip() or None)


def load_db_listing_rows(db_path: str | Path) -> list[DbListingRow]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(collected_products)")}
        if not columns:
            return []
        wanted = ["sku", "status", "listing_id", "title", "url", "cost_breakdown", "optimization"]
        select = [col for col in wanted if col in columns]
        if "sku" not in select:
            return []
        rows = conn.execute(
            f"SELECT {', '.join(select)} FROM collected_products"
        ).fetchall()
    finally:
        conn.close()

    out: list[DbListingRow] = []
    for row in rows:
        data = dict(row)
        sku = str(data.get("sku") or "").strip()
        if not sku:
            continue
        optimization = data.get("optimization")
        out.append(DbListingRow(
            sku=sku,
            listing_id=str(data.get("listing_id") or "").strip(),
            status=str(data.get("status") or "").strip(),
            title=str(data.get("title") or ""),
            source_url=str(data.get("url") or ""),
            cost_breakdown=data.get("cost_breakdown"),
            optimization=optimization,
            category_lock=_has_category_lock(optimization),
        ))
    return out


def _index_db_rows(rows: Iterable[DbListingRow]) -> tuple[dict[str, DbListingRow], dict[str, DbListingRow]]:
    by_sku: dict[str, DbListingRow] = {}
    by_listing_id: dict[str, DbListingRow] = {}
    for row in rows:
        if row.sku:
            by_sku[row.sku] = row
        lid = str(row.listing_id or "").strip()
        if lid and not lid.startswith("DRAFT-"):
            by_listing_id[lid] = row
    return by_sku, by_listing_id


def build_coverage_report(
    live_listings: Iterable[LiveListing],
    db_rows: Iterable[DbListingRow],
    *,
    fetch_complete: bool = True,
    logger_: Optional[logging.Logger] = None,
) -> CoverageReport:
    """Match live eBay listings to local rows; log orphans, do not invent them."""
    log = logger_ or logger
    live = [item for item in live_listings if item.item_id]
    db_list = list(db_rows)
    by_sku, by_listing_id = _index_db_rows(db_list)

    covered: list[CoverageMatch] = []
    orphans: list[LiveListing] = []
    seen_skus: set[str] = set()
    live_item_ids = {item.item_id for item in live}
    live_skus = {item.sku for item in live if item.sku}

    for item in live:
        row = None
        match_by = ""
        if item.sku:
            if item.sku in by_sku:
                row = by_sku[item.sku]
                match_by = "sku"
            else:
                # SKU is present on eBay but absent locally. Do not bind the
                # live item to some other row just because listing_id collided.
                orphans.append(item)
                log.warning(
                    "LIVE_ORPHAN sku=%s item_id=%s — live on eBay but missing from DB; "
                    "not inventing a row",
                    item.sku,
                    item.item_id,
                )
                continue
        elif item.item_id in by_listing_id:
            row = by_listing_id[item.item_id]
            match_by = "listing_id"

        if row is None:
            orphans.append(item)
            log.warning(
                "LIVE_ORPHAN sku=%s item_id=%s — live on eBay but missing from DB; "
                "not inventing a row",
                item.sku or "-",
                item.item_id,
            )
            continue

        if row.sku in seen_skus:
            continue
        seen_skus.add(row.sku)
        product = row.product_dict(item.item_id)
        covered.append(CoverageMatch(
            sku=row.sku,
            live_item_id=item.item_id,
            db_status=row.status,
            db_listing_id=row.listing_id,
            match_by=match_by,
            product=product,
            category_lock=row.category_lock,
        ))

    published_not_live: list[DbListingRow] = []
    for row in db_list:
        if str(row.status or "").upper() != "PUBLISHED":
            continue
        lid = str(row.listing_id or "").strip()
        sku_live = bool(row.sku) and row.sku in live_skus
        id_live = bool(lid) and not lid.startswith("DRAFT-") and lid in live_item_ids
        if sku_live or id_live:
            continue
        published_not_live.append(row)
        log.warning(
            "PUBLISHED_NOT_LIVE sku=%s listing_id=%s — DB PUBLISHED but not in "
            "eBay ActiveList; quantity writes skipped",
            row.sku,
            lid or "-",
        )

    report = CoverageReport(
        fetch_complete=fetch_complete,
        live_count=len(live),
        covered=covered,
        orphans=orphans,
        published_not_live=published_not_live,
    )
    log.info(
        "Live coverage: live=%d mapped=%d orphans=%d published_not_live=%d "
        "drifted=%d complete=%s",
        report.live_count,
        len(report.covered),
        len(report.orphans),
        len(report.published_not_live),
        len(report.drifted),
        fetch_complete,
    )
    return report


def sync_scope_products(report: CoverageReport) -> list[dict]:
    """Listings the stock gate may touch.

    Complete fetch: only live, mapped listings.
    Incomplete fetch: also keep DB PUBLISHED rows we could not confirm as
    ended, so a truncated ActiveList cannot drop in-stock listings from the
    qty=0 gate. Confirmed published-not-live rows are never included.
    """
    products = list(report.covered_products)
    if report.fetch_complete:
        return products

    covered_skus = {match.sku for match in report.covered}
    for row in report.published_not_live:
        if row.sku in covered_skus:
            continue
        product = row.product_dict()
        # Partial ActiveList: keep the qty=0 gate, but never restock/relist
        # a listing we did not actually see as live.
        product["sync_mode"] = "zero_only"
        products.append(product)
    return products
