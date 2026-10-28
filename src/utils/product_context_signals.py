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
    "study", "home office", "hallway", "foyer", "nursery", "office",
    "indoor",
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
    "vanity",
    "club",
    "guest",
    "warehouse",
    "garage",
)

_ROOM_ALT = "|".join(re.escape(name) for name in sorted(_ROOM_OR_LOCATION, key=len, reverse=True))
# Placement lists may be comma, space, &, /, or "or" separated, and may mix
# non-room words (Vanity, Club, Guest, Office) with rooms.
_ROOM_LIST_RE = re.compile(
    rf"\bfor\s+(?:the\s+)?(?:{_ROOM_ALT})"
    rf"(?:\s*(?:,\s*|\s*&\s*|\s*/\s*|,\s*or\s+|\s+or\s+|\s+)(?:the\s+)?(?:{_ROOM_ALT}))*",
    re.IGNORECASE,
)

# Outdoor counts only in the head clause — text before the first placement cue.
_HEAD_CLAUSE_SPLIT_RE = re.compile(
    r"\b(?:perfect\s+for|ideal\s+for|suitable\s+for|great\s+for|"
    r"use\s+in|used\s+in|use\s+on|for)\b",
    re.IGNORECASE,
)

_OUTDOOR_MARKER_RE = re.compile(
    r"\b(?:outdoor|patio|garden|backyard|poolside|deck|balcony|porch)\b",
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
    r"\b(?:and|with)\b.{0,48}\b(?:ottomans?|footstools?)\b"
    r"|\b(?:sectional|sofa|couch|loveseat|recliner)\b.{0,80}\b(?:ottomans?|footstools?)\b"
    r")",
    re.IGNORECASE,
)

# Dual-use copy is not an outdoor product signal. "Outdoor Indoor" is the
# reverse of the usual indoor/outdoor pair and must be stripped the same way.
_MIXED_INDOOR_OUTDOOR_RE = re.compile(
    r"\b(?:"
    r"indoor\s*(?:&\s*|and\s*|/\s*|-\s*|or\s+)outdoor"
    r"|outdoor\s*(?:&\s*|and\s*|/\s*|-\s*|or\s+)indoor"
    r"|indoor\s+outdoor"
    r"|outdoor\s+indoor"
    r"|in\s*/\s*outdoor"
    r"|for\s+indoor\s+and\s+outdoor"
    r"|indoor\s+and\s+outdoor\s+use"
    r"|indoor\s+or\s+outdoor(?:\s+use)?"
    r")\b",
    re.IGNORECASE,
)

_TABLE_AND_CHAIRS_RE = re.compile(
    r"\b(?:dining\s+table|bistro\s+table|patio\s+table|outdoor\s+table|"
    r"kitchen\s+table|table)"
    r".{0,48}(?:\+|and|with|&)\s*(?:\d+\s+)?(?:dining\s+)?(?:chairs?|benches|bench|stools?)\b",
    re.IGNORECASE,
)

_N_PIECE_DINING_RE = re.compile(
    r"\b\d+\s*-?\s*piece\b.{0,48}\bdining\b",
    re.IGNORECASE,
)

_SEATING_NEGATION_RE = re.compile(
    r"\b(?:chairs?\s+not\s+included|without\s+chairs?|table\s+only)\b",
    re.IGNORECASE,
)

_CAPACITY_PHRASE_RE = re.compile(
    r"\b(?:for\s+\d+\s*[-–to]+\s*\d+\s+people|for\s+\d+\s+persons?|"
    r"seats?\s+\d+|(\d+)\s*-?\s*person(?:s)?)\b",
    re.IGNORECASE,
)

_TABLE_WORDS = (
    "dining table",
    "bistro table",
    "bar table",
    "patio table",
    "outdoor table",
    "kitchen table",
    "table and chairs",
    "table and chair",
)
_SEATING_WORDS = ("chair", "chairs", "bench", "benches", "stool", "stools")
_CHAISE_LOUNGE_WORDS = (
    "chaise",
    "chaise lounge",
    "lounge chair",
    "lounger",
    "sun lounger",
    "recliner",
)
_SIDE_END_TABLE_WORDS = ("side table", "end table")

