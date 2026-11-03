"""One fail-closed zero-fee gate for every eBay create / relist path.

Sergey's rule: never list if any fee line is above $0.00. Category checks run
first. Promotional discounts are logged and never netted against the gross fee.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional

from src.utils.publish_fee_preview import (
    HIGH_FEE_PATH_PREFIXES,
    HIGH_FEE_PATH_SEGMENTS,
    _blacklist_applies_to_store,
    _ids_from_blacklist_payload,
    _path_entries_from_blacklist_payload,
    _path_segments,
    _read_blacklist_payload,
    resolve_category_taxonomy_path,
    resolve_high_fee_blacklist_path,
)

logger = logging.getLogger(__name__)

REASON_FEE_NONZERO = "fee_nonzero"
REASON_PREVIEW_UNAVAILABLE = "fee_preview_unavailable"
REASON_HIGH_FEE_CATEGORY = "high_fee_category"
REASON_BLACKLIST_UNAVAILABLE = "high_fee_blacklist_unavailable"
REASON_CATEGORY_UNKNOWN = "category_unknown"

SOURCE_INVENTORY = "inventory.getListingFees"
SOURCE_VERIFY_ADD = "trading.VerifyAddFixedPriceItem"
SOURCE_VERIFY_RELIST = "trading.VerifyRelistItem"

_INSERTION_FEE_NAMES = frozenset({"INSERTIONFEE", "INSERTION_FEE"})
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class FeeLine:
    fee_type: str
    amount: Decimal
    currency: str
    source: str
    promotional_discount: Decimal | None = None


@dataclass(frozen=True)
class FeeGateResult:
    ok: bool
    reason: str | None
    lines: tuple[FeeLine, ...]
    category_id: str
    category_path: str


@dataclass(frozen=True)
class FeeGateRaw:
    source: str
    payload: Any = None
    http_status: int | None = None
    ack: str | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class BlacklistState:
    available: bool
    excluded_for_store: bool
    ids: frozenset[str]
    paths: tuple[str, ...]
    source: str


class ZeroFeeGateBlocked(RuntimeError):
    def __init__(self, result: FeeGateResult, message: str | None = None):
        self.result = result
        super().__init__(message or result.reason or "blocked_fee")


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_category_id(value: Any) -> str:
    return _collapse_ws(str(value or ""))


def normalize_path_segments(*parts: str) -> list[str]:
    return _path_segments(*parts)


def load_blacklist_state(path: Optional[str] = None) -> BlacklistState:
    file_path = resolve_high_fee_blacklist_path(path)
    source = str(file_path)
    data = _read_blacklist_payload(path)
    if data is None:
        return BlacklistState(
            available=False,
            excluded_for_store=False,
            ids=frozenset(),
            paths=(),
            source=source,
        )
    if not _blacklist_applies_to_store(data):
        return BlacklistState(
            available=True,
            excluded_for_store=True,
            ids=frozenset(),
            paths=(),
            source=source,
        )
    ids = frozenset(_ids_from_blacklist_payload(data))
    paths = tuple(_path_entries_from_blacklist_payload(data))
    if not ids and not paths:
        return BlacklistState(
            available=False,
            excluded_for_store=False,
            ids=frozenset(),
            paths=(),
            source=source,
        )
    return BlacklistState(
        available=True,
        excluded_for_store=False,
        ids=ids,
        paths=paths,
        source=source,
    )


def _prefix_matches(segments: list[str]) -> bool:
    if not segments:
        return False
    for prefix in HIGH_FEE_PATH_PREFIXES:
        prefix_segs = normalize_path_segments(prefix)
        if prefix_segs and segments[: len(prefix_segs)] == prefix_segs:
            return True
    return False


def category_match_reason(
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    *,
    blacklist: BlacklistState | None = None,
    blacklist_path: Optional[str] = None,
) -> str | None:
    """Return a category-stage block reason, or None if the category is allowed."""
    cid = normalize_category_id(category_id)
    path_text = str(category_path or "")
    name_text = str(category_name or "")
    segments = normalize_path_segments(path_text, name_text)
    incoming = normalize_path_segments(path_text)
    if not cid and not incoming:
        return REASON_CATEGORY_UNKNOWN

    state = blacklist if blacklist is not None else load_blacklist_state(blacklist_path)
    if cid and any(cid == normalize_category_id(item) for item in state.ids):
        return REASON_HIGH_FEE_CATEGORY
    if any(segment in HIGH_FEE_PATH_SEGMENTS for segment in segments):
        return REASON_HIGH_FEE_CATEGORY
    if _prefix_matches(incoming or segments):
        return REASON_HIGH_FEE_CATEGORY
    if incoming:
        for extra in state.paths:
            if incoming == normalize_path_segments(extra):
                return REASON_HIGH_FEE_CATEGORY
    if not state.available and not state.excluded_for_store:
        return REASON_BLACKLIST_UNAVAILABLE
    return None


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _parse_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _is_insertion_fee(fee_type: str) -> bool:
    return str(fee_type or "").replace("_", "").replace(" ", "").upper() in _INSERTION_FEE_NAMES


def _normalize_raw(preview_value: Any) -> FeeGateRaw:
    if isinstance(preview_value, FeeGateRaw):
        return preview_value
    if preview_value is None:
        return FeeGateRaw(source=SOURCE_INVENTORY, payload=None)
    if isinstance(preview_value, Mapping):
        return FeeGateRaw(source=SOURCE_INVENTORY, payload=preview_value, http_status=200)
    if isinstance(preview_value, str):
        return FeeGateRaw(source=SOURCE_VERIFY_ADD, payload=preview_value)
    raise TypeError(f"unsupported fee preview type: {type(preview_value)!r}")


def _inventory_fee_lines(payload: Any, source: str) -> tuple[FeeLine, ...] | str:
    if payload is None or not isinstance(payload, Mapping):
        return REASON_PREVIEW_UNAVAILABLE
    summaries = payload.get("feeSummaries")
    if not isinstance(summaries, list) or not summaries:
        return REASON_PREVIEW_UNAVAILABLE
    lines: list[FeeLine] = []
    has_insertion = False
    for summary in summaries:
        if not isinstance(summary, Mapping):
            return REASON_PREVIEW_UNAVAILABLE
        fees = summary.get("fees")
        if not isinstance(fees, list):
            return REASON_PREVIEW_UNAVAILABLE
        for fee in fees:
            if not isinstance(fee, Mapping):
                return REASON_PREVIEW_UNAVAILABLE
            fee_type = str(fee.get("feeType") or fee.get("name") or "")
            amount_node = fee.get("amount")
            if isinstance(amount_node, Mapping):
                raw_amount = amount_node.get("value")
                currency = str(amount_node.get("currency") or "").strip()
            else:
                raw_amount = amount_node
                currency = str(fee.get("currency") or "").strip()
            amount = _parse_decimal(raw_amount)
            if amount is None or amount < _ZERO:
                return REASON_PREVIEW_UNAVAILABLE
            if not currency or currency.upper() != "USD":
                return REASON_PREVIEW_UNAVAILABLE
            discount = None
            promo = fee.get("promotionalDiscount") or fee.get("PromotionalDiscount")
            if promo is not None:
                if isinstance(promo, Mapping):
                    discount = _parse_decimal(promo.get("value"))
                    promo_ccy = str(promo.get("currency") or currency).strip()
                else:
                    discount = _parse_decimal(promo)
                    promo_ccy = currency
                if discount is None or (promo_ccy and promo_ccy.upper() != "USD"):
                    return REASON_PREVIEW_UNAVAILABLE
            if _is_insertion_fee(fee_type):
                has_insertion = True
            lines.append(
                FeeLine(
                    fee_type=fee_type or "UNKNOWN",
                    amount=amount,
                    currency=currency.upper(),
                    source=source,
                    promotional_discount=discount,
                )
            )
    if not has_insertion:
        return REASON_PREVIEW_UNAVAILABLE
    return tuple(lines)


def _trading_ack(payload: str) -> str:
    match = re.search(r"<Ack>\s*([^<]+)\s*</Ack>", str(payload or ""), re.I)
    return (match.group(1) if match else "").strip()


def _trading_fee_lines(payload: Any, source: str, *, http_status: int | None, ack: str | None) -> tuple[FeeLine, ...] | str:
    if http_status is not None and http_status != 200:
        return REASON_PREVIEW_UNAVAILABLE
    body = str(payload or "")
    if not body.strip():
        return REASON_PREVIEW_UNAVAILABLE
    resolved_ack = (ack or _trading_ack(body)).strip()
    if resolved_ack.lower() not in {"success", "warning"}:
        return REASON_PREVIEW_UNAVAILABLE
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return REASON_PREVIEW_UNAVAILABLE
    fees_node = None
    for el in root.iter():
        if _local_name(el.tag) == "Fees":
            fees_node = el
            break
    if fees_node is None:
        return REASON_PREVIEW_UNAVAILABLE
    lines: list[FeeLine] = []
    has_insertion = False
    fee_children = [child for child in list(fees_node) if _local_name(child.tag) == "Fee"]
    if not fee_children:
        return REASON_PREVIEW_UNAVAILABLE
    for fee_el in fee_children:
        name = ""
        amount = None
        currency = ""
        discount = None
        for child in list(fee_el):
            local = _local_name(child.tag)
            if local == "Name":
                name = (child.text or "").strip()
            elif local == "Fee":
                amount = _parse_decimal(child.text)
                currency = str(child.attrib.get("currencyID") or child.attrib.get("currency") or "").strip()
            elif local == "PromotionalDiscount":
                discount = _parse_decimal(child.text)
                promo_ccy = str(child.attrib.get("currencyID") or child.attrib.get("currency") or "").strip()
                if discount is None or (promo_ccy and promo_ccy.upper() != "USD"):
                    return REASON_PREVIEW_UNAVAILABLE
        if amount is None or amount < _ZERO:
            return REASON_PREVIEW_UNAVAILABLE
        if not currency or currency.upper() != "USD":
            return REASON_PREVIEW_UNAVAILABLE
        if _is_insertion_fee(name):
            has_insertion = True
        lines.append(
            FeeLine(
                fee_type=name or "UNKNOWN",
                amount=amount,
                currency=currency.upper(),
                source=source,
                promotional_discount=discount,
            )
        )
    if not has_insertion:
        return REASON_PREVIEW_UNAVAILABLE
    return tuple(lines)


def parse_fee_preview(raw: FeeGateRaw) -> tuple[FeeLine, ...] | str:
    if raw.error is not None:
        return REASON_PREVIEW_UNAVAILABLE
    source = str(raw.source or "")
    if source.startswith("inventory"):
        if raw.http_status is not None and raw.http_status != 200:
            return REASON_PREVIEW_UNAVAILABLE
        return _inventory_fee_lines(raw.payload, source or SOURCE_INVENTORY)
    if source.startswith("trading"):
        return _trading_fee_lines(
            raw.payload,
            source,
            http_status=raw.http_status,
            ack=raw.ack,
        )
    if isinstance(raw.payload, Mapping):
        return _inventory_fee_lines(raw.payload, source or SOURCE_INVENTORY)
    if isinstance(raw.payload, str):
        return _trading_fee_lines(raw.payload, source or SOURCE_VERIFY_ADD, http_status=raw.http_status, ack=raw.ack)
    return REASON_PREVIEW_UNAVAILABLE


def _nonzero_reason(lines: tuple[FeeLine, ...]) -> str | None:
    for line in lines:
        if line.amount > _ZERO:
            return REASON_FEE_NONZERO
    return None


def evaluate_fee_gate(
    *,
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    preview: Callable[[], Any] | None = None,
    listing_fees: Any = None,
    lookup_error: BaseException | None = None,
    lookup_attempted: bool = False,
    blacklist_path: Optional[str] = None,
) -> FeeGateResult:
    cid = normalize_category_id(category_id)
    path = str(category_path or "")
    incoming = normalize_path_segments(path)
    fee_only = preview is None and (listing_fees is not None or lookup_error is not None or lookup_attempted)
    if not cid and not incoming and not fee_only:
        return FeeGateResult(False, REASON_CATEGORY_UNKNOWN, (), cid, path)
    state = load_blacklist_state(blacklist_path)
    cat_reason = category_match_reason(
        cid,
        path,
        category_name,
        blacklist=state,
        blacklist_path=blacklist_path,
    )
    if cat_reason == REASON_CATEGORY_UNKNOWN and fee_only:
        cat_reason = None
    if cat_reason:
        return FeeGateResult(False, cat_reason, (), cid, path)

    if preview is None and not lookup_attempted and listing_fees is None and lookup_error is None:
        return FeeGateResult(True, None, (), cid, path)

    raw: FeeGateRaw
    if preview is not None:
        try:
            raw = _normalize_raw(preview())
        except Exception as exc:
            raw = FeeGateRaw(source=SOURCE_INVENTORY, error=exc)
    elif lookup_error is not None:
        raw = FeeGateRaw(source=SOURCE_INVENTORY, error=lookup_error)
    else:
        raw = _normalize_raw(listing_fees)

    parsed = parse_fee_preview(raw)
    if isinstance(parsed, str):
        return FeeGateResult(False, parsed, (), cid, path)
    fee_reason = _nonzero_reason(parsed)
    if fee_reason:
        return FeeGateResult(False, fee_reason, parsed, cid, path)
    return FeeGateResult(True, None, parsed, cid, path)


def _format_amount(value: Decimal) -> str:
    return format(value, "f")


def fee_preview_payload(
    result: FeeGateResult,
    *,
    path_name: str,
    sku: str,
    offer_id: str = "",
    item_id: str = "",
) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "reason": result.reason,
        "sku": sku,
        "path_name": path_name,
        "offer_id": offer_id,
        "item_id": item_id,
        "category_id": result.category_id,
        "category_path": result.category_path,
        "lines": [
            {
                "fee_type": line.fee_type,
                "amount": _format_amount(line.amount),
                "currency": line.currency,
                "source": line.source,
                "promotional_discount": (
                    _format_amount(line.promotional_discount)
                    if line.promotional_discount is not None
                    else None
                ),
            }
            for line in result.lines
        ],
    }


def log_fee_gate(
    result: FeeGateResult,
    *,
    path_name: str,
    sku: str,
    offer_id: str = "",
    item_id: str = "",
) -> None:
    log = logger.warning if not result.ok else logger.info
    if result.lines:
        for line in result.lines:
            log(
                "fee_gate sku=%s path_name=%s offer_id=%s item_id=%s category_id=%s "
                "category_path=%s fee_type=%s amount=%s currency=%s source=%s "
                "promotional_discount=%s gate_reason=%s",
                sku,
                path_name,
                offer_id,
                item_id,
                result.category_id,
                result.category_path,
                line.fee_type,
                _format_amount(line.amount),
                line.currency,
                line.source,
                (
                    _format_amount(line.promotional_discount)
                    if line.promotional_discount is not None
                    else ""
                ),
                result.reason or "ok",
            )
    else:
        log(
            "fee_gate sku=%s path_name=%s offer_id=%s item_id=%s category_id=%s "
            "category_path=%s fee_type= amount= currency= source= "
            "promotional_discount= gate_reason=%s",
            sku,
            path_name,
            offer_id,
            item_id,
            result.category_id,
            result.category_path,
            result.reason or "ok",
        )


def append_fee_preview(results_sink: list | None, payload: Mapping[str, Any]) -> None:
    if results_sink is None:
        return
    results_sink.append(dict(payload))


def assert_zero_fee_before_live(
    *,
    path_name: str,
    sku: str,
    category_id: str,
    category_path: str,
    preview: Callable[[], Any],
    offer_id: str = "",
    item_id: str = "",
    results_sink: list | None = None,
    blacklist_path: Optional[str] = None,
    category_name: str = "",
) -> FeeGateResult:
    result = evaluate_fee_gate(
        category_id=category_id,
        category_path=category_path,
        category_name=category_name,
        preview=preview,
        lookup_attempted=True,
        blacklist_path=blacklist_path,
    )
    payload = fee_preview_payload(
        result,
        path_name=path_name,
        sku=sku,
        offer_id=offer_id,
        item_id=item_id,
    )
    log_fee_gate(result, path_name=path_name, sku=sku, offer_id=offer_id, item_id=item_id)
    append_fee_preview(results_sink, payload)
    if not result.ok:
        raise ZeroFeeGateBlocked(result)
    return result


def preview_inventory_listing_fees(ebay_client: Any, offer_id: str) -> FeeGateRaw:
    getter = getattr(ebay_client, "get_listing_fees", None)
    if not callable(getter):
        raise RuntimeError("get_listing_fees is not available")
    payload = getter(offer_id)
    return FeeGateRaw(source=SOURCE_INVENTORY, payload=payload, http_status=200)


def preview_inventory_group_listing_fees(ebay_client: Any, offer_ids: list[str]) -> FeeGateRaw:
    ids = [str(item).strip() for item in offer_ids if str(item or "").strip()]
    if not ids:
        raise RuntimeError("no offer ids for inventory item group fee preview")
    lines: list[dict[str, Any]] = []
    last_payload: Any = None
    for offer_id in ids:
        raw = preview_inventory_listing_fees(ebay_client, offer_id)
        parsed = parse_fee_preview(raw)
        if isinstance(parsed, str):
            return raw
        last_payload = raw.payload
        if isinstance(raw.payload, Mapping):
            lines.extend(raw.payload.get("feeSummaries") or [])
    if lines:
        return FeeGateRaw(
            source=SOURCE_INVENTORY,
            payload={"feeSummaries": lines},
            http_status=200,
        )
    return FeeGateRaw(source=SOURCE_INVENTORY, payload=last_payload, http_status=200)


def withdraw_unpublished_offer(ebay_client: Any, offer_id: str) -> None:
    deleter = getattr(ebay_client, "delete_offer", None)
    if not callable(deleter):
        logger.warning("Cannot withdraw unpublished offer %s: delete_offer missing", offer_id)
        return
    try:
        deleter(offer_id)
        logger.info("Withdrew unpublished offer %s after fee gate block", offer_id)
    except Exception as exc:
        logger.warning("Failed to withdraw unpublished offer %s: %s", offer_id, exc)


def publish_offer_if_zero_fee(
    *,
    ebay_client: Any,
    offer_id: str,
    path_name: str,
    sku: str,
    category_id: str,
    category_path: str = "",
    category_name: str = "",
    results_sink: list | None = None,
    withdraw_on_block: bool = True,
    blacklist_path: Optional[str] = None,
) -> Any:
    resolved_path = str(category_path or "") or resolve_category_taxonomy_path(category_id)
    try:
        assert_zero_fee_before_live(
            path_name=path_name,
            sku=sku,
            category_id=category_id,
            category_path=resolved_path,
            category_name=category_name,
            preview=lambda: preview_inventory_listing_fees(ebay_client, offer_id),
            offer_id=str(offer_id or ""),
            results_sink=results_sink,
            blacklist_path=blacklist_path,
        )
    except ZeroFeeGateBlocked:
        if withdraw_on_block:
            withdraw_unpublished_offer(ebay_client, offer_id)
        raise
    return ebay_client.publish_offer(offer_id)


def wrapper_reason(result: FeeGateResult) -> str | None:
    """Map gate reasons onto the existing evaluate_insertion_fee_preview contract."""
    if result.ok:
        return None
    if result.reason in {REASON_HIGH_FEE_CATEGORY, REASON_FEE_NONZERO}:
        return "high_insertion_fee"
    return result.reason


def human_block_reason(result: FeeGateResult, *, category_name: str = "") -> str | None:
    if result.ok:
        return None
    label = result.category_id or category_name or result.category_path or "?"
    if result.reason == REASON_HIGH_FEE_CATEGORY:
        return (
            f"BLOCKED_HIGH_FEE: category {label} "
            "is on the high-fee blacklist or path-segment list"
        )
    if result.reason == REASON_FEE_NONZERO:
        amounts = ", ".join(
            f"{line.fee_type}={_format_amount(line.amount)} {line.currency}"
            for line in result.lines
            if line.amount > _ZERO
        )
        return f"BLOCKED_HIGH_FEE: category {label} fee {amounts}"
    if result.reason == REASON_BLACKLIST_UNAVAILABLE:
        return (
            f"BLOCKED_HIGH_FEE_UNVERIFIED: category {label} "
            "high-fee blacklist missing, unreadable, or empty"
        )
    if result.reason == REASON_CATEGORY_UNKNOWN:
        return (
            f"BLOCKED_HIGH_FEE_UNVERIFIED: category {label} "
            "id and path unresolved"
        )
    return (
        f"BLOCKED_HIGH_FEE_UNVERIFIED: category {label} "
        f"{result.reason or 'fee preview unavailable'}"
    )
