"""Garden / outdoor-living lifestyle listing prompt + deterministic finalizer.

The ``garden_lifestyle`` template style (GrovePop instance — outdoor · garden ·
pet home goods) produces a fresh, natural lifestyle description instead of the
navy/gold furniture template. Same proven architecture as ``auto_technical``:
the brand banner, hero stat band, SPECIFICATIONS table and footer are ALL
rendered deterministically from the item specifics; the LLM writes ONLY the
middle prose (intro + KEY FEATURES + PERFECT FOR). So design and legibility are
consistent regardless of the model, nothing double-wraps the chrome, and the
copy stays source-grounded.

Output shape matches the furniture optimizer so downstream publish is unchanged:
``{title, description(HTML str), aspects(dict[list]), features, categoryId?}``.
"""

from __future__ import annotations

import html
from typing import Any, Dict, Mapping

# Reuse the generic, palette-neutral deterministic helpers from the auto template.
from src.services.auto_technical_prompt import (
    _combine_dimensions,
    _normalize_aspects,
)

# Botanical palette: deep garden green + leaf accent + warm cream.
_GREEN = "#2e5d43"
_GREEN_DK = "#204030"
_LEAF = "#6fae7f"
_SAND = "#c8a86a"
_INK = "#26302a"
_MUTED = "#5c6b60"

# Hero stat cards: the numbers a home/garden buyer scans. Short values only.
_HERO_KEYS = (
    "Material", "Dimensions (L × W × H)", "Item Weight", "Color", "Capacity",
    "Maximum Weight Capacity", "Set Includes", "Shape", "Style",
)


def _banner_block(profile: Any) -> str:
    brand = html.escape(str(getattr(profile, "brand_name", "") or "").upper())
    tagline = html.escape(str(getattr(profile, "brand_tagline", "") or ""))
    return (
        f'<div style="text-align:center;padding:30px 15px;'
        f'background:linear-gradient(135deg,{_GREEN} 0%,{_GREEN_DK} 100%);font-family:Georgia,serif;">'
        f'<h1 style="margin:0;font-size:28px;font-weight:400;letter-spacing:5px;color:#fff;">{brand}</h1>'
        f'<p style="margin:8px 0 0;font-size:12px;color:{_LEAF};letter-spacing:3px;text-transform:uppercase;">{tagline}</p></div>'
    )


def _footer_block(profile: Any) -> str:
    footer = str(getattr(profile, "footer_html", "") or "").strip()
    if footer:
        return footer
    l1 = html.escape(str(getattr(profile, "description_footer_line1", "") or ""))
    l2 = html.escape(str(getattr(profile, "description_footer_line2", "") or ""))
    return (
        f'<div style="text-align:center;padding:22px;background:{_GREEN};margin-top:20px;font-family:Arial,sans-serif;">'
        f'<p style="margin:0;font-size:13px;color:{_SAND};letter-spacing:1px;">{l1}</p>'
        f'<p style="margin:6px 0 0;font-size:11px;color:#dce6df;">{l2}</p></div>'
    )


def _hero_band(aspects: Mapping[str, Any]) -> str:
    """Deterministic stat-card band (Material / Dimensions / Weight …)."""
    norm: Dict[str, str] = {}
    for k, v in (aspects or {}).items():
        val = ", ".join(str(x).strip() for x in v if str(x).strip()) if isinstance(v, (list, tuple)) else str(v).strip()
        if val:
            norm[str(k).strip()] = val
    _combine_dimensions(norm)  # fold Item/Assembled L/W/H -> one "Dimensions (L × W × H)"
    cards = []
    seen = set()
    for key in _HERO_KEYS:
        val = norm.get(key)
        if not val or key in seen or len(val) > 26:
            continue
        seen.add(key)
        cards.append(
            f'<div style="flex:1 1 130px;min-width:130px;background:#fff;border:1px solid #e3e8e2;'
            f'border-top:3px solid {_LEAF};border-radius:8px;padding:14px;text-align:center;">'
            f'<div style="font-size:19px;font-weight:700;color:{_INK};line-height:1.2;">{html.escape(val)}</div>'
            f'<div style="margin-top:6px;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{_MUTED};">{html.escape(key)}</div>'
            "</div>"
        )
        if len(cards) == 3:
            break
    if len(cards) < 2:
        return ""
    return f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:18px;">{"".join(cards)}</div>'


