"""Conversion-oriented English listing copy helpers.

Builds persuasive KEY FEATURES bullets that are still product-grounded
(title / attributes / specs / source description), not keyword soup and not
unsupported safety claims.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from src.utils.publish_validation import first_aspect_text

_TOKEN_SPLIT = re.compile(r"[,;/|+]+|\s{2,}")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" -•,;|")


def _is_keyword_soup(text: str) -> bool:
    """True for space-joined tags like 'armrest easy to assemble king size storage'."""
    t = _clean(text)
    if not t:
        return True
    words = _words(t)
    if len(words) < 4:
        return False
    # No sentence punctuation and many short tokens → soup
    if re.search(r"[.!?]", t):
        return False
    if len(words) >= 5 and " " in t and not re.search(r"\b(with|for|that|which|your|the|and)\b", t, re.I):
        # "easy to assemble" has "to" — still soup if all lowercase tags
        avg = sum(len(w) for w in words) / max(len(words), 1)
        if avg < 7 and not t[:1].isupper():
            return True
    # Single bullet that is just 4+ bare feature nouns
    if len(words) >= 6 and t.count(" ") >= 5 and not re.search(r"[,:;—–-]", t):
        return True
    return False


def _product_label(title: str, aspects: Mapping[str, Any] | None = None) -> str:
    type_val = first_aspect_text(dict(aspects or {}), "Type") or ""
    if type_val:
        return type_val
    t = title or "piece"
    # First noun-ish chunk
    m = re.search(
        r"\b("
        r"ottoman|bench|sofa|couch|chair|recliner|table|nightstand|cabinet|"
        r"vanity|mirror|vase|stroller|wagon|bookcase|shelf|dresser|bed|"
        r"sectional|loveseat|stool|desk|stand|rack|cart"
        r")s?\b",
        t,
        re.I,
    )
    if m:
        return m.group(1).lower()
    return "piece"


def _aspect_or_attr(aspects: Mapping, attrs: Mapping, specs: Mapping, *keys: str) -> str:
    for key in keys:
        for bag in (aspects, attrs, specs):
            if not bag:
                continue
            if key in bag:
                val = bag[key]
                if isinstance(val, list):
                    val = val[0] if val else ""
                val = _clean(str(val))
                if val:
                    return val
            # case-insensitive key match
            for k, v in bag.items():
                if str(k).lower() == key.lower():
                    if isinstance(v, list):
                        v = v[0] if v else ""
                    v = _clean(str(v))
                    if v:
                        return v
    return ""


def _expand_token(token: str, *, label: str, title: str) -> str | None:
    t = _clean(token).lower()
    if not t or len(t) < 3:
        return None
    # Skip pure numbers / colors alone
    if re.fullmatch(r"[\d.\s\"'inxcm]+", t):
        return None

    rules = [
        (r"easy\s*to\s*assemble|easy\s*assembly|assembly",
         f"Assembly is straightforward—most homes set up this {label} quickly with the included hardware and clear instructions."),
        (r"no\s*assembly|fully\s*assembled|ready\s*to\s*use",
         f"Arrives ready to enjoy with little to no assembly so you can place and use this {label} the same day."),
        (r"storage|drawer|compartment|cubby",
         f"Thoughtful storage keeps everyday essentials organized so the top surface stays clean and usable."),
        (r"upholster|chenille|fabric|velvet|corduroy|microsuede|linen",
         f"Soft, inviting upholstery adds texture and comfort that elevates bedroom or living-room style."),
        (r"metal|steel|iron frame|sturdy|durable|solid",
         f"A sturdy build is designed for everyday use so the {label} feels stable and long-lasting."),
        (r"wood|mdf|particle|oak|walnut|pine",
         f"Quality wood-based construction delivers a clean, finished look that pairs with modern or classic décor."),
        (r"multi[\s-]*purpose|versatile|multi[\s-]*function",
         f"A versatile design works as seating, an accent piece, or extra surface space wherever you need it."),
        (r"king\s*size|end\s*of\s*bed|bedroom",
         f"Sized to sit beautifully at the foot of a bed or along a wall, adding polish to the bedroom layout."),
        (r"armrest",
         f"Supportive armrest styling improves comfort when you sit to put on shoes or take a quick break."),
        (r"gold\s*leg|metal\s*leg|wood\s*leg|tapered",
         f"Finished legs complete the silhouette and lift the {label} for a lighter, more refined profile."),
        (r"cushion|padded|plush|foam",
         f"Padded comfort makes this {label} a place you actually want to sit—not just a showpiece."),
        (r"fold|convert|sleeper",
         f"Flexible design adapts to daily lounging and overnight guests without needing a second piece of furniture."),
        (r"recline|rocker|swivel",
         f"Smooth motion features help you settle in and relax after a long day."),
        (r"usb|charg|outlet|power",
         f"Built-in power access keeps phones and devices charged right where you sit or work."),
        (r"glass|tempered",
         f"Glass elements create a bright, open look while showcasing décor inside or reflecting light in the room."),
        (r"mirror",
         f"A clear, reflective surface helps the room feel larger and makes outfit checks effortless."),
        (r"adjust",
         f"Adjustable elements let you fine-tune the setup for your space and routine."),
        (r"anti[\s-]*tip|safety",
         f"Safety-minded design details help keep the piece secure in busy homes."),
        (r"soft[\s-]*close|damp",
         f"Soft-close motion reduces slamming noise and keeps drawers or doors feeling premium day after day."),
        (r"rattan|woven",
         f"Woven texture brings warm, natural character that photographs well and pairs with farmhouse or modern décor."),
        (r"waterproof|water\s*resist|scratch|stain",
         f"Practical surface performance stands up better to daily life so the piece stays looking newer longer."),
    ]
    for pat, sentence in rules:
        if re.search(pat, t, re.I):
            return sentence
    # Generic benefit from a readable phrase
    if len(_words(token)) >= 3 and not _is_keyword_soup(token):
        phrase = _clean(token)
        if phrase[0].islower():
            phrase = phrase[0].upper() + phrase[1:]
        if not phrase.endswith("."):
            phrase += "."
        return phrase
    if len(_words(token)) <= 3:
        nice = _clean(token)
        return f"Designed with {nice.lower()} in mind to improve everyday comfort and usability."
    return None


def build_conversion_feature_bullets(
    *,
    title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    aspects: Mapping[str, Any] | None = None,
    characteristics: list[str] | None = None,
    limit: int = 6,
) -> list[str]:
    """Return 4–6 conversion-focused English feature bullets."""
    from scripts.audit_fix_active_listings import extract_source_feature_bullets, strip_html_text

    attrs = dict(attributes or {})
    specs = dict(specs or {})
    aspects = dict(aspects or {})
    label = _product_label(title, aspects)
    bullets: list[str] = []
    seen: set[str] = set()

    def _add(sentence: str | None) -> None:
        s = _clean(sentence or "")
        if not s or _is_keyword_soup(s):
            return
        words = _words(s)
        if len(words) < 6:
            return
        key = s.casefold()
        if key in seen:
            return
        # Near-duplicate guard
        if any(key in e or e in key for e in seen if len(e) > 20):
            return
        seen.add(key)
        if not s.endswith((".", "!", "?")):
            s += "."
        bullets.append(s)

    # 1) Real English sentences from source (best)
    for b in extract_source_feature_bullets(source_description or "", limit=limit):
        if not _is_keyword_soup(b):
            _add(b)

    # 2) Expand characteristics / feature tokens
    raw_tokens: list[str] = []
    for c in characteristics or []:
        c = _clean(str(c))
        if not c:
            continue
        if _is_keyword_soup(c) or (" " in c and len(_words(c)) >= 5 and not re.search(r"[.!?]", c)):
            raw_tokens.extend([p for p in re.split(r"[|,;/]+|\s{2,}", c) if p.strip()])
            # also split simple space soup into chunks of known phrases is hard; expand whole
            raw_tokens.append(c)
        else:
            raw_tokens.append(c)
    for tok in raw_tokens:
        if len(bullets) >= limit:
            break
        _add(_expand_token(tok, label=label, title=title or ""))

    # 3) Grounded product facts → benefit lines
    material = _aspect_or_attr(aspects, attrs, specs, "Material", "Main Material", "Upholstery Fabric", "Fabric Type")
    color = _aspect_or_attr(aspects, attrs, specs, "Color", "Main Color", "Finish")
    length = _aspect_or_attr(aspects, attrs, specs, "Item Length")
    width = _aspect_or_attr(aspects, attrs, specs, "Item Width")
    height = _aspect_or_attr(aspects, attrs, specs, "Item Height")
    assembly = _aspect_or_attr(aspects, attrs, specs, "Assembly Required") or ""
    weight_cap = _aspect_or_attr(aspects, attrs, specs, "Weight Capacity", "Maximum Weight Capacity", "Load Capacity")

    if material and len(bullets) < limit:
        _add(
            f"Finished in {_clean(material).lower()}, this {label} brings a polished look that pairs easily with modern or classic décor."
        )
    if length and len(bullets) < limit:
        ln = re.search(r"(\d+(?:\.\d+)?)", length)
        if ln:
            _add(
                f"At about {ln.group(1)} inches long, it is sized to make a clear visual impact without overwhelming the room."
            )
    if weight_cap and len(bullets) < limit:
        _add(
            f"Built for real daily use with a weight capacity of {_clean(weight_cap)}, so the family can sit and rely on it confidently."
        )
    if assembly:
        al = assembly.lower()
        if al.startswith("no") and len(bullets) < limit:
            _add(
                f"Minimal assembly keeps setup stress-free—place this {label} and start using it almost immediately."
            )
        elif al.startswith("yes") and len(bullets) < limit:
            _add(
                "Assembly stays approachable for most DIYers with included hardware and step-by-step guidance."
            )
    if color and len(bullets) < limit:
        _add(
            f"The {_clean(color).lower()} finish coordinates with popular palettes so styling the space feels effortless."
        )

    # 4) Title-driven fallbacks so we never ship a single soup bullet
    title_l = (title or "").lower()
    if "storage" in title_l and len(bullets) < limit:
        _add("Integrated storage options help hide clutter while keeping frequently used items within reach.")
    if any(k in title_l for k in ("ottoman", "bench", "seat")) and len(bullets) < limit:
        _add(
            f"Comfortable seating height makes this {label} practical for putting on shoes, hosting guests, or adding extra perches."
        )
    if any(k in title_l for k in ("mirror", "vanity")) and len(bullets) < limit:
        _add("A clean reflective surface supports daily routines and brightens the wall it hangs or stands against.")
    if any(k in title_l for k in ("table", "desk", "nightstand", "end table")) and len(bullets) < limit:
        _add("A stable surface is ready for lamps, books, devices, and décor without looking bulky.")

    # Guarantee minimum 4 solid bullets
    fallbacks = [
        f"Designed as a high-utility {label} that improves how the room looks and works every day.",
        "Clean lines and balanced proportions help the piece photograph well and blend into real homes—not just showrooms.",
        "Practical details reduce daily friction so you get more value from the purchase long after unboxing.",
        "Pairs with AquaVerve’s focus on premium home furnishings: style you notice, function you use.",
    ]
    for fb in fallbacks:
        if len(bullets) >= max(4, min(limit, 6)):
            break
        _add(fb)

    return bullets[:limit]


def is_thin_key_features_description(description: str) -> bool:
    """Detect store-template pages with keyword-soup KEY FEATURES."""
    desc = description or ""
    if "key features" not in desc.lower():
        return False
    items = re.findall(r"<li\b[^>]*>(.*?)</li>", desc, flags=re.I | re.S)
    if not items:
        return True
    plain_items = [_clean(re.sub(r"<[^>]+>", " ", it)) for it in items]
    plain_items = [p for p in plain_items if p]
    if not plain_items:
        return True
    # Only keyword-soup / tag piles count as thin — short real sentences are fine.
    if len(plain_items) == 1 and _is_keyword_soup(plain_items[0]):
        return True
    soup_count = sum(1 for p in plain_items if _is_keyword_soup(p))
    return soup_count >= max(1, (len(plain_items) + 1) // 2)
