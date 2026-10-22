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
hit, and each one had to be fixed twice because the logic was duplicated:

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

# Indoor upholstery / room signals that make a lone room-list "balcony"
# insufficient for outdoor_context_strict (N817P237961B).
INDOOR_UPHOLSTERY_MARKERS: tuple[str, ...] = (
    "velvet",
    "upholstered",
    "living room",
)

# Placement lists ("for Living Room, Bedroom, Dining Room, Balcony") are not
# product-type evidence. Strip them before deciding the head noun.
_ROOM_OR_LOCATION: tuple[str, ...] = (
    "living room",
    "dining room",
    "home office",
    "dorm room",
    "kids room",
    "children's room",
    "childrens room",
    "bedroom",
    "balcony",
    "patio",
    "garden",
    "entryway",
    "dormitory",
    "office",
    "hallway",
    "foyer",
    "nursery",
    "study",
    "playroom",
    "kitchen",
    "backyard",
    "poolside",
    "deck",
    "porch",
    "apartment",
    "studio",
)

_ROOM_ALT = "|".join(re.escape(name) for name in sorted(_ROOM_OR_LOCATION, key=len, reverse=True))
_ROOM_LIST_RE = re.compile(
    rf"\bfor\s+(?:the\s+)?(?:{_ROOM_ALT})"
    rf"(?:\s*(?:,\s*|\s*&\s*|\s*/\s*|,\s*or\s+|\s+or\s+)(?:the\s+)?(?:{_ROOM_ALT}))*",
    re.IGNORECASE,
)

# Set / bundle / compound titles are not a single head noun. Primary-noun
# early returns must not invent a remap for these (dining set → dining_chair,
# sofa with ottoman → ottoman, bunk bed + nightstand → nightstand, …).
_COMPOUND_PHRASES: tuple[str, ...] = (
    "dining set",
    "dining table set",
    "table set",
    "patio set",
    "patio furniture set",
    "conversation set",
    "furniture set",
    "bed frame",
    "bunk bed",
    "platform bed",
    "hall tree",
    "shoe bench",
    "shoe storage bench",
    "wine rack",
)

_OTTOMAN_COMPOUND_RE = re.compile(
    r"(?:"
    r"\b(?:and|with)\b.{0,48}\bottoman\b"
    r"|\b(?:sectional|sofa|couch|recliner)\b.{0,80}\bottoman\b"
    r")",
    re.IGNORECASE,
)

# "Dining Table and 6 Dining Chairs" is one set. The count sits between the
# table and the chairs, so a contiguous "dining table and chairs" phrase misses it
# and the chair head-noun wins.
_CHAIR_NOT_PRODUCT = r"(?!\s+(?:height|rail|rails|cushion|cushions|cover|covers|pad|pads|mat|mats)\b)"
_DUAL_USE_INDOOR_OUTDOOR_RE = re.compile(
    r"\bindoor(?:\s*&\s*|\s+and\s+|\s*/\s*|\s+)outdoor\b",
    re.IGNORECASE,
)


def strip_dual_use_indoor_outdoor(text: str) -> str:
    """Drop ``indoor & outdoor`` so the phrase is not an outdoor product signal."""
    cleaned = _DUAL_USE_INDOOR_OUTDOOR_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def has_dual_use_indoor_outdoor(text: str) -> bool:
    return _DUAL_USE_INDOOR_OUTDOOR_RE.search(str(text or "")) is not None


_DINING_TABLE_AND_CHAIRS_RE = re.compile(
    rf"(?:"
    rf"\b(?:dining|kitchen)\s+tables?\b(?:\s+\w+){{0,8}}\s*(?:and|&|with)\s+"
    rf"(?:(?:\d+|a|an|the|set|of|two|four|six|eight)\s+){{0,4}}"
    rf"(?:\w+\s+){{0,4}}chairs?\b{_CHAIR_NOT_PRODUCT}"
    rf"|"
    rf"\bchairs?\b{_CHAIR_NOT_PRODUCT}(?:\s+\w+){{0,6}}\s*(?:and|&|with)\s+"
    rf"(?:(?:\d+|a|an|the|set|of)\s+){{0,3}}"
    rf"(?:\w+\s+){{0,4}}(?:dining|kitchen)\s+tables?\b"
    rf")",
    re.IGNORECASE,
)


def title_names_dining_table_and_chairs(text: str) -> bool:
    """True when the title bundles a dining/kitchen table with chairs."""
    return _DINING_TABLE_AND_CHAIRS_RE.search(str(text or "")) is not None


