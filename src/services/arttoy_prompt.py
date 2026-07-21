"""Art-toy / blind-box listing prompt + deterministic finalizer.

The ``arttoy_hype`` template style (blind-box instance) produces a Hypebeast /
streetwear description instead of the furniture template. This module holds:

  - the LLM prompt text (system + user), style-specific and banned-term aware;
  - ``finalize_arttoy_listing``: a deterministic post-processor that injects the
    instance footer, guarantees the Chinese-translation fields, normalizes
    aspects, and HARD-FAILS on any banned term the model leaked.

The prompt is derived from the user's ebay-listing-pro app: same Hypebeast
aesthetic and inline-CSS-only rule, same POP MART / IP-word prohibition. The
deterministic finalizer is what makes the safety real — the model is *told* to
avoid banned terms, ``finalize_arttoy_listing`` *enforces* it.

Output shape matches the furniture optimizer so downstream publish is unchanged:
``{title, description(HTML str), aspects(dict[list]), categoryId?}`` plus the
additive ``titleCN`` / ``descriptionCN`` fields for operator review.
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, Mapping, Optional

# Fallback list if an arttoy instance forgot to configure banned_terms.
_DEFAULT_ARTTOY_BANNED = (
    "POP MART", "Original", "Genuine", "Authentic", "Licensed", "Official", "OEM",
)

_SAFE_ALTERNATIVES = "Designer Toy, Blind Box Figure, Collectible Art Toy, or the character name"


def _banned_terms(profile: Any) -> tuple:
    terms = tuple(getattr(profile, "banned_terms", ()) or ())
    return terms or _DEFAULT_ARTTOY_BANNED


def build_arttoy_system_prompt(profile: Any) -> str:
    banned = ", ".join(_banned_terms(profile))
    return f"""You are an expert cross-border e-commerce copywriter creating eBay listings for designer art toys and blind-box collectibles.

**CONTENT & DESIGN RULES (CRITICAL):**
1. HTML Description ('description'): act as a professional web designer. Produce a visually rich, self-contained HTML block.
   - COMPATIBILITY: use INLINE CSS (style="...") for ALL styling. Do NOT use CSS classes — eBay strips external/`class` styles.
   - STYLE: trendy streetwear / "Hypebeast" 潮玩 aesthetic. High-contrast bold colors (deep black with neon acid-green or cyberpunk-pink accents, or ultra-clean white with heavy black borders). Use emoji icons like 🔥 ⚡ 💥 💎.
   - LAYOUT: a header banner div containing the title; a readable content container; a flexbox row of edgy feature cards (border + hard box-shadow); a monospace specs table with solid borders; a distinctive package-contents block.
   - Do NOT include any Shipping / Returns / Policies section — the system appends that automatically.
   - Do NOT output <html>, <head>, or <body> tags. Return only the inner content <div> structure.
2. Detail level: description text must be detailed and comprehensive.
3. PROHIBITED TERMS (STRICT — IP/VeRO safety): NEVER use any of: {banned}. Do not translate 正品/正版 into any of them. Use generic terms instead: {_SAFE_ALTERNATIVES}.
4. TRANSLATION: also provide Chinese translations of the title ('titleCN') and the full description ('descriptionCN', using <p> tags).

**ANALYSIS RULES:**
- Only describe features/materials explicitly supported by the provided source data. Do NOT invent counts, editions, materials, or certifications.
- Fill relevant Item Specifics ('aspects') with accurate values from the source.
- BRAND aspect: art toys carry their own IP, not our store name. Set 'Brand' to the item's own series/designer/character from the source ONLY IF it is not a prohibited term; if the only available brand is prohibited or none is given, set 'Brand' to "Unbranded". NEVER put our store name in Brand.

