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
from dataclasses import dataclass
from typing import Any

from src.utils.product_context_signals import (
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
)

logger = logging.getLogger(__name__)

OUTDOOR_CHAIR = "79684"
INDOOR_CHAIR = "54235"
INDOOR_SEATING = "38208"
OTTOMAN = "20490"
BENCH = "262980"
INDOOR_TABLE = "38204"
END_TABLE = "38200"
NIGHTSTAND = "38199"
OUTDOOR_TABLE = "112590"
PATIO_SET = "139849"
DINING_SET = "107578"
DISPLAY_CABINET = "20493"
WINE_RACK = "20689"
DESK = "88057"
KITCHEN_ISLAND = "177000"

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
_SIDE_TABLE_LOCK_STORED = frozenset({"121851", "79682", "138996", "75671"})
HALL_TREE = "261263"
SIDEBOARD = "183322"
OUTDOOR_CHAISE = "79682"


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
    }:
        return False
    if head in _SIDE_END_HEADS:
        return True
    return False


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

    head = first_product_noun(hay)
    if head in _BAR_CABINET_HEADS:
        if as_safe_category_id(stored) == "20487":
            return CategoryDecision(None, "keep", "bar_cabinet_lock")
        return CategoryDecision(None, "unknown", "bar_cabinet_not_wine_rack")
    if head in _WINE_RACK_HEADS:
        return CategoryDecision(WINE_RACK, "wine_rack", "wine_rack_head", "Wine Racks & Bottle Holders")
    if head == "hall tree":
        return CategoryDecision(HALL_TREE, "hall_tree", "hall_tree_head", "Hall Trees & Stands")
    if head in {"sideboard", "buffet"} and as_safe_category_id(stored) not in {"183322", "63557"}:
        return CategoryDecision(SIDEBOARD, "sideboard", "sideboard_head", "Sideboards & Buffets")
    if head in {"chaise", "chaise lounge", "lounger", "sun lounger"}:
        if as_safe_category_id(stored) in {"79682", "79684", "138996"}:
            return CategoryDecision(None, "keep", "outdoor_chaise_lock")
        if title_has_clear_outdoor_word(hay) and not title_has_mixed_indoor_outdoor(hay):
            return CategoryDecision(OUTDOOR_CHAISE, "outdoor_chaise", "chaise_lounge", "Outdoor Chaise Lounges")
        return CategoryDecision(None, "unknown", "chaise_not_dining_set")

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


def resolve_category_decision(
    live_title: Any = "",
    giga_title: Any = "",
    description: Any = "",
    stored_category: Any = "",
    stored_name: Any = "",
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

        if stored_leaf_locked(title, stored):
            return CategoryDecision(None, "keep", "stored_leaf_lock")

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

        if stored_leaf_locked(title, stored):
            return CategoryDecision(None, "keep", "stored_leaf_lock")

        if decision.category_id and is_outdoor_to_indoor_downgrade(stored, decision.category_id, title):
            return CategoryDecision(None, "keep", "no_outdoor_indoor_downgrade")

        if decision.kind == "unknown" or not decision.category_id:
            return CategoryDecision(None, "unknown", decision.reason, conflict=decision.conflict)

        if decision.category_id == stored:
            return CategoryDecision(None, "keep", decision.reason, decision.category_name)

        return decision
    except Exception as exc:
        logger.warning("resolve_category_decision failed closed: %s", exc)
        return CategoryDecision(None, "unknown", "error")


def decision_category_id(decision: CategoryDecision, stored_category: Any = "") -> str | None:
    """The leaf to write, or None when the stored leaf should stay / stay empty."""
    stored = as_safe_category_id(stored_category)
    if decision.kind in {"keep", "unknown"} or not decision.category_id:
        return None
    if decision.category_id == stored:
        return None
    return decision.category_id
