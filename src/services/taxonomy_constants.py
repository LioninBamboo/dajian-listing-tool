"""Shared taxonomy constants and helpers used across publish, audit, and UI flows."""

from __future__ import annotations


INVALID_CATEGORY_REMAP = {
    "118218": "54235",   # Accent Chairs -> Chairs
    "20466": "114397",   # Legacy dresser category -> current leaf dresser category
    "68240": "103430",   # Legacy wardrobes category -> Armoires & Wardrobes
    "10321": "262017",   # Legacy jewelry organizer category -> Jewelry Boxes/Organizers
    "177815": "38204",   # Historical wrong category -> Tables
    "177816": "107578",  # Historical wrong category -> Dining Sets
    # 2026-07-22 auto parts: these ids were used for their intended NAMES but in
    # eBay's Motors tree (100) they resolve to unrelated parts — tow hitches were
    # filed as brake pad wear sensors. They also 400 in tree 0. Remap so stored
    # products carrying them get corrected instead of re-publishing the mistake.
    "262210": "33650",   # "Running Boards" -> Running Boards & Step Bars
    "174020": "33653",   # "Trailer Hitches" -> Trailer Hitches (Motors)
    "174021": "121984",  # "Hitch Cargo Carriers" -> Cargo Boxes, Bags & Baskets
    "262216": "33651",   # "Roof Racks" (really Anchors) -> Roof Racks & Cross Bars
    "262093": "262150",  # "Tailgate Parts" -> Lift Supports, Latches, Hinges
}

# Stored categories the pipeline keeps instead of re-matching. Motors ids only —
# see INVALID_CATEGORY_REMAP for why the previous set was wrong (protecting them
# is what locked the bad categories in).
PROTECTED_STORED_CATEGORY_IDS = {
    "33653",    # Trailer Hitches
    "121984",   # Cargo Boxes, Bags & Baskets
    "33650",    # Running Boards & Step Bars
    "33651",    # Roof Racks & Cross Bars
    "262150",   # Lift Supports, Latches, Hinges
    "85040",    # Bike Trailers (Sporting Goods, tree 0)
}


def normalize_legacy_category_id(category_id: str | None) -> str:
    cid = str(category_id or "").strip()
    if not cid:
        return ""
    return INVALID_CATEGORY_REMAP.get(cid, cid)