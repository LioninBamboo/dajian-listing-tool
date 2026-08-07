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

# Botanical palette: deep garden green + leaf accent + warm sand + cream tints.
_GREEN = "#2e5d43"
_GREEN_DK = "#1c3b2b"
_LEAF = "#8fc9a3"
_SAND = "#cbab6b"
_INK = "#25302a"
_MUTED = "#5c6b60"
_CREAM = "#f3f7f2"
_LINE = "#e4ebe4"

# Hero stat cards: the numbers a home/garden buyer scans. Short values only.
_HERO_KEYS = (
    "Material", "Dimensions (L × W × H)", "Item Weight", "Color", "Capacity",
    "Maximum Weight Capacity", "Set Includes", "Shape", "Style",
)

# One scoped <style> block instead of repeating inline styles on every element —
# eBay's Inventory description allows <style>, and it survives _compress_html
# (which only collapses whitespace between tags). This keeps a rich, layered
# design well under the 4000-char inventory cap. Class prefix ``gpl-`` is unique.
_STYLE_BLOCK = (
    "<style>"
    ".gpl-w{max-width:900px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;"
    f"color:{_INK};background:#fff;border:1px solid {_LINE};border-radius:12px;overflow:hidden}}"
    f".gpl-bar{{background:linear-gradient(135deg,{_GREEN},{_GREEN_DK});text-align:center}}"
    f".gpl-ban{{padding:22px 20px 18px;border-bottom:3px solid {_SAND};font-family:Georgia,serif}}"
    ".gpl-brand{font-size:27px;letter-spacing:7px;color:#fff}"
    f".gpl-tag{{font-size:10px;letter-spacing:3px;text-transform:uppercase;color:{_LEAF};margin-top:7px}}"
    ".gpl-hero{display:flex;flex-wrap:wrap;gap:12px;padding:22px 22px 4px}"
    f".gpl-card{{flex:1 1 130px;min-width:116px;background:{_CREAM};border-top:3px solid {_SAND};"
    f"border-radius:10px;padding:15px 10px;text-align:center}}"
    f".gpl-cv{{font-size:19px;font-weight:800;color:{_GREEN};line-height:1.25}}"
    f".gpl-ck{{margin-top:6px;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:{_MUTED}}}"
    ".gpl-body{padding:20px 24px 4px}"
    f".gpl-intro{{font-size:15px;line-height:1.75;margin:0}}"
    f".gpl-h3{{margin:22px 0 13px;font-size:13px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;"
    f"color:{_GREEN};border-left:4px solid {_SAND};padding-left:11px}}"
    ".gpl-ul{list-style:none;padding:0;margin:0}"
    f".gpl-li{{display:flex;gap:10px;margin:0 0 11px;padding-bottom:11px;border-bottom:1px solid {_LINE}}}"
    ".gpl-li:last-child{border:0;margin:0;padding:0}"
    f".gpl-ico{{color:{_LEAF};flex:0 0 auto}}"
    ".gpl-ft{font-size:14px;line-height:1.6}"
    f".gpl-lead{{color:{_GREEN};font-weight:700}}"
    f".gpl-pf{{background:{_CREAM};border-left:4px solid {_SAND};border-radius:9px;padding:15px 18px;margin-top:20px}}"
    f".gpl-pfh{{font-size:12px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:{_GREEN};margin-bottom:7px}}"
    f".gpl-pfb{{font-size:13.5px;line-height:1.7;color:{_MUTED}}}"
    f".gpl-foot{{padding:15px 18px;border-top:3px solid {_SAND};margin-top:22px}}"
    f".gpl-f1{{font-size:12px;color:{_SAND};letter-spacing:1px}}"
    ".gpl-f2{font-size:10px;color:#cfe0d4;margin-top:4px}"
    "</style>"
)


def _banner_block(profile: Any) -> str:
    brand = html.escape(str(getattr(profile, "brand_name", "") or "").upper())
    tagline = html.escape(str(getattr(profile, "brand_tagline", "") or ""))
    return (f'<div class="gpl-bar gpl-ban"><div class="gpl-brand">{brand}</div>'
            f'<div class="gpl-tag">{tagline}</div></div>')


