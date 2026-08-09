"""Shared taxonomy constants and helpers used across publish, audit, and UI flows."""

from __future__ import annotations


INVALID_CATEGORY_REMAP = {
    "118218": "54235",   # Accent Chairs -> Chairs
    "20466": "114397",   # Legacy dresser category -> current leaf dresser category
    "68240": "103430",   # Legacy wardrobes category -> Armoires & Wardrobes
    "10321": "262017",   # Legacy jewelry organizer category -> Jewelry Boxes/Organizers
    "177815": "38204",   # Historical wrong category -> Tables
    "177816": "107578",  # Historical wrong category -> Dining Sets
}

PROTECTED_STORED_CATEGORY_IDS = {
    "174020",
    "174021",
    "262210",
    "262216",
    "262093",
    "85040",
    # Keep specialized furniture leaves from being remapped to generic cabinets.
    "20689",   # Wine Racks & Bottle Holders (bar / liquor cabinets)
    "261263",  # Hall Trees & Stands
}


def normalize_legacy_category_id(category_id: str | None) -> str:
    cid = str(category_id or "").strip()
    if not cid:
        return ""
    return INVALID_CATEGORY_REMAP.get(cid, cid)