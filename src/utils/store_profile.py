"""Per-instance store profile configuration.

Each deployment instance (main furniture account, auto-parts sub-account, ...)
carries its own ``config/store_profile.yaml``. All defaults below equal the
current main-account values, so a missing or partial YAML keeps existing
behavior byte-identical.

Usage:
    from src.utils.store_profile import get_store_profile
    profile = get_store_profile()
    profile.brand_name  # "AquaVerve" on the main instance
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PROFILE_PATH = _PROJECT_ROOT / "config" / "store_profile.yaml"
# Untracked per-instance override: lets a sub-account checkout keep its own
# profile without dirtying the tracked default (which would conflict on pull).
_LOCAL_PROFILE_PATH = _PROJECT_ROOT / "config" / "store_profile.local.yaml"

_lock = threading.Lock()
_cached_profile: Optional["StoreProfile"] = None


@dataclass(frozen=True)
class StoreProfile:
    # Store identity
    brand_name: str = "AquaVerve"
    brand_tagline: str = "PREMIUM HOME FURNISHINGS"
    description_footer_line1: str = "✦ Ships from US Warehouse ✦"
    description_footer_line2: str = "Quality Guaranteed • Fast US Shipping • Trusted Seller"
    promotion_prefix: str = "AquaVerve Auto Sale"
    # What this instance sells. Used to sanity-check collected products against
    # the store they were sent to (see product_router). Kept separate from
    # template_style because a store's copy template can change independently of
    # what it sells (e.g. auto parts ran on the furniture template at first).
    store_kind: str = "furniture"   # furniture | auto | arttoy

    # The Sell-API marketplace for offers/policies. US selling is always EBAY_US —
    # the Inventory API rejects EBAY_MOTORS_US here ("Could not serialize field
    # [marketplaceId]", errorId 2004). Motors is a *catalog*, not a marketplace.
    ebay_marketplace_id: str = "EBAY_US"

    # Taxonomy category tree for category/aspect lookups. Independent of the
    # marketplace above: eBay Motors Parts & Accessories are listed ON EBAY_US
    # but their category ids (33651 Roof Racks, 33653 Trailer Hitches, ...) live
    # only in tree 100 and 400 in tree 0. Auto-parts instances set "100".
    category_tree_id: str = "0"

    # eBay account-owned identifiers
    merchant_location_key: str = "DAJIAN_LA_WAREHOUSE"
    fallback_fulfillment_policy_id: str = "321897899021"
    fallback_return_policy_id: str = "321896608021"
    fallback_payment_policy_id: str = "321896606021"

    # Quality-gate description markers (lowercase substrings)
    quality_banner_marker: str = "aquaverve"
    # 2026-07-22: was "california" — the store footer was changed to
    # "✦ Ships from US Warehouse ✦" on 7/15 but this marker was not updated,
    # so every rebuilt description failed has_footer and the rewrite pipeline
    # rejected 100% of candidates (zero pushes 7/17–7/22). Match on the stable
    # part of the footer instead of a city name.
    quality_footer_marker: str = "ships from"

    # Listing generation (v2). Defaults keep furniture/auto instances unchanged.
    template_style: str = "furniture_classic"   # furniture_classic | arttoy_hype
    footer_html: str = ""                        # whole-block footer; empty => line1/line2
    banned_terms: tuple = ()                     # hard-blocked terms (empty => guard is a no-op)
    # True (furniture/auto): stamp the store brand as the product Brand aspect —
    # generic goods carry the house brand. False (art toys): each item keeps its
    # own IP/brand from the source; a missing Brand defaults to "Unbranded".
    force_house_brand: bool = True

    # Pricing (v2). cost_plus = existing PricingEngine model (furniture/auto).
    # undercut = blind-box: price a hair under the collected source listing.
    pricing_strategy: str = "cost_plus"          # cost_plus | undercut
    undercut_pct: float = 0.03                   # fraction below the collected source TOTAL (price+shipping)
    undercut_min_abs: float = 0.0                # also at least this many $ below source total
    price_ends_99: bool = False                  # round to a .99 psychological price
    # How the undercut TOTAL is split into item price vs shipping shown to buyer.
    # free  = list price = total, shipping $0 (Best Match friendly). fixed = charge
    # fixed_shipping_amount, item price = total - shipping (category-norm, protects
    # outbound cost on remorse returns).
    shipping_model: str = "free"                 # free | fixed
    fixed_shipping_amount: float = 8.99          # used only when shipping_model == fixed

    # Listing channel. "inventory" = Sell Inventory API (furniture/general, the
    # default and only path that works for tree-0 categories). "trading" = legacy
    # Trading API AddFixedPriceItem — REQUIRED for eBay Motors parts categories,
    # which the Inventory API rejects (errorId 25005). Auto-parts instances that
    # list into Motors set this to "trading".
    listing_channel: str = "inventory"   # inventory | trading
    # eBay Trading SiteID for the trading channel. 0 = eBay.com US, 100 = eBay
    # Motors US. Only consulted when listing_channel == "trading".
    ebay_site_id: str = "0"
    # Ship-from shown on Trading listings (the Inventory API takes this from the
    # merchant location instead). Defaults to the main LA warehouse.
    warehouse_location: str = "Los Angeles, CA"
    warehouse_postal: str = "90001"
    # Return policy for eBay Motors PARTS & ACCESSORIES publishes (the Trading
    # channel). eBay MANDATES 30-day, SELLER-paid return shipping for Motors P&A,
    # so a buyer-paid return policy is rejected at publish. Everything else — the
    # furniture main store, tools, any non-Motors listing — uses the buyer-paid
    # fallback_return_policy_id (30-day). Empty => fall back to that same id.
    motors_return_policy_id: str = ""

    # Local server
    server_port: int = 8000

    @property
    def brand_name_lower(self) -> str:
        return self.brand_name.lower()

    @property
    def server_base_url(self) -> str:
        return f"http://localhost:{self.server_port}"

    def fallback_listing_policies(self) -> Dict[str, str]:
        return {
            "fulfillmentPolicyId": self.fallback_fulfillment_policy_id,
            "returnPolicyId": self.fallback_return_policy_id,
            "paymentPolicyId": self.fallback_payment_policy_id,
        }

    def motors_listing_policies(self) -> Dict[str, str]:
        """Policies for a Motors P&A (Trading) publish: seller-paid returns.

        Same fulfillment/payment as the fallback, but the return policy is the
        Motors seller-paid one (eBay mandates it for Parts & Accessories). Falls
        back to the buyer-paid return id when no Motors return policy is set, so
        non-auto instances are unaffected.
        """
        policies = self.fallback_listing_policies()
        if self.motors_return_policy_id:
            policies["returnPolicyId"] = self.motors_return_policy_id
        return policies

    @property
    def banned_terms_lower(self) -> tuple:
        return tuple(t.lower() for t in self.banned_terms)

    @property
    def is_motors(self) -> bool:
        """True when this instance lists against the eBay Motors catalog."""
        return str(self.category_tree_id) == "100"

    @property
    def default_brand(self) -> str:
        """Brand to use when an item has none.

        Generic-goods instances stamp the house brand; art-toy instances keep
        per-item IP and fall back to "Unbranded" rather than the store name.
        """
        return self.brand_name if self.force_house_brand else "Unbranded"


_SECTION_FIELD_MAP = {
    "store": {
        "store_kind",
        "brand_name",
        "brand_tagline",
        "description_footer_line1",
        "description_footer_line2",
        "promotion_prefix",
    },
    "ebay": {
        "ebay_marketplace_id",
        "category_tree_id",
        "listing_channel",
        "ebay_site_id",
        "warehouse_location",
        "warehouse_postal",
        "merchant_location_key",
        "fallback_fulfillment_policy_id",
        "fallback_return_policy_id",
        "fallback_payment_policy_id",
        "motors_return_policy_id",
    },
    "quality_gate": {
        "quality_banner_marker",
        "quality_footer_marker",
    },
    "listing": {
        "template_style",
        "footer_html",
        "banned_terms",
        "force_house_brand",
    },
    "pricing": {
        "pricing_strategy",
        "undercut_pct",
        "undercut_min_abs",
        "price_ends_99",
        "shipping_model",
        "fixed_shipping_amount",
    },
    "server": {
        "server_port",
    },
}

# YAML keys inside each section may omit the section-derived prefix,
# e.g. quality_gate.banner_marker -> quality_banner_marker.
_SECTION_KEY_ALIASES = {
    "ebay": {
        "marketplace_id": "ebay_marketplace_id",
    },
    "quality_gate": {
        "banner_marker": "quality_banner_marker",
        "footer_marker": "quality_footer_marker",
    },
    "server": {
        "port": "server_port",
    },
}


_INT_FIELDS = {"server_port"}
_FLOAT_FIELDS = {"undercut_pct", "undercut_min_abs", "fixed_shipping_amount"}
_BOOL_FIELDS = {"price_ends_99", "force_house_brand"}
_TUPLE_FIELDS = {"banned_terms"}


def _coerce(field_name: str, value: Any) -> Any:
    if field_name in _INT_FIELDS:
        return int(value)
    if field_name in _FLOAT_FIELDS:
        return float(value)
    if field_name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if field_name in _TUPLE_FIELDS:
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value if str(v).strip())
        # allow a single scalar or comma-separated string
        return tuple(t.strip() for t in str(value).split(",") if t.strip())
    return str(value)


def _flatten_yaml(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map sectioned YAML onto flat StoreProfile field names."""
    valid_fields = {f.name for f in fields(StoreProfile)}
    flat: Dict[str, Any] = {}
    for section, entries in (data or {}).items():
        if not isinstance(entries, dict):
            continue
        aliases = _SECTION_KEY_ALIASES.get(section, {})
        allowed = _SECTION_FIELD_MAP.get(section, set())
        for key, value in entries.items():
            field_name = aliases.get(key, key)
            if field_name in allowed and field_name in valid_fields and value is not None:
                flat[field_name] = _coerce(field_name, value)
    return flat