def _footer_block(profile: Any) -> str:
    footer = str(getattr(profile, "footer_html", "") or "").strip()
    if footer:
        return footer
    l1 = html.escape(str(getattr(profile, "description_footer_line1", "") or ""))
    l2 = html.escape(str(getattr(profile, "description_footer_line2", "") or ""))
    return (f'<div class="gpl-bar gpl-foot"><div class="gpl-f1">{l1}</div>'
            f'<div class="gpl-f2">{l2}</div></div>')


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
            f'<div class="gpl-card"><div class="gpl-cv">{html.escape(val)}</div>'
            f'<div class="gpl-ck">{html.escape(key)}</div></div>'
        )
        if len(cards) == 3:
            break
    if len(cards) < 2:
        return ""
    return f'<div class="gpl-hero">{"".join(cards)}</div>'


def build_garden_lifestyle_system_prompt(profile: Any) -> str:
    brand = getattr(profile, "brand_name", "") or "the store"
    return f"""You are an expert eBay lifestyle copywriter for {brand}, a US-warehouse OUTDOOR · GARDEN · PET home-goods store.

**HOW THIS WORKS:** The system OWNS the entire visual design (brand banner, hero stat cards, the KEY FEATURES
layout, the PERFECT FOR card, the footer). You do NOT write any HTML, CSS, <div>, <li>, or emoji — you provide
only TEXT, and the system styles it. Return three copy fields:
  - "intro": one warm, benefit-led sentence introducing the product (plain text).
  - "features": an array of 4–6 short strings, each "Lead-in: benefit detail." (e.g.
    "Sturdy MGO Build: durable, natural-textured finish that ages beautifully outdoors."). Plain text only.
  - "perfect_for": one or two sentences naming 2–3 real use scenes (patio, balcony, garden bed, sunroom,
    entryway, for a pet) the product genuinely suits. Plain text.

**VOICE — write like a lifestyle brand, sell the benefit:** Frame the TRUE source facts as buyer benefits with
warm, aspirational language. You MAY use descriptive/aesthetic adjectives and benefit framing — sturdy, natural,
decorative, versatile, stylish, freestanding, easy to move, a warm rustic look, roomy planting space, etc. Turn a
spec into a benefit ("magnesium oxide construction for a sturdy, natural-looking piece"). This is how the copy earns
the click.

**FACT SAFETY — HARD (an automated guard rejects violations):** The ONE thing you must never do is assert a NEW
VERIFIABLE PROPERTY the source doesn't state:
- No material/finish it isn't made of; no capacity, count, dimension, or certification not in the source.
- No "-proof" / "-resistant" / durability or weather rating unless that exact word is in the source
  (NOT "weatherproof", "frost-proof", "UV-resistant", "corrosion-resistant", "waterproof", "rustproof", "heavy-duty").
- Aesthetic/benefit framing of real facts is fine; inventing a testable claim is not.
- Fill Item Specifics ('aspects') accurately; values MUST be lists of strings. The 'Brand' aspect is set by the system.
  Fill AS MANY relevant aspects as the source supports (Type, Material, Color, Shape, Features, Indoor/Outdoor,
  Style, Room, Set Includes …) — eBay ranks and filters on these, so more accurate specifics = more visibility.

**TITLE (eBay Cassini search — THIS is where keywords rank, not the description):**
- Use 75–80 of the 80 characters — never waste the space. Title Case, no brand name, no fluff (New/Best/Sale).
- FRONT-LOAD the highest-search terms buyers actually type. Weave in as many of the provided trending keywords
  as read naturally; include the product noun + key descriptors (size, color, material, shape, use). Prefer
  words buyers search (e.g. "Planter Box", "Flower Pot", "Large", "Outdoor", "Yard") over codes nobody searches.

**OUTPUT FORMAT:** a SINGLE valid JSON object, no markdown fences, NO HTML anywhere:
{{
  "title": "75-80 char keyword-front-loaded SEO title, no brand name",
  "intro": "one warm benefit-led sentence, plain text",
  "features": ["Lead-in: benefit detail.", "Lead-in: benefit detail.", "..."],
  "perfect_for": "one or two sentences of real use scenes, plain text",
  "aspects": {{ "Type": ["Planter"], "Material": ["Magnesium Oxide (MGO)"], "Color": ["Rust"], "Shape": ["Square"] }},
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


def _clip(text: str, cap: int) -> str:
    """Truncate at a word boundary with an ellipsis, keeping sentences readable."""
    t = str(text or "").strip()
    if len(t) <= cap:
        return t
    cut = t[:cap].rsplit(" ", 1)[0].rstrip(",;: ")
    return cut + "…"


def _render_features(features: Any, max_feats: int = 6, detail_cap: int = 10_000) -> str:
    """KEY FEATURES rendered deterministically from the LLM's structured list —
    a sand-accented section header + leaf-bulleted rows with a bold green lead-in.
    ``max_feats``/``detail_cap`` let the caller shrink the block to fit the char cap."""
    rows = []
    for feat in (features or []):
        s = str(feat).strip()
        if not s:
            continue
        if ":" in s:
            label, detail = s.split(":", 1)
            inner = (f'<span class="gpl-lead">{html.escape(label.strip())}:</span> '
                     f'{html.escape(_clip(detail, detail_cap))}')
        else:
            inner = html.escape(_clip(s, detail_cap))
        rows.append(
            f'<li class="gpl-li"><span class="gpl-ico">🌿</span>'
            f'<span class="gpl-ft">{inner}</span></li>'
        )
        if len(rows) >= max_feats:
            break
    if not rows:
        return ""
    return (f'<h3 class="gpl-h3">Key Features</h3>'
            f'<ul class="gpl-ul">{"".join(rows)}</ul>')


def finalize_garden_lifestyle_listing(data: Mapping[str, Any], profile: Any) -> Dict[str, Any]:
    """Build the WHOLE description deterministically from the LLM's structured
    content (intro / features / perfect_for) — the model supplies only text, code
    controls every pixel: slim banner, hero stat cards, an intro line, a
    sand-accented KEY FEATURES section, a cream PERFECT FOR card, slim footer.
    """
    result: Dict[str, Any] = dict(data or {})
    title = str(result.get("title") or "").strip()[:80]
    aspects = _normalize_aspects(result.get("aspects"))

    if getattr(profile, "force_house_brand", False):
        aspects["Brand"] = [getattr(profile, "brand_name", "") or "Unbranded"]
    elif not aspects.get("Brand"):
        aspects["Brand"] = [getattr(profile, "default_brand", "Unbranded")]

    intro = str(result.get("intro") or "").strip()
    perfect_for = str(result.get("perfect_for") or "").strip()
    features = result.get("features") if isinstance(result.get("features"), list) else []

    banner = _banner_block(profile)
    hero = _hero_band(aspects)
    footer = _footer_block(profile)

    def _assemble(intro_cap: int, max_feats: int, detail_cap: int, pf_cap: int) -> str:
        intro_html = (f'<p class="gpl-intro">{html.escape(_clip(intro, intro_cap))}</p>'
                      ) if intro else ""
        feats_html = _render_features(features, max_feats=max_feats, detail_cap=detail_cap)
        pf_html = (
            f'<div class="gpl-pf"><div class="gpl-pfh">Perfect For</div>'
            f'<div class="gpl-pfb">{html.escape(_clip(perfect_for, pf_cap))}</div></div>'
        ) if perfect_for else ""
        content = f'<div class="gpl-body">{intro_html}{feats_html}{pf_html}</div>'
        return f'{_STYLE_BLOCK}<div class="gpl-w">{banner}{hero}{content}{footer}</div>'

    # Inventory API caps descriptions at 4000 chars; the inline-styled chrome is
    # fixed overhead, so budget the prose down progressively until it fits.
    CAP = 3900
    description = _assemble(240, 6, 10_000, 260)
    if len(description) > CAP:
        for max_feats, detail_cap, intro_cap, pf_cap in (
            (6, 200, 220, 240), (6, 150, 190, 200), (5, 130, 170, 180),
            (5, 100, 150, 150), (4, 90, 130, 130), (4, 70, 110, 110),
        ):
            description = _assemble(intro_cap, max_feats, detail_cap, pf_cap)
            if len(description) <= CAP:
                break

    result["title"] = title
    result["description"] = description
    result["aspects"] = aspects
    result["features"] = features
    return result
