"""Auto-parts / tools listing prompt + deterministic finalizer.

The ``auto_technical`` template style (AquaRides instance — auto parts + tools)
produces a technical, fitment-aware description instead of the furniture
template. It has two MODES, auto-detected from the source text:

  - ``fitment`` — a vehicle part (hitch, roof rack, running board, brake kit…).
    Copy leans on direct-replacement / OE-style spec, placement on vehicle, and
    a "verify fitment" nudge. The exact Year-Make-Model list is NOT written into
    the description — it lives in eBay's structured ItemCompatibilityList, added
    at publish time by the Trading channel — so the copy never fabricates a
    fitment table.
  - ``tool`` — a garage / workshop tool (floor jack, impact wrench, socket set…).
    Copy leans on specs (drive size, torque range, power source), use cases,
    durability and what's in the box. No vehicle fitment section.

Mirrors ``arttoy_prompt``: LLM prompt (system + user) + a deterministic
``finalize_auto_technical_listing`` that injects the instance footer, forces the
house brand for generic goods, and normalizes aspects. Output shape matches the
furniture optimizer so downstream publish is unchanged:
``{title, description(HTML str), aspects(dict[list]), features, categoryId?}``.
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, Mapping, Optional

AUTO_MODE_FITMENT = "fitment"
AUTO_MODE_TOOL = "tool"

# Garage / workshop TOOL signals. A hit here (with no vehicle-fitment data)
# routes to tool mode. Kept tight to avoid stealing genuine parts — e.g. a
# "running board" is a part, not a board; "jack stand" / "floor jack" are tools.
_TOOL_SIGNALS = (
    "floor jack", "hydraulic jack", "bottle jack", "scissor jack", "jack stand",
    "impact wrench", "impact driver", "torque wrench", "socket set", "socket wrench",
    "ratchet", "breaker bar", "wrench set", "spanner", "pliers", "screwdriver set",
    "drill", "grinder", "polisher", "pressure washer", "air compressor",
    "creeper", "car ramp", "service ramp", "engine hoist", "engine stand",
    "oil filter wrench", "obd", "code reader", "scan tool", "multimeter",
    "tire inflator", "lug wrench", "torque multiplier", "tool kit", "tool set",
)

# Vehicle-PART signals — presence routes to fitment mode even without structured
# compatibility data (a universal-fit part still uses fitment copy, just with no
# vehicle section). Order matters only for readability, not logic.
_PART_SIGNALS = (
    "hitch", "tow bar", "roof rack", "cargo box", "cargo carrier", "running board",
    "nerf bar", "side step", "bull bar", "bumper", "fender", "grille", "spoiler",
    "mud flap", "mud guard", "splash guard", "floor mat", "cargo liner", "seat cover",
    "mirror", "headlight", "tail light", "fog light", "brake", "rotor", "brake pad",
    "caliper", "suspension", "strut", "shock", "coilover", "control arm", "tie rod",
    "ball joint", "wheel bearing", "exhaust", "muffler", "catalytic", "radiator",
    "intercooler", "turbo", "intake", "alternator", "starter", "fuel pump",
    "lift support", "tailgate", "window regulator", "door handle", "wiper",
    "cv axle", "differential", "transmission mount", "engine mount",
)


def detect_auto_mode(
    title: Any,
    description: Any = "",
    aspects: Optional[Mapping[str, Any]] = None,
    compatibility: Any = None,
) -> str:
    """Classify a source item as a vehicle part (fitment) or a tool.

    Precedence:
      1. Structured vehicle compatibility present -> fitment (it's a part).
      2. A tool signal AND no part signal -> tool.
      3. Otherwise -> fitment (the auto store's default; universal-fit parts and
         ambiguous items get part copy with no vehicle section, never tool copy).
    """
    if compatibility:
        # A non-empty CompatibilityAnalysis-like object or list => it's a part.
        entries = getattr(compatibility, "compatible_products", compatibility)
        mode = getattr(compatibility, "mode", None)
        if entries and mode != "not_applicable":
            return AUTO_MODE_FITMENT
        if isinstance(compatibility, (list, tuple)) and entries:
            return AUTO_MODE_FITMENT

    hay = f"{title or ''} {description or ''}".lower()
    for key, vals in (aspects or {}).items():
        hay += " " + str(key).lower() + " "
        if isinstance(vals, (list, tuple)):
            hay += " ".join(str(v).lower() for v in vals)
        else:
            hay += str(vals).lower()

    has_tool = any(sig in hay for sig in _TOOL_SIGNALS)
    has_part = any(sig in hay for sig in _PART_SIGNALS)
    if has_tool and not has_part:
        return AUTO_MODE_TOOL
    return AUTO_MODE_FITMENT


def build_auto_technical_system_prompt(profile: Any, mode: str) -> str:
    brand = getattr(profile, "brand_name", "") or "the store"
    tagline = getattr(profile, "brand_tagline", "") or ""
    box_label = (
        "WHAT'S IN THE BOX"
        if mode == AUTO_MODE_TOOL
        else "FITMENT / COMPATIBILITY note + PACKAGE INCLUDES"
    )
    if mode == AUTO_MODE_TOOL:
        role = "automotive TOOLS & garage-equipment"
        focus = (
            "- Lead with the concrete SPECS the buyer filters on: drive size, "
            "torque/pressure range, capacity (tons/PSI), power source "
            "(cordless/pneumatic/manual/electric), material, number of pieces.\n"
            "- Describe real use cases (what jobs it does), build quality and "
            "durability, and exactly WHAT'S IN THE BOX.\n"
            "- Do NOT add any vehicle Year/Make/Model fitment — tools are universal."
        )
        aspects_hint = (
            '{ "Type": ["Impact Wrench"], "Power Source": ["Corded Electric"], '
            '"Drive Size": ["1/2 in"], "Material": ["Chrome Vanadium Steel"], '
            '"Number of Pieces": ["1"], "Features": ["Variable Speed"] }'
        )
    else:
        role = "automotive PARTS & accessories"
        focus = (
            "- Lead with placement on the vehicle and the direct-replacement / "
            "upgrade value (fit, finish, function).\n"
            "- Include a short FITMENT / COMPATIBILITY note telling the buyer to "
            "verify their Year-Make-Model against the compatibility chart. Do NOT "
            "invent a Year/Make/Model list, part numbers, or OE/interchange numbers "
            "— the exact fitment is shown by eBay's compatibility widget, not here.\n"
            "- Cover material/finish, install difficulty, and any hardware included."
        )
        aspects_hint = (
            '{ "Type": ["Receiver Hitch"], "Placement on Vehicle": ["Rear"], '
            '"Fitment Type": ["Direct Replacement"], "Material": ["Steel"], '
            '"Finish": ["Black Powder Coat"], "Features": ["Class III"] }'
        )
    return f"""You are an expert eBay copywriter for a US-warehouse {role} store ({brand}).

**CONTENT & DESIGN RULES (CRITICAL):**
1. HTML Description ('description'): produce a self-contained, visually clean HTML block.
   - COMPATIBILITY: use INLINE CSS (style="...") for ALL styling. NEVER use CSS classes — eBay strips them.
   - STYLE: technical / industrial automotive look — dark steel + a single accent (red or safety-orange), clean spec tables, no fluff. Emoji sparingly (🔧 ⚙️ ✅) if at all.
   - LAYOUT: a header banner div with the product name; a KEY FEATURES list; a SPECIFICATIONS table; a {box_label} block.
   - Do NOT include any Shipping / Returns / Policies section — the system appends that automatically.
   - Do NOT output <html>, <head>, or <body> tags. Return only the inner content <div>.
2. FOCUS FOR THIS ITEM:
{focus}

**ANALYSIS RULES (fact safety — HARD, an automated guard rejects violations):**
- Describe ONLY specs/materials/dimensions explicitly present in the source data. Do NOT invent numbers, part numbers, OE/interchange numbers, torque figures, or capacities.
- Use the source's LITERAL words for every claim. Do NOT rephrase, upgrade, or embellish an attribute:
  · if the source says "Class 3", write "Class 3" — NOT "Class III";
  · if the source's Fitment Type is "Vehicle Specific Fit", say exactly that — do NOT substitute "Direct Replacement";
  · do NOT add adjectives the source never uses (e.g. "heavy-duty", "premium", "universal", "OE-grade", "direct replacement") — these are treated as fabricated claims.
- Every feature bullet and every adjective must be traceable to a source aspect value or the source description text, word for word.
- Fill Item Specifics ('aspects') with accurate values from the source; values MUST be lists of strings.
- The 'Brand' aspect is set deterministically by the system afterward — you may omit it.

**OUTPUT FORMAT:** return a SINGLE VALID JSON object, no markdown fences, no prose:
{{
  "title": "SEO title, max 80 chars, keyword-front-loaded, no brand name",
  "description": "<div style='...'>full inline-CSS HTML</div>",
  "aspects": {aspects_hint},
  "features": ["selling point 1", "selling point 2"],
  "categoryId": "eBay numeric category id or null"
}}

Tagline for the header (optional): {tagline}"""


def build_auto_technical_user_prompt(
    *,
    title: str,
    description: str,
    mode: str,
    attributes: Optional[Mapping[str, Any]] = None,
    specs: Optional[Mapping[str, Any]] = None,
    market_intel: Optional[Mapping[str, Any]] = None,
) -> str:
    attrs = dict(attributes or {})
    sp = dict(specs or {})
    kind = "tool / garage-equipment" if mode == AUTO_MODE_TOOL else "vehicle part / accessory"
    parts = [
        f"Create an optimized eBay {kind} listing from this source data.",
        f"- Source title: {title}",
        f"- Source item specifics: {json.dumps(attrs, ensure_ascii=False) if attrs else 'N/A'}",
        f"- Source specs: {json.dumps(sp, ensure_ascii=False) if sp else 'N/A'}",
        f"- Source description: {(description or '')[:1500] or 'N/A'}",
    ]
    if market_intel:
        kw = market_intel.get("top_keywords") or []
        common = market_intel.get("common_aspects") or {}
        stats = market_intel.get("price_stats") or {}
        if kw:
            parts.append(f"- Trending keywords (weave into title): {', '.join(kw[:15])}")
        if common:
            joined = "; ".join(f"{k}: {', '.join(v[:3])}" for k, v in list(common.items())[:15])
            parts.append(f"- Item Specifics used by top sellers (include when supported by source): {joined}")
        if stats:
            parts.append(f"- Market price band: ${stats.get('min',0)}-${stats.get('max',0)}, avg ${stats.get('avg',0)}")
    return "\n".join(parts)


def _footer_block(profile: Any) -> str:
    footer = str(getattr(profile, "footer_html", "") or "").strip()
    if footer:
        return footer
    l1 = str(getattr(profile, "description_footer_line1", "") or "")
    l2 = str(getattr(profile, "description_footer_line2", "") or "")
    return (
        '<div style="text-align:center;padding:20px;background:#1a1a1a;color:#ff5722;'
        'margin-top:20px;font-family:Arial,sans-serif;">'
        f'<p style="margin:0;font-size:13px;">{l1}</p>'
        f'<p style="margin:6px 0 0;font-size:11px;color:#ddd;">{l2}</p></div>'
    )


def _normalize_aspects(aspects: Any) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for key, value in (aspects or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            vals = [str(v).strip() for v in value if str(v).strip()]
        else:
            vals = [str(value).strip()] if str(value).strip() else []
        if vals:
            out[str(key)] = vals
    return out


def finalize_auto_technical_listing(
    data: Mapping[str, Any], profile: Any, mode: str = AUTO_MODE_FITMENT
) -> Dict[str, Any]:
    """Deterministic post-processing for an auto-parts / tool LLM result.

    - normalize aspect values to lists;
    - force the house brand for generic goods when the instance opts in
      (``force_house_brand``) — otherwise keep the source brand or "Unbranded";
    - append the instance footer once (idempotent via the footer marker);
    - clamp the title to eBay's 80-char limit.
    """
    result: Dict[str, Any] = dict(data or {})

    title = str(result.get("title") or "").strip()[:80]
    description = str(result.get("description") or "").strip()
    aspects = _normalize_aspects(result.get("aspects"))

    # Generic auto goods carry the STORE brand (AquaRides), so buyers search and
    # trust one house brand. force_house_brand=false (a future real-brand tool
    # sub-store) keeps whatever the source gave, or "Unbranded".
    if getattr(profile, "force_house_brand", False):
        aspects["Brand"] = [getattr(profile, "brand_name", "") or "Unbranded"]
    elif not aspects.get("Brand"):
        aspects["Brand"] = [getattr(profile, "default_brand", "Unbranded")]

    footer = _footer_block(profile)
    footer_marker = str(getattr(profile, "quality_footer_marker", "") or "").lower()
    desc_unescaped = html.unescape(description).lower()
    already = bool(footer_marker) and footer_marker in desc_unescaped
    if description and footer and not already:
        description = f"{description}\n{footer}"

    result["title"] = title
    result["description"] = description
    result["aspects"] = aspects
    result["mode"] = mode
    if not isinstance(result.get("features"), list):
        result["features"] = []
    return result