# Leftmost product noun wins. Longer phrases are preferred on the same start.
_FIRST_NOUN_PHRASES: tuple[str, ...] = (
    "outdoor daybed",
    "patio daybed",
    "daybed",
    "nesting coffee table",
    "coffee table",
    "cocktail table",
    "nesting table",
    "dog crate",
    "dog kennel",
    "pet cage",
    "pet crate",
    "cat litter box",
    "litter box enclosure",
    "litter enclosure",
    "cat furniture",
    "raised garden bed",
    "garden bed",
    "planter",
    "steel wagon",
    "console table",
    "chaise lounge cushion",
    "lounger cushion",
    "chaise lounge",
    "sun lounger",
    "lounger",
    "chaise",
    "coffee bar cabinet",
    "bar cabinet",
    "liquor cabinet",
    "wine cabinet",
    "wine rack",
    "bottle holder",
    "stemware rack",
    "glass display cabinet",
    "display cabinet",
    "curio cabinet",
    "medicine cabinet",
    "bathroom cabinet",
    "pantry cabinet",
    "bookshelf",
    "bookcase",
    "hall tree",
    "sideboard",
    "buffet",
    "dining table",
    "bistro table",
    "bar table",
    "end table",
    "side table",
    "sectional sofa",
    "conversation set",
    "patio furniture set",
    "furniture set",
    "patio set",
    "storage footstool",
    "footstool",
    "ottoman",
    "office chair mat",
    "office chair",
    "gaming chair",
    "accent chair",
    "dining chair",
    "bar stool",
    "bathroom vanity",
    "vanity table",
    "makeup vanity",
    "kitchen island cart",
    "kitchen island",
    "kitchen cart",
    "trimming tool",
    "string trimmer",
    "drill press",
    "fruit and wine press",
    "fruit press",
    "wine press",
    "garlic press",
    "grill cart",
    "grill table",
    "fireplace screen",
    "filing cabinet",
    "dome climber",
    "jungle gym",
    "dining table set",
    "nook dining set",
    "bar table set",
    "table set",
    "wardrobe",
    "fogger",
    "bench",
    "chair mat",
    "table top",
    "tabletop",
    "tool chest",
    "dresser",
    "dog bed",
    "club armchair",
    "papasan chair",
    "egg chair",
    "rocking chair",
    "adirondack chair",
    "folding chair",
    "patio chair",
    "outdoor chair",
    "garden stool",
    "armchair",
    "lounge chair",
    "hutch desk",
    "chair cushion",
    "chair cover",
    "seat cushion",
    "slipcover",
    "sofa",
    "couch",
    "loveseat",
    "wagon",
    "tv stand",
    "media console",
    "entertainment center",
    "dining set",
)

# Real product heads that must not be skipped so a later whitelist noun wins.
_DENY_HEAD_NOUNS: tuple[str, ...] = (
    "hutch",
    "pantry",
    "cupboard",
    "cabinet",
    "tv stand",
    "media console",
    "entertainment center",
    "cushion",
    "pad",
    "cover",
    "slipcover",
    "mattress",
    "vanity",
    "office chair mat",
    "chair mat",
    "table top",
    "tabletop",
    "garlic press",
)

_ACCESSORY_HEAD_NOUNS: frozenset[str] = frozenset(
    {
        "chaise lounge cushion",
        "lounger cushion",
        "chair cushion",
        "chair cover",
        "seat cushion",
        "slipcover",
        "cushion",
        "pad",
        "cover",
        "office chair mat",
        "chair mat",
        "table top",
        "tabletop",
    }
)

UNKNOWN_HEAD_PREFIX = "__unknown_head__:"

_FIRST_GROUP_SPLIT_RE = re.compile(
    r"\s+(?:with|for|and)\s+|[,|+]| \| |\s+[-–—]\s+",
    re.IGNORECASE,
)
# Phrases that contain a connector; scan the unsplit head so "Fruit and Wine Press"
# is not split into a bare "Fruit".
_CONNECTOR_HEAD_PHRASES: tuple[str, ...] = (
    "fruit and wine press",
    "fruit and wine",
)
_TV_STAND_HYPHEN_RE = re.compile(r"\btv\s*-\s*stand\b", re.IGNORECASE)
_ACCESSORY_ONLY_RE = re.compile(
    r"\b(?:cushion|cover|pad|slipcover|mattress)\s+only\b",
    re.IGNORECASE,
)
_FURNITURE_THEN_ACCESSORY_RE = re.compile(
    r"\b(?:chaise(?:\s+lounge)?|lounger|sun\s+lounger|chair|sofa|couch)"
    r"s?\s+(?:cushion|cover|pad|slipcover)s?\b",
    re.IGNORECASE,
)


