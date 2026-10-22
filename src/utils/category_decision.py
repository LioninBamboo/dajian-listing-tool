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
    has_any_phrase,
    head_clause,
    is_outdoor_to_indoor_downgrade,
    stored_leaf_locked,
    strip_room_location_lists,
    title_has_clear_outdoor_word,
    title_has_mixed_indoor_outdoor,
    title_is_chair_accessory,
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


def _display_cabinet_title(title: str) -> bool:
    hay = as_safe_text(title)
    return has_any_phrase(hay, ("display cabinet", "curio cabinet", "glass display", "glass cabinet"))


def _sewing_or_craft_table(title: str) -> bool:
    return has_any_phrase(as_safe_text(title), ("sewing table", "craft table", "sewing machine table"))


def _side_or_end_table_head(title: str) -> bool:
    core = strip_room_location_lists(head_clause(title) or title)
    if has_any_phrase(core, ("nightstand", "bedside table")):
        return False
    return has_any_phrase(core, ("end table", "side table"))


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

    if _display_cabinet_title(hay):
        return CategoryDecision(DISPLAY_CABINET, "display_cabinet", "display_cabinet", "Display Cabinets")

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

    if _side_or_end_table_head(hay):
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

    if proposed == WINE_RACK and _display_cabinet_title(hay):
        return CategoryDecision(DISPLAY_CABINET, "display_cabinet", "display_not_wine", "Display Cabinets")

    if proposed == INDOOR_CHAIR and title_is_chair_accessory(hay):
        return CategoryDecision(None, "unknown", "accessory_not_dining_chair")

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
