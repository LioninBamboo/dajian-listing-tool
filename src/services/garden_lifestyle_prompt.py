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
    _spec_block,
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
        if len(cards) == 4:
            break
    if len(cards) < 2:
        return ""
    return f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:18px;">{"".join(cards)}</div>'


def build_garden_lifestyle_system_prompt(profile: Any) -> str:
    brand = getattr(profile, "brand_name", "") or "the store"
    return f"""You are an expert eBay lifestyle copywriter for {brand}, a US-warehouse OUTDOOR · GARDEN · PET home-goods store.

**CONTENT & DESIGN RULES (CRITICAL):**
The system deterministically renders the brand banner, a hero stat-card band, the SPECIFICATIONS table and
the footer from 'aspects'. YOUR 'description' must contain ONLY the middle prose, in this order, nothing else:
  - A one-sentence intro (14px {_INK}) — what it is and its headline lifestyle benefit.
  - KEY FEATURES: title 15px bold UPPERCASE {_GREEN} with a 1px #e3e8e2 bottom rule, then 4–6 bullets,
    each 15px {_INK} line-height 1.75, starting with a <strong> lead-in then the detail.
  - PERFECT FOR: a short block (title 14px bold {_GREEN}) painting 2–3 real use scenes (patio, balcony,
    garden bed, sunroom, entryway, for a pet, etc.) — only scenes the product actually suits.
  - Use INLINE CSS only (no CSS classes). Do NOT render a banner, stat band, SPECIFICATIONS table,
    any <table>/<th>/<td>, or a footer — the system adds all of those. Put every spec value into 'aspects'.
  - Palette: green accent {_GREEN}, leaf {_LEAF}, ink {_INK}. Warm, natural, aspirational — not corporate.

**ANALYSIS RULES (fact safety — HARD, an automated guard rejects violations):**
- Describe ONLY materials/specs/dimensions explicitly present in the source. Do NOT invent counts, capacities,
  certifications, or weather/UV/frost claims the source never states.
- Use the source's LITERAL words. Do NOT rephrase or upgrade an attribute, and do NOT add adjectives the
  source never uses (e.g. "premium", "industrial", "modern", "weatherproof", "frost-proof", "UV-resistant",
  "pre-assembled") unless that exact word is in the source — they are treated as fabricated claims.
- Every feature and adjective must trace to a source aspect value or the source description text, word for word.
- Fill Item Specifics ('aspects') accurately; values MUST be lists of strings. The 'Brand' aspect is set by the system.

**OUTPUT FORMAT:** a SINGLE valid JSON object, no markdown fences:
{{
  "title": "SEO title, max 80 chars, keyword-front-loaded, no brand name",
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

    # Deterministic chrome. Idempotent on the brand name so nothing double-wraps.
    brand_l = str(getattr(profile, "brand_name", "") or "").lower()
    if description and brand_l not in html.unescape(description).lower():
        banner = _banner_block(profile)
        hero = _hero_band(aspects)
        head = f"{banner}\n{hero}" if hero else banner
        description = f"{head}\n{description}"

    spec = _spec_block(aspects)
    if description and spec and "<th" not in description:
        description = f"{description}\n{spec}"

    footer = _footer_block(profile)
    footer_marker = str(getattr(profile, "quality_footer_marker", "") or "").lower()
    if description and footer and not (footer_marker and footer_marker in html.unescape(description).lower()):
        description = f"{description}\n{footer}"

    result["title"] = title
    result["description"] = description
    result["aspects"] = aspects
    if not isinstance(result.get("features"), list):
        result["features"] = []
    return result
