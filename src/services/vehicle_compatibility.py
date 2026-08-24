"""
Vehicle compatibility helpers for eBay Motors listings.

This module does two things:
1. Parse structured `compatibleProducts` records for the Inventory API
2. Classify Motors fitment as vehicle-specific, universal-fit, or text-only
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

UTC = getattr(datetime, "UTC", timezone.utc)
CURRENT_YEAR = datetime.now(UTC).year

EBAY_MOTORS_CATEGORIES = {
    "174020",  # Trailer Hitches
    "174021",  # Hitch Cargo Carriers
    "262210",  # Running Boards & Nerf Bars
    "262216",  # Roof Racks & Cross Bars
    "262093",  # Tailgate Parts
}

UNIVERSAL_FITMENT_CATEGORIES = {
    "174021",
    "262216",
    "262093",
}

VEHICLE_MAKES = {
    "acura", "audi", "bmw", "buick", "cadillac", "chevrolet", "chevy",
    "chrysler", "dodge", "ford", "gmc", "honda", "hyundai", "infiniti",
    "isuzu", "jeep", "kia", "land rover", "lexus", "lincoln", "mazda",
    "mercedes", "mitsubishi", "nissan", "oldsmobile", "pontiac", "ram",
    "saab", "saturn", "subaru", "suzuki", "toyota", "volkswagen", "volvo",
    "vw",
}

MAKE_NORMALIZE = {
    "chevy": "Chevrolet",
    "vw": "Volkswagen",
    "land rover": "Land Rover",
    "gmc": "GMC",
    "ram": "Ram",
}

KNOWN_MODELS = {
    "4runner": "Toyota",
    "acadia": "GMC",
    "armada": "Nissan",
    "b-series": "Mazda",
    "b2300": "Mazda",
    "b3000": "Mazda",
    "b4000": "Mazda",
    "blazer": "Chevrolet",
    "bronco": "Ford",
    "canyon": "GMC",
    "camry": "Toyota",
    "charger": "Dodge",
    "challenger": "Dodge",
    "cherokee": "Jeep",
    "colorado": "Chevrolet",
    "compass": "Jeep",
    "corolla": "Toyota",
    "crosstrek": "Subaru",
    "cx-5": "Mazda",
    "cx-9": "Mazda",
    "defender 90": "Land Rover",
    "defender 110": "Land Rover",
    "defender 130": "Land Rover",
    "durango": "Dodge",
    "edge": "Ford",
    "equinox": "Chevrolet",
    "escape": "Ford",
    "expedition": "Ford",
    "explorer": "Ford",
    "f-150": "Ford",
    "f150": "Ford",
    "f-250": "Ford",
    "f-350": "Ford",
    "forester": "Subaru",
    "frontier": "Nissan",
    "gladiator": "Jeep",
    "grand cherokee": "Jeep",
    "highlander": "Toyota",
    "hr-v": "Honda",
    "maverick": "Ford",
    "murano": "Nissan",
    "outback": "Subaru",
    "passport": "Honda",
    "pathfinder": "Nissan",
    "palisade": "Hyundai",
    "pilot": "Honda",
    "ranger": "Ford",
    "rav4": "Toyota",
    "renegade": "Jeep",
    "ridgeline": "Honda",
    "rogue": "Nissan",
    "santa fe": "Hyundai",
    "sequoia": "Toyota",
    "sierra": "GMC",
    "silverado": "Chevrolet",
    "suburban": "Chevrolet",
    "tacoma": "Toyota",
    "tahoe": "Chevrolet",
    "terrain": "GMC",
    "titan": "Nissan",
    "trailblazer": "Chevrolet",
    "traverse": "Chevrolet",
    "tundra": "Toyota",
    "tucson": "Hyundai",
    "wrangler": "Jeep",
    "yukon": "GMC",
}

SKIP_MODEL_WORDS = {
    "access", "accessory", "aftermarket", "and", "black", "cab", "coated",
    "crew", "custom", "double", "extended", "fit", "fits", "for", "inch",
    "inches", "kit", "lifted", "matte", "model", "models", "most", "off-road",
    "offroad", "pickup", "pickups", "powder", "rear", "replacement", "right",
    "road", "steel", "step", "steps", "suv", "suvs", "truck", "trucks",
    "tube", "universal", "vehicles", "with", "ensure", "ensures", "fitment",
    "perfect", "points", "zero", "modifications",
}

UNIVERSAL_HINT_PATTERNS = (
    r"\buniversal fit\b",
    r"\bfits all standard\b",
    r"\ball standard open utility trailers\b",
    r"\bstandard class iii/iv 2\b",
    r"\bstandard 2-inch receiver\b",
    r"\bmost factory or aftermarket crossbars\b",
    r"\bno drilling or modification required\b",
    r"\bdesigned for suvs, trucks, crossovers, and rvs\b",
    r"\bideal for suvs, trucks, sedans, and crossovers\b",
)

GENERIC_VEHICLE_PATTERNS = (
    r"\bcrew cab\b",
    r"\bdouble cab\b",
    r"\bextended cab\b",
    r"\baccess cab\b",
    r"\btruck\b",
    r"\bsuv\b",
    r"\bwrangler\b",
    r"\btacoma\b",
    r"\branger\b",
    r"\bgladiator\b",
    r"\bsilverado\b",
    r"\bsierra\b",
    r"\btundra\b",
    r"\b4runner\b",
    r"\bf-150\b",
    r"\bb2300\b",
    r"\bb3000\b",
    r"\bb4000\b",
)


@dataclass
class CompatibilityAnalysis:
    mode: str = "not_applicable"
    compatible_products: List[Dict[str, Any]] = field(default_factory=list)
    fitment_type: Optional[str] = None
    source: str = "none"
    summary: str = ""
    issues: List[str] = field(default_factory=list)
    note: str = ""


class VehicleCompatibilityParser:
    """Extract vehicle compatibility data for Motors products."""

    def extract(self, title: str, description: str, aspects: Dict[str, Any]) -> List[Dict[str, Any]]:
        normalized = _normalize_aspects(aspects)

        explicit_vehicle_fields = self._from_vehicle_fields(normalized)
        if explicit_vehicle_fields:
            return explicit_vehicle_fields

        compat = self._from_aspects(normalized, title, description)
        if compat:
            return compat

        return self._from_text(_clean_text(f"{title} {description}"))

    def _from_vehicle_fields(self, aspects: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        makes = [_normalize_make_value(value) for value in aspects.get("Vehicle Make", [])]
        makes = _sorted_unique([value for value in makes if value])
        year_values = aspects.get("Vehicle Year Range", []) or aspects.get("Vehicle Year", [])
        years = _expand_year_values(year_values)
        model_values = aspects.get("Vehicle Model", [])
        note = _note_from_cab_values(aspects.get("Vehicle Cab Type", []))

        if not makes or not years:
            return []

        models: List[str] = []
        for raw_model in model_values:
            clean_model = _clean_vehicle_text(raw_model)
            if not clean_model:
                continue
            for make in makes:
                if clean_model.lower().startswith(make.lower() + " "):
                    clean_model = clean_model[len(make):].strip()
            for segment in re.split(r"\s*,\s*|\s*/\s*", clean_model):
                segment = _clean_vehicle_text(segment)
                if segment:
                    models.append(_canonical_model(segment))

        entries: List[Dict[str, Any]] = []
        if models:
            mapped = self._match_models_to_makes(makes, models)
            if mapped:
                for make, model_list in mapped.items():
                    for year in years:
                        for model in model_list:
                            entries.append(self._build_entry(year, make, model, note))
                return _dedupe_entries(entries)

        # eBay US Motors rejects a compatibility row without Model.  Keep the
        # item in manual review when source data exposes only Make + Year.
        return []

    def _from_aspects(
        self,
        aspects: Dict[str, List[str]],
        title: str,
        description: str,
    ) -> List[Dict[str, Any]]:
        text = _clean_text(f"{title} {description}")
        makes = _sorted_unique(aspects.get("Compatible Make", []))
        models = _sorted_unique(aspects.get("Compatible Model", []))
        years = _expand_year_values(aspects.get("Compatible Year", []))

        if makes and years:
            # Prefer the text parser when multiple makes are present so we do not
            # incorrectly cross-join Ford/Mazda-style fitment sets.
            parsed_from_text = self._from_text(text)
            if parsed_from_text:
                return parsed_from_text

            entries: List[Dict[str, Any]] = []
            note = _extract_fitment_note(text)
            if models:
                if len(makes) == 1:
                    for year in years:
                        for model in models:
                            entries.append(self._build_entry(year, makes[0], model, note))
                else:
                    mapped = self._match_models_to_makes(makes, models)
                    for make, model_list in mapped.items():
                        for year in years:
                            for model in model_list:
                                entries.append(self._build_entry(year, make, model, note))
            else:
                # eBay US Motors requires Year + Make + Model for manual fitment.
                return []
            return _dedupe_entries(entries)

        compatibility_values = aspects.get("Compatibility", [])
        if compatibility_values:
            compat_text = _clean_text(" ".join(compatibility_values))
            parsed_from_text = self._from_text(compat_text)
            if parsed_from_text:
                return parsed_from_text

        return []

    def _from_text(self, text: str) -> List[Dict[str, Any]]:
        text = _clean_text(text)
        if not text:
            return []

        entries: List[Dict[str, Any]] = []
        note = _extract_fitment_note(text)

        # Pattern: "... Ford F-150, Chevrolet Silverado ... models from 2007 onward"
        shared_year_pattern = re.compile(
            r"(?:including|fits?|compatible with|designed for)\s+"
            r"(.{10,180}?)\s+models?\s+from\s+(\d{4})\s*(?:onward|\+)",
            re.IGNORECASE,
        )
        for match in shared_year_pattern.finditer(text):
            vehicle_list = match.group(1).strip()
            start_year = int(match.group(2))
            for make, model in self._parse_vehicle_list(vehicle_list):
                if not model:
                    continue
                for year in range(start_year, CURRENT_YEAR + 1):
                    entries.append(self._build_entry(str(year), make, model, note))

        # Pattern: "1983-2011 Ford Ranger" / "1994-2009 Mazda B2300/B3000/B4000"
        year_range_pattern = re.compile(
            r"(\d{4})\s*(?:-|–|—|to|through|thru)\s*(\d{4})\s+"
            r"([A-Z][A-Za-z0-9/&\-\s]{2,80})",
            re.IGNORECASE,
        )
        range_clauses = re.split(
            r"\s+\band\b\s+(?=\d{4}\s*(?:-|–|—|to|through|thru)\s*\d{4}\b)",
            text,
            flags=re.IGNORECASE,
        )
        for clause in range_clauses:
            for match in year_range_pattern.finditer(clause):
                start_year = int(match.group(1))
                end_year = int(match.group(2))
                vehicle_text = self._trim_vehicle_segment(match.group(3))
                for make, model in self._parse_vehicle_list(vehicle_text):
                    if not model:
                        continue
                    for year in range(start_year, end_year + 1):
                        entries.append(self._build_entry(str(year), make, model, note))

        # Pattern: "for 2015+ Toyota Tacoma"
        open_year_pattern = re.compile(
            r"(?:for|fits?|compatible with|designed for|installation on)\s+"
            r"(\d{4})\+?\s+([A-Z][A-Za-z0-9/&\-\s]{2,80})",
            re.IGNORECASE,
        )
        for match in open_year_pattern.finditer(text):
            start_year = int(match.group(1))
            vehicle_text = self._trim_vehicle_segment(match.group(2))
            for make, model in self._parse_vehicle_list(vehicle_text):
                if not model:
                    continue
                for year in range(start_year, CURRENT_YEAR + 1):
                    entries.append(self._build_entry(str(year), make, model, note))

        if entries:
            logger.info("[COMPAT] Parsed %s compatibility entries from text", len(entries))
        return _dedupe_entries(entries)

    def _parse_vehicle_list(self, text: str) -> List[Tuple[str, str]]:
        text = _clean_vehicle_text(text)
        if not text:
            return []

        segments = re.split(r"\s*,\s*|\s+\band\b\s+", text, flags=re.IGNORECASE)
        results: List[Tuple[str, str]] = []
        for segment in segments:
            results.extend(self._parse_make_models(segment))
        return results

    def _parse_make_models(self, text: str) -> List[Tuple[str, str]]:
        text = _clean_vehicle_text(text)
        if not text:
            return []

        make, remainder = self._extract_make(text)
        if not make:
            return self._infer_from_known_models(text)

        if not remainder:
            return [(make, "")]

        models = self._extract_models(make, remainder)
        if not models:
            return [(make, "")]
        return [(make, model) for model in models]

    def _extract_make(self, text: str) -> Tuple[Optional[str], str]:
        lowered = text.lower()
        for candidate in sorted(VEHICLE_MAKES, key=len, reverse=True):
            if lowered.startswith(candidate + " ") or lowered == candidate:
                raw_remainder = text[len(candidate):].strip()
                return MAKE_NORMALIZE.get(candidate, candidate.title()), raw_remainder
        return None, text

    def _infer_from_known_models(self, text: str) -> List[Tuple[str, str]]:
        lowered = text.lower()
        results: List[Tuple[str, str]] = []
        for model, make in sorted(KNOWN_MODELS.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"\b{re.escape(model)}\b", lowered):
                results.append((make, _canonical_model(model)))
        return _dedupe_pairs(results)

    def _extract_models(self, make: str, text: str) -> List[str]:
        text = _clean_vehicle_text(text)
        if not text:
            return []

        lowered = text.lower()

        explicit_matches: List[str] = []
        for model_name, model_make in sorted(KNOWN_MODELS.items(), key=lambda item: len(item[0]), reverse=True):
            if model_make != make:
                continue
            if re.search(rf"\b{re.escape(model_name)}\b", lowered):
                explicit_matches.append(_canonical_model(model_name))
                lowered = re.sub(rf"\b{re.escape(model_name)}\b", " ", lowered)

        if explicit_matches:
            return _sorted_unique(explicit_matches)

        token_groups = [seg.strip() for seg in re.split(r"/", text) if seg.strip()]
        if len(token_groups) > 1:
            slash_models: List[str] = []
            for token in token_groups:
                token = self._extract_primary_model_token(token)
                if token:
                    slash_models.append(token)
            if slash_models:
                return _sorted_unique(slash_models)

        model = self._extract_primary_model_token(text)
        return [model] if model else []

    def _extract_primary_model_token(self, text: str) -> Optional[str]:
        words = []
        for token in re.split(r"\s+", text):
            clean = token.strip(" ,;:.()[]-")
            lower = clean.lower()
            if not clean or lower in SKIP_MODEL_WORDS:
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?", clean):
                continue
            if lower in {"jku", "jk", "jt"}:
                continue
            words.append(clean)
            if len(words) >= 2:
                break

        if not words:
            return None
        candidate = " ".join(words)
        return _canonical_model(candidate)

    def _match_models_to_makes(self, makes: List[str], models: List[str]) -> Dict[str, List[str]]:
        mapped = {make: [] for make in makes}
        for model in models:
            canonical = model.strip()
            inferred_make = KNOWN_MODELS.get(canonical.lower())
            if inferred_make and inferred_make in mapped:
                mapped[inferred_make].append(_canonical_model(canonical))
                continue
            if len(makes) == 1:
                mapped[makes[0]].append(_canonical_model(canonical))

        return {make: _sorted_unique(model_list) for make, model_list in mapped.items() if model_list}

    def _trim_vehicle_segment(self, text: str) -> str:
        text = _clean_vehicle_text(text)
        stop_markers = (
            " no drilling",
            " no cutting",
            " perfect for",
            " ideal for",
            " bracket placement",
            " bolt-on",
            " verify cab size",
            " for daily",
            " ensures",
            " ensure",
            " owners ",
            " up to ",
            " rated ",
            " includes ",
            " with ",
            " and all mounting hardware",
        )
        lowered = text.lower()
        cut = len(text)
        year_range_match = re.search(
            r"\b\d{4}\s*(?:-|–|—|to|through|thru)\s*\d{4}\b",
            text,
            flags=re.IGNORECASE,
        )
        if year_range_match:
            cut = min(cut, year_range_match.start())
        for marker in stop_markers:
            idx = lowered.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        return text[:cut].strip(" ,;.")

    def _build_entry(
        self,
        year: str,
        make: str,
        model: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        props = [
            {"name": "Year", "value": str(year)},
            {"name": "Make", "value": make},
        ]
        if model:
            props.append({"name": "Model", "value": model})
        entry: Dict[str, Any] = {"compatibilityProperties": props}
        if note:
            entry["notes"] = note
        return entry


def analyze_ebay_motors_compatibility(
    category_id: str,
    title: str,
    description: str,
    aspects: Dict[str, Any],
    is_motors_store: bool = False,
) -> CompatibilityAnalysis:
    """Classify Motors fitment and return structured compatibility data when possible.

    ``is_motors_store`` bypasses the tree-0 category whitelist for instances that
    publish against the eBay Motors catalog (category tree 100). Those stores use
    tree-100 leaf ids — 33653 Trailer Hitches, 33650 Running Boards, … — which are
    NOT in the tree-0 ``EBAY_MOTORS_CATEGORIES`` set, so without this a hitch would
    silently publish with no fitment. Running the parser on any tree-100 category
    is safe: it self-gates on the presence of vehicle data (a tool or universal
    accessory just yields empty ``compatible_products``, never a bogus fitment).
    Default False preserves the tree-0 gate for the furniture main store.
    """

    category_id = str(category_id or "").strip()
    if not is_motors_store and category_id not in EBAY_MOTORS_CATEGORIES:
        return CompatibilityAnalysis(mode="not_applicable")

    parser = VehicleCompatibilityParser()
    normalized_aspects = _normalize_aspects(aspects)
    compatible_products = parser.extract(title, description, normalized_aspects)
    combined_text = _clean_text(f"{title} {description} {' '.join(_flatten_aspects(normalized_aspects))}")
    note = _extract_fitment_note(combined_text)

    if compatible_products:
        fitment_type = _normalize_fitment_type(
            normalized_aspects.get("Fitment Type", []),
            fallback="Direct Replacement",
        )
        if fitment_type.lower() in {"performance/custom", "performance custom", "universal"}:
            fitment_type = "Direct Replacement"
        return CompatibilityAnalysis(
            mode="specific",
            compatible_products=compatible_products,
            fitment_type=fitment_type,
            source="structured",
            note=note,
            summary=f"{len(compatible_products)} structured compatibility entries ready for eBay Motors",
        )

    if _looks_universal_fit(category_id, combined_text, normalized_aspects):
        return CompatibilityAnalysis(
            mode="universal",
            fitment_type="Universal",
            source="text",
            note=note,
            summary="Universal-fit Motors item; publish without structured compatibleProducts",
        )

    if _has_vehicle_mentions(combined_text, normalized_aspects):
        return CompatibilityAnalysis(
            mode="generic_vehicle",
            fitment_type=_normalize_fitment_type(
                normalized_aspects.get("Fitment Type", []),
                fallback="Performance/Custom",
            ),
            source="text",
            note=note,
            summary="Vehicle mentions detected but year/model precision is incomplete for API fitment",
            issues=["Vehicle hints found, but structured eBay Motors compatibility would be incomplete."],
        )

    issues = ["No vehicle compatibility data detected for this Motors item."]
    return CompatibilityAnalysis(
        mode="needs_review",
        fitment_type=_normalize_fitment_type(
            normalized_aspects.get("Fitment Type", []),
            fallback="Performance/Custom",
        ),
        source="none",
        note=note,
        summary="Compatibility needs manual review",
        issues=issues,
    )


def apply_compatibility_aspects(
    category_id: str,
    aspects: Dict[str, Any],
    analysis: CompatibilityAnalysis,
) -> Dict[str, List[str]]:
    """Normalize Motors-related aspects based on the compatibility analysis."""

    updated = _normalize_aspects(aspects)
    if analysis.mode == "not_applicable":
        return updated

    if analysis.mode == "specific":
        years: List[str] = []
        makes: List[str] = []
        models: List[str] = []
        for entry in analysis.compatible_products:
            prop_map = {
                item.get("name"): item.get("value")
                for item in entry.get("compatibilityProperties", [])
            }
            if prop_map.get("Year"):
                years.append(str(prop_map["Year"]))
            if prop_map.get("Make"):
                makes.append(str(prop_map["Make"]))
            if prop_map.get("Model"):
                models.append(str(prop_map["Model"]))

        if years:
            updated["Compatible Year"] = _sort_year_strings(years)
        if makes:
            updated["Compatible Make"] = _sorted_unique(makes)
        if models:
            updated["Compatible Model"] = _sorted_unique(models)

        current_fitment = _normalize_fitment_type(updated.get("Fitment Type"), "Direct Replacement")
        if current_fitment.lower() in {"performance/custom", "performance custom", "universal"}:
            current_fitment = "Direct Replacement"
        updated["Fitment Type"] = [current_fitment]
    elif analysis.mode == "universal":
        updated["Fitment Type"] = ["Universal"]
        for key in ("Compatible Year", "Compatible Make", "Compatible Model"):
            updated.pop(key, None)
    elif analysis.mode in {"generic_vehicle", "needs_review"}:
        updated["Fitment Type"] = [_normalize_fitment_type(updated.get("Fitment Type"), analysis.fitment_type or "Performance/Custom")]
        for key in ("Compatible Year", "Compatible Make", "Compatible Model"):
            if not updated.get(key):
                updated.pop(key, None)

    model_values = updated.get("Model", [])
    if model_values:
        joined = " ".join(model_values).lower()
        if any(keyword in joined for keyword in ("fits", "compatible", "utility trailer", "open utility")):
            updated.pop("Model", None)

    return updated


def serialize_compatibility_analysis(analysis: CompatibilityAnalysis) -> Dict[str, Any]:
    return {
        "mode": analysis.mode,
        "fitmentType": analysis.fitment_type,
        "source": analysis.source,
        "summary": analysis.summary,
        "issues": list(analysis.issues),
        "note": analysis.note,
        "compatibleProducts": analysis.compatible_products,
    }


def get_compatibility_for_product(title: str, description: str, aspects: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Backward-compatible convenience wrapper."""
    parser = VehicleCompatibilityParser()
    return parser.extract(title, description, aspects)


