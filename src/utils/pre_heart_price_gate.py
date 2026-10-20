"""Pre-heart / pre-collect price gate using GIGA list cargo + cached eBay market.

Runs BEFORE wishlist heart and detail collect so far-above SKUs never burn
heart/collect/FactSheet/LLM effort. Complements ``copy_skip_gate`` which still
blocks LLM copy at analyze when a live median arrives later.

Drop rule (same band as conversion_diagnoser.FLOOR_OVER_MARKET_LOCK /
copy_skip_gate.NEAR_MARKET_BAND = 1.30):

    SAFE_15(list_price, list_shipping) > market_median * 1.30
        → drop with reason FLOOR_OVER_MARKET_LOCK when MARKET_GATE_ENABLED
          is true; otherwise keep with far_above_market_advisory=True

If no Browse/Terapeak cache median is available, allow through and set
``no_market_cache=True`` (same spirit as SAFE_15_NO_MARKET at analyze).
Missing shipping is NOT treated as 0 (that understated SAFE_15 and
false-kept SKUs that later failed at analyze). Preferred path: fetch
real GIGA ``shippingFee`` via the price API before the gate decision.
If shipping is still unknown after the fetch, keep with
``shipping_unknown=True`` and skip the market drop (do not invent freight).

Cache lookup order:
1. Exact Browse cache key from title search-words (same hash as QwenOptimizer)
2. Exact key from an optional niche/search keyword
3. Soft overlap against cached top_keywords (optional; default on)
"""

from __future__ import annotations

import hashlib
import os
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.utils.copy_skip_gate import (
    NEAR_MARKET_BAND,
    market_gate_enabled,
    market_median_from_intel,
    safe_15_price,
    supplier_cargo_cost,
)

# Alias kept explicit for callers / logs (Sergey standing name).
FLOOR_OVER_MARKET_LOCK = NEAR_MARKET_BAND  # 1.30

REASON_FLOOR_OVER_MARKET_LOCK = "FLOOR_OVER_MARKET_LOCK"

_NOISE_WORDS = {
    "with",
    "and",
    "for",
    "the",
    "set",
    "of",
    "in",
    "to",
    "a",
    "an",
    "new",
    "brand",
    "hot",
    "sale",
    "free",
    "shipping",
    "fast",
    "black",
    "white",
    "gray",
    "grey",
    "brown",
    "beige",
    "blue",
    "red",
    "green",
    "pink",
    "gold",
    "silver",
    "natural",
    "walnut",
    "oak",
    "small",
    "medium",
    "large",
    "xl",
    "xxl",
    # Dimension / pack / ambient fluff that poisoned Browse queries
    # (e.g. "Inch Decorative Tropical Ceiling Fan" → median ).
    "inch",
    "inches",
    "foot",
    "feet",
    "ft",
    "cm",
    "mm",
    "lbs",
    "lb",
    "kg",
    "pound",
    "pounds",
    "pc",
    "pcs",
    "piece",
    "pieces",
    "tier",
    "tiers",
    "pack",
    "packs",
    "pair",
    "pairs",
    "decorative",
    "decor",
    "decoration",
    "indoor",
    "outdoor",
    "covered",
    "reversible",
    "remote",
    "control",
    "controls",
    "video",
    "living",
    "room",
    "rooms",
    "home",
    "office",
    "house",
    "patio",
    "bedroom",
    "study",
    "kitchen",
    "bathroom",
    "entryway",
    "hallway",
    "closet",
    "garden",
    "yard",
    "kids",
    "kid",
    "children",
    "adult",
    "adults",
    "modern",
    "vintage",
    "style",
    "stylish",
    "elegant",
    "premium",
    "luxury",
    "heavy",
    "duty",
    "quiet",
    "speed",
    "speeds",
    "motor",
    "brass",
    "clear",
    "soft",
    "close",
    "hidden",
    "built",
    "usb",
    "port",
    "phone",
    "holder",
    "size",
    "twin",
    "full",
    "queen",
    "king",
}

