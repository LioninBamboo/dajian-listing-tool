"""Classify a collected product and decide which store instance should list it.

One codebase serves several stores (furniture main, auto parts, blind box). A
product collected from any channel should land in the instance that actually
sells that category — auto parts go to the auto-parts sub-store, the main store
stays furniture. This module is the decision layer: pure, testable, no I/O.

Signals, in order of trust:
  1. supplier category text (DaJian/GIGA `category` field) — the supplier already
     classified it, so a clear hit here beats title guessing;
  2. title/description keywords, with a guard against furniture words that merely
     look automotive ("Engineered Wood" contains "engine", a pet stroller has
     "wheels") — those false positives are why keyword-only routing misfires.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

# Routing targets. Values match the instance keys used by callers/config.
AUTO = "auto"
FURNITURE = "furniture"
ARTTOY = "arttoy"
UNKNOWN = "unknown"

# Sergey 09-25: Main is a home store and may list everything except auto parts.
# The flag only controls whether a garage-tool classification becomes a
# wrong_store hold. Default off: tools never block a draft. Tool phrases still
# beat "cabinet" so a tool chest is not classified as furniture. Tests cover
# both values. Unknown never blocks.
TOOLS_ROUTE_TO_AUTO = False

# Supplier category fragments that are decisively automotive.
_SUPPLIER_AUTO = (
    "auto", "car ", "truck", "vehicle", "motorcycle", "towing", "cargo carrier",
    "roof rack", "parts", "tire", "wheel accessor",
)

# Title/description terms that are automotive with little ambiguity. Multi-word
# entries are matched with flexible whitespace.
_AUTO_TERMS = (
    "trailer hitch", "tow hitch", "hitch receiver", "hitch mount",
    "running board", "nerf bar", "side step bar",
    "roof rack", "cross bar", "crossbar", "cargo carrier", "cargo basket",
    "roof basket", "rooftop carrier", "bull bar", "brush guard",
    "fender flare", "mud flap", "splash guard", "tonneau cover",
    "tailgate assist", "tailgate lift", "liftgate",
    "brake pad", "brake rotor", "brake caliper", "spark plug", "oxygen sensor",
    "fuel pump", "radiator", "alternator", "starter motor", "catalytic converter",
    "shock absorber", "strut assembly", "control arm", "ball joint", "tie rod",
    "wheel hub", "wheel bearing", "cv axle", "drive shaft", "u joint",
    "headlight assembly", "tail light assembly", "fog light",
    "side mirror", "windshield wiper", "engine mount", "oil filter",
    "air intake", "exhaust manifold", "muffler", "catback",
    "car cover", "seat cover", "floor mat", "cargo liner", "trunk organizer",
    "jump starter", "obd2", "obd-ii", "tire inflator", "car jack", "scissor jack",
    "air tank", "air compressor", "compressor tank",
    "winch", "leveling kit", "lift kit", "skid plate",
)

# Words that look automotive but usually are not, in this catalog. Each of these
# was an actual misroute when the classifier was run over the live product table:
# a treadmill has a "running board", a golf caddy has a "trunk organizer".
_AUTO_FALSE_FRIENDS = (
    "engineered wood", "engineered board", "pet stroller", "dog stroller",
    "shopping cart", "garden cart", "wheelbarrow", "office chair wheel",
    "race car bed", "car bed", "toy car", "bike trailer", "bicycle trailer",
    "treadmill", "exercise bike", "rowing machine",
    "golf bag", "golf club", "golf clubs", "golf cart",
    # An outdoor kitchen island has a "splash guard" (backsplash), not a mud flap.
    "kitchen island", "outdoor kitchen", "countertop", "backsplash", "sink cabinet",
)

_ARTTOY_TERMS = (
    "blind box", "blindbox", "art toy", "designer toy", "vinyl figure",
    "mystery box", "trading figure", "figurine set",
)

# Checked before the furniture "cabinet" term. "tool drawer" and "tool gift"
# are intentionally absent so they do not steal a kitchen cabinet or a gift.
_TOOL_TERMS = (
    "tool chest", "tool box", "tool cart", "tool storage", "tool cabinet",
    "mechanic creeper", "creeper", "floor jack", "bolt cutter", "garage gift",
    "mechanic",
)

_FURNITURE_TERMS = (
    "sofa", "couch", "armchair", "dining table", "coffee table", "bed frame",
    "nightstand", "dresser", "wardrobe", "bookshelf", "cabinet", "ottoman",
    "sectional", "recliner", "desk", "stool", "bench", "shelving unit",
)

# Publish-only. These words flag household goods on Main (a clothes-rack
# crossbar, a radiator cover, a hall-tree floor mat). Roof racks still match
# "roof rack" / "cargo carrier". "radiator" is narrowed below, not dropped.
_PUBLISH_DROPPED_AUTO_TERMS = frozenset({
    "cross bar", "crossbar", "seat cover", "floor mat", "air compressor", "winch",
})
_PUBLISH_AUTO_TERMS = tuple(
    term for term in _AUTO_TERMS
    if term not in _PUBLISH_DROPPED_AUTO_TERMS and term != "radiator"
)

# On the auto store these are garage/shop words, not a furniture mismatch.
_AQUA_GARAGE_FURNITURE_TERMS = (
    "storage cabinet", "work bench", "bench", "stool", "cabinet",
    "grinder", "vise", "mechanic",
)

# Motors leaves that must not publish on a non-auto store. The floor-jack
# adapter stays wrong_store through this set, not through the bare "cross bar".
_MOTORS_PUBLISH_LEAF_IDS = frozenset({
    "262216", "262210", "174020", "174021", "262093",
})


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _has_term(haystack: str, terms) -> Optional[str]:
    for t in terms:
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in t.split()) + r"(?!\w)"
        if re.search(pattern, haystack):
            return t
    return None


def _supplier_has(supplier: str, frag: str) -> bool:
    """Supplier text match. Single token ``auto`` is a whole word.

    ``automatic``, ``autoclave``, and ``automobile`` are not the auto store.
    Multi-word fragments stay substring matches (``cargo carrier`` in
    ``cargo carriers``).
    """
    if frag == "auto":
        return re.search(r"(?<![a-z0-9])auto(?![a-z0-9])", supplier) is not None
    return frag in supplier


def classify_product(
    *,
    title: str = "",
    description: str = "",
    attributes: Optional[Mapping[str, Any]] = None,
    supplier_category: str = "",
) -> Dict[str, Any]:
    """Classify a product for routing.

    Returns ``{"target": AUTO|FURNITURE|ARTTOY|UNKNOWN, "reason": str,
    "matched": str}``. The reason is meant to be logged so a misroute can be
    traced to the exact signal that caused it.
    """
    attrs = " ".join(
        f"{k} {v if not isinstance(v, (list, tuple)) else ' '.join(map(str, v))}"
        for k, v in (attributes or {}).items()
    )
    hay = _norm(f"{title} {description[:2000]} {attrs}")
    supplier = _norm(supplier_category)

    # Blind box / art toy is unambiguous enough to check first.
    hit = _has_term(hay, _ARTTOY_TERMS)
    if hit:
        return {"target": ARTTOY, "reason": "art-toy term", "matched": hit}

    # 1) Supplier category is the most trustworthy signal.
    for frag in _SUPPLIER_AUTO:
        if _supplier_has(supplier, frag):
            return {"target": AUTO, "reason": "supplier category", "matched": frag}

    # 2) Title/description terms, unless a known false friend explains the hit.
    false_friend = _has_term(hay, _AUTO_FALSE_FRIENDS)
    hit = _has_term(hay, _AUTO_TERMS)
    if hit and not false_friend:
        return {"target": AUTO, "reason": "auto term", "matched": hit}
    if hit and false_friend:
        return {
            "target": FURNITURE if _has_term(hay, _FURNITURE_TERMS) else UNKNOWN,
            "reason": f"auto term '{hit}' overridden by '{false_friend}'",
            "matched": false_friend,
        }

    # Tool phrases always beat furniture "cabinet". The hold flag does not
    # change this label; publish_wrong_store_reason applies it.
    hit = _has_term(hay, _TOOL_TERMS)
    if hit and not false_friend:
        return {"target": AUTO, "reason": "garage tool term", "matched": hit}

    hit = _has_term(hay, _FURNITURE_TERMS)
    if hit:
        return {"target": FURNITURE, "reason": "furniture term", "matched": hit}

    return {"target": UNKNOWN, "reason": "no decisive signal", "matched": ""}


def route_for_product(**kwargs) -> str:
    """Convenience wrapper returning just the routing target."""
    return classify_product(**kwargs)["target"]


def collection_route_note(
    *,
    title: str = "",
    description: str = "",
    attributes: Optional[Mapping[str, Any]] = None,
    supplier_category: str = "",
    store_kind: str = "",
    tools_route_to_auto: Optional[bool] = None,
) -> str:
    """Same note the extension collect path logs. Does not move the row."""
    routing = classify_product(
        title=title,
        description=description,
        attributes=attributes,
        supplier_category=supplier_category,
    )
    this_store = str(store_kind or "").strip().lower()
    if routing["target"] not in (this_store, UNKNOWN):
        return (
            f"ROUTING: looks like '{routing['target']}' "
            f"({routing['reason']}: {routing['matched']}) but this instance is "
            f"'{this_store}' — belongs in the {routing['target']} store."
        )
    return f"Routing: {routing['target']} ({routing['reason']})"


def pilot_collect_logs(
    title: str = "",
    description: str = "",
    attributes: Optional[Mapping[str, Any]] = None,
    supplier_category: str = "",
    store_kind: str = "furniture",
    tools_route_to_auto: Optional[bool] = None,
    existing_logs: Optional[list] = None,
) -> list:
    """Collection logs for pilot_collect_garden. Never a store move."""
    logs = list(existing_logs or ["pilot_collect_garden"])
    if "pilot_collect_garden" not in logs:
        logs.append("pilot_collect_garden")
    note = collection_route_note(
        title=title,
        description=description,
        attributes=attributes,
        supplier_category=supplier_category,
        store_kind=store_kind,
    )
    if note.startswith("ROUTING:") and note not in logs:
        logs.append(note)
    return logs


def _phrase_pattern(phrase: str) -> str:
    words = [re.escape(word) for word in str(phrase).split() if word]
    return r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)"


def _without_phrases(haystack: str, phrases) -> str:
    cleaned = haystack
    for phrase in sorted(phrases, key=len, reverse=True):
        cleaned = re.sub(_phrase_pattern(phrase), " ", cleaned)
    return cleaned


# A woodworking "router lift kit" is not a suspension kit. Keep the auto
# term only with a vehicle word, or drop it next to these household words.
_LIFT_KIT_NON_AUTO = (
    "router", "router table", "woodworking", "table saw", "bed", "desk", "tv", "cabinet",
)
_LIFT_KIT_AUTO_CONTEXT = (
    "truck", "jeep", "suv", "car", "vehicle", "suspension", "leveling", "body lift",
    "ford", "chevy", "silverado", "f150", "wrangler", "ram", "tacoma", "gmc",
    "toyota", "dodge",
)


def _lift_kit_is_automotive(haystack: str) -> bool:
    if _has_term(haystack, _LIFT_KIT_NON_AUTO):
        return False
    return _has_term(haystack, _LIFT_KIT_AUTO_CONTEXT) is not None


def _publish_auto_hit(haystack: str) -> Optional[str]:
    hit = _has_term(haystack, _PUBLISH_AUTO_TERMS)
    if hit == "lift kit" and not _lift_kit_is_automotive(haystack):
        hit = None
    if hit:
        return hit
    # "radiator cover" is household. A standalone radiator still is not.
    scrubbed = _without_phrases(haystack, ("radiator cover",))
    if _has_term(scrubbed, ("radiator",)):
        return "radiator"
    return None


def _publish_furniture_hit(haystack: str, store_kind: str) -> Optional[str]:
    if store_kind == AUTO:
        haystack = _without_phrases(haystack, _AQUA_GARAGE_FURNITURE_TERMS)
    return _has_term(haystack, _FURNITURE_TERMS)


def _classify_for_publish(
    *,
    title: str = "",
    supplier_category: str = "",
    store_kind: str = "",
) -> Dict[str, Any]:
    """Publish routing. Title and supplier category only, whole words.

    Description and attribute blobs are not part of this haystack.
    """
    hay = _norm(title)
    supplier = _norm(supplier_category)
    this = str(store_kind or "").strip().lower()

    hit = _has_term(hay, _ARTTOY_TERMS)
    if hit:
        return {"target": ARTTOY, "reason": "art-toy term", "matched": hit}

    for frag in _SUPPLIER_AUTO:
        token = frag.strip()
        if token and re.search(_phrase_pattern(token), supplier):
            return {"target": AUTO, "reason": "supplier category", "matched": token}

    false_friend = _has_term(hay, _AUTO_FALSE_FRIENDS)
    hit = _publish_auto_hit(hay)
    if hit and not false_friend:
        return {"target": AUTO, "reason": "auto term", "matched": hit}
    if hit and false_friend:
        furniture_hit = _publish_furniture_hit(hay, this)
        return {
            "target": FURNITURE if furniture_hit else UNKNOWN,
            "reason": f"auto term '{hit}' overridden by '{false_friend}'",
            "matched": false_friend,
        }

    hit = _has_term(hay, _TOOL_TERMS)
    if hit and not false_friend:
        return {"target": AUTO, "reason": "garage tool term", "matched": hit}

    hit = _publish_furniture_hit(hay, this)
    if hit:
        return {"target": FURNITURE, "reason": "furniture term", "matched": hit}

    return {"target": UNKNOWN, "reason": "no decisive signal", "matched": ""}


def _has_motors_publish_leaf(category_ids) -> bool:
    if not category_ids:
        return False
    if isinstance(category_ids, str):
        category_ids = [category_ids]
    return any(str(cid or "").strip() in _MOTORS_PUBLISH_LEAF_IDS for cid in category_ids)


def publish_wrong_store_reason(
    *,
    title: str = "",
    description: str = "",
    attributes: Optional[Mapping[str, Any]] = None,
    supplier_category: str = "",
    store_kind: str = "",
    tools_route_to_auto: Optional[bool] = None,
    category_ids=None,
) -> Optional[str]:
    """Publish preflight. ``None`` means do not block.

    Uses the title and the supplier category only. ``description`` and
    ``attributes`` are accepted and ignored so a seat cover or a floor mat
    mentioned only there cannot hold the row. Main accepts every non-Motors
    product. Garage tools block only when ``TOOLS_ROUTE_TO_AUTO`` is on and
    this store is not furniture/auto. A known Motors leaf blocks a non-auto
    store even when the title has no auto term.
    """
    del description, attributes
    routing = _classify_for_publish(
        title=title,
        supplier_category=supplier_category,
        store_kind=store_kind,
    )
    target = routing["target"]
    this = str(store_kind or "").strip().lower()
    reason = None
    if target not in (UNKNOWN, this, ""):
        route_tools = TOOLS_ROUTE_TO_AUTO if tools_route_to_auto is None else bool(tools_route_to_auto)
        if routing["reason"] == "garage tool term":
            if route_tools and this not in {"furniture", "main", "garden", "auto", ""}:
                reason = f"wrong_store:{target}"
        elif target == AUTO and this != AUTO:
            reason = f"wrong_store:{target}"
        elif this not in {"furniture", "main", ""}:
            reason = f"wrong_store:{target}"
    if reason:
        return reason
    if this != AUTO and _has_motors_publish_leaf(category_ids):
        return "wrong_store:auto"
    return None