def _clean_text(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2022": " ",
        "\u00a0": " ",
        "иC": "-",
        "每": "-",
        "бк": " ",
        "б┴": " x ",
        "℅": " x ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_vehicle_text(text: str) -> str:
    text = _clean_text(text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:pickup|pickups|truck|trucks|suv|suvs|vehicle|vehicles)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:crew|double|extended|access)\s+cab\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:in|inch|inches|lb|lbs|mm)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;.")


def _normalize_aspects(aspects: Dict[str, Any]) -> Dict[str, List[str]]:
    normalized: Dict[str, List[str]] = {}
    if not isinstance(aspects, dict):
        return normalized

    for key, value in aspects.items():
        if isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
        elif value is None:
            values = []
        else:
            values = [str(value).strip()]
        if values:
            normalized[str(key)] = values
    return normalized


def _flatten_aspects(aspects: Dict[str, List[str]]) -> List[str]:
    values: List[str] = []
    for aspect_values in aspects.values():
        values.extend(aspect_values)
    return values


def _expand_year_values(values: Iterable[str]) -> List[str]:
    years = set()
    for raw in values:
        text = _clean_text(str(raw))
        if not text:
            continue

        for part in re.split(r"\s*[,;/]\s*", text):
            if not part:
                continue
            range_match = re.fullmatch(
                r"(\d{4})\s*(?:-|–|—|to|through|thru)\s*(\d{4})",
                part,
                flags=re.IGNORECASE,
            )
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2))
                for year in range(start, end + 1):
                    years.add(str(year))
                continue

            plus_match = re.fullmatch(r"(\d{4})\+?", part)
            if plus_match:
                years.add(plus_match.group(1))
                continue

            if re.fullmatch(r"\d{4}", part):
                years.add(part)
    return sorted(years, key=lambda year: int(year))