def build_garden_lifestyle_system_prompt(profile: Any) -> str:
    brand = getattr(profile, "brand_name", "") or "the store"
    return f"""You are an expert eBay lifestyle copywriter for {brand}, a US-warehouse OUTDOOR · GARDEN · PET home-goods store.

**CONTENT & DESIGN RULES (CRITICAL):**
The system deterministically renders the brand banner, the hero stat-card band and the footer. YOUR
'description' contains ONLY the middle prose, in this exact order and NOTHING else — inline CSS only, no
classes, and do NOT render a banner, stat band, any <table>/<th>/<td>, or a footer (the system adds those):
  1. INTRO — one warm sentence: <p style="font-size:15px;line-height:1.7;color:{_INK};margin:4px 0 20px;">…</p>
  2. KEY FEATURES — a section, generously spaced (this is the design centrepiece). The heading text MUST
     read "Key Features" and the items MUST be <li> elements (both are required):
     <h3 style="margin:0 0 14px;font-size:14px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:{_GREEN};">Key Features</h3>
     <ul style="list-style:none;padding:0;margin:0;">  then 4–6 items, each with breathing room + a leaf accent:
     <li style="display:flex;gap:10px;margin-bottom:12px;align-items:flex-start;">
       <span style="color:{_LEAF};font-size:16px;line-height:1.5;">🌿</span>
       <span style="font-size:15px;line-height:1.6;color:{_INK};"><strong style="color:{_GREEN};">Lead-in:</strong> detail.</span></li>
     </ul>
  3. PERFECT FOR — a soft rounded highlight card (NOT plain text):
     <div style="margin-top:6px;background:#eef4ef;border-radius:10px;padding:16px 18px;">
       <h4 style="margin:0 0 8px;font-size:13px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:{_GREEN};">Perfect For</h4>
       <p style="margin:0;font-size:14px;line-height:1.7;color:{_MUTED};">2–3 real use scenes (patio, balcony, garden bed, sunroom, entryway, for a pet) the product truly suits.</p></div>
  Aesthetic: bright, natural, aspirational home-&-garden feel — plenty of whitespace, never cramped or corporate.

**ANALYSIS RULES (fact safety — HARD, an automated guard rejects violations):**
- Describe ONLY materials/specs/dimensions explicitly present in the source. Do NOT invent counts, capacities,
  certifications, or weather/UV/frost claims the source never states.
- Use the source's LITERAL words. Do NOT rephrase or upgrade an attribute, and do NOT add adjectives the
  source never uses (e.g. "premium", "industrial", "modern", "weatherproof", "frost-proof", "UV-resistant",
  "pre-assembled") unless that exact word is in the source — they are treated as fabricated claims.
- Every feature and adjective must trace to a source aspect value or the source description text, word for word.
- Fill Item Specifics ('aspects') accurately; values MUST be lists of strings. The 'Brand' aspect is set by the system.
  Fill AS MANY relevant aspects as the source supports (Type, Material, Color, Shape, Features, Indoor/Outdoor,
  Style, Room, Set Includes …) — eBay ranks and filters on these, so more accurate specifics = more visibility.

**TITLE (eBay Cassini search — THIS is where keywords rank, not the description):**
- Use 75–80 of the 80 characters — never waste the space. Title Case, no brand name, no fluff (New/Best/Sale).
- FRONT-LOAD the highest-search terms buyers actually type. Weave in as many of the provided trending keywords
  as read naturally; include the product noun + key descriptors (size, color, material, shape, use). Prefer
  words buyers search (e.g. "Planter Box", "Flower Pot", "Large", "Outdoor", "Yard") over codes nobody searches.

**OUTPUT FORMAT:** a SINGLE valid JSON object, no markdown fences:
{{
  "title": "75-80 char keyword-front-loaded SEO title, no brand name",
  "description": "<div>intro + KEY FEATURES + PERFECT FOR, inline-CSS only</div>",
  "aspects": {{ "Type": ["Planter"], "Material": ["Magnesium Oxide (MGO)"], "Color": ["Rust"], "Shape": ["Square"] }},
  "features": ["selling point 1", "selling point 2"],
  "categoryId": "eBay numeric category id or null"
}}"""