# Multi-word product nouns first (longest match wins). Keep furniture +
# common GIGA NA types that soft_top_keywords previously mangled.
_PRODUCT_PHRASES: tuple[str, ...] = (
    "sectional sofa",
    "modular sofa",
    "sleeper sofa",
    "reclining sofa",
    "sofa bed",
    "gaming chair",
    "office chair",
    "dining chair",
    "accent chair",
    "rocking chair",
    "lounge chair",
    "bar stool",
    "bar stools",
    "counter stool",
    "coffee table",
    "dining table",
    "side table",
    "end table",
    "console table",
    "kitchen island",
    "standing desk",
    "bed frame",
    "platform bed",
    "loft bed",
    "bunk bed",
    "murphy bed",
    "tv stand",
    "entertainment center",
    "media console",
    "storage cabinet",
    "display cabinet",
    "wine cabinet",
    "file cabinet",
    "sideboard buffet",
    "buffet cabinet",
    "bathroom vanity",
    "ceiling fan",
    "chandelier fan",
    "flush mount",
    "pendant light",
    "shoe rack",
    "shoe storage",
    "plant stand",
    "plant shelf",
    "artificial ficus tree",
    "ficus tree",
    "artificial tree",
    "artificial plant",
    "jump starter",
    "fire pit",
    "adirondack chair",
    "folding chair",
    "dog ramp",
    "dog treadmill",
    "garden statue",
    "solar statue",
    "jewelry armoire",
    "sandpit",
    "sandbox",
    "bookshelf",
    "bookcase",
    "nightstand",
    "dresser",
    "wardrobe",
    "ottoman",
    "loveseat",
    "recliner",
    "chaise lounge",
    "sofa",
    "couch",
    "futon",
    "daybed",
    "chair",
    "table",
    "desk",
    "bed",
    "cabinet",
    "vanity",
    "mirror",
    "bench",
    "stool",
    "fan",
    "tree",
    "rack",
    "shelf",
    "stand",
    "credenza",
    "sideboard",
    "buffet",
    "island",
)

_PRODUCT_STEMS = {
    "fan",
    "bookcase",
    "bookshelf",
    "sofa",
    "couch",
    "chair",
    "table",
    "desk",
    "bed",
    "cabinet",
    "vanity",
    "mirror",
    "bench",
    "stool",
    "tree",
    "plant",
    "rack",
    "shelf",
    "stand",
    "credenza",
    "sideboard",
    "buffet",
    "island",
    "ottoman",
    "loveseat",
    "recliner",
    "futon",
    "daybed",
    "dresser",
    "nightstand",
    "wardrobe",
    "lamp",
    "light",
    "chandelier",
    "fountain",
    "statue",
    "ramp",
    "treadmill",
    "sandbox",
    "sandpit",
    "armoire",
}

_KEEP_MODIFIERS = (
    "tropical",
    "palm",
    "artificial",
    "ficus",
    "freestanding",
    "folding",
    "modular",
    "sectional",
    "convertible",
    "reclining",
    "sleeper",
    "storage",
    "narrow",
    "corner",
    "swivel",
    "upholstered",
    "leather",
    "fabric",
    "velvet",
    "wood",
    "wooden",
    "metal",
    "glass",
    "marble",
    "crystal",
    "mahogany",
    "gingham",
    "geometric",
    "carved",
    "low",
    "tall",
    "solid",
)

_DEFAULT_TTL_SECS = 6 * 60 * 60  # match QwenOptimizer browse cache TTL


@dataclass
class PreHeartPriceDecision:
    drop: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def allow(self) -> bool:
        return not self.drop