def _sort_year_strings(values: Iterable[str]) -> List[str]:
    unique = {str(value) for value in values if re.fullmatch(r"\d{4}", str(value))}
    return sorted(unique, key=lambda year: int(year))


def _canonical_model(model: str) -> str:
    text = model.strip()
    if not text:
        return text
    lowered = text.lower()
    if lowered == "4runner":
        return "4Runner"
    if lowered in {"f-150", "f150"}:
        return "F-150"
    if lowered in {"b2300", "b3000", "b4000", "jk", "jku", "jt"}:
        return text.upper()
    if lowered == "b-series":
        return "B-Series"
    if re.fullmatch(r"\d{4}", text):
        return text
    return " ".join(word if any(char.isdigit() for char in word) else word.title() for word in text.split())


def _normalize_fitment_type(values: Optional[Iterable[str]], fallback: str) -> str:
    for value in values or []:
        clean = str(value).strip()
        if clean:
            return clean
    return fallback


def _normalize_make_value(raw: str) -> str:
    lowered = str(raw or "").strip().lower()
    if not lowered:
        return ""
    return MAKE_NORMALIZE.get(lowered, lowered.title())


def _note_from_cab_values(values: Iterable[str]) -> str:
    notes = []
    for value in values or []:
        clean = _clean_text(value)
        if clean and "cab" in clean.lower():
            notes.append(clean)
    if not notes:
        return ""
    return "; ".join(_sorted_unique(notes))[:250]