def build_garden_lifestyle_user_prompt(
    *,
    title: str,
    description: str,
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    market_intel: Mapping[str, Any] | None = None,
) -> str:
    import json
    attrs = dict(attributes or {})
    sp = dict(specs or {})
    parts = [
        "Create an optimized eBay outdoor/garden/home listing from this source data.",
        f"- Source title: {title}",
        f"- Source item specifics: {json.dumps(attrs, ensure_ascii=False) if attrs else 'N/A'}",
        f"- Source specs: {json.dumps(sp, ensure_ascii=False) if sp else 'N/A'}",
        f"- Source description: {(description or '')[:1500] or 'N/A'}",
    ]
    if market_intel:
        kw = market_intel.get("top_keywords") or []
        stats = market_intel.get("price_stats") or {}
        if kw:
            parts.append(f"- Trending keywords (weave into title): {', '.join(kw[:15])}")
        if stats:
            parts.append(f"- Market price band: ${stats.get('min',0)}-${stats.get('max',0)}, avg ${stats.get('avg',0)}")
    return "\n".join(parts)


def finalize_garden_lifestyle_listing(data: Mapping[str, Any], profile: Any) -> Dict[str, Any]:
    """Deterministic post-processing: brand banner + hero band + LLM prose + spec table + footer."""
    result: Dict[str, Any] = dict(data or {})
    title = str(result.get("title") or "").strip()[:80]
    description = str(result.get("description") or "").strip()
    aspects = _normalize_aspects(result.get("aspects"))

    if getattr(profile, "force_house_brand", False):
        aspects["Brand"] = [getattr(profile, "brand_name", "") or "Unbranded"]
    elif not aspects.get("Brand"):
        aspects["Brand"] = [getattr(profile, "default_brand", "Unbranded")]

    # Deterministic chrome, idempotent on the brand name so nothing double-wraps.
    brand_l = str(getattr(profile, "brand_name", "") or "").lower()
    head = ""
    if description and brand_l not in html.unescape(description).lower():
        banner = _banner_block(profile)
        hero = _hero_band(aspects)
        head = f"{banner}\n{hero}\n" if hero else f"{banner}\n"

    footer = ""
    footer_marker = str(getattr(profile, "quality_footer_marker", "") or "").lower()
    fblock = _footer_block(profile)
    if description and fblock and not (footer_marker and footer_marker in html.unescape(description).lower()):
        footer = fblock

    # eBay's Inventory API caps the whole description at 4000 chars (unlike
    # Trading). The banner + hero stat cards + inline-CSS prose + footer already
    # fill most of that, so there is NO deterministic spec table here — eBay's
    # native item-specifics panel shows the full spec grid, and the hero cards
    # surface the key numbers. Hard-cap the body as a last resort.
    body_html = description
    parts = [p for p in (head + body_html, footer) if p]
    description = "\n".join(parts)
    if len(description) > 3990 and footer:
        # trim the prose at a safe tag boundary, keep banner+hero+footer intact
        keep = 3990 - len(head) - len(footer) - 2
        cut = body_html[:max(0, keep)]
        cut = cut[:cut.rfind("</")] if "</" in cut else cut
        description = "\n".join(p for p in (head + cut, footer) if p)

    result["title"] = title
    result["description"] = description
    result["aspects"] = aspects
    if not isinstance(result.get("features"), list):
        result["features"] = []
    return result
