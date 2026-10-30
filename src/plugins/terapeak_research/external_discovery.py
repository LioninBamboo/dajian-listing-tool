"""External (uncollected) GigaCloud opportunity discovery.

Pivot rationale (2026-05-06): the existing
:meth:`IntelligenceService.auto_discover_opportunities` only re-ranks the
``collected_products`` table, which today is dominated by already-PUBLISHED
rows. Operators complained the daily MI digest keeps surfacing the same
already-listed SKUs and burns Qwen tokens for no new decision.

This module implements the inverse flow:

  1. Page through the GigaCloud "Buyer favorites" catalog
     (:meth:`DaJianClient.get_product_list_with_page_info`, sort=4 = newest).
  2. Drop SKUs that are **currently live** locally
     (``status IN ('PUBLISHED','READY')``) so live inventory never re-surfaces.
     ENDED / DELISTED / ERROR / COLLECTED / PENDING rows ARE eligible — they
     represent unlisted opportunities the operator can re-publish.
  3. Drop SKUs in the MI blacklist.
  4. For the remaining candidates, fetch detail + price in batches,
     compute landed cost via :class:`PricingEngine`, run
     :meth:`IntelligenceService.analyze_market` against extracted keywords,
     and score with the existing opportunity-score pipeline.
  5. Return opportunity dicts shaped like
     :meth:`IntelligenceService.auto_discover_opportunities` results, plus
     ``external=True`` and a ``status`` field reflecting whether the SKU
     already exists locally (e.g. ``ENDED``) or is brand new
     (``UNCOLLECTED``).

This path deliberately does NOT call Qwen — the user is upset about wasted
tokens. Qwen optimization happens only after the SKU is moved to ``PENDING``
and the normal MI auto-prepare flow picks it up.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

from src.services.pricing_engine import PricingEngine

logger = logging.getLogger(__name__)


def _build_default_dajian_client():
    """Build a DaJianClient from env vars; return None if creds missing."""
    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    if not client_id or not client_secret:
        return None
    from src.clients.dajian_client import DaJianClient
    return DaJianClient(client_id, client_secret)


# SKUs in these statuses are "currently live" and must be filtered out.
# Anything else (ENDED, DELISTED, ERROR, COLLECTED, PENDING, ...) is a real
# opportunity for re-publish.
_LIVE_STATUSES = {"PUBLISHED", "READY"}


def _load_local_status_index(db_path: str) -> Dict[str, str]:
    """Return a dict ``{sku: status}`` for every row in collected_products.

    Used to (a) skip SKUs that are currently live, and (b) tag results so the
    UI can distinguish brand-new SKUs from re-publish candidates.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute("SELECT sku, status FROM collected_products")
            return {row[0]: (row[1] or "") for row in cur.fetchall() if row and row[0]}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("external_discovery: failed to load local status index: %s", exc)
        return {}


def _iter_external_candidates(
    dajian_client,
    page_size: int,
    max_pages: int,
    sort: int = 4,
) -> Iterable[Dict]:
    """Yield raw favorites records from GigaCloud, newest-first."""
    for page in range(1, max_pages + 1):
        try:
            page_data = dajian_client.get_product_list_with_page_info(
                page=page, page_size=page_size, sort=sort
            )
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning(
                "external_discovery: get_product_list page=%s failed: %s", page, exc
            )
            return
        records = (page_data or {}).get("records") or []
        if not records:
            return
        for rec in records:
            yield rec
        if len(records) < page_size:
            return


def _chunked(items: List, size: int) -> Iterable[List]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _select_price(price_record: Dict) -> Optional[float]:
    """Pick the realistic acquisition price.

    Prefer ``discountedPrice`` (active promo), then ``exclusivePrice``
    (buyer-tier price), then ``price``. Returns ``None`` if none usable.
    """
    if not price_record:
        return None
    for key in ("discountedPrice", "exclusivePrice", "price"):
        val = price_record.get(key)
        try:
            f = float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def _select_shipping(price_record: Dict) -> float:
    """Pick a conservative shipping estimate.

    Use the explicit ``shippingFee`` if present, otherwise the high end of
    ``shippingFeeRange`` so cost is never under-estimated.
    """
    if not price_record:
        return 0.0
    val = price_record.get("shippingFee")
    try:
        f = float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        f = 0.0
    if f > 0:
        return f
    rng = price_record.get("shippingFeeRange") or {}
    try:
        hi = float(rng.get("maxAmount", 0) or 0)
    except (TypeError, ValueError):
        hi = 0.0
    return hi


