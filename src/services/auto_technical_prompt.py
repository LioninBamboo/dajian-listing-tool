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
    box_label = "WHAT'S IN THE BOX" if mode == AUTO_MODE_TOOL else "PACKAGE INCLUDES"
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
            "- Lead with placement on the vehicle and the fit/finish/function value, "
            "using ONLY the source's own wording for the fitment type.\n"
            "- Do NOT add any Fitment / Compatibility / 'compatible models' section, "
            "and do NOT list or allude to Year/Make/Model, part numbers, or "
            "OE/interchange numbers. eBay renders the full vehicle compatibility "
            "table natively below the description — a fitment section here is "
            "redundant and leaves a dangling, empty 'models include:' line.\n"
            "- Cover material/finish, install difficulty, and any hardware included."
        )
        aspects_hint = (
            '{ "Type": ["Receiver Hitch"], "Placement on Vehicle": ["Rear"], '
            '"Fitment Type": ["Direct Replacement"], "Material": ["Steel"], '
            '"Finish": ["Black Powder Coat"], "Features": ["Class III"] }'
        )
    return f"""You are an expert eBay copywriter for a US-warehouse {role} store ({brand}).

**CONTENT & DESIGN RULES (CRITICAL):**
1. HTML Description ('description'): a self-contained, MOBILE-FIRST inline-CSS block (most eBay
   traffic is phones — one column, generous tap spacing, no fixed widths). Use INLINE CSS
   (style="...") for ALL styling; NEVER use CSS classes (eBay strips them). No <html>/<head>/<body>.
   Follow this DESIGN SYSTEM exactly for a consistent, high-contrast, premium-technical look:
   - Palette: ink #14161a · steel #1f2329 · safety-orange accent #ff5722 · hairline #e5e7eb ·
     light row #f6f7f9 · white #fff. High contrast ALWAYS — never dark text on a dark fill.
   - HERO SPEC BAND (first thing after the title): a wrapping flex row of 2–4 "stat cards" holding
     the buyer's KEY DECISION NUMBERS (e.g. the class/grade, the load/capacity figure, the size).
     Each card = big bold value (28px, #14161a) + a small UPPERCASE label under it (11px, #6b7280,
     letter-spacing:1px); white card, 1px #e5e7eb border, padding 14px, border-radius 8px. THIS BAND
     IS THE CONVERSION FOCUS. Use ONLY numbers/values present in the source; omit the band if none.
   - KEY FEATURES: title 15px bold UPPERCASE #ff5722 with a 1px #e5e7eb bottom rule. 4–6 bullets,
     each 15px #14161a line-height 1.75, starting with a <strong> lead-in phrase, then the detail.
   - Do NOT build a SPECIFICATIONS or specs table and do NOT use any <table>/<th>/<td> — the system
     renders the specifications table automatically from the item specifics. Put every spec value into
     'aspects' instead (that is what gets tabulated).
   - {box_label}: one short line/list of exactly what ships in the box.
   - Do NOT add a Shipping / Returns / Policies section — the system appends the footer automatically.
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


def _banner_block(profile: Any) -> str:
    """Deterministic auto-styled brand banner (dark steel + safety-orange rule).

    Prepended by finalize so every auto listing opens with a consistent, branded
    header in the auto theme — not the furniture navy/gold shell — and carries the
    store banner marker for the QC template check.
    """
    brand = html.escape(str(getattr(profile, "brand_name", "") or "").upper())
    tagline = html.escape(str(getattr(profile, "brand_tagline", "") or ""))
    return (
        '<div style="text-align:center;padding:26px 15px;background:#1a1a1a;'
        'border-bottom:4px solid #ff5722;font-family:Arial,sans-serif;">'
        f'<h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:3px;color:#fff;">{brand}</h1>'
        f'<p style="margin:8px 0 0;font-size:12px;color:#ff5722;letter-spacing:2px;">{tagline}</p></div>'
    )


_SPEC_HIDDEN = {"brand", "features", "california prop 65 warning", "item sku",
                "unit of measurement", "number of packages", "qty", "extra info"}


def _spec_block(aspects: Mapping[str, Any]) -> str:
    """Deterministic HIGH-CONTRAST specifications table built from item specifics.

    The LLM cannot be trusted to keep dark text off dark fills, so the spec table
    — the part buyers actually scan — is rendered in code: dark header + white
    text, alternating white / #f6f7f9 rows, grey labels, bold dark values.
    """
    rows = []
    for key, value in (aspects or {}).items():
        k = str(key).strip()
        if not k or k.lower() in _SPEC_HIDDEN:
            continue
        if isinstance(value, (list, tuple)):
            val = ", ".join(str(v).strip() for v in value if str(v).strip())
        else:
            val = str(value).strip()
        if not val:
            continue
        rows.append((html.escape(k), html.escape(val)))
    if not rows:
        return ""
    body = ""
    for i, (k, v) in enumerate(rows):
        bg = "#ffffff" if i % 2 == 0 else "#f6f7f9"
        body += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:11px 14px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:14px;width:42%;">{k}</td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #e5e7eb;color:#14161a;font-size:14px;font-weight:700;">{v}</td>'
            f"</tr>"
        )
    return (
        '<div style="margin-top:22px;">'
        '<h3 style="margin:0 0 12px;font-size:15px;font-weight:800;letter-spacing:1px;'
        'text-transform:uppercase;color:#ff5722;border-bottom:1px solid #e5e7eb;padding-bottom:6px;">Specifications</h3>'
        '<table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;">'
        '<tr style="background:#1f2329;">'
        '<th style="text-align:left;padding:11px 14px;color:#ffffff;font-size:13px;text-transform:uppercase;letter-spacing:.5px;">Attribute</th>'
        '<th style="text-align:left;padding:11px 14px;color:#ffffff;font-size:13px;text-transform:uppercase;letter-spacing:.5px;">Value</th>'
        f"</tr>{body}</table></div>"
    )


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

    # Prepend the branded auto banner once (idempotent via the store banner
    # marker — brand_name.upper() contains it). Gives the auto template its own
    # complete chrome so nothing downstream wraps it in the furniture shell.
    banner = _banner_block(profile)
    # Idempotency keys on the brand name — that is what the banner actually
    # contains (quality_banner_marker can differ from brand_name).
    brand_l = str(getattr(profile, "brand_name", "") or "").lower()
    has_banner = bool(brand_l) and brand_l in html.unescape(description).lower()
    if description and banner and not has_banner:
        description = f"{banner}\n{description}"

    # Inject the deterministic high-contrast spec table (once) before the footer,
    # so the buyer-critical specs are always legible regardless of the LLM.
    spec = _spec_block(aspects)
    if description and spec and "<th" not in description:
        description = f"{description}\n{spec}"

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
