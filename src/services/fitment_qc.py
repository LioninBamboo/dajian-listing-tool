"""Vehicle fitment (车型适用性) parsing + QC for Motors listings.

GigaCloud provides no structured Year/Make/Model, so the only fitment signal
lives in the title (e.g. "Running Boards For 2019-2026 Chevy Silverado 1500").
This module:

  * parse_title_fitment(title)  -> list[VehicleFit]  (empty = universal)
  * classify_fitment(title)     -> "vehicle_specific" | "universal"
  * check_fitment_qc(title, live_compat) -> list[FitmentIssue]

QC catches three failure modes:
  MISSING   — title names a specific vehicle but the live ItemCompatibilityList
              is empty (buyer can't filter by their car; eBay may suppress it).
  MISMATCH  — a make/model in the live compatibility list is NOT supported by
              the title text (possible wrong fitment).
  SUSPECT_UNIVERSAL — title says "universal / fits most" yet also names a
              specific vehicle+year (contradictory claim, human review).

Building/applying the ItemCompatibilityList is intentionally a separate step
(``build_compatible_products``) so QC can run read-only first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

# Make -> canonical name + recognised model tokens. Extend as new vehicles appear;
# unknown makes are still detected generically via _MAKE_ALIASES for the QC pass.
_MAKES: dict[str, dict[str, Any]] = {
    "chevrolet": {"aliases": ["chevrolet", "chevy"], "models": [
        "silverado 1500", "silverado 2500", "silverado 3500", "silverado", "colorado", "tahoe", "suburban"]},
    "gmc": {"aliases": ["gmc"], "models": ["sierra 1500", "sierra 2500", "sierra", "canyon", "yukon"]},
    "toyota": {"aliases": ["toyota"], "models": [
        "tacoma", "tundra", "4runner", "tacoma double cab", "sienna", "highlander"
    ]},
    "ford": {"aliases": ["ford"], "models": ["f-150", "f150", "f-250", "f250", "ranger", "bronco", "f-350"]},
    "ram": {"aliases": ["ram", "dodge ram"], "models": ["ram 1500", "ram 2500", "1500", "2500"]},
    "dodge": {"aliases": ["dodge"], "models": ["ram 1500", "ram 2500", "durango"]},
    "jeep": {"aliases": ["jeep"], "models": ["wrangler jl", "wrangler jk", "wrangler", "gladiator", "cherokee"]},
    "mazda": {"aliases": ["mazda"], "models": ["b-series", "b series", "bt-50"]},
    "land rover": {"aliases": ["land rover"], "models": ["defender", "range rover", "discovery"]},
    "nissan": {"aliases": ["nissan"], "models": ["frontier", "titan"]},
    "honda": {"aliases": ["honda"], "models": ["ridgeline"]},
}
_MAKE_ALIASES = {alias: canon for canon, cfg in _MAKES.items() for alias in cfg["aliases"]}

# "for/fits" a universal fit — do NOT treat these as vehicle-specific on their own.
_UNIVERSAL_MARKERS = (
    "universal", "fits most", "fit most", "for vehicles", "for most vehicles",
    "for suv truck", "for suvs", "for trucks", "for car", "for cars",
)

_YEAR = r"(?:19|20)\d{2}"
# 2005-2023 / 2019–2026 / 2007-2026
_YEAR_RANGE = re.compile(rf"\b({_YEAR})\s*[-–—]\s*({_YEAR})\b")
_YEAR_SINGLE = re.compile(rf"\b({_YEAR})\b")


@dataclass
class VehicleFit:
    make: str
    model: Optional[str]
    year_from: Optional[int]
    year_to: Optional[int]

    def as_dict(self) -> dict[str, Any]:
        return {"make": self.make, "model": self.model,
                "year_from": self.year_from, "year_to": self.year_to}


@dataclass
class FitmentIssue:
    code: str          # missing_fitment | fitment_mismatch | suspect_universal
    severity: str      # CRITICAL | HIGH | MEDIUM
    detail: str
    parsed: list = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).lower()


# Normalize Ford truck model spellings so title "F150" matches live "F-150".
_MODEL_CANON_MAP = {
    "f150": "f-150",
    "f-150": "f-150",
    "f250": "f-250",
    "f-250": "f-250",
    "f350": "f-350",
    "f-350": "f-350",
}

_DEFENDER_SERIES_PATTERN = re.compile(
    r"\bdefender\b[\s/-]*(?:90|110|130)(?:[\s/,-]+(?:90|110|130))*\b"
)


def _canonical_model_token(token: str) -> str:
    """Lowercased canonical model token for QC comparisons and catalog build."""
    t = _norm(token).replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip()
    if t in _MODEL_CANON_MAP:
        return _MODEL_CANON_MAP[t]
    # Strip spaces around hyphenated F-series already covered; leave others.
    compact = t.replace(" ", "")
    if compact in _MODEL_CANON_MAP:
        return _MODEL_CANON_MAP[compact]
    return t


def _title_mentions_token(title_norm: str, token: str) -> bool:
    """True if title contains token or any known alias of that token."""
    lowered = _norm(token)
    if lowered.startswith("defender ") and _DEFENDER_SERIES_PATTERN.search(title_norm):
        suffix = lowered.split(" ", 1)[1].strip()
        if suffix in {"90", "110", "130"}:
            return True

    canon = _canonical_model_token(token)
    candidates = {token.lower(), canon}
    for raw, mapped in _MODEL_CANON_MAP.items():
        if mapped == canon:
            candidates.add(raw)
    # Also allow undashed forms of hyphenated models (f-150 -> f150).
    if "-" in canon:
        candidates.add(canon.replace("-", ""))
    for cand in candidates:
        if not cand:
            continue
        if re.search(rf"\b{re.escape(cand)}\b", title_norm):
            return True
        # Titles often write "F150" without word boundaries around digits alone.
        if cand.replace("-", "") in title_norm.replace("-", "").replace(" ", ""):
            # Require the compact form appears as contiguous alnum in title.
            compact_title = re.sub(r"[^a-z0-9]", "", title_norm)
            compact_cand = re.sub(r"[^a-z0-9]", "", cand)
            if compact_cand and compact_cand in compact_title:
                return True
    return False


def _distinctive_models() -> list[tuple[str, str]]:
    """Return (model, make) pairs long enough to identify a make without the brand word.

    Short numeric tokens like ``1500`` are skipped — they appear in many truck titles.
    """
    pairs: list[tuple[str, str]] = []
    for canon, cfg in _MAKES.items():
        for model in cfg["models"]:
            # Keep short Ford truck codes (f150) — they are distinctive enough.
            if model.isdigit():
                continue
            if len(model) < 5 and _canonical_model_token(model) not in _MODEL_CANON_MAP.values():
                continue
            pairs.append((model, canon))
    pairs.sort(key=lambda item: -len(item[0]))
    return pairs


def _years_near(text: str, start: int) -> tuple[Optional[int], Optional[int]]:
    around = text[max(0, start - 40):start + 70]
    year_range = _YEAR_RANGE.search(around)
    if year_range:
        return int(year_range.group(1)), int(year_range.group(2))
    single = _YEAR_SINGLE.search(around)
    if single:
        year = int(single.group(1))
        return year, year
    return None, None


def parse_title_fitment(title: str) -> list[VehicleFit]:
    """Extract concrete (make [+model] [+year-range]) vehicles named in the title.

    Returns [] when the title names no specific make (i.e. universal). A make with
    no year is still returned (year None) — e.g. 'for Jeep Wrangler'."""
    t = _norm(title)
    fits: list[VehicleFit] = []
    for alias, canon in sorted(_MAKE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(rf"\b{re.escape(alias)}\b", t):
            # window: from this make mention to the next make mention (or +60 chars)
            start = m.start()
            window = t[start:start + 70]
            model = None
            for cand in _MAKES[canon]["models"]:
                if cand in window:
                    model = _canonical_model_token(cand)
                    break
            yf, yt = _years_near(t, start)
            fits.append(VehicleFit(make=canon, model=model, year_from=yf, year_to=yt))
    # Titles often say "Silverado/Sierra" without repeating GMC. Treat a distinctive
    # model token as evidence of its parent make.
    have = {(f.make, f.model) for f in fits}
    for model, canon in _distinctive_models():
        if any(
            f.make == canon and f.model and (f.model == model or model in f.model or f.model in model)
            for f in fits
        ):
            continue
        for m in re.finditer(rf"\b{re.escape(model)}\b", t):
            if (canon, model) in have:
                break
            yf, yt = _years_near(t, m.start())
            fits.append(VehicleFit(make=canon, model=model, year_from=yf, year_to=yt))
            have.add((canon, model))
            break
    # dedupe by (make, model, year_from, year_to)
    seen = set()
    out = []
    for f in fits:
        key = (f.make, f.model, f.year_from, f.year_to)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def classify_fitment(title: str) -> str:
    """vehicle_specific if the title names a real make; else universal."""
    return "vehicle_specific" if parse_title_fitment(title) else "universal"


def _live_makes_models(live_compat: Optional[Sequence[Mapping[str, Any]]]) -> set[str]:
    """Flatten an eBay ItemCompatibilityList (compatibleProducts) into lowercased
    make/model tokens for cross-checking against the title."""
    tokens: set[str] = set()
    for entry in live_compat or []:
        props = (entry or {}).get("compatibilityProperties") or (entry or {}).get("nameValueList") or []
        for p in props:
            name = str(p.get("name", "")).lower()
            val = str(p.get("value", "")).lower().strip()
            if name in ("make", "model") and val:
                tokens.add(val)
    return tokens


def check_fitment_qc(title: str, live_compat: Optional[Sequence[Mapping[str, Any]]]) -> list[FitmentIssue]:
    """Read-only QC for one listing's fitment vs its title."""
    issues: list[FitmentIssue] = []
    t = _norm(title)
    parsed = parse_title_fitment(title)
    has_universal_marker = any(mk in t for mk in _UNIVERSAL_MARKERS)
    live_tokens = _live_makes_models(live_compat)
    n_live = len(list(live_compat or []))

    # 1) title names a specific vehicle but nothing is attached
    if parsed and n_live == 0:
        # a bare make with no year/model is weak evidence; require a year OR model
        strong = [f for f in parsed if f.year_from or f.model]
        if strong:
            issues.append(FitmentIssue(
                code="missing_fitment",
                severity="HIGH",
                detail=("标题声称适配 " + "; ".join(
                    f"{f.make}{(' '+f.model) if f.model else ''}"
                    f"{(' '+str(f.year_from)+'-'+str(f.year_to)) if f.year_from else ''}"
                    for f in strong) + " 但 ItemCompatibilityList 为空"),
                parsed=[f.as_dict() for f in parsed],
            ))

    # 2) a make/model in the live list is not supported by the title text
    if live_tokens:
        title_makes = {f.make for f in parsed} | {
            canon for alias, canon in _MAKE_ALIASES.items()
            if re.search(rf"\b{re.escape(alias)}\b", t)
        } | {
            canon for model, canon in _distinctive_models()
            if re.search(rf"\b{re.escape(model)}\b", t)
        }
        for tok in live_tokens:
            canon = _MAKE_ALIASES.get(tok, tok)
            if canon in title_makes:
                continue
            if tok in t or _title_mentions_token(t, tok):
                continue
            # Live Model=F150 vs title F-150 (or reverse) must not flag mismatch.
            if _canonical_model_token(tok) and _title_mentions_token(t, tok):
                continue
            issues.append(FitmentIssue(
                code="fitment_mismatch",
                severity="HIGH",
                detail=f"适配列表含 '{tok}' 但标题未提及（疑似错挂）",
                parsed=[f.as_dict() for f in parsed],
            ))
            break

    # 3) contradictory: universal claim + specific vehicle+year
    if has_universal_marker and any(f.year_from and f.model for f in parsed):
        issues.append(FitmentIssue(
            code="suspect_universal",
            severity="MEDIUM",
            detail="标题同时声称 'universal/通用' 和具体车型年份（表述矛盾，需人工复核）",
            parsed=[f.as_dict() for f in parsed],
        ))

    return issues