class StoreProfileError(RuntimeError):
    """A store profile file exists but could not be loaded.

    Raised instead of silently falling back to defaults: on a sub-account
    checkout the defaults are ANOTHER instance's brand/policies, so a silent
    fallback would publish listings under the wrong store. Fail loud so the
    misconfiguration is fixed before anything goes live.
    """


def load_store_profile(path: Optional[os.PathLike] = None) -> StoreProfile:
    """Load a profile from YAML.

    Resolution order: explicit ``path`` > ``STORE_PROFILE_PATH`` env >
    ``config/store_profile.local.yaml`` > tracked ``config/store_profile.yaml``.

    No profile file present -> built-in defaults (equal to the main account).
    A file that is present but unreadable/unparsable -> StoreProfileError.
    Unknown keys are ignored so the tracked default can grow new fields safely.
    """
    if path:
        profile_path = Path(path)
    elif os.getenv("STORE_PROFILE_PATH"):
        profile_path = Path(os.getenv("STORE_PROFILE_PATH"))
    elif _LOCAL_PROFILE_PATH.exists():
        profile_path = _LOCAL_PROFILE_PATH
    else:
        profile_path = _DEFAULT_PROFILE_PATH
    if not profile_path.exists():
        return StoreProfile()

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        raise StoreProfileError(
            f"pyyaml is required to read store profile {profile_path}; "
            "install requirements.txt. Refusing to fall back to default "
            "profile, which is another instance's brand."
        ) from exc

    try:
        with open(profile_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise StoreProfileError(f"cannot read store profile {profile_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StoreProfileError(
            f"store profile {profile_path} must be a mapping, got {type(data).__name__}"
        )
    return StoreProfile(**_flatten_yaml(data))


def get_store_profile() -> StoreProfile:
    """Process-wide cached profile."""
    global _cached_profile
    if _cached_profile is None:
        with _lock:
            if _cached_profile is None:
                _cached_profile = load_store_profile()
    return _cached_profile


def reset_store_profile_cache() -> None:
    """Test hook: force re-load on next get_store_profile()."""
    global _cached_profile
    with _lock:
        _cached_profile = None