def as_safe_text(value) -> str:
    """Coerce None / non-str / HTML-laden text to a plain string. Never raises."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    try:
        text = str(value)
    except Exception:
        return ""
    if not text or text in {"None", "nan"}:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def as_safe_category_id(value) -> str:
    """Accept '', None, or int category ids. Dicts/lists become ''."""
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    try:
        text = str(value).strip()
    except Exception:
        return ""
    return text


def head_clause(text: str) -> str:
    """Product head clause: text before the first placement cue."""
    hay = as_safe_text(text)
    if not hay:
        return ""
    match = _HEAD_CLAUSE_SPLIT_RE.search(hay)
    if not match:
        return hay
    return hay[: match.start()].strip()


def title_has_mixed_indoor_outdoor(text: str) -> bool:
    """True for indoor & outdoor / indoor/outdoor / Outdoor Indoor dual-use titles."""
    hay = as_safe_text(text)
    if not hay:
        return False
    return _MIXED_INDOOR_OUTDOOR_RE.search(hay) is not None


def strip_mixed_indoor_outdoor(text: str) -> str:
    cleaned = _MIXED_INDOOR_OUTDOOR_RE.sub(" ", as_safe_text(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def title_has_clear_outdoor_word(text: str) -> bool:
    """Outdoor/patio/garden in the HEAD CLAUSE after rejecting mixed wording."""
    hay = as_safe_text(text)
    if not hay or title_has_mixed_indoor_outdoor(hay):
        return False
    clause = strip_mixed_indoor_outdoor(head_clause(hay))
    if not clause:
        return False
    return bool(_OUTDOOR_MARKER_RE.search(clause))


def _first_product_noun_clause(text: str) -> str:
    """Head-clause product group before with/for/and/,/|/+ / spaced hyphen."""
    core = strip_room_location_lists(head_clause(as_safe_text(text)) or as_safe_text(text))
    if not core:
        return ""
    core = _TV_STAND_HYPHEN_RE.sub("tv stand", core)
    split = _FIRST_GROUP_SPLIT_RE.search(core)
    if split:
        core = core[: split.start()].strip()
    return core


def _scan_head_phrases(core: str, phrases: tuple[str, ...]) -> tuple[int, int, str] | None:
    best: tuple[int, int, str] | None = None
    for phrase in phrases:
        match = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(phrase)}s?(?![A-Za-z0-9])",
            core,
            re.IGNORECASE,
        )
        if not match:
            continue
        candidate = (match.start(), -len(phrase), phrase)
        if best is None or candidate < best:
            best = candidate
    return best


def first_product_noun(text: str) -> str | None:
    """Head noun of the first product group. Deny-list heads fail closed.

    Does not skip a real head (hutch, tv stand, cushion, …) to reach a later
    whitelisted noun such as buffet or sideboard. Accessory heads stay
    accessories. Word boundaries only: stable/Tablet are not table.
    """
    core = _first_product_noun_clause(text)
    hay = as_safe_text(text)
    unsplit = strip_room_location_lists(head_clause(hay) or hay)
    unsplit = _TV_STAND_HYPHEN_RE.sub("tv stand", unsplit)
    connector = _scan_head_phrases(unsplit, _CONNECTOR_HEAD_PHRASES) if unsplit else None
    if not core and connector is None:
        return None
    if not core:
        return connector[2]
    if _ACCESSORY_ONLY_RE.search(as_safe_text(text)) or _FURNITURE_THEN_ACCESSORY_RE.search(core):
        accessory = _scan_head_phrases(
            core + " cushion",
            tuple(_ACCESSORY_HEAD_NOUNS),
        )
        if accessory:
            return accessory[2]
        return f"{UNKNOWN_HEAD_PREFIX}cushion"

    whitelist = _FIRST_NOUN_PHRASES
    deny = tuple(noun for noun in _DENY_HEAD_NOUNS if noun not in whitelist)
    combined = whitelist + deny
    best = _scan_head_phrases(core, combined)
    if best is None:
        return connector[2] if connector is not None else None
    phrase = best[2]
    if connector is not None:
        phrase_in_unsplit = _scan_head_phrases(unsplit, (phrase,))
        if phrase_in_unsplit is None or connector[0] < phrase_in_unsplit[0]:
            return connector[2]
    # "Hutch Desk": deny noun used as a modifier before a known product noun.
    if phrase in _DENY_HEAD_NOUNS and phrase not in {
        "tv stand",
        "media console",
        "entertainment center",
        "chaise lounge cushion",
        "lounger cushion",
        "chair cushion",
        "chair cover",
        "seat cushion",
        "slipcover",
    }:
        remainder = core[best[0] + len(phrase) :].strip()
        later = _scan_head_phrases(remainder, whitelist)
        if later is not None and later[0] <= 1:
            return later[2]
        return f"{UNKNOWN_HEAD_PREFIX}{phrase}"
    if phrase in _ACCESSORY_HEAD_NOUNS:
        return phrase
    if phrase in _DENY_HEAD_NOUNS and phrase not in {
        "tv stand",
        "media console",
        "entertainment center",
    }:
        return f"{UNKNOWN_HEAD_PREFIX}{phrase}"
    return phrase


def unknown_head_noun(head: str | None) -> str | None:
    """Return the deny-list noun when ``first_product_noun`` failed closed."""
    if not head or not head.startswith(UNKNOWN_HEAD_PREFIX):
        return None
    return head[len(UNKNOWN_HEAD_PREFIX) :]


def title_is_chair_accessory(text: str) -> bool:
    """Accessory only when the accessory noun IS the head noun.

    'Chair with cushion' / 'Dining Chairs … Navy Cushions' stay chairs.
    'Chair cushions', 'cushions for chairs', 'Only chair cushion' are accessories.
    """
    hay = as_safe_text(text)
    if not hay:
        return False
    if re.search(
        r"\b(?:with|includes?)\b.{0,24}\b(?:cushions?|covers?|pads?|pillows?|slipcovers?)\b",
        hay,
        re.IGNORECASE,
    ):
        return False
    head = first_product_noun(hay)
    if head in {
        "dining chair",
        "armchair",
        "club armchair",
        "lounge chair",
        "chaise",
        "chaise lounge",
        "sofa",
        "couch",
        "loveseat",
        "papasan chair",
        "egg chair",
        "rocking chair",
        "adirondack chair",
        "folding chair",
        "patio chair",
        "outdoor chair",
    }:
        return False
    if head in _ACCESSORY_HEAD_NOUNS or (head and head.startswith(UNKNOWN_HEAD_PREFIX) and unknown_head_noun(head) in _ACCESSORY_HEAD_NOUNS):
        return True
    if re.search(r"\bonly\b.{0,24}\b(?:chair\s+)?cushions?\b", hay, re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:cushions?|covers?|pads?|slipcovers?)\s+for\b.{0,48}\bchairs?\b",
        hay,
        re.IGNORECASE,
    ):
        return True
    if head:
        return False
    # Head noun is the accessory: "chair cushion(s)", "seat cushion", "slipcover".
    if re.search(
        r"\b(?:chair|seat)\s+(?:cushions?|covers?|pads?|pillows?)\b",
        hay,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"\bslipcovers?\b", hay, re.IGNORECASE) and has_any_phrase(hay, ("chair", "chairs")):
        return True
    if re.search(r"\bchair\s+covers?\b", hay, re.IGNORECASE) or re.search(
        r"\b(?:patio|dining|outdoor)\s+chair\s+covers?\b",
        hay,
        re.IGNORECASE,
    ):
        return True
    return False


def _has_seating_evidence(text: str) -> bool:
    hay = as_safe_text(text)
    if not hay or _SEATING_NEGATION_RE.search(hay):
        return False
    return has_any_phrase(hay, _SEATING_WORDS)


def _has_table_word(text: str) -> bool:
    return has_any_phrase(as_safe_text(text), _TABLE_WORDS)


def _is_chair_only_n_piece(text: str) -> bool:
    hay = as_safe_text(text)
    if not hay:
        return False
    if not re.search(r"\b\d+\s*-?\s*piece\b", hay, re.IGNORECASE):
        return False
    if not has_any_phrase(hay, ("chair", "chairs")):
        return False
    if has_any_phrase(
        hay,
        (
            "dining set",
            "dining table set",
            "patio dining set",
            "outdoor dining set",
            "table and chair",
            "table and chairs",
        ),
    ):
        return False
    if has_any_phrase(hay, ("dining table", "bistro table", "patio table", "outdoor table", "kitchen table")):
        return False
    return True


def title_is_dining_set_bundle(text: str) -> bool:
    """Table+chairs or an N-piece dining set is a SET, not a chair SKU.

    A set needs seating evidence in the same title (chair/bench/stool), as
    either 'table and/with/+ N chairs' or 'N-piece/set' plus both a table word
    and a seating word. Capacity phrases and 'Chairs Not Included' are not sets.
    N-piece chair-only titles are chairs, never 139849.
    """
    hay = as_safe_text(text)
    if not hay:
        return False
    if has_any_phrase(
        hay,
        (
            "kids table",
            "kids activity table",
            "children table",
            "children's table",
            "childrens table",
            "toddler table",
            "play table",
            "activity table",
        ),
    ):
        return False
    if _SEATING_NEGATION_RE.search(hay):
        return False
    if has_any_phrase(hay, _CHAISE_LOUNGE_WORDS) and not has_any_phrase(
        hay, ("dining table", "bistro table", "bar table")
    ):
        return False
    if _is_chair_only_n_piece(hay):
        return False
    seating = _has_seating_evidence(hay)
    table = _has_table_word(hay)
    capacity_only = bool(_CAPACITY_PHRASE_RE.search(hay)) and not seating
    if _TABLE_AND_CHAIRS_RE.search(hay) and seating:
        return True
    if table and seating and has_any_phrase(hay, ("set", "piece")):
        return True
    if has_any_phrase(
        hay,
        (
            "dining set",
            "dining table set",
            "patio dining set",
            "outdoor dining set",
            "table and chair set",
            "table and chairs set",
        ),
    ):
        if capacity_only:
            return False
        if seating or table:
            return True
        # "7-Piece Patio Dining Set" names a set without repeating table/chairs.
        if _N_PIECE_DINING_RE.search(hay) and has_any_phrase(hay, ("set",)):
            return True
        return False
    if _N_PIECE_DINING_RE.search(hay) and has_any_phrase(hay, ("set",)) and seating and table:
        return True
    return False


def title_allows_outdoor_chair_remap(text: str) -> bool:
    """79684 may fire only on a title outdoor word, never mixed or a set/accessory."""
    hay = as_safe_text(text)
    if not hay:
        return False
    if not title_has_clear_outdoor_word(hay):
        return False
    if title_has_mixed_indoor_outdoor(hay):
        return False
    if title_is_chair_accessory(hay):
        return False
    if title_is_dining_set_bundle(hay):
        return False
    return True


_KITCHEN_ISLAND_TABLE_RE = re.compile(
    r"\bkitchen\s+island\b.{0,48}\b(?:dining\s+)?table\b",
    re.IGNORECASE,
)
_COFFEE_TABLE_OTTOMAN_RE = re.compile(
    r"\bcoffee\s+table\b.{0,64}\bottomans?\b",
    re.IGNORECASE,
)
_LOFT_DESK_RE = re.compile(
    r"\b(?:loft\s+bed|bunk\s+bed)\b.{0,64}\b(?:desk|wardrobe)\b",
    re.IGNORECASE,
)
_CHAIR_AND_OTTOMAN_SET_RE = re.compile(
    r"\b(?:accent\s+)?chairs?\b.{0,32}\band\b.{0,32}\b(?:ottoman|footstool)\b.{0,16}\bset\b",
    re.IGNORECASE,
)
_SOFA_OTTOMAN_RE = re.compile(
    r"\b(?:sectional|sofa|couch|loveseat)\b.{0,80}\b(?:ottomans?|footstools?)\b",
    re.IGNORECASE,
)
_PATIO_SET_RE = re.compile(
    r"\b(?:conversation|patio|outdoor\s+furniture|outdoor\s+sofa|furniture)\s+set\b"
    r"|\b(?:sofa|furniture)\s+set\b",
    re.IGNORECASE,
)


def has_compound_product_signal(text: str) -> bool:
    """True when the title is a set/bundle/compound, not a single product noun."""
    hay = as_safe_text(text)
    if not hay:
        return False
    if has_any_phrase(hay, ("table tennis", "ping pong", "ping-pong")):
        return False
    if (
        has_any_phrase(hay, ("coffee table", "cocktail table", "nesting table"))
        and not _COFFEE_TABLE_OTTOMAN_RE.search(hay)
        and not has_any_phrase(
            hay,
            (
                "conversation set",
                "patio set",
                "patio furniture set",
                "dining set",
                "furniture set",
            ),
        )
    ):
        return False
    if re.search(
        r"\bset of\s+\d+\b.{0,64}\b(?:club |dining |outdoor |patio )?chairs?\b",
        hay,
        re.IGNORECASE,
    ) and not title_is_dining_set_bundle(hay):
        return False
    if has_any_phrase(hay, _COMPOUND_PHRASES):
        return True
    if title_is_dining_set_bundle(hay):
        return True
    if has_phrase(hay, "desk"):
        return True
    if _KITCHEN_ISLAND_TABLE_RE.search(hay):
        return True
    if _COFFEE_TABLE_OTTOMAN_RE.search(hay):
        return True
    if _LOFT_DESK_RE.search(hay):
        return True
    if _CHAIR_AND_OTTOMAN_SET_RE.search(hay):
        return True
    if _SOFA_OTTOMAN_RE.search(hay):
        return True
    if _PATIO_SET_RE.search(hay):
        return True
    return _OTTOMAN_COMPOUND_RE.search(hay) is not None


def compound_product_family(text: str) -> str | None:
    """Family kind for a compound title, 'unknown' if unmapped, None if not a compound.

    A compound hit must never fall through to the ottoman/cabinet keyword ladder.
    """
    hay = as_safe_text(text)
    if not hay or not has_compound_product_signal(hay):
        return None
    if title_is_dining_set_bundle(hay):
        return "dining_set"
    chair_set_head = re.search(
        r"\bset of\s+\d+\b.{0,64}\b(?:club |dining |outdoor |patio |chaise |lounge )?chairs?\b",
        hay,
        re.IGNORECASE,
    )
    if chair_set_head and not title_is_dining_set_bundle(hay):
        return None
    head = first_product_noun(hay)
    chair_heads = {
        "dining chair",
        "armchair",
        "club armchair",
        "lounge chair",
        "chaise",
        "chaise lounge",
        "lounger",
        "sun lounger",
    }
    patio_phrases = (
        "patio furniture set",
        "patio set",
        "patio conversation",
        "conversation set",
        "outdoor conversation set",
        "outdoor furniture set",
        "outdoor sectional",
        "outdoor sofa set",
        "furniture sofa set",
        "furniture set",
    )
    if head not in chair_heads and (
        has_any_phrase(hay, patio_phrases)
        or (_PATIO_SET_RE.search(hay) and title_has_clear_outdoor_word(hay))
    ):
        if title_has_clear_outdoor_word(hay):
            return "patio_furniture_set"
        if has_any_phrase(hay, ("sofa", "couch", "loveseat", "sectional", "living room")):
            return "sofa"
        return "unknown"
    if _KITCHEN_ISLAND_TABLE_RE.search(hay):
        return "kitchen_island"
    if _COFFEE_TABLE_OTTOMAN_RE.search(hay):
        return "coffee_table"
    if _SOFA_OTTOMAN_RE.search(hay) or (
        has_any_phrase(hay, ("sectional", "sofa", "couch", "loveseat"))
        and has_any_phrase(hay, ("ottoman", "ottomans", "footstool", "footstools"))
    ):
        return "sofa"
    if _CHAIR_AND_OTTOMAN_SET_RE.search(hay):
        return "armchair"
    if _LOFT_DESK_RE.search(hay) or has_any_phrase(hay, ("bunk bed", "loft bed", "platform bed", "bed frame")):
        return "bed"
    if has_any_phrase(
        hay,
        ("computer desk", "writing desk", "l-shaped desk", "l shaped desk", "corner desk", "home office desk"),
    ):
        return "desk"
    if has_any_phrase(hay, ("kids table", "play table", "activity table", "toddler table")):
        return "kids_table_set"
    return "unknown"


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
    hay = as_safe_text(text)
    if not hay:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(phrase)}s?(?![A-Za-z0-9])", hay, re.IGNORECASE) is not None


def has_any_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    return any(has_phrase(text, phrase) for phrase in phrases)


# Camping cook stations. Bare "outdoor cooking" is not a marker: grill carts and
# outdoor kitchen islands stay on their upstream route. has_phrase already
# allows one trailing s, so "camping tables" / "camp kitchens" match without a
# separate plural entry.
CAMPING_KITCHEN_PHRASES = (
    "camping kitchen", "camp kitchen",
    "camping cook station", "camping cooking station",
    "camping table",
)
CAMPING_CONTEXT_WORDS = ("camping", "camp")
COOK_STATION_PHRASES = ("cook station", "cooking station")
CAMPING_KITCHEN_EXCLUDE = (
    "grill", "grills", "griddle", "griddles", "bbq", "barbecue", "smoker", "smokers",
    "island", "islands", "kitchen island", "cart", "carts",
)


def is_camping_kitchen_title(title: str) -> bool:
    """True for a camping kitchen / camp table title, never a grill or cart.

    Word-boundary phrases only. An exclude word anywhere in the title wins.
    Description text is not consulted; callers must pass the title.
    """
    t = (title or "").lower()
    if has_any_phrase(t, CAMPING_KITCHEN_EXCLUDE):
        return False
    if has_any_phrase(t, CAMPING_KITCHEN_PHRASES):
        return True
    return has_any_phrase(t, COOK_STATION_PHRASES) and has_any_phrase(t, CAMPING_CONTEXT_WORDS)


def strip_room_location_lists(text: str) -> str:
    """Drop 'for Living Room, Bedroom, Balcony' lists so they cannot pick a type."""
    cleaned = _ROOM_LIST_RE.sub(" ", as_safe_text(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def primary_product_noun(live_title: str, source_title: str = "") -> str | None:
    """Head product noun, live title first. GIGA stuffing must not override it.

    Compound/set titles (dining set, bunk bed + nightstand, chair and ottoman,
    …) return None — uncertain means no remap, never a new profile.
    """
    for blob in (live_title, source_title):
        core = strip_room_location_lists(as_safe_text(blob))
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
            r"\bwith\b.{0,60}\b(?:storage ottomans?|ottoman bench|ottomans?)\b",
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
    """Any outdoor placement word, word-bounded. Decker ≠ deck, gardening ≠ garden."""
    hay = as_safe_text(text)
    if not hay:
        return False
    return bool(_OUTDOOR_MARKER_RE.search(hay))


# Stored leaf → head-noun family. If the live head noun matches, do not remap.
STORED_LEAF_LOCK: dict[str, tuple[str, ...]] = {
    "177000": ("kitchen island", "kitchen cart", "prep station", "kitchen cart"),
    "179444": ("tool chest", "tool cabinet", "tool storage", "tool storage cabinet", "tool box"),
    "179506": ("dolly",),
    "16515": ("seesaw", "swing", "slide"),
    "20488": ("tv stand", "entertainment center"),
    "38205": ("console table",),
    "66690": ("rocker", "glider", "rocking chair"),
    "75671": ("wagon", "cart"),
    "42425": ("medicine cabinet", "bathroom cabinet"),
    "42428": ("medicine cabinet", "bathroom cabinet"),
    "183322": ("sideboard", "buffet"),
    "63557": ("sideboard", "buffet"),
    "121851": ("dog crate", "kennel", "dog kennel", "pet cage", "pet crate"),
    "175754": ("bunk bed", "loft bed"),
    "262980": ("bench",),
    "20490": ("ottoman",),
    "88057": ("sewing table", "craft table", "sewing machine table"),
    "20487": ("bar cabinet", "liquor cabinet"),
    "32878": ("vanity", "makeup table", "bathroom vanity"),
    "176991": ("cabinet", "bathroom cabinet", "vanity"),
    "63108": ("chicken coop", "hutch", "rabbit hutch", "coop"),
    "131588": ("mattress",),
    "161059": ("trash can", "garbage bin", "bin shed", "trash can storage"),
}

OUTDOOR_STORED_LEAVES = frozenset({"79684", "79682", "139849", "112590", "138996"})
INDOOR_REMAP_LEAVES = frozenset({"38208", "54235", "38200", "38204", "107578"})
INDOOR_EVIDENCE_PHRASES = (
    "living room",
    "bedroom",
    "velvet",
    "upholstered",
    "office",
    "indoor",
)


def stored_leaf_locked(title: str, stored_category: str) -> bool:
    """True when the live head noun matches the stored leaf's family — do not remap."""
    stored = as_safe_category_id(stored_category)
    hay = as_safe_text(title)
    if not stored or not hay:
        return False
    phrases = STORED_LEAF_LOCK.get(stored)
    if not phrases:
        return False
    core = strip_room_location_lists(head_clause(hay)) or hay
    core = re.sub(r"\bmedicine\s+glass\s+cabinet\b", "medicine cabinet", core, flags=re.IGNORECASE)
    core = re.sub(r"\bglass\s+medicine\s+cabinet\b", "medicine cabinet", core, flags=re.IGNORECASE)
    if stored == "262980":
        # Lock benches only when "bench" is the head noun, not workbench/chaise stuffing.
        return bool(primary_product_noun(core) in {"dining_bench", "storage_bench"} or (
            has_phrase(core, "bench") and not has_any_phrase(core, ("workbench", "work bench"))
        ))
    if stored == "20490":
        head = first_product_noun(core)
        return head in {"ottoman", "storage footstool", "footstool"} or primary_product_noun(core) in {
            "ottoman",
            "storage_ottoman",
            "ottoman_bench",
        }
    if stored == "75671":
        # Kitchen cart is 177000; wagon/utility cart stays 75671.
        return has_any_phrase(core, ("wagon", "utility cart", "garden cart", "steel wagon"))
    return has_any_phrase(core, phrases)


