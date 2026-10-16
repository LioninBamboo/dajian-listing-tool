from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.services.product_router import ARTTOY, AUTO, FURNITURE, classify_product
from src.utils.mi_draft_origin import MI_DRAFT_ORIGIN

MI_AUTO_PUBLISH_MIN_SCORE = 50
_READY_STATUSES = {"READY", "READY_TO_PUBLISH"}


def empty_auto_prepare_result() -> dict[str, Any]:
    return {
        "success": 0,
        "failed": 0,
        "requested": 0,
        "matched": 0,
        "prepared_skus": [],
    }


def auto_prepare_mi_opportunity_drafts(
    opportunities: list[dict[str, Any]] | None,
    *,
    analyze_collected_products: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    candidate_skus: list[str] = []
    seen_skus: set[str] = set()

    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue

        sku = str(opportunity.get("sku") or "").strip()
        status = str(opportunity.get("status") or "").strip().upper()
        if not sku or sku in seen_skus or status not in {"PENDING", "COLLECTED"}:
            continue

        seen_skus.add(sku)
        candidate_skus.append(sku)

    if not candidate_skus:
        return empty_auto_prepare_result()

    auto_prepare_result = analyze_collected_products(
        sku_filter=candidate_skus,
        eligible_statuses=("PENDING", "COLLECTED"),
        draft_origin=MI_DRAFT_ORIGIN,
    )
    prepared_skus = {
        str(sku).strip() for sku in (auto_prepare_result.get("prepared_skus") or []) if str(sku).strip()
    }

    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue
        sku = str(opportunity.get("sku") or "").strip()
        if sku in prepared_skus:
            opportunity["status"] = "READY"
            opportunity["draft_origin"] = MI_DRAFT_ORIGIN

    return auto_prepare_result


def _opportunity_score(opportunity: dict[str, Any]) -> float:
    raw = opportunity.get("opportunity_score")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_recommended(opportunity: dict[str, Any]) -> bool:
    recommendation = str(opportunity.get("recommendation") or "")
    if recommendation.startswith("❌"):
        return False
    return _opportunity_score(opportunity) >= MI_AUTO_PUBLISH_MIN_SCORE


def _store_kind_allows(title: str, store_kind: str) -> bool:
    target = classify_product(title=title).get("target")
    kind = str(store_kind or "furniture").strip().lower()
    if kind in {"furniture", "arttoy"} and target == AUTO:
        return False
    if kind == "auto" and target in {FURNITURE, ARTTOY}:
        return False
    return True


def lookup_supplier_stock(sku: str) -> int | None:
    """Best-effort GIGA qty. ``None`` means unknown; never raises."""
    try:
        from src.utils.ebay_quantity import fetch_live_supplier_quantity
        return fetch_live_supplier_quantity(sku)
    except Exception:
        return None


def select_mi_auto_publish_skus(
    opportunities: list[dict[str, Any]] | None,
    *,
    limit: int = 10,
    store_kind: str = "furniture",
    stock_lookup: Callable[[str], int | None] | None = None,
    exclude_skus: Collection[str] | None = None,
) -> list[str]:
    """READY MI SKUs that are allowed to enter auto-publish.

    Shared by scheduler_daemon and marketing_ops_digest. Stock lookup is
    optional: ``0`` skips the SKU, ``None`` (unknown / lookup failed) does not.
    ``exclude_skus`` drops SKUs already FactSheet-failed today so digest
    does not retry the same draft every morning.
    """
    cap = max(1, int(limit or 10))
    selected: list[str] = []
    seen: set[str] = set()
    blocked = {str(sku).strip() for sku in (exclude_skus or []) if str(sku).strip()}

    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue
        sku = str(opportunity.get("sku") or "").strip()
        status = str(opportunity.get("status") or "").strip().upper()
        if not sku or sku in seen or sku in blocked or status not in _READY_STATUSES:
            continue
        if not _is_recommended(opportunity):
            continue
        title = str(opportunity.get("title") or "")
        if not _store_kind_allows(title, store_kind):
            continue
        if stock_lookup is not None:
            try:
                quantity = stock_lookup(sku)
            except Exception:
                quantity = None
            if quantity is not None and int(quantity) <= 0:
                continue
        seen.add(sku)
        selected.append(sku)
        if len(selected) >= cap:
            break
    return selected


def summarize_mi_auto_publish_gates(
    opportunities: Iterable[dict[str, Any]] | None,
) -> dict[str, int]:
    """Counts for skip observability. Does not change the publish threshold."""
    ready = 0
    below_min_score = 0
    not_recommended = 0
    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue
        status = str(opportunity.get("status") or "").strip().upper()
        if status not in _READY_STATUSES:
            continue
        ready += 1
        if str(opportunity.get("recommendation") or "").startswith("❌"):
            not_recommended += 1
        if _opportunity_score(opportunity) < MI_AUTO_PUBLISH_MIN_SCORE:
            below_min_score += 1
    return {
        "ready": ready,
        "below_min_score": below_min_score,
        "not_recommended": not_recommended,
        "min_score": MI_AUTO_PUBLISH_MIN_SCORE,
    }


def load_today_factsheet_failed_skus(
    logs_dir: str | Path,
    *,
    today: str | None = None,
) -> set[str]:
    """SKUs whose today's publish_results already recorded a FactSheet error."""
    day = today or datetime.now().strftime("%Y%m%d")
    return _factsheet_failed_skus_for_day(Path(logs_dir), day)


def _factsheet_failed_skus_for_day(root: Path, day: str) -> set[str]:
    failed: set[str] = set()
    if not root.is_dir():
        return failed
    for path in root.glob(f"publish_results_{day}_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload if isinstance(payload, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "").lower() != "error":
                continue
            message = str(row.get("message") or "")
            if "factsheet" not in message.lower():
                continue
            sku = str(row.get("sku") or "").strip()
            if sku:
                failed.add(sku)
    return failed


def load_semantic_human_queue_skus(logs_dir: str | Path) -> set[str]:
    """First field of each line in semantic_rewrite_human_queue.txt."""
    path = Path(logs_dir) / "semantic_rewrite_human_queue.txt"
    skus: set[str] = set()
    if not path.is_file():
        return skus
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return skus
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        sku = raw.replace("\t", " ").split(" ", 1)[0].strip()
        if sku:
            skus.add(sku)
    return skus


def load_factsheet_blocked_skus(
    logs_dir: str | Path,
    *,
    today: str | None = None,
    lookback_days: int = 3,
) -> set[str]:
    """FactSheet publish errors over recent days + semantic human-queue SKUs."""
    root = Path(logs_dir)
    blocked: set[str] = set()
    day = datetime.strptime(
        today or datetime.now().strftime("%Y%m%d"),
        "%Y%m%d",
    ).date()
    days = max(1, int(lookback_days or 1))
    for offset in range(days):
        d = (day - timedelta(days=offset)).strftime("%Y%m%d")
        blocked |= _factsheet_failed_skus_for_day(root, d)
    blocked |= load_semantic_human_queue_skus(root)
    return blocked


def latest_today_publish_has_factsheet_error(
    logs_dir: str | Path,
    *,
    today: str | None = None,
) -> bool:
    """True if the newest publish_results for *today* contains a FactSheet error."""
    day = today or datetime.now().strftime("%Y%m%d")
    root = Path(logs_dir)
    if not root.is_dir():
        return False
    paths = sorted(
        root.glob(f"publish_results_{day}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not paths:
        return False
    try:
        payload = json.loads(paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").lower() != "error":
            continue
        if "factsheet" in str(row.get("message") or "").lower():
            return True
    return False