def has_compound_product_signal(text: str) -> bool:
    """True when the title is a set/bundle/compound, not a single product noun."""
    hay = str(text or "")
    if not hay:
        return False
    if has_any_phrase(hay, _COMPOUND_PHRASES):
        return True
    if title_names_dining_table_and_chairs(hay):
        return True
    if has_phrase(hay, "desk"):
        return True
    return _OTTOMAN_COMPOUND_RE.search(hay) is not None


# Head-noun families, most specific first. "storage cabinet" is intentionally
# absent: it is a keyword-stuffing phrase, not a product type.
_PRIMARY_NOUNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workbench", ("workbench", "work bench")),
    ("nightstand", ("nightstand", "bedside table")),
    ("dresser", ("dresser", "chest of drawers")),
    ("storage_ottoman", ("storage ottoman", "lift top ottoman", "storage footstool")),
    ("ottoman_bench", ("ottoman bench",)),
    ("storage_bench", ("storage bench",)),
    ("ottoman", ("ottoman",)),
    ("daybed", ("outdoor daybed", "patio daybed", "daybed", "sunbed")),
    ("accent_chair", ("accent chair", "armchair", "reading chair")),
    ("dining_chair", ("dining chair", "dining chairs")),
    ("outdoor_chair", (
        "outdoor chair",
        "patio chair",
        "patio armchair",
        "outdoor armchair",
        "sun lounger",
        "camping chair",
    )),
    ("pantry", ("kitchen pantry", "pantry cabinet", "freestanding pantry", "pantry storage")),
    ("dining_bench", ("dining bench", "upholstered bench")),
    ("sofa", ("sectional sofa", "loveseat", "sleeper sofa", "sofa", "couch")),
)


def has_phrase(text: str, phrase: str) -> bool:
    """Word-boundary phrase match. ``bench with`` must not hit ``workbench with``.

    A trailing ``s`` is allowed so ``dining chair`` matches ``dining chairs``.
    """
    if not phrase:
        return False
    hay = str(text or "")
    if not hay:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(phrase)}s?(?![A-Za-z0-9])", hay, re.IGNORECASE) is not None


def has_any_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    return any(has_phrase(text, phrase) for phrase in phrases)


def strip_room_location_lists(text: str) -> str:
    """Drop 'for Living Room, Bedroom, Balcony' lists so they cannot pick a type."""
    cleaned = _ROOM_LIST_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def primary_product_noun(live_title: str, source_title: str = "") -> str | None:
    """Head product noun, live title first. GIGA stuffing must not override it.

    Compound/set titles (dining set, bunk bed + nightstand, chair and ottoman,
    …) return None — uncertain means no remap, never a new profile.
    """
    for blob in (live_title, source_title):
        core = strip_room_location_lists(blob or "")
        if not core:
            continue
        if has_compound_product_signal(core):
            return None
        has_dining_chair = has_any_phrase(core, ("dining chair", "dining chairs"))
        has_accent_chair = has_any_phrase(core, ("accent chair", "accent chairs"))
        # Dining-chair copy that also stuffs "accent chairs" is uncertain — no remap.
        if has_dining_chair and has_accent_chair:
            return None
        # "Sofa with Storage Ottoman" is a sofa. A title whose head noun is the
        # ottoman/bench, with "sofa" only as stuffing, stays ottoman/bench.
        accessory_ottoman = re.search(
            r"\bwith\b.{0,60}\b(?:storage ottoman|ottoman bench|ottoman)\b",
            core,
            re.IGNORECASE,
        )
        if accessory_ottoman and has_any_phrase(
            core,
            ("single sofa", "sectional sofa", "sleeper sofa", "loveseat", "armchair", "accent chair", "sofa", "couch"),
        ):
            if has_any_phrase(core, ("armchair", "accent chair", "reading chair")):
                return "accent_chair"
            return "sofa"
        for kind, phrases in _PRIMARY_NOUNS:
            if has_any_phrase(core, phrases):
                return kind
    return None


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
    signal must appear in the TITLE to count. A room-list "balcony" plus indoor
    upholstery (velvet / upholstered / living room) is never enough.
    """
    title = str(title or "")
    full_text = str(full_text or "")
    title_core = strip_room_location_lists(title)
    full_core = strip_room_location_lists(full_text)
    indoor_upholstery = has_any_phrase(full_text, INDOOR_UPHOLSTERY_MARKERS) or names_indoor_room(full_text)
    if indoor_upholstery:
        return has_outdoor_marker(title_core)
    if names_indoor_room(full_text):
        return has_outdoor_marker(title_core)
    return has_outdoor_marker(full_core)


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
