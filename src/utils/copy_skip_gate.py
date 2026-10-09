"""Cheap publishability gates that run before any LLM listing copy.

Unlistable SKUs must not pay for titles, descriptions, bullets, FactSheet
invents, or image-caption generation. The checks here use only data already
on the product (plus an optional live supplier quote). They do not invent
dimensions, materials, or Year/Make/Model.

Reason codes (stored on the product log and ``cost_breakdown``):

- ``blacklist_seller`` — SKU matches the N735* / known skip-seller prefixes
- ``cargo_cost_over_200`` — supplier product + cargo (shipping) > USD 200
- ``unclear_dims`` — item L/W/H missing, placeholder, or not source-grounded
- ``far_above_market`` — a real market median exists and the SAFE_15 price
  still sits above the near-market band (median × 1.15)
- ``factsheet_material_conflict`` — source-grounded material clash that
  FactSheet already fails without an LLM invent

``SAFE_15_NO_MARKET`` (no median) stays publishable. Do not skip copy for
missing market data alone.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from src.services.pricing_engine import PricingEngine
from src.utils.dimension_helpers import (
    extract_numeric_inches,
    extract_product_dimensions_from_text,
)
from src.utils.listing_fact_sheet import (
    compare_fact_sheets,
    lexical_materials,
)

# N735* is the standing seller blacklist. Extend this tuple for other
# known skip-seller prefixes (matched at the start of the SKU).
SKIP_SELLER_PREFIXES: tuple[str, ...] = ("N735",)

# Supplier product price + cargo/shipping. Insurance and payment fees are
# not part of this cap.
MAX_SUPPLIER_CARGO_COST_USD = 200.0

# Near-market band ceiling used when dry-run would still publish at SAFE_15.
# Same 15% over median as conversion_diagnoser.FLOOR_OVER_MARKET_LOCK /
# PRICE_OVERPRICED and sales-health OVERPRICED_THRESHOLD.
NEAR_MARKET_BAND = 1.15

REASON_BLACKLIST_SELLER = "blacklist_seller"
REASON_CARGO_COST = "cargo_cost_over_200"
REASON_UNCLEAR_DIMS = "unclear_dims"
REASON_FAR_ABOVE = "far_above_market"
REASON_MATERIAL_CONFLICT = "factsheet_material_conflict"

COPY_SKIP_STATUS = "SKIPPED"

_AXIS_KEY = {
    "length": re.compile(
        r"^(?:assembled|overall|product|item)\s+length\b",
        re.IGNORECASE,
    ),
    "width": re.compile(
        r"^(?:assembled|overall|product|item)\s+width\b",
        re.IGNORECASE,
    ),
    "height": re.compile(
        r"^(?:assembled|overall|product|item)\s+height\b",
        re.IGNORECASE,
    ),
}

_PRIMARY_MATERIAL_KEYS = {
    "material",
    "main material",
    "frame material",
    "upholstery material",
    "seat material",
    "top material",
    "材质",
    "主材质",
}

_PLACEHOLDER_DIM = re.compile(
    r"\b(?:see\s+description|refer\s+to|not\s+specified|not\s+applicable|"
    r"n/?a|unknown)\b",
    re.IGNORECASE,
)


@dataclass
class CopySkipDecision:
    skip: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return self.reasons[0] if self.reasons else ""


def _utcnow_stamp() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def seller_is_blacklisted(sku: str) -> bool:
    text = str(sku or "").strip().upper()
    if not text:
        return False
    return any(text.startswith(prefix.upper()) for prefix in SKIP_SELLER_PREFIXES)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def supplier_cargo_cost(product_price: Any, shipping: Any) -> float | None:
    """GIGA order base: product price + cargo/shipping. None when unknown."""
    price = _as_float(product_price)
    ship = _as_float(shipping)
    if price is None and ship is None:
        return None
    price = 0.0 if price is None else price
    ship = 0.0 if ship is None else ship
    if price < 0 or ship < 0:
        return None
    if price == 0.0 and ship == 0.0:
        return None
    return price + ship


def lookup_live_supplier_quote(sku: str) -> dict[str, Any] | None:
    """Live DaJian price + shipping when credentials exist. Never raises.

    Returns ``{"price": float, "shipping": float}`` or None. A missing quote
    must not be treated as zero cost.
    """
    key = os.getenv("DAJIAN_API_KEY")
    secret = os.getenv("DAJIAN_API_SECRET")
    if not key or not secret or not str(sku or "").strip():
        return None
    try:
        from src.clients.dajian_client import DaJianClient

        info = DaJianClient(key, secret).get_stock_info(sku) or {}
    except Exception:
        return None
    if info.get("error"):
        return None
    price = _as_float(info.get("price"))
    shipping = _as_float(info.get("shipping_fee"))
    if (price is None or price <= 0) and (shipping is None or shipping <= 0):
        return None
    quote: dict[str, Any] = {}
    if price is not None and price > 0:
        quote["price"] = price
    if shipping is not None and shipping >= 0 and "price" in quote:
        quote["shipping"] = shipping
    return quote or None


def apply_live_supplier_quote(product: Any, quote: Mapping[str, Any] | None) -> bool:
    """Write a live quote onto ``product.price`` / ``product.shipping``."""
    if not quote:
        return False
    changed = False
    price = _as_float(quote.get("price"))
    shipping = _as_float(quote.get("shipping"))
    if price is not None and price > 0 and _as_float(getattr(product, "price", None)) != price:
        product.price = price
        changed = True
    if shipping is not None and shipping >= 0 and _as_float(getattr(product, "shipping", None)) != shipping:
        product.shipping = shipping
        changed = True
    return changed


def _is_package_key(key: str) -> bool:
    return bool(re.search(r"\b(package|shipping|carton|box)\b", str(key or ""), re.IGNORECASE))


def _axis_for_key(key: str) -> str | None:
    text = str(key or "").strip()
    if not text or _is_package_key(text):
        return None
    for axis, pattern in _AXIS_KEY.items():
        if pattern.search(text):
            return axis
    return None


def _is_placeholder_dim(raw: Any) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    return bool(_PLACEHOLDER_DIM.search(text))


def resolve_item_dimensions(
    attributes: Mapping[str, Any] | None,
    specs: Mapping[str, Any] | None,
    description: str = "",
) -> tuple[dict[str, float | None], bool]:
    """Source-grounded item L/W/H. Package dims are ignored.

    Returns ``(dims, saw_placeholder)``. Missing axes stay None — callers
    must not invent a number.
    """
    found: dict[str, float | None] = {"length": None, "width": None, "height": None}
    saw_placeholder = False
    for source in (attributes or {}, specs or {}):
        if not isinstance(source, Mapping):
            continue
        for key, raw in source.items():
            axis = _axis_for_key(str(key))
            if axis is None:
                continue
            if _is_placeholder_dim(raw):
                saw_placeholder = True
                continue
            if found[axis] is not None:
                continue
            number = extract_numeric_inches(str(raw)) if raw not in (None, "") else None
            if number is not None and 0 < number <= 500:
                found[axis] = number
    if not all(found[axis] for axis in ("length", "width", "height")):
        parsed = extract_product_dimensions_from_text(description or "")
        for axis in ("length", "width", "height"):
            if found[axis] is not None:
                continue
            value = parsed.get(axis)
            if value is not None and 0 < float(value) <= 500:
                found[axis] = float(value)
    return found, saw_placeholder


def _dims_incomplete(dims: Mapping[str, Any]) -> bool:
    return any(dims.get(axis) is None for axis in ("length", "width", "height"))


def _sheet(materials: set[str] | list[str]) -> dict[str, Any]:
    return {
        "materials": sorted({str(item).strip().lower() for item in materials if str(item).strip()}),
        "features": [],
        "counts": {},
        "capacity": None,
        "certifications": [],
        "dimensions": {},
    }


def _structured_material_values(
    attributes: Mapping[str, Any] | None,
    specs: Mapping[str, Any] | None,
) -> list[str]:
    values: list[str] = []
    for source in (attributes or {}, specs or {}):
        if not isinstance(source, Mapping):
            continue
        for key, raw in source.items():
            if str(key or "").strip().lower() not in _PRIMARY_MATERIAL_KEYS:
                continue
            text = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
            rendered = str(text or "").strip()
            if rendered:
                values.append(rendered)
    return values


def _upgrade_conflicts(title: str, structured_values: list[str], description: str) -> list[str]:
    """Title already names a forbidden material upgrade of a structured source.

    ``claim_diff_engine`` flags these as CRITICAL without an LLM. Shared tokens
    such as leather-in-PU-leather do not hide the upgrade. Description text
    that already states the upgrade is source-grounded and is not a conflict.
    """
    from src.utils.claim_diff_engine import MATERIAL_UPGRADE_CHAINS

    structured_blob = " ".join(structured_values).lower()
    grounded = f"{structured_blob}\n{description or ''}".lower()
    title_l = (title or "").lower()
    found: list[str] = []
    for base, upgrades in MATERIAL_UPGRADE_CHAINS.items():
        if not re.search(rf"\b{re.escape(base)}\b", structured_blob):
            continue
        for upgrade in upgrades:
            if not re.search(rf"\b{re.escape(upgrade)}\b", title_l):
                continue
            if re.search(rf"\b{re.escape(upgrade)}\b", grounded):
                continue
            if upgrade not in found:
                found.append(upgrade)
    return found


def source_hard_material_conflict(
    *,
    title: str = "",
    description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
) -> list[str]:
    """Material clashes that already fail with no LLM invent.

    Two deterministic checks, both source-grounded:

    - FactSheet ``compare_fact_sheets`` on structured materials vs title
      lexicon hits (canvas vs rubberwood). Description may ground a title
      word. Aluminium/aluminum and other synonym groups do not conflict.
    - Material-upgrade chains (PU leather → genuine leather, MDF → solid
      wood). FactSheet token overlap hides some of these; the upgrade table
      does not.

    No structured material field means we cannot prove a conflict yet.
    """
    structured_values = _structured_material_values(attributes, specs)
    if not structured_values:
        return []
    source_materials: set[str] = set()
    for value in structured_values:
        source_materials |= lexical_materials(value)
    title_materials = lexical_materials(title or "")
    claims: list[str] = []
    if source_materials and title_materials:
        source_text = " ".join(structured_values)
        if description:
            source_text = f"{source_text}\n{description}"
        violations = compare_fact_sheets(
            _sheet(source_materials),
            _sheet(title_materials),
            live_text=title or "",
            source_text=source_text,
        )
        for violation in violations:
            if violation.get("claim_type") != "semantic_material":
                continue
            if violation.get("severity") != "CRITICAL":
                continue
            claim = str(violation.get("claim_text") or "").strip()
            if claim and claim not in claims:
                claims.append(claim)
    for upgrade in _upgrade_conflicts(title, structured_values, description):
        if upgrade not in claims:
            claims.append(upgrade)
    return claims


def safe_15_price(product_price: Any, shipping: Any, *, is_oversize: bool = False) -> float | None:
    cost = supplier_cargo_cost(product_price, shipping)
    if cost is None:
        return None
    # SAFE_15 is computed on landed GIGA cost, matching batch_publish.
    landed = PricingEngine.calculate_dajian_cost(
        product_price or 0,
        shipping or 0,
        is_oversize=is_oversize,
    )["total_dajian_cost"]
    if not landed or landed <= 0:
        return None
    return float(PricingEngine.calculate_selling_price(landed, 0.15)["selling_price"])


def market_median_from_intel(intel: Mapping[str, Any] | None) -> float | None:
    if not intel:
        return None
    stats = intel.get("price_stats") or {}
    if not isinstance(stats, Mapping):
        return None
    for key in ("median", "average", "avg"):
        number = _as_float(stats.get(key))
        if number is not None and number > 0:
            return number
    return None


def cargo_gate_applies(specs: Mapping[str, Any] | None, *, live_quote_applied: bool) -> bool:
    """eBay-link drafts store an observed listing price, not GIGA cargo.

    A live supplier quote replaces that price, so the cargo cap applies again.
    """
    if live_quote_applied:
        return True
    basis = str((specs or {}).get("_price_basis") or "").strip().lower()
    return basis != "ebay_listing"


def evaluate_copy_skip(
    *,
    sku: str = "",
    product_price: Any = None,
    shipping: Any = None,
    is_oversize: bool = False,
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    description: str = "",
    title: str = "",
    market_median: Any = None,
    include_market: bool = True,
    dims_mode: str = "require",
    apply_cargo: bool = True,
) -> CopySkipDecision:
    """Return every cheap skip reason that already applies.

    ``dims_mode="require"`` (analyze): missing item L/W/H skips copy.
    ``dims_mode="defer_missing"`` (collect intake): only explicit placeholders
    that still do not resolve skip. Empty dims wait for supplier enrichment
    inside analyze. ``include_market=False`` skips the far-above check so
    callers can avoid a Browse/Terapeak fetch for items already blocked.
    """
    reasons: list[str] = []
    detail: dict[str, Any] = {"sku": str(sku or "")}

    if seller_is_blacklisted(sku):
        reasons.append(REASON_BLACKLIST_SELLER)
        detail["seller_prefix"] = next(
            prefix for prefix in SKIP_SELLER_PREFIXES if str(sku or "").upper().startswith(prefix.upper())
        )

    cargo = supplier_cargo_cost(product_price, shipping)
    if cargo is not None:
        detail["supplier_cargo_cost_usd"] = round(cargo, 2)
        if apply_cargo and cargo > MAX_SUPPLIER_CARGO_COST_USD:
            reasons.append(REASON_CARGO_COST)

    dims, saw_placeholder = resolve_item_dimensions(attributes, specs, description)
    detail["item_dims"] = {axis: dims.get(axis) for axis in ("length", "width", "height")}
    incomplete = _dims_incomplete(dims)
    if dims_mode == "defer_missing":
        if incomplete and saw_placeholder:
            reasons.append(REASON_UNCLEAR_DIMS)
            detail["dims_state"] = "placeholder"
    elif incomplete:
        reasons.append(REASON_UNCLEAR_DIMS)
        detail["dims_state"] = "placeholder" if saw_placeholder else "missing"

    conflicts = source_hard_material_conflict(
        title=title,
        description=description,
        attributes=attributes,
        specs=specs,
    )
    if conflicts:
        reasons.append(REASON_MATERIAL_CONFLICT)
        detail["material_conflicts"] = conflicts

    median = _as_float(market_median)
    if include_market and median is not None and median > 0:
        detail["market_median"] = median
        safe_price = safe_15_price(product_price, shipping, is_oversize=is_oversize)
        ceiling = median * NEAR_MARKET_BAND
        detail["near_market_ceiling"] = round(ceiling, 2)
        if safe_price is not None:
            detail["safe_15_price"] = safe_price
            if safe_price > ceiling:
                reasons.append(REASON_FAR_ABOVE)
    elif include_market:
        detail["pricing_basis"] = "SAFE_15_NO_MARKET"

    # Stable order even if a caller inspects a partial evaluation.
    order = (
        REASON_BLACKLIST_SELLER,
        REASON_CARGO_COST,
        REASON_UNCLEAR_DIMS,
        REASON_MATERIAL_CONFLICT,
        REASON_FAR_ABOVE,
    )
    ranked = [code for code in order if code in reasons]
    return CopySkipDecision(skip=bool(ranked), reasons=ranked, detail=detail)


def copy_skip_log_lines(decision: CopySkipDecision, *, at: str | None = None) -> list[str]:
    stamp = at or _utcnow_stamp()
    lines = [f"copy_skip:{code} at {stamp}" for code in decision.reasons]
    if decision.reasons:
        lines.append(f"copy_skip reasons={','.join(decision.reasons)}")
    return lines


def merge_copy_skip_cost(
    cost_breakdown: Mapping[str, Any] | None,
    decision: CopySkipDecision,
) -> dict[str, Any]:
    merged = dict(cost_breakdown or {})
    merged["copy_skip_reasons"] = list(decision.reasons)
    merged["copy_skip_detail"] = dict(decision.detail)
    return merged


def apply_copy_skip(
    product: Any,
    decision: CopySkipDecision,
    *,
    cost_breakdown: Mapping[str, Any] | None = None,
    at: str | None = None,
) -> None:
    """Mark a product skipped. Does not write listing title/description copy."""
    if not decision.skip:
        return
    product.status = COPY_SKIP_STATUS
    logs = list(getattr(product, "logs", None) or [])
    logs.extend(copy_skip_log_lines(decision, at=at))
    product.logs = logs
    base = cost_breakdown if cost_breakdown is not None else getattr(product, "cost_breakdown", None)
    product.cost_breakdown = merge_copy_skip_cost(base, decision)
