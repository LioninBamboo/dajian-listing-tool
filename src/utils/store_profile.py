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

    # eBay account-owned identifiers
    merchant_location_key: str = "DAJIAN_LA_WAREHOUSE"
    fallback_fulfillment_policy_id: str = "321897899021"
    fallback_return_policy_id: str = "321896608021"
    fallback_payment_policy_id: str = "321896606021"

    # Quality-gate description markers (lowercase substrings)
    quality_banner_marker: str = "aquaverve"
    quality_footer_marker: str = "california"

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


_SECTION_FIELD_MAP = {
    "store": {
        "brand_name",
        "brand_tagline",
        "description_footer_line1",
        "description_footer_line2",
        "promotion_prefix",
    },
    "ebay": {
        "merchant_location_key",
        "fallback_fulfillment_policy_id",
        "fallback_return_policy_id",
        "fallback_payment_policy_id",
    },
    "quality_gate": {
        "quality_banner_marker",
        "quality_footer_marker",
    },
    "server": {
        "server_port",
    },
}

# YAML keys inside each section may omit the section-derived prefix,
# e.g. quality_gate.banner_marker -> quality_banner_marker.
_SECTION_KEY_ALIASES = {
    "quality_gate": {
        "banner_marker": "quality_banner_marker",
        "footer_marker": "quality_footer_marker",
    },
    "server": {
        "port": "server_port",
    },
}


def _coerce(field_name: str, value: Any) -> Any:
    if field_name == "server_port":
        return int(value)
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


def load_store_profile(path: Optional[os.PathLike] = None) -> StoreProfile:
    """Load a profile from YAML; unknown keys ignored, missing file -> defaults."""
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

        with open(profile_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return StoreProfile()
        return StoreProfile(**_flatten_yaml(data))
    except Exception:
        # A malformed profile must never take the pipeline down; defaults are
        # the current main-account values.
        return StoreProfile()


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
