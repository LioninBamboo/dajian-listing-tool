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
HOLD_TREE_UNAVAILABLE_ERROR = (
    "category tree unavailable; refresh failed; no publish"
)

_SUGGESTED_ALTERNATIVES = {
    "38200": "38204",
    "20493": "20487",
    "177000": "260157",
    "261263": "22656",
}


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


def suggested_alternative_for(cid: str | None, kind: str = "", title: str = "") -> str | None:
    stored = as_safe_category_id(cid)
    remapped = _legacy_remap_target(stored)
    if remapped:
        return remapped
    if stored in _SUGGESTED_ALTERNATIVES:
        return _SUGGESTED_ALTERNATIVES[stored]
    if kind in {"side_table"} or first_product_noun(title) in _SIDE_END_HEADS:
        return "38204"
    if kind in {"display_cabinet"}:
        return DISPLAY_CABINET_LEAF
    if kind in {"kitchen_island"} or "kitchen island" in as_safe_text(title).lower():
        return KITCHEN_ISLAND_LEAF
    if kind in {"outdoor_table"} or _looks_like_grill_table_title(title):
        return OUTDOOR_TABLE
    if kind in {"hall_tree"}:
        return HALL_TREE
    return None


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
    if head in _TV_STAND_HEADS:
        if as_safe_category_id(stored) == "20488":
            return CategoryDecision(None, "keep", "tv_stand_lock")
        return CategoryDecision(None, "unknown", "unknown_head_noun")
    if head in _BAR_CABINET_HEADS:
        if as_safe_category_id(stored) == "20487":
            return CategoryDecision(None, "keep", "bar_cabinet_lock")
        return CategoryDecision(None, "unknown", "bar_cabinet_not_wine_rack")
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


def apply_category_write_gate(
    original_stored: Any,
    candidate_id: Any,
    title: str = "",
    *,
    listing_status: str = "",
    reason: str = "publish_candidate",
    kind: str = "classified",
    index=None,
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

    if chosen == stored and stored:
        stored_ok, stored_reason = validate_target_category(stored, "", title, index=index)
        if stored_reason == "tree_unavailable":
            warn_once_tree_unavailable()
            if unpublished:
                return None, "hold_tree_unavailable"
            return stored, "keep"
        if not stored_ok and unpublished:
            return None, "hold_implausible"
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
) -> tuple[str | None, str, str]:
    """Shared choke point for every manual/API publish category write.

    Returns ``(category_id, action, error)``. ``error`` is set on hold;
    callers must not publish or write a category when it is non-empty.
    Unapproved remaps keep the stored id (report-only).
    """
    gated, action = apply_category_write_gate(
        original_stored,
        candidate_id,
        title,
        listing_status=listing_status,
        reason=reason,
        kind=kind,
        index=index,
    )
    if action == "hold_empty":
        return None, action, HOLD_EMPTY_PUBLISH_ERROR
    if action == "hold_implausible":
        return None, action, hold_implausible_publish_error(original_stored, title)
    if action == "hold_tree_unavailable":
        return None, action, tree_unavailable_publish_error()
    return gated, action, ""
