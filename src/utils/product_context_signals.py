"""Shared indoor/outdoor context signals for category classification.

Category classification lives in two engines: publishing asks
``EbayCategoryMatcher.canonicalize_category``, the live audit asks
``listing_quality_gate.classify_listing_profile``. Merging them outright is not
safe — each is more specific than the other in different places (the profile
generalises a patio dining table to "Tables"; the matcher generalises an indoor
storage bench to whatever its keyword ladder hits first).

What IS safe, and what actually caused every incident so far, is the shared
*context* question: "does this copy place the product outdoors?" Three live
mis-categorisations in 2026-07 all came from answering it with a bare keyword
hit, and each had to be fixed twice because the logic was duplicated:

    "cozy even in cooler weather"        -> bell tent  -> Ice Chests & Coolers
    "or a leisure bench on the balcony"  -> indoor bench -> Outdoor Daybeds  (x2 engines)

So the signals live here, once. Both engines import them; a fix lands in both.
"""

from __future__ import annotations

import re

OUTDOOR_MARKERS: tuple[str, ...] = (
    "outdoor", "patio", "garden", "backyard", "poolside", "deck", "balcony", "porch",
)

# Rooms that place a product indoors explicitly enough to outweigh one passing
# outdoor mention in a list of possible placements.
INDOOR_ROOM_MARKERS: tuple[str, ...] = (
    "living room", "bedroom", "entryway", "dormitory", "dorm room",
    "study", "home office", "hallway", "foyer", "nursery",
)


def has_outdoor_marker(text: str) -> bool:
    """Any outdoor placement word anywhere in the text."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in OUTDOOR_MARKERS)


def names_indoor_room(text: str) -> bool:
    """Does the copy explicitly place the product in a named indoor room?"""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in INDOOR_ROOM_MARKERS)


def outdoor_context(title: str, full_text: str) -> bool:
    """Is this product actually an outdoor product?

    A single incidental mention ("...or a leisure bench on the balcony") does not
    make an indoor product outdoor. When the copy names indoor rooms, the outdoor
    signal must appear in the TITLE to count.
    """
    if names_indoor_room(full_text):
        return has_outdoor_marker(title)
    return has_outdoor_marker(full_text)


# "cooler" is also the comparative of "cool". Outdoor copy is full of it, and a
# bare \bcooler\b match tried to move bell tents into Ice Chests & Coolers.
_COMPARATIVE_COOLER = re.compile(
    r"\bcooler\s+(?:weather|temperatures?|months?|days?|nights?|evenings?|"
    r"seasons?|climates?|air|conditions?|environments?|areas?|regions?)\b",
    re.IGNORECASE,
)
_COOLER_PRODUCT_PHRASES: tuple[str, ...] = (
    "hard cooler", "insulated cooler", "ice chest", "portable cooler", "cooler can",
)


def mentions_cooler_product(text: str) -> bool:
    """True only when "cooler" names a product, not a temperature."""
    lowered = str(text or "").lower()
    if any(phrase in lowered for phrase in _COOLER_PRODUCT_PHRASES):
        return True
    if re.search(r"\bcooler\b", lowered) is None:
        return False
    return _COMPARATIVE_COOLER.search(lowered) is None
