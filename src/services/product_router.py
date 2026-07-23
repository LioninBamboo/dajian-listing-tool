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

_FURNITURE_TERMS = (
    "sofa", "couch", "armchair", "dining table", "coffee table", "bed frame",
    "nightstand", "dresser", "wardrobe", "bookshelf", "cabinet", "ottoman",
    "sectional", "recliner", "desk", "stool", "bench", "shelving unit",
)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _has_term(haystack: str, terms) -> Optional[str]:
    for t in terms:
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in t.split()) + r"(?!\w)"
        if re.search(pattern, haystack):
            return t
    return None


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
        if frag in supplier:
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

    hit = _has_term(hay, _FURNITURE_TERMS)
    if hit:
        return {"target": FURNITURE, "reason": "furniture term", "matched": hit}

    return {"target": UNKNOWN, "reason": "no decisive signal", "matched": ""}


def route_for_product(**kwargs) -> str:
    """Convenience wrapper returning just the routing target."""
    return classify_product(**kwargs)["target"]