def has_indoor_evidence(title: str) -> bool:
    """Indoor evidence in the live head clause. PE wicker / rattan is not indoor."""
    clause = head_clause(as_safe_text(title))
    if not clause:
        return False
    if title_has_clear_outdoor_word(title) and not has_any_phrase(clause, ("living room", "bedroom", "velvet", "upholstered", "office")):
        # "indoor" next to outdoor is dual-use, not indoor evidence.
        if has_phrase(clause, "indoor") and has_phrase(clause, "outdoor"):
            return False
    return has_any_phrase(clause, INDOOR_EVIDENCE_PHRASES) and not (
        has_phrase(clause, "indoor") and title_has_mixed_indoor_outdoor(title)
    )


def is_outdoor_to_indoor_downgrade(stored: str, proposed: str, title: str) -> bool:
    """Block outdoor stored leaf → indoor leaf without explicit indoor evidence."""
    stored_id = as_safe_category_id(stored)
    proposed_id = as_safe_category_id(proposed)
    if stored_id not in OUTDOOR_STORED_LEAVES:
        return False
    if proposed_id not in INDOOR_REMAP_LEAVES:
        return False
    if proposed_id == stored_id:
        return False
    return not has_indoor_evidence(title)


def names_indoor_room(text: str) -> bool:
    """Does the copy explicitly place the product in a named indoor room?"""
    hay = as_safe_text(text)
    if not hay:
        return False
    return has_any_phrase(hay, INDOOR_ROOM_MARKERS)


def outdoor_context(title: str, full_text: str) -> bool:
    """Is this product actually an outdoor product?

    A single incidental mention ("...or a leisure bench on the balcony") does not
    make an indoor product outdoor. When the copy names indoor rooms, the outdoor
    signal must appear in the TITLE to count. A room-list "balcony" plus indoor
    upholstery (velvet / upholstered / living room) is never enough.
    """
    title = as_safe_text(title)
    full_text = as_safe_text(full_text)
    title_core = strip_room_location_lists(head_clause(title) or title)
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
