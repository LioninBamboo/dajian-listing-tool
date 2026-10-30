"""Vehicle fitment (车型适用性) parsing + QC for Motors listings.

GigaCloud provides no structured Year/Make/Model, so the only fitment signal
lives in the title (e.g. "Running Boards For 2019-2026 Chevy Silverado 1500").
This module:

  * parse_title_fitment(title)  -> list[VehicleFit]  (empty = universal)
  * classify_fitment(title)     -> "vehicle_specific" | "universal"
  * check_fitment_qc(title, live_compat) -> list[FitmentIssue]

Where upstream ``vehicle_compatibility`` already parses the same Year/Make/Model
signal, this module calls it and collapses the result. Title-only cases with no
year (e.g. "Jeep Wrangler JL") still use the local scanner so we do not invent
a year.

QC catches three failure modes:
  MISSING   — title names a specific vehicle but the live ItemCompatibilityList
              is empty (buyer can't filter by their car; eBay may suppress it).
  MISMATCH  — a make/model/year in the live compatibility list is NOT supported
              by the title text (possible wrong fitment).
  SUSPECT_UNIVERSAL — title says "universal / fits most" yet also names a
              specific vehicle+year (contradictory claim, human review).

Building/applying the ItemCompatibilityList is intentionally a separate step
(``build_compatible_products`` / ``resolve_apply_fitment``) so QC can run
read-only first. ``--apply`` never invents fitment from the title alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from src.services.vehicle_compatibility import (
    CURRENT_YEAR,
    KNOWN_MODELS,
    MAKE_NORMALIZE,
    VehicleCompatibilityParser,
)

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
# Numbers followed by a unit are dimensions / weights, not model years.
_YEAR_UNIT_AFTER = r"(?!\s*(?:in(?:ch(?:es)?)?\b|cm\b|mm\b|lbs?\b|pounds?\b|kg\b|v\b|volts?\b|ft\b|feet\b|\"|''))"
# 2005-2023 / 2019–2026 / 2007-2026 / 2024 2025 / 2021+
_YEAR_RANGE = re.compile(rf"\b({_YEAR})\s*[-–—]\s*({_YEAR})\b{_YEAR_UNIT_AFTER}")
_YEAR_RANGE_SPACE = re.compile(rf"\b({_YEAR})\s+({_YEAR})\b{_YEAR_UNIT_AFTER}")
_YEAR_PLUS = re.compile(rf"\b({_YEAR})\+")
_YEAR_SINGLE = re.compile(rf"\b({_YEAR})\b{_YEAR_UNIT_AFTER}")
_RAM_TOOL_TAIL = re.compile(r"\s+(?:jack|press|pump|cylinder|hoist)\b")


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
    compact = t.replace(" ", "")
    if compact in _MODEL_CANON_MAP:
        return _MODEL_CANON_MAP[compact]
    return t


def _canonical_make(token: str) -> str:
    lowered = _norm(token)
    if lowered in _MAKE_ALIASES:
        return _MAKE_ALIASES[lowered]
    mapped = MAKE_NORMALIZE.get(lowered)
    if mapped:
        return mapped.lower()
    return lowered


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
    if "-" in canon:
        candidates.add(canon.replace("-", ""))
    for cand in candidates:
        if not cand:
            continue
        if re.search(rf"\b{re.escape(cand)}\b", title_norm):
            return True
        compact_title = re.sub(r"[^a-z0-9]", "", title_norm)
        compact_cand = re.sub(r"[^a-z0-9]", "", cand)
        if compact_cand and compact_cand in compact_title:
            return True
    return False


def _distinctive_models() -> list[tuple[str, str]]:
    """Return (model, make) pairs long enough to identify a make without the brand word.

    Short numeric tokens like ``1500`` are skipped — they appear in many truck titles.
    Dodge is never inferred from a Ram model unless the title says "dodge".
    """
    pairs: list[tuple[str, str]] = []
    for canon, cfg in _MAKES.items():
        for model in cfg["models"]:
            if model.isdigit():
                continue
            if len(model) < 5 and _canonical_model_token(model) not in _MODEL_CANON_MAP.values():
                continue
            pairs.append((model, canon))
    for model, make in KNOWN_MODELS.items():
        canon = _canonical_make(make)
        if canon == "dodge" and "ram" in model:
            continue
        pairs.append((model, canon))
    pairs.sort(key=lambda item: -len(item[0]))
    return pairs


def _years_near(text: str, start: int) -> tuple[Optional[int], Optional[int]]:
    around = text[max(0, start - 40):start + 70]
    year_range = _YEAR_RANGE.search(around)
    if year_range:
        return int(year_range.group(1)), int(year_range.group(2))
    space_range = _YEAR_RANGE_SPACE.search(around)
    if space_range:
        return int(space_range.group(1)), int(space_range.group(2))
    plus = _YEAR_PLUS.search(around)
    if plus:
        return int(plus.group(1)), int(CURRENT_YEAR)
    single = _YEAR_SINGLE.search(around)
    if single:
        year = int(single.group(1))
        return year, year
    return None, None


def _fits_from_upstream_entries(entries: Sequence[Mapping[str, Any]]) -> list[VehicleFit]:
    """Collapse Year/Make/Model rows from VehicleCompatibilityParser into ranges."""
    grouped: dict[tuple[str, Optional[str]], list[int]] = {}
    for entry in entries or []:
        props = {
            str(p.get("name") or "").strip().lower(): str(p.get("value") or "").strip()
            for p in (entry or {}).get("compatibilityProperties") or []
        }
        make = _canonical_make(props.get("make") or "")
        model = _canonical_model_token(props.get("model") or "") or None
        if not make:
            continue
        year_raw = props.get("year") or ""
        try:
            year = int(year_raw) if year_raw else None
        except ValueError:
            year = None
        key = (make, model)
        grouped.setdefault(key, [])
        if year is not None:
            grouped[key].append(year)
    out: list[VehicleFit] = []
    for (make, model), years in grouped.items():
        if years:
            out.append(VehicleFit(make=make, model=model, year_from=min(years), year_to=max(years)))
        else:
            out.append(VehicleFit(make=make, model=model, year_from=None, year_to=None))
    return out


def _scan_title_makes(title: str) -> list[VehicleFit]:
    """Title scanner for make/model mentions, including year-less titles."""
    t = _norm(title)
    fits: list[VehicleFit] = []
    for alias, canon in sorted(_MAKE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if canon == "dodge" and not re.search(r"\bdodge\b", t):
            continue
        for m in re.finditer(rf"\b{re.escape(alias)}\b", t):
            if alias == "ram" and _RAM_TOOL_TAIL.match(t[m.end():]):
                continue
            start = m.start()
            window = t[start:start + 70]
            model = None
            for cand in _MAKES[canon]["models"]:
                if cand in window:
                    model = _canonical_model_token(cand)
                    break
            yf, yt = _years_near(t, start)
            fits.append(VehicleFit(make=canon, model=model, year_from=yf, year_to=yt))
    have = {(f.make, _canonical_model_token(f.model or "")) for f in fits}
    for model, canon in _distinctive_models():
        if canon == "dodge" and not re.search(r"\bdodge\b", t):
            continue
        if any(
            f.make == canon
            and f.model
            and (
                _canonical_model_token(f.model) == _canonical_model_token(model)
                or model in f.model
                or f.model in model
            )
            for f in fits
        ):
            continue
        for m in re.finditer(rf"\b{re.escape(model)}\b", t):
            key = (canon, _canonical_model_token(model))
            if key in have:
                break
            yf, yt = _years_near(t, m.start())
            fits.append(VehicleFit(make=canon, model=_canonical_model_token(model), year_from=yf, year_to=yt))
            have.add(key)
            break
    return fits


def _collapse_alias_fits(fits: Sequence[VehicleFit]) -> list[VehicleFit]:
    """Dedupe F-150 / F150 and drop a short model when a longer one exists."""
    normalized = [
        VehicleFit(
            make=f.make,
            model=_canonical_model_token(f.model) if f.model else f.model,
            year_from=f.year_from,
            year_to=f.year_to,
        )
        for f in fits
    ]
    collapsed: list[VehicleFit] = []
    seen: set[tuple[str, str, Optional[int], Optional[int]]] = set()
    for f in normalized:
        if any(
            o.make == f.make
            and o.model
            and f.model
            and o.model != f.model
            and f.model in o.model
            and o.year_from == f.year_from
            and o.year_to == f.year_to
            for o in normalized
        ):
            continue
        key = (f.make, f.model or "", f.year_from, f.year_to)
        if key in seen:
            continue
        seen.add(key)
        collapsed.append(f)
    return collapsed


def parse_title_fitment(title: str) -> list[VehicleFit]:
    """Extract concrete (make [+model] [+year-range]) vehicles named in the title.

    Returns [] when the title names no specific make (i.e. universal). A make with
    no year is still returned (year None) — e.g. 'for Jeep Wrangler'.

    The title scanner is the QC source of truth (word-boundary / unit-aware).
    Upstream ``VehicleCompatibilityParser.extract`` is reused when the scanner
    finds nothing, or to fill a missing year range for the same make+model.
    """
    scanned = _collapse_alias_fits(_scan_title_makes(title))
    upstream_fits = _collapse_alias_fits(
        _fits_from_upstream_entries(
            VehicleCompatibilityParser().extract(str(title or ""), "", {})
        )
    )
    if not scanned:
        return upstream_fits
    filled: list[VehicleFit] = []
    for f in scanned:
        if f.year_from is None:
            match = next(
                (
                    u
                    for u in upstream_fits
                    if u.make == f.make
                    and _canonical_model_token(u.model or "") == _canonical_model_token(f.model or "")
                    and u.year_from is not None
                ),
                None,
            )
            if match:
                filled.append(
                    VehicleFit(make=f.make, model=f.model, year_from=match.year_from, year_to=match.year_to)
                )
                continue
        filled.append(f)
    return filled


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


def _live_years(live_compat: Optional[Sequence[Mapping[str, Any]]]) -> set[int]:
    years: set[int] = set()
    for entry in live_compat or []:
        props = (entry or {}).get("compatibilityProperties") or (entry or {}).get("nameValueList") or []
        for p in props:
            if str(p.get("name", "")).lower() != "year":
                continue
            raw = str(p.get("value", "")).strip()
            if raw.isdigit():
                years.add(int(raw))
    return years


def _title_years(parsed: Sequence[VehicleFit]) -> set[int]:
    years: set[int] = set()
    for fit in parsed:
        if fit.year_from and fit.year_to:
            years.update(range(int(fit.year_from), int(fit.year_to) + 1))
        elif fit.year_from:
            years.add(int(fit.year_from))
    return years


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
            if _canonical_model_token(tok) and _title_mentions_token(t, tok):
                continue
            issues.append(FitmentIssue(
                code="fitment_mismatch",
                severity="HIGH",
                detail=f"适配列表含 '{tok}' 但标题未提及（疑似错挂）",
                parsed=[f.as_dict() for f in parsed],
            ))
            break

    # 2b) live year coverage is broader than the title year range (F1).
    title_year_set = _title_years(parsed)
    live_year_set = _live_years(live_compat)
    if title_year_set and live_year_set and (live_year_set - title_year_set):
        extra = sorted(live_year_set - title_year_set)
        issues.append(FitmentIssue(
            code="fitment_mismatch",
            severity="HIGH",
            detail=(
                f"标题年份 {min(title_year_set)}-{max(title_year_set)} "
                f"与 live 适配年份不符（多出 {extra[0]}-{extra[-1]}，需人工复核）"
            ),
            parsed=[f.as_dict() for f in parsed],
        ))

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
        if not f.year_from:
            continue
        years = (
            [f.year_from]
            if f.year_from and not f.year_to
            else list(range(f.year_from, f.year_to + 1))
        )
        make_disp = "Chevrolet" if f.make == "chevrolet" else f.make.title()
        model_disp = _CATALOG_MODEL.get(f.model, f.model.title())
        for y in years:
            props = [
                {"name": "Year", "value": str(y)},
                {"name": "Make", "value": make_disp},
                {"name": "Model", "value": model_disp},
            ]
            out.append({"compatibilityProperties": props})
    return out


def _fit_key(fit: VehicleFit) -> tuple[str, str]:
    return (fit.make, _canonical_model_token(fit.model or ""))


def _year_ranges_overlap(left: VehicleFit, right: VehicleFit) -> bool:
    # Apply requires a year range on both sides. Make+Model-only is not a match.
    if left.year_from is None or right.year_from is None:
        return False
    left_to = left.year_to if left.year_to is not None else left.year_from
    right_to = right.year_to if right.year_to is not None else right.year_from
    return not (left_to < right.year_from or right_to < left.year_from)


def _stored_mismatches_giga_title(
    title: str,
    stored_compatible_products: Sequence[Mapping[str, Any]],
) -> bool:
    """True when a stored list is not supported by the GIGA/listing title."""
    return any(
        issue.code == "fitment_mismatch"
        for issue in check_fitment_qc(title, stored_compatible_products)
    )


def source_supports_title_fitment(
    title: str,
    *,
    source_title: str = "",
    source_description: str = "",
    stored_compatible_products: Optional[Sequence[Mapping[str, Any]]] = None,
) -> bool:
    """True when GIGA source has the same make + model + year range."""
    check_title = (source_title or title or "").strip()
    if stored_compatible_products:
        if not any(f.year_from for f in parse_title_fitment(check_title)):
            return False
        return not _stored_mismatches_giga_title(check_title, stored_compatible_products)
    title_fits = [f for f in parse_title_fitment(title) if f.make and f.model and f.year_from]
    if not title_fits:
        return False
    source_blob = f"{source_title or ''} {source_description or ''}".strip()
    source_fits = parse_title_fitment(source_blob) if source_blob else []
    # Reuse the upstream text parser on the stored GIGA source (no invented category).
    source_fits.extend(
        _fits_from_upstream_entries(
            VehicleCompatibilityParser().extract(source_title or "", source_description or "", {})
        )
    )
    for needed in title_fits:
        if not any(
            _fit_key(needed) == _fit_key(have)
            and have.year_from is not None
            and _year_ranges_overlap(needed, have)
            for have in source_fits
        ):
            return False
    return bool(source_fits)


def resolve_apply_fitment(
    title: str,
    *,
    source_title: str = "",
    source_description: str = "",
    stored_compatible_products: Optional[Sequence[Mapping[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], str]:
    """Choose a fitment list to push, or skip.

    A stored list is pushed only when it matches the GIGA source title
    (``check_fitment_qc`` finds no ``fitment_mismatch``) and that title
    names a year range the list can be confirmed against. Make+Model rows
    without a year are never applied.
    """
    stored = [dict(entry) for entry in (stored_compatible_products or []) if entry]
    check_title = (source_title or title or "").strip()
    if stored:
        if not any(f.year_from for f in parse_title_fitment(check_title)):
            return [], "skipped_title_no_year"
        if _stored_mismatches_giga_title(check_title, stored):
            return [], "skipped_stored_mismatch"
        if not all(
            any(
                str(prop.get("name") or "").lower() == "year"
                and str(prop.get("value") or "").strip()
                for prop in (entry.get("compatibilityProperties") or [])
            )
            for entry in stored
        ):
            return [], "skipped_no_source_fitment"
        return stored, ""
    if not source_supports_title_fitment(
        title,
        source_title=source_title,
        source_description=source_description,
        stored_compatible_products=stored,
    ):
        return [], "skipped_no_source_fitment"
    built = build_compatible_products(title)
    if not built:
        return [], "skipped_no_source_fitment"
    return built, ""