# eBay's compatibility catalog uses the base model name, not the generation code
# (JK/JL are Wrangler generations, not catalog models). Map parsed models onto
# catalog names before building — an unmapped model is used as-is.
_CATALOG_MODEL = {
    "wrangler jk": "Wrangler",
    "wrangler jl": "Wrangler",
    "silverado 1500": "Silverado 1500",
    "sierra 1500": "Sierra 1500",
    "b-series": "B-Series",
    "b series": "B-Series",
    "f-150": "F-150",
    "f150": "F-150",
    "f-250": "F-250",
    "f250": "F-250",
    "f-350": "F-350",
    "f350": "F-350",
}


def build_compatible_products(title: str) -> list[dict[str, Any]]:
    """Turn parsed title fitment into eBay ``compatibleProducts`` entries.

    NOTE: this expands a year range into one entry per year with Make+Model only
    (no Trim/Engine, which GigaCloud doesn't provide). eBay validates the
    Year-Make-Model against its catalog at publish time, so an invalid combo is
    rejected there rather than silently fabricated. Universal parts return []."""
    out: list[dict[str, Any]] = []
    for f in parse_title_fitment(title):
        if not (f.make and f.model):
            continue
        years = ([f.year_from] if f.year_from and not f.year_to else
                 list(range(f.year_from, f.year_to + 1)) if f.year_from and f.year_to else [None])
        make_disp = "Chevrolet" if f.make == "chevrolet" else f.make.title()
        model_disp = _CATALOG_MODEL.get(f.model, f.model.title())
        for y in years:
            props = [{"name": "Make", "value": make_disp},
                     {"name": "Model", "value": model_disp}]
            if y:
                props.insert(0, {"name": "Year", "value": str(y)})
            out.append({"compatibilityProperties": props})
    return out
