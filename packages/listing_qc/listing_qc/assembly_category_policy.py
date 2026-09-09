"""Category policy for assembly_package_conflict auto-Yes.

Ops rule (GrovePop 2026-08-26): except Baskets/Pots gabion (20518), APC residuals
with strong flat-pack evidence are treated as Assembly Required=Yes.
"""

from __future__ import annotations

# Explicit skip: gabion / baskets-pots — no installation expected.
ASSEMBLY_SKIP_NO_INSTALL_CATEGORY_IDS = frozenset({"20518"})

# Categories that may auto-set Assembly Required=Yes when APC + strong.
# Kept as an allowlist so unknown categories stay human-gated.
ASSEMBLY_AUTO_YES_CATEGORY_IDS = frozenset({
    "54235",   # Chairs
    "79684",   # Outdoor Chairs
    "79682",   # Patio Chairs
    "38208",   # Sofas, Armchairs & Couches
    "63108",   # Cages, Hutches & Enclosure
    "116380",  # Strollers
    "75671",   # Wheelbarrows, Carts & Wagons
    "177031",  # Outdoor Furniture Covers
})


def category_allows_assembly_auto_yes(category_id: str | int | None) -> bool:
    """True when categoryId is in the auto-Yes allowlist and not the gabion skip set."""
    cid = str(category_id or "").strip()
    if not cid:
        return False
    if cid in ASSEMBLY_SKIP_NO_INSTALL_CATEGORY_IDS:
        return False
    return cid in ASSEMBLY_AUTO_YES_CATEGORY_IDS