def extract_search_keywords(title: str) -> str:
    """Build a Browse/market-cache query that prefers product-type nouns.

    Shared by pre-heart gate and (via QwenOptimizer delegate) cache warm.
    Strips dimension units, pack counts, room/ambient fluff, and brand-y
    adjectives so we do not query "Inch Decorative Tropical Ceiling Fan".
    """
    if not title:
        return ""
    cleaned = re.sub(r"\[.*?\]", " ", title)
    cleaned = re.sub(
        r"\d+(?:\.\d+)?\s*(?:inch(?:es)?|in\b|ft\b|feet|foot|cm\b|mm\b|lbs?\b|kg\b|pounds?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bset\s+of\s+\d+\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+\s*(?:pc|pcs|piece|pieces|tier|tiers|-pack|pack)s?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\d+(?:\.\d+)?", " ", cleaned)
    cleaned = re.sub(r"[^a-zA-Z\s&-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    title_lower = cleaned.lower()

    # Earliest word-boundary match wins; longer phrase preferred at same start.
    # Avoids "stand" matching inside "standing", and "display cabinet"
    # stealing a title that leads with Bookcase.
    core = None
    best_key = None  # (start, -length)
    for phrase in _PRODUCT_PHRASES:
        pat = r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b"
        m = re.search(pat, title_lower)
        if not m:
            continue
        key = (m.start(), -len(phrase))
        if best_key is None or key < best_key:
            best_key = key
            core = phrase

    words = re.findall(r"[a-zA-Z]+", cleaned)
    kept = [w for w in words if w.lower() not in _NOISE_WORDS and len(w) > 2]

    if core:
        core_tokens = core.split()
        mods: list[str] = []
        for w in kept:
            wl = w.lower()
            if wl in core_tokens or wl in {m.lower() for m in mods}:
                continue
            if wl in _KEEP_MODIFIERS or any(wl == m for m in _KEEP_MODIFIERS):
                mods.append(w)
                if len(mods) >= 2:
                    break
        # Preserve original capitalization from title when possible
        core_out = []
        lower_map = {w.lower(): w for w in words}
        for tok in core_tokens:
            core_out.append(lower_map.get(tok, tok))
        parts = mods + core_out
        return " ".join(parts[:5])

    return " ".join(kept[:5])


def _product_stems_in(tokens: set[str]) -> set[str]:
    return {t for t in tokens if t in _PRODUCT_STEMS}


def browse_cache_path(cache_dir: Path, search_words: str, category_id: str | None = None) -> Path:
    key = hashlib.sha1(f"{search_words}|{category_id or ''}".encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{key}.json"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None



def fetch_giga_price_quotes(skus: Sequence[str]) -> dict[str, dict[str, float]]:
    """Batch-read GIGA price + shippingFee.

    Returns `{sku: {"price": float, "shipping": float}}` for fields that
    are present. Missing/null fields are omitted (never coerced to 0).
    """
    out: dict[str, dict[str, float]] = {}
    clean = [str(s).strip() for s in skus if str(s or "").strip()]
    if not clean:
        return out
    key = os.getenv("DAJIAN_API_KEY")
    secret = os.getenv("DAJIAN_API_SECRET")
    if not key or not secret:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        key = os.getenv("DAJIAN_API_KEY")
        secret = os.getenv("DAJIAN_API_SECRET")
    if not key or not secret:
        return out
    try:
        from src.clients.dajian_client import DaJianClient

        client = DaJianClient(key, secret)
        for i in range(0, len(clean), 200):
            chunk = clean[i : i + 200]
            rows = client.get_product_prices(chunk) or []
            by_sku: dict[str, Mapping[str, Any]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                sku = str(row.get("sku") or "").strip()
                if sku:
                    by_sku[sku] = row
            for sku in chunk:
                info = by_sku.get(sku)
                if not info:
                    continue
                quote: dict[str, float] = {}
                price = _as_float(info.get("price"))
                if price is None:
                    price = _as_float(info.get("discountedPrice")) or _as_float(info.get("exclusivePrice"))
                if price is not None:
                    quote["price"] = float(price)
                raw = info.get("shippingFee")
                if raw is None or raw == "":
                    rng = info.get("shippingFeeRange")
                    if isinstance(rng, Mapping):
                        raw = rng.get("minAmount")
                ship = _parse_shipping(raw)
                if ship is not None:
                    quote["shipping"] = float(ship)
                if quote:
                    out[sku] = quote
    except Exception:
        pass
    return out


def fetch_giga_shipping_fees(skus: Sequence[str]) -> dict[str, float | None]:
    """Batch-read GIGA `shippingFee` (wrapper over `fetch_giga_price_quotes`)."""
    quotes = fetch_giga_price_quotes(skus)
    out: dict[str, float | None] = {str(s).strip(): None for s in skus if str(s or "").strip()}
    for sku, quote in quotes.items():
        if "shipping" in quote:
            out[sku] = float(quote["shipping"])
    return out

def _parse_shipping(value: Any) -> float | None:
    """Parse shipping amount. Explicit 0 is valid; None/'' → unknown."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _shipping_provided(shipping: Any) -> bool:
    """True when caller passed an explicit shipping value (including 0)."""
    return _parse_shipping(shipping) is not None


def _load_cache_file(path: Path, *, allow_stale: bool, ttl_secs: float) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        cached_at = float(payload.get("cached_at") or 0)
        age = time.time() - cached_at
        if age < 0:
            return None
        if (not allow_stale) and ttl_secs > 0 and age > ttl_secs:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _median_from_payload(data: Mapping[str, Any] | None) -> float | None:
    if not data:
        return None
    direct = market_median_from_intel(data)
    if direct is not None:
        return direct
    stats = data.get("price_stats") if isinstance(data.get("price_stats"), Mapping) else {}
    for key in ("median", "median_price", "avg", "average", "avg_price"):
        number = _as_float((stats or {}).get(key) or data.get(key))
        if number is not None:
            return number
    return None


def lookup_cached_market_median(
    title: str = "",
    *,
    keyword: str | None = None,
    category_id: str | None = None,
    cache_dirs: Sequence[Path | str] | None = None,
    allow_stale: bool = True,
    ttl_secs: float = _DEFAULT_TTL_SECS,
    soft_match: bool = True,
    min_soft_overlap: int = 2,
) -> dict[str, Any]:
    """Return ``{median, source, search_words, path}`` or empty median.

    ``allow_stale=True`` by default for pre-heart: a slightly old median is
    still enough to skip obvious far-above hearts; analyze will refresh.
    """
    dirs = [Path(p) for p in (cache_dirs or [Path("cache") / "browse_market_intel"])]
    title_words = extract_search_keywords(title)
    candidates: list[tuple[str, str, Path]] = []
    if title_words:
        candidates.append(("title_exact", title_words, Path()))
    kw = " ".join(re.findall(r"[a-zA-Z0-9]+", keyword or "")).strip()
    if kw:
        # Niche scripts pass raw GIGA search strings; normalize like titles.
        kw_norm = extract_search_keywords(kw) or kw.lower()
        candidates.append(("keyword_exact", kw_norm, Path()))
        # Also try lightly-normalized raw kw so older cache keys still hit.
        kw_raw = " ".join(re.findall(r"[a-zA-Z0-9]+", kw)).strip()
        if kw_raw and kw_raw.lower() != kw_norm.lower():
            candidates.append(("keyword_exact_raw", kw_raw, Path()))

    for cache_dir in dirs:
        if not cache_dir.exists():
            continue
        for source, search_words, _ in candidates:
            path = browse_cache_path(cache_dir, search_words, category_id)
            data = _load_cache_file(path, allow_stale=allow_stale, ttl_secs=ttl_secs)
            median = _median_from_payload(data)
            if median is not None:
                return {
                    "median": median,
                    "source": source,
                    "search_words": search_words,
                    "path": str(path),
                    "pricing_basis": (data or {}).get("pricing_basis") or "BROWSE_CACHE",
                    "total_listings": (data or {}).get("total_listings"),
                }

    if not soft_match:
        return {"median": None, "source": "none", "search_words": title_words}

    # Soft match on cleaned product-centric tokens only (not raw title ambient
    # words like Living/Room/Home/Office that false-hit unrelated caches).
    seed = " ".join(x for x in (title_words, extract_search_keywords(keyword or ""), keyword or "") if x)
    query_tokens = {
        t.lower()
        for t in re.findall(r"[a-zA-Z]+", seed)
        if len(t) > 2 and t.lower() not in _NOISE_WORDS
    }
    product_need = _product_stems_in(query_tokens)
    if not query_tokens:
        return {"median": None, "source": "none", "search_words": title_words}

    best: dict[str, Any] | None = None
    best_score = 0
    for cache_dir in dirs:
        if not cache_dir.exists():
            continue
        for path in cache_dir.glob("*.json"):
            data = _load_cache_file(path, allow_stale=allow_stale, ttl_secs=ttl_secs)
            if not data:
                continue
            median = _median_from_payload(data)
            if median is None:
                continue
            top = {str(x).lower() for x in (data.get("top_keywords") or []) if str(x).strip()}
            overlap_set = query_tokens & top
            overlap = len(overlap_set)
            if overlap < min_soft_overlap:
                continue
            # Require at least one shared product-type stem when the query has one.
            if product_need and not (overlap_set & product_need):
                continue
            if overlap > best_score:
                best_score = overlap
                best = {
                    "median": median,
                    "source": "soft_top_keywords",
                    "search_words": title_words,
                    "path": str(path),
                    "overlap": overlap,
                    "pricing_basis": data.get("pricing_basis") or "BROWSE_CACHE",
                    "total_listings": data.get("total_listings"),
                }
    return best or {"median": None, "source": "none", "search_words": title_words}



def evaluate_pre_heart_price(
    *,
    title: str = "",
    product_price: Any = None,
    shipping: Any = None,
    keyword: str | None = None,
    category_id: str | None = None,
    sku: str = "",
    cache_dirs: Sequence[Path | str] | None = None,
    allow_stale: bool = True,
    soft_match: bool = True,
    market_median: Any = None,
    shipping_lookup: Mapping[str, Any] | None = None,
    fetch_shipping: bool = True,
) -> PreHeartPriceDecision:
    """Drop far-above list rows before heart/collect.

    ``market_median`` may be injected (tests / dry-run replay). Otherwise the
    Browse/Terapeak disk cache is consulted.

    Shipping: use the caller value when provided. When missing, look up
    ``shipping_lookup[sku]`` or (if ``fetch_shipping``) the GIGA price API.
    Never default missing freight to 0 — that understated SAFE_15 and
    false-kept items. If still unknown, keep with ``shipping_unknown``.
    """
    detail: dict[str, Any] = {"sku": str(sku or ""), "title": (title or "")[:120]}
    price = _as_float(product_price)
    sku_key = str(sku or "").strip()
    ship: float | None = None
    shipping_unknown = False
    api_price: float | None = None

    if _shipping_provided(shipping):
        ship = float(_parse_shipping(shipping))
        detail["shipping_source"] = "caller"
    else:
        looked: Any = None
        source = None
        if shipping_lookup is not None and sku_key:
            if sku_key in shipping_lookup:
                looked = shipping_lookup[sku_key]
                source = "shipping_lookup"
            elif sku_key.upper() in shipping_lookup:
                looked = shipping_lookup[sku_key.upper()]
                source = "shipping_lookup"
        if looked is None and fetch_shipping and sku_key:
            quote = fetch_giga_price_quotes([sku_key]).get(sku_key) or {}
            if "shipping" in quote:
                looked = quote["shipping"]
            if "price" in quote:
                api_price = quote["price"]
            source = "giga_price_api"
        ship = _parse_shipping(looked)
        if ship is None:
            shipping_unknown = True
            detail["shipping_unknown"] = True
            detail["shipping_missing"] = True  # backward-compatible flag
        else:
            detail["shipping_source"] = source or "shipping_lookup"

    if api_price is not None and not shipping_unknown:
        price = float(api_price)
        detail["product_price_source"] = "giga_price_api"
    if price is None:
        detail["pricing_basis"] = "NO_LIST_PRICE"
        detail["no_market_cache"] = False
        return PreHeartPriceDecision(drop=False, detail=detail)

    detail["product_price"] = price
    if shipping_unknown:
        detail["pricing_basis"] = "SHIPPING_UNKNOWN"
        detail["no_market_cache"] = False
        # Do not invent freight=0 for SAFE_15 / market drop.
        return PreHeartPriceDecision(drop=False, detail=detail)

    assert ship is not None
    detail["shipping"] = ship
    cargo = supplier_cargo_cost(price, ship)
    if cargo is not None:
        detail["supplier_cargo_cost_usd"] = round(cargo, 2)

    safe = safe_15_price(price, ship)
    if safe is None:
        detail["pricing_basis"] = "NO_SAFE_15"
        return PreHeartPriceDecision(drop=False, detail=detail)
    detail["safe_15_price"] = round(float(safe), 2)

    median = _as_float(market_median)
    cache_hit: dict[str, Any] = {}
    if median is None:
        cache_hit = lookup_cached_market_median(
            title,
            keyword=keyword,
            category_id=category_id,
            cache_dirs=cache_dirs,
            allow_stale=allow_stale,
            soft_match=soft_match,
        )
        median = _as_float(cache_hit.get("median"))
        detail["market_cache"] = {
            "source": cache_hit.get("source"),
            "search_words": cache_hit.get("search_words"),
            "path": cache_hit.get("path"),
            "overlap": cache_hit.get("overlap"),
        }

    if median is None:
        detail["no_market_cache"] = True
        detail["pricing_basis"] = "SAFE_15_NO_MARKET"
        return PreHeartPriceDecision(drop=False, detail=detail)

    ceiling = median * FLOOR_OVER_MARKET_LOCK
    detail["market_median"] = round(float(median), 2)
    detail["near_market_ceiling"] = round(float(ceiling), 2)
    detail["floor_over_market_lock"] = FLOOR_OVER_MARKET_LOCK
    detail["no_market_cache"] = False
    detail["ratio_safe_over_market"] = round(float(safe) / float(median), 3) if median else None

    if float(safe) > float(ceiling):
        detail["pricing_basis"] = cache_hit.get("pricing_basis") or "BROWSE_CACHE"
        detail["market_ratio"] = detail.get("ratio_safe_over_market")
        if market_gate_enabled():
            return PreHeartPriceDecision(
                drop=True,
                reason=REASON_FLOOR_OVER_MARKET_LOCK,
                detail=detail,
            )
        # MARKET_GATE_ENABLED=false: keep item; market numbers stay advisory.
        detail["far_above_market_advisory"] = True
        detail["floor_over_market_advisory"] = True

    detail["pricing_basis"] = cache_hit.get("pricing_basis") or "BROWSE_CACHE"
    return PreHeartPriceDecision(drop=False, detail=detail)



def filter_pre_heart_items(
    items: Iterable[Mapping[str, Any]],
    *,
    cache_dirs: Sequence[Path | str] | None = None,
    price_key: str = "cargo_usd",
    shipping_key: str = "shipping",
    title_key: str = "title",
    keyword_key: str = "kw",
    sku_key: str = "sku",
    soft_match: bool = True,
    fetch_shipping: bool = True,
    shipping_lookup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Split list-scrape rows into keep / drop for the heart queue.

    When ``fetch_shipping`` is True, SKUs with missing list shipping are
    batch-resolved via GIGA ``get_product_prices`` before each decision.
    """
    rows = [dict(raw) for raw in items]
    lookup: dict[str, Any] = dict(shipping_lookup or {})
    price_lookup: dict[str, float] = {}
    if fetch_shipping:
        need: list[str] = []
        for row in rows:
            ship_raw = row.get(shipping_key)
            sku = str(row.get(sku_key) or "").strip()
            if sku and not _shipping_provided(ship_raw) and sku not in lookup:
                need.append(sku)
        if need:
            quotes = fetch_giga_price_quotes(need)
            for sku, quote in quotes.items():
                if "shipping" in quote:
                    lookup[sku] = quote["shipping"]
                if "price" in quote:
                    price_lookup[sku] = quote["price"]
    keep: list[dict[str, Any]] = []
    drop: list[dict[str, Any]] = []
    no_cache = 0
    shipping_unknown_n = 0
    far_above_advisory_n = 0
    for row in rows:
        sku = str(row.get(sku_key) or "").strip()
        product_price = row.get(price_key)
        # List scrape often understates price when shipping was absent; prefer
        # the same GIGA price API quote we used for freight.
        if (
            not _shipping_provided(row.get(shipping_key))
            and sku in price_lookup
        ):
            product_price = price_lookup[sku]
            row[price_key] = product_price
            row["giga_api_price"] = product_price
        decision = evaluate_pre_heart_price(
            title=str(row.get(title_key) or ""),
            product_price=product_price,
            shipping=row.get(shipping_key),
            keyword=str(row.get(keyword_key) or "") or None,
            sku=sku,
            cache_dirs=cache_dirs,
            soft_match=soft_match,
            shipping_lookup=lookup or None,
            fetch_shipping=False,  # already batch-fetched above
        )
        row["pre_heart_price"] = {
            "drop": decision.drop,
            "reason": decision.reason,
            "detail": decision.detail,
        }
        if decision.detail.get("no_market_cache"):
            no_cache += 1
        if decision.detail.get("shipping_unknown"):
            shipping_unknown_n += 1
        if decision.detail.get("far_above_market_advisory") or decision.detail.get("floor_over_market_advisory"):
            far_above_advisory_n += 1
        # Persist resolved shipping onto the row for downstream heart/collect.
        if not decision.detail.get("shipping_unknown") and "shipping" in decision.detail:
            if not _shipping_provided(row.get(shipping_key)):
                row[shipping_key] = decision.detail["shipping"]
        if decision.drop:
            drop.append(row)
        else:
            keep.append(row)
    return {
        "keep": keep,
        "drop": drop,
        "stats": {
            "input": len(keep) + len(drop),
            "keep": len(keep),
            "drop": len(drop),
            "no_market_cache": no_cache,
            "shipping_unknown": shipping_unknown_n,
            "far_above_advisory": far_above_advisory_n,
            "drop_reason": REASON_FLOOR_OVER_MARKET_LOCK,
            "band": FLOOR_OVER_MARKET_LOCK,
        },
    }