def _is_oversize(detail_record: Dict) -> bool:
    """Heuristic: treat anything > 70 lbs / > 30 kg as freight-bracket."""
    if not detail_record:
        return False
    try:
        weight = float(detail_record.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0.0
    unit = (detail_record.get("weightUnit") or "").lower()
    if unit in ("kg", "kgs"):
        return weight >= 30.0
    return weight >= 70.0


def discover_external_opportunities(
    intel,
    dajian_client=None,
    *,
    min_margin: float = 0.20,
    max_results: int = 20,
    max_candidates: int = 200,
    page_size: int = 100,
    max_pages: int = 5,
    db_path: Optional[str] = None,
    return_diagnostics: bool = False,
):
    """Discover GigaCloud catalog opportunities not yet present locally.

    Args:
        intel: An :class:`IntelligenceService` instance (provides
            ``analyze_market``, ``calculate_smart_price``,
            ``_extract_search_keywords``, ``_calculate_opportunity_score``,
            ``_get_discovery_recommendation`` and ``_load_mi_blacklist``).
        dajian_client: Optional pre-built DaJianClient. When None, one is
            built from ``DAJIAN_API_KEY`` / ``DAJIAN_API_SECRET``.
        min_margin: Minimum net-margin filter (matches local discovery).
        max_results: Cap returned opportunities (default 20).
        max_candidates: Stop fetching after evaluating this many fresh SKUs.
        page_size, max_pages: GigaCloud pagination knobs (page_size minimum
            is 100 per API contract; ignored values are clamped upstream).
        db_path: Override path to ``ebay_collection.db`` (test hook).

    Returns:
        List of opportunity dicts sorted by ``opportunity_score`` desc.
        Empty list when DaJianClient creds are missing or no fresh
        candidates clear the filters.
    """
    client = dajian_client or _build_default_dajian_client()
    if client is None:
        logger.info("external_discovery: DAJIAN credentials missing, skipping")
        return []

    db_path = db_path or getattr(intel, "db_path", str(PROJECT_ROOT / "ebay_collection.db"))
    local_status = _load_local_status_index(db_path)
    blacklist = intel._load_mi_blacklist()

    diag = {
        "scanned": 0,
        "skipped_live": 0,
        "skipped_blacklist": 0,
        "candidates": 0,
        "dropped_no_price": 0,
        "dropped_no_title": 0,
        "dropped_no_keyword": 0,
        "dropped_no_market": 0,
        "dropped_low_margin": 0,
        "kept": 0,
    }

    candidate_skus: List[str] = []
    candidate_meta: Dict[str, Dict] = {}
    for rec in _iter_external_candidates(client, page_size=page_size, max_pages=max_pages):
        diag["scanned"] += 1
        sku = (rec.get("sku") or rec.get("skuCode") or "").strip()
        if not sku:
            continue
        if local_status.get(sku) in _LIVE_STATUSES:
            diag["skipped_live"] += 1
            continue
        if sku in blacklist:
            diag["skipped_blacklist"] += 1
            continue
        candidate_skus.append(sku)
        candidate_meta[sku] = rec
        if len(candidate_skus) >= max_candidates:
            break
    diag["candidates"] = len(candidate_skus)

    if not candidate_skus:
        logger.info("external_discovery: no GigaCloud candidates passed filters %s", diag)
        if return_diagnostics:
            return [], diag
        return []
    fresh_skus = candidate_skus  # legacy local alias for the rest of the body
    fresh_meta = candidate_meta

    details_by_sku: Dict[str, Dict] = {}
    prices_by_sku: Dict[str, Dict] = {}
    for batch in _chunked(fresh_skus, 50):
        try:
            details = client.get_product_details(batch) or []
            for d in details:
                if d and d.get("sku"):
                    details_by_sku[d["sku"]] = d
        except Exception as exc:
            logger.warning("external_discovery: get_product_details failed: %s", exc)
        try:
            prices = client.get_product_prices(batch) or []
            for p in prices:
                if p and p.get("sku"):
                    prices_by_sku[p["sku"]] = p
        except Exception as exc:
            logger.warning("external_discovery: get_product_prices failed: %s", exc)

    opportunities: List[Dict] = []
    now_iso = datetime.now().isoformat()
    for sku in fresh_skus:
        detail = details_by_sku.get(sku) or fresh_meta.get(sku, {})
        price_rec = prices_by_sku.get(sku) or {}

        product_price = _select_price(price_rec)
        if not product_price:
            diag["dropped_no_price"] += 1
            continue
        shipping_cost = _select_shipping(price_rec)
        cost_breakdown = PricingEngine.calculate_dajian_cost(
            product_price=product_price,
            shipping_cost=shipping_cost,
            is_oversize=_is_oversize(detail),
        )
        total_cost = cost_breakdown["total_dajian_cost"]
        if total_cost <= 0:
            diag["dropped_no_price"] += 1
            continue

        title = (detail.get("productName") or detail.get("name") or "").strip()
        if not title:
            diag["dropped_no_title"] += 1
            continue

        keywords = intel._extract_search_keywords(title)
        if not keywords:
            diag["dropped_no_keyword"] += 1
            continue

        try:
            market = intel.analyze_market(keywords)
        except Exception as exc:
            logger.warning("external_discovery: analyze_market(%s) failed: %s", keywords, exc)
            diag["dropped_no_market"] += 1
            continue
        if market.avg_price <= 0:
            diag["dropped_no_market"] += 1
            continue

        smart = intel.calculate_smart_price(
            total_cost=total_cost,
            market_price=market.avg_price,
            min_margin=min_margin,
            max_margin=0.40,
        )
        margin_rate = float(smart.get("margin", 0))
        if margin_rate < min_margin:
            diag["dropped_low_margin"] += 1
            continue

        score = intel._calculate_opportunity_score(
            margin_rate=margin_rate,
            competition_level=market.competition_level,
            price_spread=market.price_spread,
        )

        images = detail.get("imageUrls") or []
        if not isinstance(images, list):
            images = []

        existing_status = local_status.get(sku, "")
        diag["kept"] += 1
        opportunities.append({
            "sku": sku,
            "title": title,
            "existing_status": existing_status,
            "search_keywords": keywords,
            "dajian_cost": round(total_cost, 2),
            "product_price": product_price,
            "shipping_cost": shipping_cost,
            "cost_breakdown": cost_breakdown,
            "market_avg_price": market.avg_price,
            "market_median_price": market.median_price,
            "suggested_price": round(float(smart["final_price"]), 2),
            "potential_profit": round(total_cost * margin_rate, 2),
            "margin_rate": round(margin_rate * 100, 1),
            "competition": market.competition_level,
            "active_total": market.active_total,
            "opportunity_score": score,
            "demand_signal_score": market.demand_signal_score,
            "demand_signal_label": market.demand_signal_label,
            "strategy": smart.get("strategy", ""),
            "recommendation": intel._get_discovery_recommendation(score, margin_rate, market),
            "image_url": images[0] if images else "",
            "images": images,
            "status": existing_status or "UNCOLLECTED",
            "external": True,
            "discovered_at": now_iso,
        })

    opportunities.sort(key=lambda x: x["opportunity_score"], reverse=True)
    result = opportunities[:max_results]
    if return_diagnostics:
        return result, diag
    return result


def ingest_external_opportunity_as_pending(
    opp: Dict,
    db_path: Optional[str] = None,
) -> str:
    """Move an external opportunity into ``collected_products`` as PENDING.

    Behaviour:

    - Brand-new SKU (no row in collected_products): INSERT a fresh PENDING row.
      Returns ``"inserted"``.
    - Existing row in a non-live status (ENDED / DELISTED / ERROR / COLLECTED
      / PENDING): flip status back to ``PENDING`` and append an MI log marker
      so the next MI auto-prepare can re-draft it. Returns ``"reactivated"``.
    - Existing row in a live status (PUBLISHED / READY): leave untouched.
      Returns ``"skipped_live"``.
    """
    sku = (opp.get("sku") or "").strip()
    if not sku:
        return "skipped_invalid"
    db_path = db_path or str(PROJECT_ROOT / "ebay_collection.db")
    now = _utcnow_naive().isoformat()
    log_marker = (
        f"[MI_EXTERNAL_INGEST] Ingested from GigaCloud catalog at {now} "
        f"(score={opp.get('opportunity_score')}, margin={opp.get('margin_rate')}%)"
    )
    cost_breakdown = opp.get("cost_breakdown") or {}
    images = opp.get("images") or []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT status, logs FROM collected_products WHERE sku = ?", (sku,)
        )
        row = cur.fetchone()
        if row is not None:
            existing_status = (row[0] or "").upper()
            if existing_status in _LIVE_STATUSES:
                return "skipped_live"
            try:
                logs = json.loads(row[1]) if row[1] else []
                if not isinstance(logs, list):
                    logs = [str(logs)]
            except (TypeError, ValueError):
                logs = []
            logs.append(log_marker)
            conn.execute(
                "UPDATE collected_products SET status = ?, logs = ?, "
                "suggested_price = COALESCE(?, suggested_price), updated_at = ? "
                "WHERE sku = ?",
                (
                    "PENDING",
                    json.dumps(logs),
                    float(opp.get("suggested_price") or 0) or None,
                    now,
                    sku,
                ),
            )
            conn.commit()
            return "reactivated"
        conn.execute(
            """
            INSERT INTO collected_products (
                sku, title, price, shipping, stock, url,
                images, videos, description, attributes, specs,
                cost_breakdown, suggested_price, optimization,
                status, listing_id, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sku,
                opp.get("title", ""),
                float(opp.get("product_price") or 0),
                float(opp.get("shipping_cost") or 0),
                99,
                opp.get("url", ""),
                json.dumps(images),
                json.dumps([]),
                "",
                json.dumps({}),
                json.dumps({}),
                json.dumps(cost_breakdown),
                float(opp.get("suggested_price") or 0) or None,
                json.dumps({}),
                "PENDING",
                None,
                json.dumps([log_marker]),
                now,
                now,
            ),
        )
        conn.commit()
        return "inserted"
    finally:
        conn.close()


def ingest_uncollected_for_mi(
    intel,
    *,
    opportunities: Optional[List[Dict]] = None,
    limit: int = 10,
    store_kind: str = "furniture",
    min_score: float = 50,
    stock_lookup: Optional[Callable] = None,
    db_path: Optional[str] = None,
    dajian_client=None,
) -> Dict[str, Any]:
    """Insert bounded GigaCloud-uncollected SKUs as PENDING for daily MI.

    Local ENDED/DELISTED rows are left to ``recycle_ended_for_mi``. Live rows
    stay untouched. Low-score, auto-family, and zero-stock SKUs are skipped.
    """
    from collections import Counter

    from src.utils.mi_opportunity_flow import MI_AUTO_PUBLISH_MIN_SCORE, _store_kind_allows

    cap = max(0, int(limit or 0))
    threshold = float(min_score if min_score is not None else MI_AUTO_PUBLISH_MIN_SCORE)
    skipped: Counter[str] = Counter()
    inserted: List[str] = []
    if cap <= 0:
        return {"inserted": [], "skipped": {}, "requested": 0}

    db_path = db_path or getattr(intel, "db_path", str(PROJECT_ROOT / "ebay_collection.db"))
    opps = opportunities
    if opps is None:
        discovered = discover_external_opportunities(
            intel,
            dajian_client=dajian_client,
            max_results=max(cap * 3, cap),
            db_path=db_path,
        )
        opps = discovered or []

    for opp in opps:
        if len(inserted) >= cap:
            break
        if not isinstance(opp, dict):
            continue
        sku = str(opp.get("sku") or "").strip()
        if not sku:
            skipped["invalid"] += 1
            continue
        existing = str(opp.get("existing_status") or opp.get("status") or "").strip().upper()
        if existing in {"PUBLISHED", "READY", "READY_TO_PUBLISH", "ENDED", "DELISTED", "PENDING", "COLLECTED", "ERROR"}:
            skipped["already_local"] += 1
            continue
        try:
            score = float(opp.get("opportunity_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score < threshold:
            skipped["low_score"] += 1
            continue
        title = str(opp.get("title") or "")
        if not _store_kind_allows(title, store_kind):
            skipped["auto_family"] += 1
            continue
        images = opp.get("images") or []
        if not isinstance(images, list) or len([u for u in images if str(u or "").strip()]) < 2:
            skipped["too_few_images"] += 1
            continue
        if stock_lookup is not None:
            try:
                quantity = stock_lookup(sku)
            except Exception:
                quantity = None
            if quantity is not None and int(quantity) <= 0:
                skipped["zero_stock"] += 1
                continue
        outcome = ingest_external_opportunity_as_pending(opp, db_path=db_path)
        if outcome == "inserted":
            inserted.append(sku)
        elif outcome == "skipped_live":
            skipped["already_local"] += 1
        else:
            skipped[outcome] += 1

    return {"inserted": inserted, "skipped": dict(skipped), "requested": cap}