def _extract_fitment_note(text: str) -> str:
    note_parts: List[str] = []
    patterns = (
        (r"\bcrew cab(?: only)?\b", "Crew Cab only"),
        (r"\bnot compatible with\b[^.]{0,100}", None),
        (r"\bverify cab size\b[^.]{0,60}", None),
        (r"\bclass iii/iv 2\b[^.]{0,40}", None),
    )
    for pattern, replacement in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            note = replacement or match.group(0).strip(" ,;.")
            note = re.sub(r"\b\d+(?:\.\d+)?\b", "", note).strip(" ,;.)(")
            if note:
                note_parts.append(note[0].upper() + note[1:])
    note = "; ".join(_sorted_unique(note_parts))
    return note[:250]


def _looks_universal_fit(
    category_id: str,
    text: str,
    aspects: Dict[str, List[str]],
) -> bool:
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in UNIVERSAL_HINT_PATTERNS):
        return True

    if category_id in UNIVERSAL_FITMENT_CATEGORIES:
        compatibility_values = " ".join(aspects.get("Compatibility", [])).lower()
        generic_vehicle_classes = {"suv", "pickup truck", "rv", "crossover", "sedan"}
        if compatibility_values and all(any(cls in value for cls in generic_vehicle_classes) for value in compatibility_values.split(",") if value):
            return True

    return False


def _has_vehicle_mentions(text: str, aspects: Dict[str, List[str]]) -> bool:
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in GENERIC_VEHICLE_PATTERNS):
        return True

    if any(re.search(rf"\b{re.escape(make)}\b", lowered) for make in VEHICLE_MAKES):
        return True
    if any(re.search(rf"\b{re.escape(model)}\b", lowered) for model in KNOWN_MODELS):
        return True

    for key in ("Compatibility", "Compatible Make", "Compatible Model", "Compatible Year"):
        if aspects.get(key):
            return True
    return False


def _dedupe_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for entry in entries:
        props = tuple(
            (prop.get("name"), str(prop.get("value")))
            for prop in entry.get("compatibilityProperties", [])
        )
        note = str(entry.get("notes", "")).strip()
        key = (props, note)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _dedupe_pairs(pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    seen = set()
    deduped: List[Tuple[str, str]] = []
    for make, model in pairs:
        key = (make, model)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((make, model))
    return deduped


def _sorted_unique(values: Iterable[str]) -> List[str]:
    unique = {str(value).strip() for value in values if str(value).strip()}
    return sorted(unique)