**OUTPUT FORMAT:** return a SINGLE VALID JSON object, no markdown fences, no prose. Structure:
{{
  "title": "SEO title, max 80 chars, no prohibited terms",
  "titleCN": "中文标题",
  "description": "<div style='...'>full inline-CSS HTML</div>",
  "descriptionCN": "<p>中文完整描述</p>",
  "aspects": {{ "Type": ["Blind Box"], "Character": ["..."], "Material": ["Vinyl"], "Theme": ["..."] }},
  "features": ["selling point 1", "selling point 2"],
  "categoryId": "eBay numeric category id or null"
}}"""


def build_arttoy_user_prompt(
    *,
    title: str,
    description: str,
    attributes: Optional[Mapping[str, Any]] = None,
    specs: Optional[Mapping[str, Any]] = None,
    market_intel: Optional[Mapping[str, Any]] = None,
) -> str:
    attrs = dict(attributes or {})
    sp = dict(specs or {})
    parts = [
        "Create an optimized eBay art-toy listing from this source data.",
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


def _footer_block(profile: Any) -> str:
    footer = str(getattr(profile, "footer_html", "") or "").strip()
    if footer:
        return footer
    l1 = str(getattr(profile, "description_footer_line1", "") or "")
    l2 = str(getattr(profile, "description_footer_line2", "") or "")
    return (
        '<div style="text-align:center;padding:20px;background:#000;color:#ccff00;'
        'margin-top:20px;font-family:\'Arial Black\',sans-serif;">'
        f'<p style="margin:0;font-size:13px;">{l1}</p>'
        f'<p style="margin:6px 0 0;font-size:11px;color:#fff;">{l2}</p></div>'
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


def finalize_arttoy_listing(data: Mapping[str, Any], profile: Any) -> Dict[str, Any]:
    """Deterministic post-processing for an art-toy LLM result.

    - append the instance footer to the description (once);
    - guarantee titleCN / descriptionCN keys (default "");
    - normalize aspect values to lists;
    - HARD-FAIL via BannedTermError if any banned term slipped through.

    Raises BannedTermError so the caller can retry with feedback or abort —
    never returns a listing that still carries a banned term.
    """
    from src.utils.banned_terms_guard import (
        assert_clean,
        clean_banned_aspects,
        strip_banned_terms,
    )

    result: Dict[str, Any] = dict(data or {})
    terms = _banned_terms(profile)

    # Deterministically STRIP banned terms first — the LLM is unreliable when the
    # source is saturated with them, so we do not depend on its compliance.
    title = strip_banned_terms(str(result.get("title") or "").strip(), terms)
    title_cn = strip_banned_terms(str(result.get("titleCN") or "").strip(), terms)
    description = strip_banned_terms(str(result.get("description") or "").strip(), terms)
    description_cn = strip_banned_terms(str(result.get("descriptionCN") or "").strip(), terms)
    aspects = clean_banned_aspects(_normalize_aspects(result.get("aspects")), terms)
    # If a banned Brand (e.g. "pop mart") got stripped away, fall back to the
    # instance default (art toys -> "Unbranded") rather than leaving it blank.
    if "Brand" in _normalize_aspects(result.get("aspects")) and "Brand" not in aspects:
        aspects["Brand"] = [getattr(profile, "default_brand", "Unbranded")]

    footer = _footer_block(profile)
    footer_marker = str(getattr(profile, "quality_footer_marker", "") or "").lower()
    # Unescape so a marker like "shipping & logistics" matches footer HTML that
    # carries "shipping &amp; logistics" — otherwise the footer double-appends.
    desc_unescaped = html.unescape(description).lower()
    already = bool(footer_marker) and footer_marker in desc_unescaped
    if description and footer and not already:
        description = f"{description}\n{footer}"

    result["title"] = title
    result["description"] = description
    result["aspects"] = aspects
    result["titleCN"] = title_cn
    result["descriptionCN"] = description_cn
    if not isinstance(result.get("features"), list):
        result["features"] = []

    # HARD backstop: after deterministic stripping this should always pass; it
    # stays as a fail-closed guard against a term the stripper somehow missed.
    assert_clean(title=f"{title} {title_cn}", description=description, aspects=aspects, terms=terms)

    return result
