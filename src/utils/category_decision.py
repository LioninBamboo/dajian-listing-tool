"""Single category-decision contract for audit and publish paths.

Both ``audit_single_product`` and ``normalize_generated_listing`` must call
``resolve_category_decision``. The two engines must agree on every SKU.

Hard rules:
- Uncertain → no remap (kind="unknown", category_id=None). Never invent a leaf.
- Canonicalize, if used, runs on the live title only. GIGA is fallback when
  the live title is empty. Description is never used for the leaf.
- Compound titles never fall through to the legacy keyword ladder.
- ``is_category_approved`` rows are never remapped by the callers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from src.utils.product_context_signals import (
    UNKNOWN_HEAD_PREFIX,
    as_safe_category_id,
    as_safe_text,
    compound_product_family,
    first_product_noun,
    has_any_phrase,
    is_outdoor_to_indoor_downgrade,
    stored_leaf_locked,
    title_has_clear_outdoor_word,
    title_has_mixed_indoor_outdoor,
    title_is_chair_accessory,
    title_is_dining_set_bundle,
    unknown_head_noun,
)

logger = logging.getLogger(__name__)

OUTDOOR_CHAIR = "79682"  # Patio Chairs (production leaf)
INDOOR_CHAIR = "54235"
INDOOR_SEATING = "38208"
OTTOMAN = "20490"
BENCH = "262980"
INDOOR_TABLE = "38204"
END_TABLE = "38200"  # not in the production tree — never write
NIGHTSTAND = "38199"
OUTDOOR_TABLE = "112590"
PATIO_SET = "139849"
DINING_SET = "107578"
DISPLAY_CABINET = "20493"  # not in the production tree — never write
WINE_RACK = "20689"
DESK = "88057"
KITCHEN_ISLAND = "177000"  # Power Tool Sets — implausible for furniture
CAT_FURNITURE = "20740"
HALL_TREE = "22656"  # Coat & Hat Racks; never write Antiques 261263
SIDEBOARD = "183322"
OUTDOOR_CHAISE = "79684"  # Loungers (production leaf)
KITCHEN_ISLAND_LEAF = "260157"
DISPLAY_CABINET_LEAF = "20487"

_LEAF_FAMILY = {
    OUTDOOR_CHAIR: "outdoor_chair",
    INDOOR_CHAIR: "dining_chair",
    INDOOR_SEATING: "seating",
    OTTOMAN: "ottoman",
    BENCH: "bench",
    INDOOR_TABLE: "table",
    END_TABLE: "side_table",
    NIGHTSTAND: "nightstand",
    OUTDOOR_TABLE: "outdoor_table",
    PATIO_SET: "patio_set",
    DINING_SET: "dining_set",
    DISPLAY_CABINET: "display",
    WINE_RACK: "wine",
    DESK: "desk",
    OUTDOOR_CHAISE: "outdoor_chair",
    "79682": "outdoor_chair",
    "175761": "table",
    "175758": "bed",
    "175754": "bed",
    "177000": "kitchen_island",
    "179444": "tool_chest",
    "121851": "dog_crate",
    "16515": "playground",
    "179506": "dolly",
    "20488": "tv_stand",
    "38205": "console",
    "66690": "rocker",
    "75671": "wagon",
    "42425": "medicine",
    "42428": "medicine",
    "183322": "sideboard",
    "63557": "sideboard",
    "22656": "hall_tree",
    "261263": "hall_tree",
    "20740": "cat_furniture",
    "20744": "dog_beds",
    "100411": "cat_litter",
}

_KIND_FAMILY = {
    "outdoor_chair": "outdoor_chair",
    "outdoor_chaise": "outdoor_chair",
    "dining_chair": "dining_chair",
    "armchair": "seating",
    "sofa": "seating",
    "ottoman": "ottoman",
    "storage_ottoman": "ottoman",
    "ottoman_bench": "ottoman",
    "indoor_bench": "bench",
    "dining_table": "table",
    "coffee_table": "table",
    "side_table": "side_table",
    "nightstand": "nightstand",
    "outdoor_table": "outdoor_table",
    "patio_furniture_set": "patio_set",
    "dining_set": "dining_set",
    "desk": "desk",
    "bed": "bed",
    "kitchen_island": "kitchen_island",
    "display_cabinet": "display",
    "hall_tree": "hall_tree",
    "sideboard": "sideboard",
    "cat_furniture": "cat_furniture",
    "unknown": "unknown",
    "keep": "keep",
}


@dataclass(frozen=True)
class CategorySuggestion:
    category_id: str
    path: str
    confidence: str
    source: str
    reason: str


@dataclass(frozen=True)
class CategoryDecision:
    category_id: str | None
    kind: str
    reason: str
    category_name: str | None = None
    conflict: bool = False
    suggested_alternative: str | None = None

    @property
    def keep(self) -> bool:
        return self.kind == "keep" or (self.category_id is None and self.kind != "unknown")


def _offline_matcher():
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Oauth:
        def get_application_token(self):
            return "offline"

    return EbayCategoryMatcher(_Oauth())


def _classify(title: str, stored: str):
    from src.utils.listing_quality_gate import classify_listing_profile

    return classify_listing_profile(title, "", stored, source_title="")


def _family_of(kind: str, category_id: str | None) -> str:
    if kind in _KIND_FAMILY:
        return _KIND_FAMILY[kind]
    if category_id and category_id in _LEAF_FAMILY:
        return _LEAF_FAMILY[category_id]
    return kind or "unknown"


_DISPLAY_HEADS = {"display cabinet", "curio cabinet", "glass display cabinet"}
_BAR_CABINET_HEADS = {"bar cabinet", "liquor cabinet", "wine cabinet", "coffee bar cabinet"}
_WINE_RACK_HEADS = {"wine rack", "bottle holder", "stemware rack"}
_SIDE_END_HEADS = {"end table", "side table"}
_COFFEE_HEADS = {"coffee table", "cocktail table", "nesting table", "nesting coffee table"}
_SIDE_TABLE_LOCK_STORED = frozenset({"121851", "79682", "79684", "138996", "75671"})


def _display_cabinet_title(title: str) -> bool:
    head = first_product_noun(title)
    if head in {"bookshelf", "bookcase", "medicine cabinet", "bathroom cabinet", "pantry cabinet"}:
        return False
    return head in _DISPLAY_HEADS


def _sewing_or_craft_table(title: str) -> bool:
    return has_any_phrase(as_safe_text(title), ("sewing table", "craft table", "sewing machine table"))


def _side_or_end_table_head(title: str, stored: str = "") -> bool:
    """True only when side/end table is the first product noun of the live head."""
    stored_id = as_safe_category_id(stored)
    head = first_product_noun(title)
    if stored_id in _SIDE_TABLE_LOCK_STORED:
        return False
    if stored_id == INDOOR_TABLE and head in _COFFEE_HEADS:
        return False
    if head in {
        "coffee table",
        "cocktail table",
        "nesting table",
        "nesting coffee table",
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
        "wagon",
        "steel wagon",
        "console table",
        "chaise",
        "chaise lounge",
        "lounger",
        "sun lounger",
        "papasan chair",
        "egg chair",
        "rocking chair",
        "adirondack chair",
        "tv stand",
        "hutch desk",
    }:
        return False
    if head and str(head).startswith(UNKNOWN_HEAD_PREFIX):
        return False
    if head in _SIDE_END_HEADS:
        return True
    return False


FURNITURE_PATH_PREFIXES = (
    "Home & Garden > Furniture",
    "Home & Garden > Yard, Garden & Outdoor Living > Patio & Garden Furniture",
    "Home & Garden > Kitchen, Dining & Bar",
    "Home & Garden > Household Supplies & Cleaning > Home Organization",
)
PET_DOG_PREFIX = "Pet Supplies > Dog Supplies"
PET_CAT_PREFIX = "Pet Supplies > Cat Supplies"
ANTIQUES_PREFIX = "Antiques"

_CAT_TITLE_RE = re.compile(r"\b(?:cat|kitten|litter|cat\s+tree)s?\b", re.IGNORECASE)
_DOG_TITLE_RE = re.compile(r"\b(?:dog|puppy|puppies|canine)s?\b", re.IGNORECASE)
_LOUNGER_HEADS = frozenset({"chaise", "chaise lounge", "lounger", "sun lounger", "outdoor recliner"})
_PATIO_CHAIR_HEADS = frozenset(
    {
        "armchair",
        "club armchair",
        "papasan chair",
        "egg chair",
        "rocking chair",
        "adirondack chair",
        "adirondack",
        "folding chair",
        "garden stool",
        "patio chair",
        "outdoor chair",
        "dining chair",
    }
)
_ADJUSTABLE_LOUNGE_RE = re.compile(r"\b(?:adjustable|reclin(?:e|er|ing))\b", re.IGNORECASE)
_TV_STAND_HEADS = frozenset({"tv stand", "media console", "entertainment center"})
_FURNITURE_KINDS = frozenset(
    {
        "outdoor_chair",
        "outdoor_chaise",
        "dining_chair",
        "armchair",
        "sofa",
        "ottoman",
        "storage_ottoman",
        "ottoman_bench",
        "indoor_bench",
        "dining_table",
        "coffee_table",
        "side_table",
        "nightstand",
        "outdoor_table",
        "patio_furniture_set",
        "dining_set",
        "desk",
        "bed",
        "kitchen_island",
        "display_cabinet",
        "hall_tree",
        "sideboard",
        "wine_rack",
    }
)
_HEAD_FITS_STORED = {
    "vanity": frozenset({"32878", "176991"}),
    "cabinet": frozenset({"20487", "176991", "42428", "38217"}),
    "pantry": frozenset({"20487", "42428", "176991"}),
    "hutch": frozenset({"20487", "63108", "176991"}),
    "mattress": frozenset({"131588"}),
    "tv stand": frozenset({"20488"}),
}
_TOOL_TITLE_RE = re.compile(
    r"\b(?:bolt\s+cutters?|vises?|clamps?|workbenches?|work\s+bench|"
    r"tool\s+sets?|air\s+tanks?|air\s+compressors?|tool\s+chest|"
    r"tool\s+cabinet|power\s+tools?|impact\s+wrench)\b",
    re.IGNORECASE,
)
_KITCHEN_ISLAND_TITLE_RE = re.compile(
    r"\b(?:kitchen\s+island|kitchen\s+cart|prep\s+station|baker(?:'s)?\s+rack)\b",
    re.IGNORECASE,
)
_SPORTS_KEEP_RE = re.compile(
    r"\b(?:treadmill|golf\s+mat|putting\s+(?:green|mat)|ping[\s-]*pong\s+table|"
    r"table\s+tennis)\b",
    re.IGNORECASE,
)
_GRILL_TABLE_RE = re.compile(
    r"\b(?:grill|bbq|barbecue)\s+tables?\b",
    re.IGNORECASE,
)
_GRILL_CART_RE = re.compile(
    r"\b(?:grill\s+carts?|bbq\s+carts?|barbecue\s+carts?|"
    r"outdoor\s+kitchen\s+islands?)\b",
    re.IGNORECASE,
)
_BARBECUE_CATEGORY_IDS = frozenset({"20725"})
_BARBECUE_PATH_PREFIXES = (
    "Home & Garden > Yard, Garden & Outdoor Living > Outdoor Heating, Cooking & Eating",
)
_TREE_UNAVAILABLE_WARNED = False

# Ship empty. Optional JSON override: data/config/approved_auto_remaps.json
# Each pair is (stored_or_empty, target_id) or (reason, target_id).
APPROVED_AUTO_REMAPS: frozenset[tuple[str, str]] = frozenset()

# Shared with batch_publish.KNOWN_PUBLISHABLE_CATEGORY_IDS so a cache snapshot
# that omitted these production leaves cannot hold a matching stored id.
TREE_KNOWN_PUBLISHABLE_LEAVES: frozenset[str] = frozenset({"38217", "79694"})
_TREE_KNOWN_PUBLISHABLE_INDEX = {
    "79694": ("Porch Swings", "900003"),
    "38217": ("China Cabinets", "900007"),
}

# High-confidence title kinds: stored leaf must be one of these or hold.
_KIND_ALLOWED_STORED = {
    "display_cabinet": frozenset({DISPLAY_CABINET_LEAF, "38217"}),
    "hall_tree": frozenset({HALL_TREE, "32880"}),
}

HOLD_EMPTY_PUBLISH_ERROR = (
    "Stored category is empty or invalid; holding publish. No category write."
)
HOLD_NOT_SELLABLE_PUBLISH_ERROR = (
    "Stored category is not a sellable leaf in this store's publish tree; "
    "holding publish. No category write."
)
CATEGORY_HOLD_ACTIONS = frozenset(
    {
        "hold_empty",
        "hold_implausible",
        "hold_tree_unavailable",
        "hold_not_sellable",
        "hold_wrong_store",
        "hold_category_key_conflict",
    }
)
HOLD_TREE_UNAVAILABLE_ERROR = (
    "category tree unavailable; refresh failed; no publish"
)
NEEDS_MANUAL_MOTORS_LEAF_REVIEW = "needs_manual_motors_leaf_review"


class CategoryGateResult(tuple):
    """``(category_id, action, error)`` plus report-only ``.flags``.

    Unpacks like the historical 3-tuple so existing callers stay intact.
    """

    def __new__(cls, category_id, action, error, flags=None, suggestion=None, reasons=None):
        inst = super().__new__(cls, (category_id, action, error))
        inst.flags = list(flags or [])
        inst.suggestion = suggestion
        inst.reasons = list(reasons or [])
        return inst


# Aqua READY tool items sitting in tree-0 furniture leaves (R7–R11).
# Decision B3 default: do not hold; only report.
MOTORS_FURNITURE_REVIEW_LEAVES: frozenset[str] = frozenset(
    {"20487", "88057", "262980"}
)

_SUGGESTED_ALTERNATIVES = {
    "38200": "38204",
    "20493": "20487",
    "177000": "260157",
    "261263": "22656",
}

# Head-noun → (leaf, family, default confidence). Tree 0 only.
_HEAD_NOUN_SUGGESTED_LEAF: dict[str, tuple[str, str, str]] = {
    "office chair": ("54235", "chair", "med"),
    "gaming chair": ("54235", "chair", "med"),
    "dining chair": ("54235", "chair", "high"),
    "accent chair": ("38208", "seating", "high"),
    "loveseat": ("38208", "seating", "high"),
    "sofa": ("38208", "seating", "high"),
    "couch": ("38208", "seating", "high"),
    "sideboard": ("183322", "sideboard", "high"),
    "buffet": ("183322", "sideboard", "high"),
    "bar stool": ("103431", "stool", "med"),
    "bathroom vanity": ("176991", "vanity", "high"),
    "vanity table": ("32878", "vanity", "high"),
    "makeup vanity": ("32878", "vanity", "high"),
    "kitchen cart": ("115753", "kitchen_cart", "high"),
    "tv stand": ("20488", "tv_stand", "high"),
    "media console": ("20488", "tv_stand", "high"),
    "entertainment center": ("20488", "tv_stand", "high"),
    "kitchen island": ("260157", "kitchen_island", "high"),
    "kitchen island cart": ("260157", "kitchen_island", "high"),
    "medicine cabinet": ("176991", "medicine", "high"),
    "trimming tool": ("71277", "trimmer", "med"),
    "string trimmer": ("71277", "trimmer", "med"),
    "fogger": ("181037", "fogger", "med"),
    "drill press": ("71296", "drill_press", "high"),
    "fruit press": ("260139", "brewing", "med"),
    "wine press": ("260139", "brewing", "med"),
    "fruit and wine press": ("260139", "brewing", "med"),
    "fruit and wine": ("260139", "brewing", "med"),
    "grill cart": ("260931", "grill_cart", "med"),
    "grill table": ("112590", "outdoor_table", "med"),
    "fireplace screen": ("38221", "fireplace", "high"),
    "filing cabinet": ("3299", "filing", "med"),
    "dome climber": ("16515", "playground", "med"),
    "jungle gym": ("16515", "playground", "med"),
    "console table": ("38204", "table", "high"),
    "side table": ("38204", "table", "high"),
    "end table": ("38204", "table", "high"),
    "dining set": ("107578", "dining_set", "high"),
    "nook dining set": ("107578", "dining_set", "high"),
    "table set": ("107578", "dining_set", "high"),
    "dining table set": ("107578", "dining_set", "high"),
    "bar table set": ("107578", "dining_set", "med"),
    "wardrobe": ("103430", "wardrobe", "high"),
    "hall tree": ("22656", "hall_tree", "high"),
    "coat rack": ("22656", "hall_tree", "high"),
    "coat stand": ("22656", "hall_tree", "high"),
    "chaise lounge": ("79684", "lounger", "high"),
    "chaise": ("79684", "lounger", "high"),
    "lounger": ("79684", "lounger", "high"),
    "sun lounger": ("79684", "lounger", "high"),
    "armchair": ("38208", "seating", "high"),
    "club armchair": ("38208", "seating", "high"),
    "patio chair": ("79682", "outdoor_chair", "high"),
    "outdoor chair": ("79682", "outdoor_chair", "high"),
    "display cabinet": ("20487", "display", "high"),
    "glass display cabinet": ("20487", "display", "high"),
    "curio cabinet": ("20487", "display", "high"),
    "bench": ("262980", "bench", "high"),
    "dresser": ("114397", "dresser", "high"),
    "dining table": ("38204", "table", "high"),
    "bar table": ("38204", "table", "med"),
}

_SUGGESTION_LEAF_FAMILY: dict[str, str] = {
    "54235": "chair",
    "38208": "seating",
    "183322": "sideboard",
    "63557": "sideboard",
    "103431": "stool",
    "32878": "vanity",
    "115753": "kitchen_cart",
    "260157": "kitchen_island",
    "176991": "medicine",
    "42425": "medicine",
    "42428": "medicine",
    "71277": "trimmer",
    "181037": "fogger",
    "71296": "drill_press",
    "260139": "brewing",
    "260931": "grill_cart",
    "20725": "grill_cart",
    "112590": "outdoor_table",
    "38221": "fireplace",
    "3299": "filing",
    "16515": "playground",
    "38204": "table",
    "38200": "table",
    "175761": "table",
    "107578": "dining_set",
    "103430": "wardrobe",
    "22656": "hall_tree",
    "261263": "hall_tree",
    "32880": "hall_tree",
    "79682": "outdoor_chair",
    "79684": "lounger",
    "20487": "display",
    "20493": "display",
    "38217": "display",
    "114397": "dresser",
    "20466": "dresser",
    "262980": "bench",
    "88057": "desk",
    "20744": "dog_beds",
}

_TOOL_APPLIANCE_TOY_FAMILIES = frozenset(
    {
        "trimmer",
        "fogger",
        "drill_press",
        "brewing",
        "playground",
        "garlic_press",
    }
)
_FURNITURE_LEAVES_FORBIDDEN_FOR_TOOLS = frozenset(
    {"38208", "88057", "20487", "38204", "54235", "107578", "262980"}
)
_GENERIC_MED_HEADS = frozenset({"chair", "cabinet", "tool system"})
_ACCESSORY_SUGGESTION_HEADS = frozenset(
    {
        "cushion",
        "cover",
        "pad",
        "slipcover",
        "chair mat",
        "office chair mat",
        "table top",
        "tabletop",
        "garlic press",
    }
)


def _category_index(index=None):
    from src.utils.category_tree_cache import load_category_index

    if index is not None:
        return dict(index)
    loaded = load_category_index()
    if not loaded:
        return {}
    flat = dict(loaded)
    for cid, meta in _TREE_KNOWN_PUBLISHABLE_INDEX.items():
        flat.setdefault(cid, meta)
    return flat


def category_tree_is_available(index=None) -> bool:
    return bool(_category_index(index))


def reset_tree_unavailable_warning() -> None:
    global _TREE_UNAVAILABLE_WARNED
    _TREE_UNAVAILABLE_WARNED = False


def warn_once_tree_unavailable(log=None) -> None:
    """One run-level warning when the tree cache is missing or stale."""
    global _TREE_UNAVAILABLE_WARNED
    if _TREE_UNAVAILABLE_WARNED:
        return
    _TREE_UNAVAILABLE_WARNED = True
    (log or logger).warning(
        "Category tree cache missing or stale; skipping per-row plausibility "
        "flags; category writes fail closed"
    )


def unpublished_listing_status(status: str = "") -> bool:
    return str(status or "").strip().upper() in {
        "",
        "READY",
        "PENDING",
        "READY_TO_PUBLISH",
    }


def publish_listing_status(status: str = "") -> str:
    """Manual/API publish treats every non-PUBLISHED row as READY (about to publish)."""
    return "PUBLISHED" if str(status or "").strip().upper() == "PUBLISHED" else "READY"


def _looks_like_tool_title(title: str) -> bool:
    return bool(_TOOL_TITLE_RE.search(as_safe_text(title)))


def _looks_like_kitchen_island_title(title: str) -> bool:
    return bool(_KITCHEN_ISLAND_TITLE_RE.search(as_safe_text(title)))


def _looks_like_grill_table_title(title: str) -> bool:
    return bool(_GRILL_TABLE_RE.search(as_safe_text(title)))


def _looks_like_grill_cart_title(title: str) -> bool:
    return bool(_GRILL_CART_RE.search(as_safe_text(title)))


def _is_barbecue_category(cid: str, index=None) -> bool:
    stored = as_safe_category_id(cid)
    if stored in _BARBECUE_CATEGORY_IDS:
        return True
    path = _category_path(stored, index=index)
    return any(path.startswith(prefix) for prefix in _BARBECUE_PATH_PREFIXES)


def legacy_remap_candidate(stored: Any) -> tuple[str, str]:
    """Raw stored id plus the INVALID_CATEGORY_REMAP candidate, if any."""
    from src.services.taxonomy_constants import normalize_legacy_category_id

    raw = as_safe_category_id(stored)
    remapped = normalize_legacy_category_id(raw)
    if remapped and raw and remapped != raw:
        return remapped, "legacy_invalid_remap"
    return remapped or raw, "publish_candidate"


def stored_category_name_for_keep(original_stored: Any, stored_name: Any = None) -> str:
    stored = as_safe_category_id(original_stored)
    name = str(stored_name or "").strip()
    if name:
        return name
    return f"Category {stored}" if stored else ""


def _confident_title_kind(title: str) -> str:
    """Kinds we will hold on when the stored leaf is the wrong family.

    Intentionally narrow so tools/vanities/cabinets/hutches/desks/sports
    titles keep a fitting stored leaf (PR #29 false-flag set).
    """
    hay = as_safe_text(title)
    if not hay:
        return ""
    if _display_cabinet_title(hay):
        return "display_cabinet"
    if _looks_like_grill_table_title(hay):
        return "outdoor_table"
    if _looks_like_kitchen_island_title(hay):
        return "kitchen_island"
    if _title_is_hall_tree_head(hay):
        return "hall_tree"
    return ""


def _title_is_hall_tree_head(title: str) -> bool:
    """True only when hall tree is the live head noun, not a later mention."""
    hay = as_safe_text(title)
    if not hay:
        return False
    head = first_product_noun(hay)
    if head == "hall tree":
        return True
    from src.utils.product_context_signals import _first_product_noun_clause

    clause = _first_product_noun_clause(hay)
    if not clause or not re.search(r"\bhall\s+tree\b", clause, re.IGNORECASE):
        return False
    # Same first-clause product named as a coat rack (T3971P401872).
    if head in {None, "coat rack", "coat stand"}:
        return True
    if head and str(head).startswith(UNKNOWN_HEAD_PREFIX):
        return True
    return False


def _legacy_remap_target(stored: str) -> str:
    from src.services.taxonomy_constants import INVALID_CATEGORY_REMAP

    return str(INVALID_CATEGORY_REMAP.get(as_safe_category_id(stored) or stored, "") or "")


def is_legacy_remap_stored(stored: Any) -> bool:
    return bool(_legacy_remap_target(as_safe_category_id(stored)))


def hold_implausible_publish_error(stored: str, title: str = "") -> str:
    stored_id = as_safe_category_id(stored)
    alt = suggested_alternative_for(stored_id, _confident_title_kind(title), title)
    msg = f"Stored category {stored_id or '(empty)'} is not publishable"
    if alt:
        msg += f"; suggested {alt}"
    return msg


def tree_unavailable_publish_error() -> str:
    from src.utils.category_tree_cache import store_runtime_tree_ids, unavailable_runtime_trees

    missing = sorted(unavailable_runtime_trees())
    owned = {str(tid) for tid in store_runtime_tree_ids()}
    if missing and set(missing) != owned:
        if len(missing) == 1:
            return f"tree {missing[0]} unavailable"
        return f"trees {', '.join(missing)} unavailable; refresh failed; no publish"
    return HOLD_TREE_UNAVAILABLE_ERROR


def _title_agrees_with_stored(title: str, stored: str, index=None) -> bool:
    """True when the stored leaf already fits the live head (do not flag)."""
    stored_id = as_safe_category_id(stored)
    hay = as_safe_text(title)
    if not stored_id or not hay:
        return False
    if stored_leaf_locked(hay, stored_id):
        return True
    head = first_product_noun(hay)
    deny = unknown_head_noun(head) or head
    if deny and stored_id in _HEAD_FITS_STORED.get(str(deny), ()):
        return True
    if deny == "mattress" or head == "mattress":
        return stored_id in _HEAD_FITS_STORED["mattress"]
    if _SPORTS_KEEP_RE.search(hay):
        return True
    if _looks_like_grill_cart_title(hay) and _is_barbecue_category(stored_id, index):
        return True
    if _looks_like_tool_title(hay):
        path = _category_path(stored_id, index=index)
        return path.startswith("Home & Garden > Tools & Workshop Equipment")
    return False


def _category_path(cid: str, index=None) -> str:
    from src.utils.category_tree_cache import build_category_path

    return str(build_category_path(str(cid or "").strip(), index=index) or "")


def _default_suggestion_tree_id() -> str:
    try:
        from src.utils.store_profile import get_store_profile

        return str(get_store_profile().category_tree_id or "0")
    except Exception:
        return "0"


def _suggestion_family_of(cid: str | None, head: str | None = None) -> str:
    stored = as_safe_category_id(cid)
    if stored and stored in _SUGGESTION_LEAF_FAMILY:
        return _SUGGESTION_LEAF_FAMILY[stored]
    if head and head in _HEAD_NOUN_SUGGESTED_LEAF:
        return _HEAD_NOUN_SUGGESTED_LEAF[head][1]
    return ""


def _leaf_is_in_store_tree(cid: str, tree_id: str, index=None) -> bool:
    target = as_safe_category_id(cid)
    if not target:
        return False
    path = _category_path(target, index=index)
    if not path:
        return False
    is_motors = path.startswith("eBay Motors")
    if str(tree_id) == "100":
        return is_motors
    return not is_motors


def _suggestion_hits_high_fee(cid: str, index=None) -> bool:
    from src.utils.publish_fee_preview import category_is_high_fee

    target = as_safe_category_id(cid)
    flat = _category_index(index)
    seen: set[str] = set()
    current = target
    while current and current not in seen:
        seen.add(current)
        path = _category_path(current, index=flat)
        name = ""
        if current in flat:
            name = str(flat[current][0] or "")
        if category_is_high_fee(category_id=current, category_path=path, category_name=name):
            return True
        _name, parent = flat.get(current, ("", ""))
        current = str(parent or "")
    return False


def _suggestion_is_valid(cid: str | None, title: str, *, tree_id: str, index=None) -> bool:
    target = as_safe_category_id(cid)
    if not target:
        return False
    ok, _reason = validate_target_category(target, "", title, index=index)
    if not ok:
        return False
    if not _leaf_is_in_store_tree(target, tree_id, index=index):
        return False
    if _suggestion_hits_high_fee(target, index=index):
        return False
    return True


def _path_shares_family(cid: str, family: str, index=None) -> bool:
    if not family:
        return False
    if _suggestion_family_of(cid) == family:
        return True
    path = _category_path(cid, index=index).lower()
    name = ""
    flat = _category_index(index)
    if cid in flat:
        name = str(flat[cid][0] or "").lower()
    tokens = {
        "chair": ("chair", "stool"),
        "seating": ("sofa", "armchair", "couch", "loveseat"),
        "sideboard": ("sideboard", "buffet"),
        "stool": ("stool",),
        "vanity": ("vanity", "makeup"),
        "kitchen_cart": ("kitchen cart",),
        "kitchen_island": ("kitchen island",),
        "medicine": ("bathroom cabinet", "medicine"),
        "trimmer": ("trimmer",),
        "fogger": ("fogger",),
        "drill_press": ("drill press",),
        "brewing": ("brewing", "wine making", "equipment"),
        "grill_cart": ("barbecue", "grill"),
        "outdoor_table": ("patio", "garden table"),
        "fireplace": ("fireplace",),
        "filing": ("filing",),
        "playground": ("swing", "slide", "gym", "playground"),
        "table": ("table",),
        "dining_set": ("dining set",),
        "wardrobe": ("wardrobe", "armoire"),
        "hall_tree": ("coat", "hat rack", "hall tree"),
        "outdoor_chair": ("patio chair",),
        "lounger": ("lounger", "chaise"),
        "display": ("cabinet", "cupboard"),
        "dresser": ("dresser",),
        "bench": ("bench",),
    }
    needles = tokens.get(family, (family.replace("_", " "),))
    blob = f"{path} {name}"
    return any(needle in blob for needle in needles)


def _stored_id_candidates(stored: str) -> list[str]:
    out: list[str] = []
    remapped = as_safe_category_id(_legacy_remap_target(stored))
    if remapped and remapped != stored:
        out.append(remapped)
    mapped = as_safe_category_id(_SUGGESTED_ALTERNATIVES.get(stored, ""))
    if mapped and mapped not in out:
        out.append(mapped)
    return out


def _matcher_hold_candidate(title: str, stored: str) -> str:
    hay = as_safe_text(title)
    if not hay:
        return ""
    try:
        matcher = _offline_matcher()
        cid, _name = matcher.canonicalize_category(hay, stored or "", None, "")
        cid = as_safe_category_id(cid)
        if cid and cid != stored:
            return cid
        fallback = matcher._fallback_category(hay)
        if fallback:
            return as_safe_category_id(fallback[0] if isinstance(fallback, (tuple, list)) else fallback)
    except Exception as exc:
        logger.warning("hold matcher fallback failed closed: %s", exc)
    return ""


def _apply_outdoor_head_override(head: str, title: str, leaf: str, family: str) -> tuple[str, str]:
    hay = as_safe_text(title)
    outdoor = title_has_clear_outdoor_word(hay) and not title_has_mixed_indoor_outdoor(hay)
    if _is_lounger_head(head, hay) or head in _LOUNGER_HEADS:
        return OUTDOOR_CHAISE, "lounger"
    if outdoor and (_is_patio_chair_head(head, hay) or head in _PATIO_CHAIR_HEADS):
        return OUTDOOR_CHAIR, "outdoor_chair"
    return leaf, family


def suggest_category_for_hold(
    title: str,
    stored_id: str | None,
    *,
    tree_id: str | None = None,
    index=None,
) -> CategorySuggestion | None:
    """Head-noun-first hold suggestion. Never writes a category."""
    hay = as_safe_text(title)
    stored = as_safe_category_id(stored_id)
    runtime_tree = str(tree_id or _default_suggestion_tree_id() or "0")
    flat = _category_index(index)

    if re.search(r"\bhuman\s+dog\s+beds?\b", hay, re.IGNORECASE):
        chosen = INDOOR_SEATING
        if _suggestion_is_valid(chosen, hay, tree_id=runtime_tree, index=flat):
            return CategorySuggestion(
                chosen,
                _category_path(chosen, index=flat),
                "low",
                "head_noun",
                "ambiguous_human_dog_bed",
            )
        return None
    if re.search(r"\bonly\s+table\s+tops?\b", hay, re.IGNORECASE):
        chosen = INDOOR_TABLE
        if _suggestion_is_valid(chosen, hay, tree_id=runtime_tree, index=flat):
            return CategorySuggestion(
                chosen,
                _category_path(chosen, index=flat),
                "low",
                "head_noun",
                "ambiguous_table_top",
            )
        return None

    head = first_product_noun(hay)
    deny = unknown_head_noun(head)
    raw_head = deny or head
    if deny and deny in _ACCESSORY_SUGGESTION_HEADS:
        return None
    if head in _ACCESSORY_SUGGESTION_HEADS:
        return None
    if not raw_head or (head and str(head).startswith(UNKNOWN_HEAD_PREFIX) and deny not in _HEAD_NOUN_SUGGESTED_LEAF):
        return None

    table_row = _HEAD_NOUN_SUGGESTED_LEAF.get(str(raw_head or ""))
    family = table_row[1] if table_row else ""
    table_leaf = table_row[0] if table_row else ""
    confidence = table_row[2] if table_row else "med"
    source = "head_noun"
    reason = f"head_noun:{raw_head}"

    if table_leaf:
        table_leaf, family = _apply_outdoor_head_override(str(raw_head), hay, table_leaf, family)
        if table_leaf == KITCHEN_ISLAND_LEAF and re.search(
            r"\b(?:extendable|dining\s+table)\b", hay, re.IGNORECASE
        ):
            confidence = "med"
        if str(raw_head) in _GENERIC_MED_HEADS:
            confidence = "med"
        chosen = table_leaf
        agreeing = [
            cand
            for cand in _stored_id_candidates(stored)
            if _suggestion_family_of(cand) == family
        ]
        if agreeing and agreeing[0] == table_leaf:
            chosen = agreeing[0]
            source = "stored_id_map"
            reason = f"stored_id_agrees:{raw_head}"
            if confidence != "med":
                confidence = "high"
        else:
            chosen = table_leaf
            source = "head_noun"
    else:
        matcher_cid = _matcher_hold_candidate(hay, stored)
        if (
            matcher_cid
            and family
            and _path_shares_family(matcher_cid, family, index=flat)
            and matcher_cid not in _FURNITURE_LEAVES_FORBIDDEN_FOR_TOOLS
        ):
            chosen = matcher_cid
            source = "matcher"
            reason = f"matcher:{raw_head}"
            confidence = "med"
        else:
            return None

    if family in _TOOL_APPLIANCE_TOY_FAMILIES and chosen in _FURNITURE_LEAVES_FORBIDDEN_FOR_TOOLS:
        return None
    if not _suggestion_is_valid(chosen, hay, tree_id=runtime_tree, index=flat):
        return None
    return CategorySuggestion(
        chosen,
        _category_path(chosen, index=flat),
        confidence,
        source,
        reason,
    )


def suggested_alternative_for(
    cid: str | None,
    kind: str = "",
    title: str = "",
    *,
    tree_id: str | None = None,
    index=None,
) -> str | None:
    suggestion = suggest_category_for_hold(
        title,
        cid,
        tree_id=tree_id,
        index=index,
    )
    return suggestion.category_id if suggestion else None


def _title_has_cat_token(title: str) -> bool:
    return bool(_CAT_TITLE_RE.search(as_safe_text(title)))


def _title_has_dog_token(title: str) -> bool:
    return bool(_DOG_TITLE_RE.search(as_safe_text(title)))


def _is_lounger_head(head: str | None, title: str) -> bool:
    if head in _LOUNGER_HEADS:
        return True
    if head == "lounge chair" and _ADJUSTABLE_LOUNGE_RE.search(as_safe_text(title)):
        return True
    return False


def _is_patio_chair_head(head: str | None, title: str) -> bool:
    if head in _PATIO_CHAIR_HEADS:
        if head == "lounge chair" and _ADJUSTABLE_LOUNGE_RE.search(as_safe_text(title)):
            return False
        return True
    hay = as_safe_text(title).lower()
    if re.search(r"\b(?:papasan|egg|adirondack|rocking)\s+chairs?\b", hay):
        return True
    return False


def pet_species_mismatch_reason(title: str, path: str) -> str | None:
    hay = as_safe_text(title)
    if not hay or not path:
        return None
    has_cat = _title_has_cat_token(hay)
    has_dog = _title_has_dog_token(hay)
    if has_cat and not has_dog and f"> Dog Supplies" in path:
        return "pet_species_mismatch"
    if has_dog and not has_cat and f"> Cat Supplies" in path:
        return "pet_species_mismatch"
    return None


_TOOL_GARAGE_PRODUCT_RE = re.compile(
    r"\b(?:tool\s+chests?|tool\s+box(?:es)?|tool\s+carts?|tool\s+storages?|"
    r"tool\s+cabinets?|floor\s+jacks?|bolt\s+cutters?|creepers?|mechanics?)\b",
    re.IGNORECASE,
)
_CREEPER_RE = re.compile(r"\bcreepers?\b", re.IGNORECASE)
_FLOOR_JACK_RE = re.compile(
    r"\b(?:floor\s+jacks?|cross\s+beams?(?:\s+adapters?)?)\b",
    re.IGNORECASE,
)
_DOG_CRATE_PRODUCT_RE = re.compile(
    r"\b(?:dog\s+crates?|dog\s+kennels?|dog\s+cages?|pet\s+cages?)\b",
    re.IGNORECASE,
)
_COLLECTIBLE_SIGNAL_RE = re.compile(
    r"\b(?:vintage|antique|collectibles?|signed|graded)\b",
    re.IGNORECASE,
)
_FLOORING_STRIP_RE = re.compile(
    r"\b(?:sealing\s+strips?|edge\s+sealing)\b",
    re.IGNORECASE,
)
_FLOOR_WORD_RE = re.compile(r"\b(?:floors?|flooring)\b", re.IGNORECASE)
_FURNITURE_CABINET_IDS = frozenset({"38221", "20487"})
_CREEPER_REJECT_IDS = frozenset({"39731", "33088"})
_ROOF_RACK_IDS = frozenset({"262216", "33651"})
_CAULK_IDS = frozenset({"184315"})
_COLLECTIBLE_LEAF_IDS = frozenset({"975", "3489"})
_TABLE_WORD_RE = re.compile(r"\btables?\b", re.IGNORECASE)
_TOOL_BOX_NAME_RE = re.compile(r"\btool\s+box(?:es)?\b", re.IGNORECASE)
_WORK_BENCH_NAME_RE = re.compile(r"\bwork\s+benches?\b", re.IGNORECASE)


def title_vs_category_implausible(
    title: str,
    category_id: str | None,
    *,
    path: str = "",
    category_name: str = "",
) -> str | None:
    """Title-family mismatch. Word boundaries only. None means this check passed.

    Does not invent a replacement leaf. Callers hold and may attach a
    report-only suggestion from a separate plausible candidate.
    """
    hay = as_safe_text(title)
    cid = as_safe_category_id(category_id) or ""
    if not hay or not cid:
        return None
    name = as_safe_text(category_name).lower()
    path_text = as_safe_text(path)
    path_low = path_text.lower()
    furniture_leaf = cid in _FURNITURE_CABINET_IDS or any(
        path_text.startswith(prefix) for prefix in FURNITURE_PATH_PREFIXES
    )
    if _TOOL_GARAGE_PRODUCT_RE.search(hay) and furniture_leaf:
        return "implausible_category"
    if _CREEPER_RE.search(hay) and (
        cid in _CREEPER_REJECT_IDS
        or _TOOL_BOX_NAME_RE.search(name)
        or _WORK_BENCH_NAME_RE.search(name)
    ):
        return "implausible_category"
    if _FLOOR_JACK_RE.search(hay) and (
        cid in _ROOF_RACK_IDS or "roof rack" in name or "roof rack" in path_low
    ):
        return "implausible_category"
    if _DOG_CRATE_PRODUCT_RE.search(hay) and (
        "end table" in name
        or "end table" in path_low
        or cid in {"54235", "38204"}
        or _TABLE_WORD_RE.search(name)
    ):
        return "implausible_category"
    collectible_leaf = (
        cid in _COLLECTIBLE_LEAF_IDS
        or path_text.startswith("Collectibles")
        or path_text.startswith("Stamps")
        or path_text.startswith(ANTIQUES_PREFIX)
        or "collectible" in name
        or "stamps" in path_low
        or name in {"stamps", "germany & colonies"}
    )
    if collectible_leaf and not _COLLECTIBLE_SIGNAL_RE.search(hay):
        return "implausible_category"
    if (
        _FLOORING_STRIP_RE.search(hay)
        and _FLOOR_WORD_RE.search(hay)
        and (
            cid in _CAULK_IDS
            or "caulk" in name
            or "sealant" in name
            or "caulks & sealants" in path_low
        )
    ):
        return "implausible_category"
    return None


def conflicting_category_keys(
    stored: Any = None,
    optimization: Any = None,
    attributes: Any = None,
) -> tuple[dict[str, str], str | None]:
    """Return ``(present, suggestion)``. Empty ``present`` means the keys agree.

    Suggestion is report-only: the non-``categoryId`` value when the other
    keys agree with each other. Nothing is written back onto the row.
    """
    opt = optimization if isinstance(optimization, dict) else {}
    attrs = attributes if isinstance(attributes, dict) else {}
    present: dict[str, str] = {}

    def _add(label: str, raw: Any) -> None:
        cid = as_safe_category_id(raw)
        if cid:
            present[label] = cid

    _add("categoryId", stored if stored not in (None, "") else opt.get("categoryId"))
    if stored not in (None, "") and "categoryId" not in present:
        _add("categoryId", opt.get("categoryId"))
    _add("category_id", opt.get("category_id"))
    _add("ebay_category_id", opt.get("ebay_category_id"))
    _add("attributes.forced_category_id", attrs.get("forced_category_id"))
    _add("attributes.category_id", attrs.get("category_id"))
    _add("attributes.ebay_category_id", attrs.get("ebay_category_id"))
    _add("attributes.categoryId", attrs.get("categoryId"))
    unique = set(present.values())
    if len(unique) <= 1:
        return {}, None
    primary = present.get("categoryId")
    others = {cid for label, cid in present.items() if label != "categoryId"}
    suggestion = next(iter(others)) if len(others) == 1 and primary not in others else None
    return present, suggestion


def validate_target_category(
    cid: str | None,
    kind: str = "",
    title: str = "",
    *,
    index=None,
) -> tuple[bool, str]:
    """Fail closed before any category write. Never substitutes another id."""
    target = as_safe_category_id(cid)
    if not target:
        return False, "invalid_target_leaf"
    flat = _category_index(index)
    if not flat:
        return False, "tree_unavailable"
    from src.utils.category_tree_cache import is_cached_leaf_category

    if target in TREE_KNOWN_PUBLISHABLE_LEAVES and target not in flat:
        flat = dict(flat)
        flat[target] = _TREE_KNOWN_PUBLISHABLE_INDEX[target]
    if not is_cached_leaf_category(target, index=flat):
        from src.utils.category_tree_cache import unavailable_runtime_trees

        if unavailable_runtime_trees():
            return False, "tree_unavailable"
        return False, "invalid_target_leaf"
    path = _category_path(target, index=flat)
    if path.startswith(ANTIQUES_PREFIX):
        return False, "implausible_target_path"
    species = pet_species_mismatch_reason(title, path)
    if species:
        return False, species
    if _looks_like_grill_cart_title(title) and _is_barbecue_category(target, index=flat):
        return True, ""
    if kind in _FURNITURE_KINDS:
        effective_kind = kind
    else:
        effective_kind = _confident_title_kind(title) or kind
    tools_path = path.startswith("Home & Garden > Tools & Workshop Equipment")
    if tools_path:
        if effective_kind in _FURNITURE_KINDS or _looks_like_kitchen_island_title(title):
            if not _looks_like_tool_title(title):
                return False, "implausible_target_path"
        elif target == KITCHEN_ISLAND and "Power Tool" in path:
            if _looks_like_kitchen_island_title(title) or effective_kind in {
                "kitchen_island",
                "side_table",
                "display_cabinet",
                "outdoor_table",
            }:
                return False, "implausible_target_path"
    if effective_kind in _FURNITURE_KINDS and target not in TREE_KNOWN_PUBLISHABLE_LEAVES:
        if not any(path.startswith(prefix) for prefix in FURNITURE_PATH_PREFIXES):
            return False, "implausible_target_path"
    if target == KITCHEN_ISLAND and "Power Tool" in path:
        if _looks_like_kitchen_island_title(title) or effective_kind in {
            "kitchen_island",
            "side_table",
            "display_cabinet",
            "outdoor_table",
        }:
            return False, "implausible_target_path"
    allowed = _KIND_ALLOWED_STORED.get(effective_kind)
    if allowed is not None and target not in allowed:
        return False, "implausible_target_path"
    meta = flat.get(target)
    cat_name = meta[0] if isinstance(meta, (list, tuple)) and meta else ""
    if title_vs_category_implausible(title, target, path=path, category_name=cat_name):
        return False, "implausible_target_path"
    return True, ""


def load_approved_auto_remaps() -> frozenset[tuple[str, str]]:
    approved = set(APPROVED_AUTO_REMAPS)
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "data" / "config" / "approved_auto_remaps.json"
    if not path.is_file():
        return frozenset(approved)
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(approved)
    pairs = payload.get("pairs") if isinstance(payload, dict) else payload
    if isinstance(pairs, list):
        for item in pairs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                approved.add((str(item[0] or ""), str(item[1] or "")))
    return frozenset(approved)


def is_approved_auto_remap(stored: str, target: str, reason: str = "") -> bool:
    approved = load_approved_auto_remaps()
    stored_id = as_safe_category_id(stored)
    target_id = as_safe_category_id(target)
    if not target_id:
        return False
    return (stored_id, target_id) in approved or (reason, target_id) in approved


def _decision(
    category_id: str | None,
    kind: str,
    reason: str,
    name: str | None = None,
    *,
    conflict: bool = False,
    suggested_alternative: str | None = None,
) -> CategoryDecision:
    return CategoryDecision(
        category_id,
        kind,
        reason,
        name,
        conflict,
        suggested_alternative,
    )


def _propose_from_title(title: str, stored: str, stored_name: str) -> CategoryDecision:
    """Propose a leaf from one title. Description is never consulted."""
    hay = as_safe_text(title)
    if not hay:
        return CategoryDecision(None, "unknown", "empty_title")

    if title_is_chair_accessory(hay):
        return CategoryDecision(None, "unknown", "chair_accessory")

    if _sewing_or_craft_table(hay):
        if as_safe_category_id(stored) == DESK:
            return CategoryDecision(None, "keep", "sewing_table_lock")
        return CategoryDecision(None, "unknown", "sewing_or_craft_table")

    if _title_has_cat_token(hay) and not _title_has_dog_token(hay) and re.search(
        r"\b(?:litter|cat\s+tree|cat\s+furniture)\b", hay, re.IGNORECASE
    ):
        from src.utils.category_tree_cache import is_cached_leaf_category

        idx = _category_index()
        cat_path = _category_path(CAT_FURNITURE, index=idx)
        if is_cached_leaf_category(CAT_FURNITURE, index=idx) and cat_path.startswith(PET_CAT_PREFIX):
            return CategoryDecision(CAT_FURNITURE, "cat_furniture", "cat_litter_cabinet", "Furniture & Scratchers")
        return CategoryDecision(None, "unknown", "pet_species_mismatch")

    head = first_product_noun(hay)
    deny_noun = unknown_head_noun(head)
    if deny_noun:
        stored_id = as_safe_category_id(stored)
        if deny_noun == "mattress":
            if stored_id in _HEAD_FITS_STORED["mattress"]:
                return CategoryDecision(None, "keep", "stored_head_fit")
            return CategoryDecision(None, "unknown", "mattress_not_accessory")
        if stored_id and stored_id in _HEAD_FITS_STORED.get(deny_noun, ()):
            return CategoryDecision(None, "keep", "stored_head_fit")
        if deny_noun == "vanity":
            # Bathroom vanity cabinets → Bathroom Cabinets; makeup tables → 32878.
            if re.search(r"\bmakeup\b|\bvanity\s+table\b", hay, re.IGNORECASE):
                if stored_id == "32878":
                    return CategoryDecision(None, "keep", "vanity_lock")
                return CategoryDecision(
                    "32878", "vanity", "makeup_vanity_head", "Vanities & Makeup Tables"
                )
            if re.search(r"\bbathroom\b|\bsink\b|\bceramic\b", hay, re.IGNORECASE):
                if stored_id in _HEAD_FITS_STORED["vanity"]:
                    return CategoryDecision(None, "keep", "vanity_lock")
                return CategoryDecision(
                    "176991", "vanity", "bathroom_vanity_head", "Bathroom Cabinets"
                )
            return CategoryDecision(None, "unknown", "unknown_head_noun")
        if deny_noun in {"cushion", "pad", "cover", "slipcover"} or title_is_chair_accessory(hay):
            return CategoryDecision(None, "unknown", "accessory")
        return CategoryDecision(None, "unknown", "unknown_head_noun")
    if head in {
        "chaise lounge cushion",
        "lounger cushion",
        "chair cushion",
        "chair cover",
        "seat cushion",
        "slipcover",
    }:
        return CategoryDecision(None, "unknown", "accessory")
    if head == "hutch desk":
        return CategoryDecision(DESK, "desk", "hutch_desk", "Desks & Tables")
    if head == "bathroom vanity":
        stored_id = as_safe_category_id(stored)
        if stored_id in _HEAD_FITS_STORED["vanity"]:
            return CategoryDecision(None, "keep", "vanity_lock")
        return CategoryDecision(
            "176991", "vanity", "bathroom_vanity_head", "Bathroom Cabinets"
        )
    if head in _TV_STAND_HEADS:
        if as_safe_category_id(stored) == "20488":
            return CategoryDecision(None, "keep", "tv_stand_lock")
        return CategoryDecision(
            "20488", "tv_stand", "tv_stand_head", "TV Stands & Entertainment Units"
        )
    if head in _BAR_CABINET_HEADS:
        stored_id = as_safe_category_id(stored)
        if stored_id == "20487":
            return CategoryDecision(None, "keep", "bar_cabinet_lock")
        if stored_id == SIDEBOARD:
            return CategoryDecision(None, "keep", "bar_cabinet_sideboard_lock")
        # Furniture bar / wine / liquor cabinets are sideboards, not Wine Racks.
        return CategoryDecision(
            SIDEBOARD, "sideboard", "bar_cabinet_sideboard", "Sideboards & Buffets"
        )
    if head in _WINE_RACK_HEADS:
        return CategoryDecision(WINE_RACK, "wine_rack", "wine_rack_head", "Wine Racks & Bottle Holders")
    if _title_is_hall_tree_head(hay):
        return CategoryDecision(HALL_TREE, "hall_tree", "hall_tree_head", "Coat & Hat Racks")
    if head in {"sideboard", "buffet"} and as_safe_category_id(stored) not in {"183322", "63557"}:
        return CategoryDecision(SIDEBOARD, "sideboard", "sideboard_head", "Sideboards & Buffets")
    if _is_lounger_head(head, hay):
        stored_id = as_safe_category_id(stored)
        if stored_id == OUTDOOR_CHAISE:
            return CategoryDecision(None, "keep", "outdoor_chaise_lock")
        if stored_id == "138996":
            return CategoryDecision(None, "keep", "outdoor_chaise_lock")
        if title_has_clear_outdoor_word(hay) and not title_has_mixed_indoor_outdoor(hay):
            return CategoryDecision(OUTDOOR_CHAISE, "outdoor_chaise", "chaise_lounge", "Loungers")
        return CategoryDecision(None, "unknown", "chaise_not_dining_set")
    if _SPORTS_KEEP_RE.search(hay) and as_safe_category_id(stored):
        return CategoryDecision(None, "keep", "stored_head_fit")
    if _looks_like_tool_title(hay) and as_safe_category_id(stored):
        tool_path = _category_path(stored)
        if tool_path.startswith("Home & Garden > Tools & Workshop Equipment"):
            return CategoryDecision(None, "keep", "stored_head_fit")
    if _is_patio_chair_head(head, hay) or re.search(
        r"\b(?:papasan|adirondack|egg)\s+chairs?\b", hay, re.IGNORECASE
    ):
        stored_id = as_safe_category_id(stored)
        if title_has_mixed_indoor_outdoor(hay):
            return CategoryDecision(None, "unknown", "mixed_indoor_outdoor")
        if title_has_clear_outdoor_word(hay):
            if stored_id == OUTDOOR_CHAIR:
                return CategoryDecision(None, "keep", "outdoor_chair_lock")
            return CategoryDecision(OUTDOOR_CHAIR, "outdoor_chair", "patio_chair_head", "Patio Chairs")
    if re.search(r"\bcoat\s+racks?\b|\bcoat\s+stand\b", hay, re.IGNORECASE):
        return CategoryDecision(HALL_TREE, "hall_tree", "coat_rack_head", "Coat & Hat Racks")

    if _display_cabinet_title(hay):
        return CategoryDecision(DISPLAY_CABINET, "display_cabinet", "display_cabinet", "Display Cabinets")

    if head in {"dining table"} and title_is_dining_set_bundle(hay) and not title_has_clear_outdoor_word(hay):
        return CategoryDecision(DINING_SET, "dining_set", "dining_table_set", "Dining Sets")
    if head == "bar table" and title_is_dining_set_bundle(hay):
        return CategoryDecision(DINING_SET, "dining_set", "bar_table_set", "Dining Sets")

    family = compound_product_family(hay)
    if family == "dining_set":
        if title_has_clear_outdoor_word(hay):
            return CategoryDecision(PATIO_SET, "dining_set", "outdoor_dining_set", "Patio & Garden Furniture Sets")
        if has_any_phrase(hay, ("dining set", "dining table set", "piece dining")):
            return CategoryDecision(DINING_SET, "dining_set", "indoor_dining_set", "Dining Sets")
        return CategoryDecision(None, "unknown", "indoor_dining_bundle")
    if family == "patio_furniture_set":
        return CategoryDecision(PATIO_SET, "patio_furniture_set", "patio_set", "Patio & Garden Furniture Sets")
    if family == "sofa":
        return CategoryDecision(INDOOR_SEATING, "sofa", "compound_sofa", "Sofas, Armchairs & Couches")
    if family == "armchair":
        return CategoryDecision(INDOOR_SEATING, "armchair", "compound_chair_ottoman", "Sofas, Armchairs & Couches")
    if family == "coffee_table":
        return CategoryDecision(INDOOR_TABLE, "coffee_table", "compound_coffee_table", "Coffee Tables")
    if family == "kitchen_island":
        if as_safe_category_id(stored) == KITCHEN_ISLAND:
            return CategoryDecision(None, "keep", "compound_kitchen_island")
        return CategoryDecision(None, "unknown", "compound_kitchen_island")
    if family == "bed":
        return CategoryDecision("175758", "bed", "compound_bed", "Beds & Bed Frames")
    if family == "desk":
        return CategoryDecision(DESK, "desk", "compound_desk", "Desks & Tables")
    if family == "kids_table_set":
        return CategoryDecision("66743", "kids_table_set", "kids_table_set", "Play Table & Chair Sets")
    if family == "unknown":
        return CategoryDecision(None, "unknown", "compound_unknown")

    try:
        profile = _classify(hay, stored)
    except Exception as exc:
        logger.warning("classify_listing_profile failed closed: %s", exc)
        return CategoryDecision(None, "unknown", "classify_error")

    proposed = as_safe_category_id(getattr(profile, "category_id", None))
    kind = str(getattr(profile, "kind", "") or "unknown")
    name = getattr(profile, "category_name", None)

    if kind == "unknown" or not proposed:
        try:
            canon_id, canon_name = _offline_matcher().canonicalize_category(
                hay, stored or "", stored_name or None, ""
            )
        except Exception as exc:
            logger.warning("canonicalize_category failed closed: %s", exc)
            canon_id, canon_name = None, None
        canon_id = as_safe_category_id(canon_id)
        if canon_id and canon_id != stored:
            proposed = canon_id
            name = canon_name or name
            kind = kind if kind != "unknown" else "canonical"
        else:
            proposed = None
            kind = "unknown"

    if _side_or_end_table_head(hay, stored):
        if title_has_clear_outdoor_word(hay) and not title_has_mixed_indoor_outdoor(hay):
            return CategoryDecision(OUTDOOR_TABLE, "outdoor_table", "outdoor_side_table", "Patio & Garden Tables")
        if proposed == NIGHTSTAND:
            proposed = END_TABLE
            kind = "side_table"
            name = "End Tables"
        elif not proposed or proposed in {INDOOR_TABLE, NIGHTSTAND}:
            proposed = END_TABLE
            kind = "side_table"
            name = "End Tables"

    if proposed == WINE_RACK and first_product_noun(hay) in _BAR_CABINET_HEADS:
        return CategoryDecision(None, "unknown", "bar_cabinet_not_wine")
    if proposed == WINE_RACK and _display_cabinet_title(hay):
        return CategoryDecision(DISPLAY_CABINET, "display_cabinet", "display_not_wine", "Display Cabinets")
    if proposed == WINE_RACK and first_product_noun(hay) not in _WINE_RACK_HEADS:
        return CategoryDecision(None, "unknown", "not_wine_rack_head")

    if proposed == INDOOR_CHAIR and title_is_chair_accessory(hay):
        return CategoryDecision(None, "unknown", "accessory_not_dining_chair")

    if proposed == END_TABLE and not _side_or_end_table_head(hay, stored):
        return CategoryDecision(None, "unknown", "secondary_side_table")

    return CategoryDecision(proposed or None, kind, "classified", name)


def _finalize_decision(
    decision: CategoryDecision,
    title: str,
    stored: str,
    *,
    index=None,
) -> CategoryDecision:
    stored_ok, stored_reason = (True, "")
    if stored:
        stored_ok, stored_reason = validate_target_category(
            stored, decision.kind, title, index=index
        )
        if stored_reason == "tree_unavailable":
            warn_once_tree_unavailable()
            return CategoryDecision(None, "keep", "tree_cache_unavailable")

    if stored and stored_ok and stored_leaf_locked(title, stored):
        return CategoryDecision(None, "keep", "stored_leaf_lock")

    if decision.category_id and is_outdoor_to_indoor_downgrade(stored, decision.category_id, title):
        return CategoryDecision(None, "keep", "no_outdoor_indoor_downgrade")

    if decision.category_id:
        ok, reason = validate_target_category(
            decision.category_id, decision.kind, title, index=index
        )
        if not ok:
            alt = suggested_alternative_for(decision.category_id, decision.kind, title)
            if stored and not stored_ok and decision.category_id == stored:
                return CategoryDecision(
                    None,
                    "unknown",
                    "stored_category_implausible",
                    suggested_alternative=alt,
                )
            return CategoryDecision(None, "unknown", reason, suggested_alternative=alt)

    if stored and not stored_ok and (decision.kind == "keep" or not decision.category_id):
        if stored_reason == "tree_unavailable":
            warn_once_tree_unavailable()
            return CategoryDecision(None, "keep", "tree_cache_unavailable")
        return CategoryDecision(
            None,
            "unknown",
            "stored_category_implausible",
            suggested_alternative=suggested_alternative_for(stored, decision.kind, title),
        )

    if decision.kind == "keep":
        return CategoryDecision(None, "keep", decision.reason, decision.category_name)

    if stored and stored_ok and (decision.kind == "unknown" or not decision.category_id):
        if _title_agrees_with_stored(title, stored, index=index):
            return CategoryDecision(None, "keep", "stored_head_fit")

    if decision.kind == "unknown" or not decision.category_id:
        return CategoryDecision(
            None,
            "unknown",
            decision.reason,
            conflict=decision.conflict,
            suggested_alternative=decision.suggested_alternative,
        )

    if decision.category_id == stored:
        return CategoryDecision(None, "keep", decision.reason, decision.category_name)

    return decision


def resolve_category_decision(
    live_title: Any = "",
    giga_title: Any = "",
    description: Any = "",
    stored_category: Any = "",
    stored_name: Any = "",
    *,
    index=None,
) -> CategoryDecision:
    """Pure category decision. Description is accepted but never used for the leaf."""
    try:
        live = as_safe_text(live_title)
        giga = as_safe_text(giga_title)
        stored = as_safe_category_id(stored_category)
        name = as_safe_text(stored_name)
        title = live or giga
        if not title:
            return CategoryDecision(None, "unknown", "empty_title")

        stored_ok = True
        if stored:
            stored_ok, stored_reason = validate_target_category(stored, "", title, index=index)
            if stored_reason == "tree_unavailable":
                warn_once_tree_unavailable()
                return CategoryDecision(None, "keep", "tree_cache_unavailable")
            if stored_ok and stored_leaf_locked(title, stored):
                return CategoryDecision(None, "keep", "stored_leaf_lock")
            if stored_ok and _title_agrees_with_stored(title, stored, index=index):
                return CategoryDecision(None, "keep", "stored_head_fit")

        if live and giga:
            live_dec = _propose_from_title(live, stored, name)
            giga_dec = _propose_from_title(giga, stored, name)
            live_fam = _family_of(live_dec.kind, live_dec.category_id)
            giga_fam = _family_of(giga_dec.kind, giga_dec.category_id)
            hold_pairs = {
                frozenset({"outdoor_chair", "dining_set"}),
                frozenset({"outdoor_chair", "patio_set"}),
                frozenset({"outdoor_chair", "table"}),
                frozenset({"dining_chair", "dining_set"}),
                frozenset({"dining_chair", "patio_set"}),
                frozenset({"dining_chair", "table"}),
                frozenset({"outdoor_chair", "seating"}),
                frozenset({"outdoor_chair", "dining_chair"}),
            }
            if (
                live_fam not in {"unknown", "keep"}
                and giga_fam not in {"unknown", "keep"}
                and live_fam != giga_fam
                and frozenset({live_fam, giga_fam}) in hold_pairs
            ):
                return CategoryDecision(None, "unknown", "title_source_conflict", conflict=True)

        decision = _propose_from_title(title, stored, name)
        return _finalize_decision(decision, title, stored, index=index)
    except Exception as exc:
        logger.warning("resolve_category_decision failed closed: %s", exc)
        return CategoryDecision(None, "unknown", "error")


def decision_category_id(
    decision: CategoryDecision,
    stored_category: Any = "",
    *,
    index=None,
) -> str | None:
    """The leaf to write, or None when the stored leaf should stay / stay empty.

    Validation and the empty APPROVED_AUTO_REMAPS human gate live here so
    audit and publish cannot disagree.
    """
    stored = as_safe_category_id(stored_category)
    if decision.kind in {"keep", "unknown"} or not decision.category_id:
        return None
    if decision.category_id == stored:
        return None
    ok, _reason = validate_target_category(
        decision.category_id, decision.kind, "", index=index
    )
    if not ok:
        return None
    if not is_approved_auto_remap(stored, decision.category_id, decision.reason):
        return None
    return decision.category_id


def publish_tree_id_for_target(
    category_id: Any = "",
    *,
    is_motors: bool | None = None,
) -> str:
    """Tree the existing routing will publish this id to.

    Aqua Trading/SiteID-100 → tree 100; Aqua ``TREE0_FORCE_INVENTORY_CATEGORIES``
    / Inventory path → tree 0; Main/GP → tree 0.
    """
    from src.services.vehicle_compatibility import TREE0_FORCE_INVENTORY_CATEGORIES

    cid = as_safe_category_id(category_id)
    if cid in TREE0_FORCE_INVENTORY_CATEGORIES:
        return "0"
    if is_motors is None:
        try:
            from src.utils.store_profile import get_store_profile

            is_motors = bool(get_store_profile().is_motors)
        except Exception:
            is_motors = False
    return "100" if is_motors else "0"


def validate_new_target_publish_tree(
    cid: Any,
    *,
    is_motors: bool | None = None,
    indexes=None,
) -> tuple[bool, str]:
    """Reject a new target that is only a leaf in the other tree.

    Report-only: never substitutes another id. Stored-id validation stays
    on the 4250f71 flat index (R7–R12 keep their keep/hold).
    """
    target = as_safe_category_id(cid)
    if not target:
        return False, "invalid_target_leaf"
    tree_id = publish_tree_id_for_target(target, is_motors=is_motors)
    from src.utils.category_tree_cache import is_cached_leaf_in_tree

    if is_cached_leaf_in_tree(target, tree_id, indexes=indexes):
        return True, ""
    other = "0" if tree_id == "100" else "100"
    if is_cached_leaf_in_tree(target, other, indexes=indexes):
        return False, "wrong_tree_leaf"
    return True, ""


def needs_manual_motors_leaf_review(
    stored: Any,
    *,
    is_motors: bool | None = None,
    indexes=None,
) -> bool:
    """B3 default: flag Aqua READY furniture leaves; do not change outcome."""
    if is_motors is None:
        try:
            from src.utils.store_profile import get_store_profile

            is_motors = bool(get_store_profile().is_motors)
        except Exception:
            return False
    if not is_motors:
        return False
    cid = as_safe_category_id(stored)
    if not cid:
        return False
    from src.services.vehicle_compatibility import TREE0_FORCE_INVENTORY_CATEGORIES

    if cid in TREE0_FORCE_INVENTORY_CATEGORIES:
        return False
    if cid in MOTORS_FURNITURE_REVIEW_LEAVES:
        return True
    from src.utils.category_tree_cache import is_cached_leaf_in_tree

    return is_cached_leaf_in_tree(cid, "0", indexes=indexes) and not is_cached_leaf_in_tree(
        cid, "100", indexes=indexes
    )


def category_gate_report_flags(
    stored: Any,
    *,
    optimization: Any = None,
    is_motors: bool | None = None,
    indexes=None,
) -> list[str]:
    flags: list[str] = []
    from src.utils.approved_category import pin_mismatch_reason

    pin = pin_mismatch_reason(optimization, stored)
    if pin:
        flags.append(pin)
    if needs_manual_motors_leaf_review(stored, is_motors=is_motors, indexes=indexes):
        flags.append(NEEDS_MANUAL_MOTORS_LEAF_REVIEW)
    return flags


class CategoryWriteAction(tuple):
    """``(category_id, action)`` plus a report-only ``.suggestion``."""

    def __new__(cls, category_id, action, suggestion=None):
        inst = super().__new__(cls, (category_id, action))
        inst.suggestion = suggestion
        return inst


def _sellable_in_store(sellable_check, category_id: str | None) -> bool:
    if sellable_check is None or not category_id:
        return True
    try:
        return bool(sellable_check(category_id))
    except Exception:
        return False


def explicit_override_target_allowed(
    target_id: Any,
    title: str = "",
    *,
    stored: Any = None,
    index=None,
    is_motors: bool | None = None,
    indexes=None,
) -> tuple[bool, str]:
    """``--override-category-lock`` + explicit id: validate leaf/tree/fee.

    Bypasses only ``APPROVED_AUTO_REMAPS``. Restoring the stored id is
    allowed (name-only SYNC_DB / live restore). High-fee still wins on a
    *new* target.
    """
    target = as_safe_category_id(target_id)
    if not target:
        return False, "invalid_target_leaf"
    stored_id = as_safe_category_id(stored)
    if target == stored_id:
        return True, ""
    ok, reason = validate_target_category(target, "classified", title, index=index)
    if not ok:
        return False, reason
    tree_ok, tree_reason = validate_new_target_publish_tree(
        target, is_motors=is_motors, indexes=indexes
    )
    if not tree_ok:
        return False, tree_reason
    from src.utils.publish_fee_preview import guard_category_write_high_fee

    blocked = guard_category_write_high_fee(category_id=target, index=index)
    if blocked:
        return False, blocked
    return True, ""


def apply_category_write_gate(
    original_stored: Any,
    candidate_id: Any,
    title: str = "",
    *,
    listing_status: str = "",
    reason: str = "publish_candidate",
    kind: str = "classified",
    index=None,
    sellable_check=None,
) -> tuple[str | None, str]:
    """Gate every publish/draft category choice.

    Returns (category_id_or_None_to_hold, action) where action is
    ``remap``, ``keep``, ``hold_empty``, ``hold_implausible``, or
    ``hold_tree_unavailable``.
    Unapproved remaps keep the original stored id. Empty stored holds.
    Invalid/implausible stored holds READY/PENDING/READY_TO_PUBLISH and
    stays manifest-only for PUBLISHED. Missing tree on a publish path holds.
    """
    stored = as_safe_category_id(original_stored)
    candidate = as_safe_category_id(candidate_id)
    unpublished = unpublished_listing_status(listing_status)
    if not category_tree_is_available(index):
        # TODO(decision B1): approved leaf keeps publishing without a tree?
        # Default: no — unpublished still hold_tree_unavailable (PR #32 r3 (f)).
        warn_once_tree_unavailable()
        if unpublished:
            return None, "hold_tree_unavailable"
        if stored:
            return stored, "keep"
        return candidate or None, "keep"

    action = "keep"
    chosen = stored
    if candidate and candidate != stored:
        ok, _reason = validate_target_category(candidate, kind, title, index=index)
        if ok:
            tree_ok, _tree_reason = validate_new_target_publish_tree(candidate)
            if not tree_ok:
                ok = False
        if ok and is_approved_auto_remap(stored, candidate, reason):
            chosen = candidate
            action = "remap"
        elif not stored:
            return None, "hold_empty"
        else:
            chosen = stored
            action = "keep"
    elif not stored:
        return None, "hold_empty"

    if unpublished and stored and is_legacy_remap_stored(stored):
        return None, "hold_implausible"

    report_suggestion = None
    if candidate and candidate != stored and _sellable_in_store(sellable_check, candidate):
        candidate_ok, _candidate_reason = validate_target_category(
            candidate, kind, title, index=index
        )
        if candidate_ok:
            report_suggestion = candidate

    if chosen == stored and stored:
        stored_ok, stored_reason = validate_target_category(stored, "", title, index=index)
        if stored_reason == "tree_unavailable":
            # TODO(decision B1): no approved-leaf exception when the tree is down.
            warn_once_tree_unavailable()
            if unpublished:
                return None, "hold_tree_unavailable"
            return stored, "keep"
        if not stored_ok and unpublished:
            return CategoryWriteAction(None, "hold_implausible", report_suggestion)
        if unpublished and not _sellable_in_store(sellable_check, stored):
            return CategoryWriteAction(None, "hold_not_sellable", report_suggestion)
    return chosen or None, action


def gate_publish_category_choice(
    original_stored: Any,
    candidate_id: Any,
    title: str = "",
    listing_status: str = "READY",
    *,
    reason: str = "publish_candidate",
    kind: str = "classified",
    index=None,
    optimization: Any = None,
    is_motors: bool | None = None,
    indexes=None,
    sellable_check=None,
) -> CategoryGateResult:
    """Shared choke point for every manual/API publish category write.

    Returns ``(category_id, action, error)`` with report-only ``.flags``.
    ``error`` is set on hold; callers must not publish or write a category
    when it is non-empty. Unapproved remaps keep the stored id (report-only).
    """
    result = apply_category_write_gate(
        original_stored,
        candidate_id,
        title,
        listing_status=listing_status,
        reason=reason,
        kind=kind,
        index=index,
        sellable_check=sellable_check,
    )
    gated, action = result
    write_suggestion = getattr(result, "suggestion", None)
    flags = category_gate_report_flags(
        original_stored,
        optimization=optimization,
        is_motors=is_motors,
        indexes=indexes,
    )
    if action == "hold_empty":
        return CategoryGateResult(
            None, action, HOLD_EMPTY_PUBLISH_ERROR, flags, reasons=["hold_empty"]
        )
    if action == "hold_implausible":
        return CategoryGateResult(
            None,
            action,
            hold_implausible_publish_error(original_stored, title),
            flags,
            suggestion=write_suggestion,
            reasons=["implausible_category"],
        )
    if action == "hold_not_sellable":
        return CategoryGateResult(
            None,
            action,
            HOLD_NOT_SELLABLE_PUBLISH_ERROR,
            flags,
            suggestion=write_suggestion,
            reasons=["not_sellable_in_store_tree"],
        )
    if action == "hold_tree_unavailable":
        return CategoryGateResult(
            None, action, tree_unavailable_publish_error(), flags, reasons=["hold_tree_unavailable"]
        )
    return CategoryGateResult(gated, action, "", flags)
