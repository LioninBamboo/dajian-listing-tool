"""READY-draft unsupported-claim scrub.

Reuses the existing claim-diff, quality-gate, and FactSheet detectors.
Removes or source-corrects only flagged unsupported claims on local READY
/ unpublished ERP drafts. Never writes to eBay, never invents measurements,
and never touches image URLs (GIGA x-cc/x-cu/x-ct/x-cs stay byte-identical).
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from pathlib import Path

from src.services.listing_qc import (
    _capacity_mentions,
    _plain_blob,
    capacity_window_is_labelled_load,
    capacity_window_is_product_weight,
    is_weight_aspect_key,
    mass_context_family,
    motors_claim_invent_issues,
    motors_deterministic_fact_violations,
    validate_listing_quality,
)
from src.utils.claim_diff_engine import (
    COUNTABLE_CLAIMS,
    FEATURE_CLAIM_PATTERNS,
    build_source_constraints,
    detect_claim_violations,
)
from src.utils.claim_strip import claim_matches_text, claim_pattern, collapse_ws, normalize_entities
from src.utils.listing_quality_gate import (
    ASSEMBLY_NO_REQUIRED_PATTERNS,
    CLAIM_PATTERNS,
)
from src.utils.logs_codec import append_log

READY_STATUSES = frozenset({"READY", "READY_TO_PUBLISH"})
LIVE_STATUSES = frozenset({"PUBLISHED"})
SCRUB_QUALITY_PREFIXES = ("unsupported_", "assembly_")
SKIP_QUALITY_PREFIXES = ("missing_supported_", "assembly_description_missing")
SOLID_WOOD_SPECIES_RE = (
    re.compile(r"\brubber(?:\s|-)?wood\b", re.I),
    re.compile(r"\beucalyptus\b", re.I),
    re.compile(r"\bacacia\b", re.I),
)
ENGINEERED_WOOD_RE = (
    re.compile(r"\bmdf\b", re.I),
    re.compile(r"\bparticle(?:\s|-)?boards?\b", re.I),
    re.compile(r"\bveneers?\b", re.I),
    re.compile(r"\bengineered\s+wood\b", re.I),
    re.compile(r"\bplywood\b", re.I),
)
SOLID_WOOD_CLAIM_RE = re.compile(r"\bsolid\s+wood\b", re.I)
LEATHER_PREFIX_RE = re.compile(r"(?:pu|p\.u\.|faux|vegan|bonded)[\s-]*$", re.I)
IMAGE_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
GIGA_IMAGE_PARAMS = ("x-cc", "x-cu", "x-ct", "x-cs")
BARE_NO_ASSEMBLY_RE = re.compile(r"\bno\s+assembly\b", re.I)
_LOAD_NUMBER_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(?P<num2>\d+(?:\.\d+)?))?",
)
_MATERIAL_FIELD_RE = re.compile(r"^material$", re.I)
_MULTI_MATERIAL_SPLIT_RE = re.compile(r"\s*(?:,|/|&|\band\b|\+)\s*", re.I)
FABRIC_FAMILY = frozenset({
    "chenille", "corduroy", "velvet", "polyester", "cotton", "linen",
    "oxford", "canvas", "boucle", "suede", "mesh", "nylon", "upholstery",
    "microsuede", "micro suede", "micro-suede", "microfiber", "microfibre",
})
_QTY_CLAIM_RE = re.compile(
    r"(?:claimed\s+)?(?P<n>\d+)[\s-]*(?P<noun>[a-z]+)(?:.*source has (?P<src>\d+))?",
    re.I,
)
_EMPTY_LI_RE = re.compile(r"<li\b[^>]*>\s*(?:<(?:strong|b|em|span)[^>]*>\s*</(?:strong|b|em|span)>\s*)*</li>", re.I)
_EMPTY_P_RE = re.compile(r"<p\b[^>]*>\s*</p>", re.I)
# Do not include `/` — `https://` would collapse to `https:,`.
_MULTI_SEP_RE = re.compile(r"(?:\s*[,;|]\s*){2,}")
_AND_AND_RE = re.compile(r"\b(?:and|or)\s+(?:and|or)\b", re.I)
_SPACE_PUNCT_RE = re.compile(r"\s+([,.;:])")
_DOUBLE_SPACE_RE = re.compile(r"[ \t]{2,}")
_LEAD_SEP_RE = re.compile(r"^[\s,;:./\-–—]+")
_TRAIL_SEP_RE = re.compile(r"[\s,;:/\-–—]+$")
_FW_TABLE = str.maketrans({
    "＆": "&",
    "，": ",",
    "．": ".",
    "　": " ",
    **{chr(0xFF10 + i): str(i) for i in range(10)},
})
MULTIPLE_MATCH_POLICY = (
    "First left-to-right match is edited first; every remaining match of the "
    "same claim is then edited. Later flagged claims see the updated text."
)

_LOCKED_MARKERS = ("database is locked", "database is busy")


@dataclass
class FlaggedClaim:
    claim_type: str
    claim_text: str
    source_evidence: str
    location: str
    detector: str
    severity: str
    code: str | None = None


@dataclass
class FieldDiff:
    field: str
    before: str
    after: str
    action: str
    claim_text: str
    source_text: str
    detector: str


@dataclass
class ScrubResult:
    sku: str
    status: str
    before_blockers: list[str] = field(default_factory=list)
    after_blockers: list[str] = field(default_factory=list)
    diffs: list[FieldDiff] = field(default_factory=list)
    needs_manual_reason: str | None = None
    images_unchanged: bool = True
    row_status: str = ""
    listing_id: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def normalize_once(text: Any) -> str:
    """Normalize once, same on both sides of a comparison."""
    raw = "" if text is None else str(text)
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.translate(_FW_TABLE)
    raw = normalize_entities(raw)
    raw = raw.replace("\xa0", " ").replace("\u3000", " ").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"\s*&\s*", " and ", raw)
    raw = collapse_ws(raw)
    return raw


def _stem_plural(token: str) -> str:
    lowered = token.casefold()
    if len(lowered) > 3 and lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def terms_equivalent(left: Any, right: Any) -> bool:
    a = [_stem_plural(part) for part in normalize_once(left).casefold().split() if part]
    b = [_stem_plural(part) for part in normalize_once(right).casefold().split() if part]
    return bool(a) and a == b


def _json_obj(value: Any) -> Any:
    if value is None or value == "":
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, (dict, list)) else {}


def _as_mapping(value: Any) -> dict[str, Any]:
    parsed = _json_obj(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _image_urls(*blobs: Any) -> list[str]:
    found: list[str] = []
    for blob in blobs:
        if blob is None:
            continue
        if isinstance(blob, (list, tuple, set)):
            for item in blob:
                found.extend(_image_urls(item))
            continue
        if isinstance(blob, Mapping):
            for item in blob.values():
                found.extend(_image_urls(item))
            continue
        text = str(blob)
        found.extend(IMAGE_URL_RE.findall(text))
    return found


def _url_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in IMAGE_URL_RE.finditer(text or "")]


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def _match_is_leather_guarded(text: str, match: re.Match[str]) -> bool:
    if match.group(0).strip().casefold() != "leather":
        return True
    return _leather_match_ok(text, match.start())


def _match_is_product_weight(text: str, match: re.Match[str]) -> bool:
    return mass_context_family(text, match.start(), match.end()) == "weight"


def _allow_edit_match(
    text: str,
    match: re.Match[str],
    urls: list[tuple[int, int]],
    *,
    skip_weight_values: bool = False,
) -> bool:
    if _overlaps((match.start(), match.end()), urls):
        return False
    if not _match_is_leather_guarded(text, match):
        return False
    if skip_weight_values and _match_is_product_weight(text, match):
        return False
    return True


def _sub_outside_urls(
    pattern: re.Pattern[str],
    text: str,
    repl: str = "",
    *,
    skip_weight_values: bool = False,
) -> str:
    if not text:
        return text or ""
    urls = _url_spans(text)
    parts: list[str] = []
    last = 0
    for match in pattern.finditer(text):
        if not _allow_edit_match(text, match, urls, skip_weight_values=skip_weight_values):
            continue
        parts.append(text[last:match.start()])
        parts.append(repl)
        last = match.end()
    parts.append(text[last:])
    return "".join(parts)


def _apply_outside_urls(text: str, transform: Callable[[str], str]) -> str:
    if not text:
        return text or ""
    urls = _url_spans(text)
    if not urls:
        return transform(text)
    parts: list[str] = []
    last = 0
    for start, end in urls:
        parts.append(transform(text[last:start]))
        parts.append(text[start:end])
        last = end
    parts.append(transform(text[last:]))
    return "".join(parts)


def cleanup_fragment(text: str) -> str:
    def _clean(chunk: str) -> str:
        cleaned = _EMPTY_LI_RE.sub("", chunk or "")
        cleaned = re.sub(r"<p\b[^>]*>\s*[.,;:]\s*</p>", "", cleaned, flags=re.I)
        cleaned = _EMPTY_P_RE.sub("", cleaned)
        cleaned = _MULTI_SEP_RE.sub(", ", cleaned)
        cleaned = _AND_AND_RE.sub(" and ", cleaned)
        cleaned = re.sub(r"\(\s*\)", "", cleaned)
        cleaned = _SPACE_PUNCT_RE.sub(r"\1", cleaned)
        cleaned = _DOUBLE_SPACE_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+\n", "\n", cleaned)
        return cleaned

    return _apply_outside_urls(text or "", _clean).strip()


def _title_survives(title: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", title or ""))


def _source_blob(source: Mapping[str, Any]) -> str:
    return _plain_blob(
        source.get("title"),
        source.get("description"),
        source.get("attributes") if isinstance(source.get("attributes"), Mapping) else {},
        source.get("specs") if isinstance(source.get("specs"), Mapping) else {},
    )


def source_has_solid_wood_species(source_blob: str) -> bool:
    return any(pattern.search(source_blob or "") for pattern in SOLID_WOOD_SPECIES_RE)


def source_has_engineered_wood(source_blob: str) -> bool:
    return any(pattern.search(source_blob or "") for pattern in ENGINEERED_WOOD_RE)


def skip_solid_wood_fix(claim: FlaggedClaim, source_blob: str) -> bool:
    """Rubberwood / eucalyptus / acacia count as solid wood — do not 'fix' them.

    MDF / particleboard / veneer / engineered wood do not count; those stay flagged.
    """
    text = f"{claim.claim_text} {claim.code or ''}"
    if not SOLID_WOOD_CLAIM_RE.search(text):
        return False
    if source_has_engineered_wood(source_blob) and not source_has_solid_wood_species(source_blob):
        return False
    return source_has_solid_wood_species(source_blob)


def _leather_match_ok(text: str, start: int) -> bool:
    prefix = text[max(0, start - 12):start]
    return not LEATHER_PREFIX_RE.search(prefix)


def _safe_claim_pattern(claim: str) -> re.Pattern[str] | None:
    pattern = claim_pattern(claim)
    if pattern is None and claim:
        words = [re.escape(part) for part in re.split(r"[\s\-_]+", claim.strip()) if part]
        if words:
            inner = r"[\s\-_]+".join(words)
            pattern = re.compile(rf"(?<![A-Za-z0-9-]){inner}(?![A-Za-z0-9])", re.IGNORECASE)
    return pattern


def whole_value_match(claim: str, text: str) -> bool:
    """Word-boundary match: `stable` is not `table`; `Leather` is not `PU Leather`."""
    if not claim or text is None:
        return False
    if terms_equivalent(claim, "leather") or normalize_once(claim).casefold() == "leather":
        pattern = re.compile(r"(?<![A-Za-z0-9])leather(?![A-Za-z0-9])", re.I)
        for match in pattern.finditer(str(text)):
            if _leather_match_ok(text, match.start()):
                return True
        return False
    return claim_matches_text(claim, str(text))


def collect_flagged_claims(
    candidate: Mapping[str, Any] | None,
    source: Mapping[str, Any] | None,
) -> list[FlaggedClaim]:
    """Run the existing detectors. This is not a parallel detector."""
    candidate = dict(candidate or {})
    source = dict(source or {})
    aspects = candidate.get("aspects") if isinstance(candidate.get("aspects"), Mapping) else {}
    source_title = str(source.get("title") or "")
    source_description = str(source.get("description") or "")
    source_attributes = source.get("attributes") if isinstance(source.get("attributes"), Mapping) else {}
    source_specs = source.get("specs") if isinstance(source.get("specs"), Mapping) else {}
    found: list[FlaggedClaim] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(claim: FlaggedClaim) -> None:
        key = (claim.detector, claim.claim_type, normalize_once(claim.claim_text).casefold())
        if key in seen:
            return
        seen.add(key)
        found.append(claim)

    constraints = build_source_constraints(
        attrs=source_attributes,
        specs=source_specs,
        source_description=source_description,
        source_title=source_title,
    )
    for violation in detect_claim_violations(
        constraints,
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        dict(aspects),
    ):
        if str(violation.severity or "").upper() not in {"CRITICAL", "HIGH"}:
            continue
        _add(
            FlaggedClaim(
                claim_type=str(violation.claim_type or ""),
                claim_text=str(violation.claim_text or ""),
                source_evidence=str(violation.source_evidence or "NOT_FOUND"),
                location=str(violation.location or ""),
                detector="claim_diff",
                severity=str(violation.severity or ""),
            )
        )

    for issue in motors_claim_invent_issues(
        candidate,
        source_title=source_title,
        source_description=source_description,
        source_attributes=source_attributes,
        source_specs=source_specs,
    ):
        message = str(issue.get("message") or "")
        _add(
            FlaggedClaim(
                claim_type=str(issue.get("code") or "claim_invent"),
                claim_text=message,
                source_evidence="NOT_FOUND",
                location=str(issue.get("field") or ""),
                detector="claim_invent",
                severity=str(issue.get("severity") or "CRITICAL"),
                code=str(issue.get("code") or ""),
            )
        )

    for violation in motors_deterministic_fact_violations(
        candidate,
        source_title=source_title,
        source_description=source_description,
        source_attributes=source_attributes,
        source_specs=source_specs,
    ):
        if str(violation.get("severity") or "").upper() not in {"CRITICAL", "HIGH"}:
            continue
        _add(
            FlaggedClaim(
                claim_type=str(violation.get("claim_type") or ""),
                claim_text=str(violation.get("claim_text") or ""),
                source_evidence=str(violation.get("source_evidence") or "NOT_FOUND"),
                location="generated content",
                detector="motors_fact",
                severity=str(violation.get("severity") or ""),
            )
        )

    try:
        quality_issues = validate_listing_quality(
            dict(candidate),
            source_title=source_title,
            source_description=source_description,
            attributes=source_attributes,
            specs=source_specs,
            images=list(candidate.get("images") or []),
            videos=list(candidate.get("videos") or []),
        )
    except Exception:
        quality_issues = []
    for issue in quality_issues or []:
        code = str(getattr(issue, "code", "") or "")
        if any(code.startswith(prefix) for prefix in SKIP_QUALITY_PREFIXES):
            continue
        if not any(code.startswith(prefix) for prefix in SCRUB_QUALITY_PREFIXES):
            continue
        _add(
            FlaggedClaim(
                claim_type=code,
                claim_text=str(getattr(issue, "message", "") or code),
                source_evidence="NOT_FOUND",
                location=str(getattr(issue, "field", "") or ""),
                detector="quality_gate",
                severity=str(getattr(issue, "severity", "BLOCKER") or "BLOCKER"),
                code=code,
            )
        )
    return found


def _blocker_texts(claims: list[FlaggedClaim]) -> list[str]:
    return list(dict.fromkeys(f"{item.claim_type}:{item.claim_text}" for item in claims if item.claim_text))


def _source_material_field_value(source: Mapping[str, Any] | None) -> str | None:
    """Whole Material field only. Never guess a fabric word from free text."""
    if not isinstance(source, Mapping):
        return None
    for mapping_key in ("attributes", "specs"):
        mapping = source.get(mapping_key)
        if not isinstance(mapping, Mapping):
            continue
        for key, value in mapping.items():
            if not _MATERIAL_FIELD_RE.match(str(key or "").strip()):
                continue
            raw_values = value if isinstance(value, (list, tuple, set)) else [value]
            for raw in raw_values:
                text = str(raw or "").strip()
                if text:
                    return text
    return None


def _single_fabric_material(value: str | None) -> str | None:
    """Replace Fabric only with one vocab fabric word. Mixed/non-fabric → None."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in _MULTI_MATERIAL_SPLIT_RE.split(text) if part.strip()]
    if len(parts) != 1:
        return None
    whole = normalize_once(parts[0]).casefold()
    for term in FABRIC_FAMILY:
        if whole == term or terms_equivalent(whole, term):
            return parts[0]
    return None


def _whole_number_in_text(wanted: str, text: str) -> bool:
    if not wanted or not text:
        return False
    return bool(re.search(rf"(?<![\d.]){re.escape(wanted)}(?![\d.])", text))


def _source_capacity_raw(source_blob: str, family: str, evidence: str) -> str | None:
    wanted = normalize_once(evidence).casefold()
    if not wanted or wanted == "not_found":
        return None
    for number, fam, raw in _capacity_mentions(source_blob):
        if fam != family:
            continue
        number_n = normalize_once(number).casefold()
        raw_n = normalize_once(raw).casefold()
        if number_n == wanted or _whole_number_in_text(wanted, raw_n):
            return raw
    return None


def _labelled_load_values(source_blob: str) -> list[tuple[str, str]]:
    """Distinct labelled capacity/load figures in source order."""
    blob = source_blob or ""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"\b(?P<num>\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(?P<num2>\d+(?:\.\d+)?))?\s*"
        r"(?P<unit>lbs?|pounds?|kgs?|kilograms?)\b",
        blob,
        re.I,
    ):
        left = blob[max(0, match.start() - 56):match.start()]
        right = blob[match.end():match.end() + 16]
        if mass_context_family(blob, match.start(), match.end()) != "load":
            continue
        if not (
            capacity_window_is_labelled_load(left)
            or capacity_window_is_labelled_load(right)
        ):
            continue
        raw = match.group(0).strip()
        token = match.group("num")
        if match.group("num2"):
            token = f"{token}-{match.group('num2')}"
        key = normalize_once(token).casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append((token, raw))
    return found


def _source_labelled_load_raw(source_blob: str, evidence: str) -> str | None:
    """Replace a load number only when exactly one labelled source load exists."""
    wanted = normalize_once(evidence).casefold()
    if not wanted or wanted == "not_found":
        return None
    labelled = _labelled_load_values(source_blob)
    if len(labelled) != 1:
        return None
    token, raw = labelled[0]
    token_n = normalize_once(token).casefold()
    raw_n = normalize_once(raw).casefold()
    if (
        token_n == wanted
        or _whole_number_in_text(wanted, raw_n)
        or any(_whole_number_in_text(part.strip(), raw_n) for part in wanted.split(",") if part.strip())
        or wanted in {token_n, raw_n}
        or _whole_number_in_text(token_n, wanted)
    ):
        return raw
    return None


def _load_phrase_pattern(generated: str) -> re.Pattern[str] | None:
    """Edit only the flagged load phrase, never every N lb in the field."""
    text = generated or ""
    number = _LOAD_NUMBER_RE.search(text)
    if number is None:
        return _safe_claim_pattern(text)
    num = re.escape(number.group("num"))
    num2 = number.group("num2")
    number_pat = rf"{num}(?:\s*(?:-|–|to)\s*{re.escape(num2)})?" if num2 else num
    return re.compile(
        rf"(?<![\d.])(?:supports?\s+(?:up\s+to\s+)?|up\s+to\s+|"
        rf"rated\s+(?:up\s+to\s+)?|capacity\s+(?:of\s+)?)?"
        rf"{number_pat}\s*(?:lb|lbs|pounds?|kgs?|kilograms?)\b(?:\s+capacity)?",
        re.I,
    )


def _capacity_family_from_claim(claim_text: str) -> str:
    match = re.search(
        r"\b(persons?|people|seats?|seaters?|occupants?|gallons?|gals?|"
        r"quarts?|liters?|litres?|lbs?|pounds?|kgs?|kilograms?)\b",
        claim_text or "",
        re.I,
    )
    if not match:
        return ""
    from src.services.listing_qc import _capacity_family

    family = _capacity_family(match.group(1))
    if family == "load" and capacity_window_is_product_weight(claim_text or ""):
        return "weight"
    return family


def _generated_capacity_phrase(claim_text: str) -> str:
    text = str(claim_text or "")
    if " (source:" in text:
        text = text.split(" (source:", 1)[0]
    return text.strip()


def _feature_patterns(name: str) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for raw in FEATURE_CLAIM_PATTERNS.get(name, ()):
        compiled.append(re.compile(raw, re.I))
    for raw in CLAIM_PATTERNS.get(name, ()):
        compiled.append(re.compile(raw, re.I))
    return compiled


def _quantity_edit(claim: FlaggedClaim) -> tuple[re.Pattern[str], str | None] | None:
    match = _QTY_CLAIM_RE.search(claim.claim_text or "")
    if not match:
        return None
    number = match.group("n")
    noun = match.group("noun")
    source_n = match.group("src") or (
        claim.source_evidence if str(claim.source_evidence).isdigit() else None
    )
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(number)}[\s-]*{re.escape(noun)}s?(?![A-Za-z0-9])",
        re.I,
    )
    if source_n and source_n != number:
        return pattern, rf"{source_n} {noun}"
    return pattern, None


def _feature_name_from_claim(claim: FlaggedClaim) -> str:
    claim_text = claim.claim_text or ""
    code = claim.code or ""
    if claim_text in FEATURE_CLAIM_PATTERNS:
        return claim_text
    if code in FEATURE_CLAIM_PATTERNS:
        return code
    stripped = code[6:] if code.startswith("claim_") else code
    if stripped in FEATURE_CLAIM_PATTERNS:
        return stripped
    blob = f"{claim_text} {code}"
    for key in FEATURE_CLAIM_PATTERNS:
        spaced = key.replace("_", " ")
        if whole_value_match(key, blob) or whole_value_match(spaced, blob):
            return key
    return ""


def _patterns_for_claim(
    claim: FlaggedClaim,
    source_blob: str,
    source: Mapping[str, Any] | None = None,
) -> list[tuple[re.Pattern[str], str | None]]:
    """Return (pattern, replacement_or_None). None replacement means remove.

    Unknown values are never mapped to a nearest guess.
    """
    edits: list[tuple[re.Pattern[str], str | None]] = []
    claim_type = (claim.claim_type or "").lower()
    claim_text = claim.claim_text or ""
    evidence = str(claim.source_evidence or "NOT_FOUND")

    if claim_type in {"unsupported_feature", "claim_unsupported_feature"} or claim_text in FEATURE_CLAIM_PATTERNS:
        name = _feature_name_from_claim(claim)
        for pattern in _feature_patterns(name):
            edits.append((pattern, None))
        return edits

    if claim_type.startswith("unsupported_") and claim_type.endswith("_claim"):
        name = claim_type[len("unsupported_"):-len("_claim")]
        for pattern in _feature_patterns(name):
            edits.append((pattern, None))
        return edits

    if claim_type.startswith("assembly_"):
        for raw in ASSEMBLY_NO_REQUIRED_PATTERNS:
            edits.append((re.compile(raw, re.I), None))
        edits.append((BARE_NO_ASSEMBLY_RE, None))
        edits.append((re.compile(r"\bfully\s+assembled\b", re.I), None))
        return edits

    if claim_type in {"unsupported_quantity", "claim_unsupported_quantity", "semantic_count"}:
        qty = _quantity_edit(claim)
        if qty:
            edits.append(qty)
        return edits

    if claim_type == "semantic_capacity":
        generated = _generated_capacity_phrase(claim_text)
        family = _capacity_family_from_claim(generated)
        replacement = None
        if family == "load":
            if evidence and evidence.upper() != "NOT_FOUND":
                replacement = _source_labelled_load_raw(source_blob, evidence)
            pattern = _load_phrase_pattern(generated)
            if pattern is not None:
                edits.append((pattern, replacement))
            return edits
        if family == "weight":
            return edits
        if evidence and evidence.upper() != "NOT_FOUND":
            raw = _source_capacity_raw(source_blob, family, evidence)
            if raw:
                replacement = raw
            elif re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", evidence):
                unit_match = re.search(
                    r"(persons?|people|seats?|seaters?|occupants?|gallons?|gals?|"
                    r"quarts?|liters?|litres?)",
                    generated,
                    re.I,
                )
                if unit_match:
                    replacement = f"{evidence} {unit_match.group(1)}"
                else:
                    replacement = evidence
        if generated:
            pattern = _safe_claim_pattern(generated)
            if pattern is not None:
                edits.append((pattern, replacement))
        return edits

    if claim_type in {"semantic_material", "material_upgrade", "unsupported_wood_species"}:
        phrase = _generated_capacity_phrase(claim_text)
        replacement = None
        if phrase.casefold() in {"fabric", "cloth"}:
            replacement = _single_fabric_material(_source_material_field_value(source))
        pattern = _safe_claim_pattern(phrase)
        if pattern is not None:
            edits.append((pattern, replacement))
        return edits

    phrase = _generated_capacity_phrase(claim_text)
    pattern = _safe_claim_pattern(phrase)
    if pattern is not None:
        edits.append((pattern, None))
    return edits


def _edit_text(
    text: str,
    pattern: re.Pattern[str],
    replacement: str | None,
    *,
    is_title: bool = False,
    skip_weight_values: bool = False,
) -> tuple[str, bool, str | None]:
    if text is None:
        return "", False, None
    original = text
    if replacement is None:
        updated = _sub_outside_urls(
            pattern, text, "", skip_weight_values=skip_weight_values
        )
    else:
        urls = _url_spans(text)
        parts: list[str] = []
        last = 0
        changed = False
        for match in pattern.finditer(text):
            if not _allow_edit_match(
                text, match, urls, skip_weight_values=skip_weight_values
            ):
                continue
            parts.append(text[last:match.start()])
            parts.append(replacement)
            last = match.end()
            changed = True
        if not changed:
            return text, False, None
        parts.append(text[last:])
        updated = "".join(parts)
    updated = cleanup_fragment(updated)
    if is_title:
        updated = _LEAD_SEP_RE.sub("", updated)
        updated = _TRAIL_SEP_RE.sub("", updated)
        updated = cleanup_fragment(updated)
        if not _title_survives(updated):
            return original, False, "removing the claim would leave an empty title"
    return updated, updated != original, None


def _edit_aspect_value(
    value: Any,
    pattern: re.Pattern[str],
    replacement: str | None,
    *,
    skip_weight_values: bool = False,
) -> Any:
    if value is None:
        return value
    text = str(value)
    updated, changed, _reason = _edit_text(
        text, pattern, replacement, skip_weight_values=skip_weight_values
    )
    if not changed:
        return value
    return updated


def apply_claim_to_candidate(
    candidate: dict[str, Any],
    claim: FlaggedClaim,
    source_blob: str,
    source: Mapping[str, Any] | None = None,
) -> tuple[list[FieldDiff], str | None]:
    diffs: list[FieldDiff] = []
    needs_manual: str | None = None
    edits = _patterns_for_claim(claim, source_blob, source)
    if not edits:
        return [], None
    skip_weight_values = (claim.claim_type or "").lower() == "semantic_capacity"

    for pattern, replacement in edits:
        action = "source_correct" if replacement else "remove"
        title_before = str(candidate.get("title") or "")
        title_after, title_changed, title_reason = _edit_text(
            title_before,
            pattern,
            replacement,
            is_title=True,
            skip_weight_values=skip_weight_values,
        )
        if title_reason:
            needs_manual = title_reason
        elif title_changed:
            candidate["title"] = title_after
            diffs.append(
                FieldDiff(
                    field="title",
                    before=title_before,
                    after=title_after,
                    action=action,
                    claim_text=claim.claim_text,
                    source_text=claim.source_evidence,
                    detector=claim.detector,
                )
            )

        desc_before = str(candidate.get("description") or "")
        desc_after, desc_changed, _reason = _edit_text(
            desc_before, pattern, replacement, skip_weight_values=skip_weight_values
        )
        if desc_changed:
            candidate["description"] = desc_after
            diffs.append(
                FieldDiff(
                    field="description",
                    before=desc_before,
                    after=desc_after,
                    action=action,
                    claim_text=claim.claim_text,
                    source_text=claim.source_evidence,
                    detector=claim.detector,
                )
            )

        aspects = candidate.get("aspects")
        if not isinstance(aspects, dict):
            continue
        aspect_changed = False
        aspects_before = copy.deepcopy(aspects)
        for key, raw in list(aspects.items()):
            if skip_weight_values and is_weight_aspect_key(str(key)):
                continue
            values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            new_values: list[Any] = []
            for item in values:
                if item in (None, ""):
                    continue
                if claim.claim_type.startswith("assembly_") and str(key).lower() == "assembly required":
                    if normalize_once(item).casefold() in {"no", "false", "n"}:
                        aspect_changed = True
                        continue
                updated = _edit_aspect_value(
                    item, pattern, replacement, skip_weight_values=skip_weight_values
                )
                if str(updated).strip() == "":
                    aspect_changed = True
                    continue
                if str(updated) != str(item):
                    aspect_changed = True
                new_values.append(updated)
            if new_values:
                aspects[key] = new_values if isinstance(raw, (list, tuple)) else new_values[0]
            else:
                aspects.pop(key, None)
                aspect_changed = True
        if aspect_changed:
            diffs.append(
                FieldDiff(
                    field="aspects",
                    before=json.dumps(aspects_before, ensure_ascii=False, sort_keys=True),
                    after=json.dumps(aspects, ensure_ascii=False, sort_keys=True),
                    action=action,
                    claim_text=claim.claim_text,
                    source_text=claim.source_evidence,
                    detector=claim.detector,
                )
            )
    return diffs, needs_manual


def scrub_candidate(
    candidate: Mapping[str, Any] | None,
    source: Mapping[str, Any] | None,
    *,
    sku: str = "",
) -> tuple[dict[str, Any], ScrubResult]:
    """Return (scrubbed candidate, result). Never invents; never adds claims."""
    working = copy.deepcopy(dict(candidate or {}))
    if not isinstance(working.get("aspects"), dict):
        working["aspects"] = {}
    source_map = dict(source or {})
    source_blob = _source_blob(source_map)
    images_before = _image_urls(
        working.get("title"),
        working.get("description"),
        working.get("aspects"),
        working.get("images"),
        source_map.get("images"),
    )
    before = collect_flagged_claims(working, source_map)
    diffs: list[FieldDiff] = []
    needs_manual: str | None = None
    for claim in before:
        if skip_solid_wood_fix(claim, source_blob):
            continue
        claim_diffs, reason = apply_claim_to_candidate(
            working, claim, source_blob, source_map
        )
        diffs.extend(claim_diffs)
        if reason:
            needs_manual = reason
    after = collect_flagged_claims(working, source_map)
    images_after = _image_urls(
        working.get("title"),
        working.get("description"),
        working.get("aspects"),
        working.get("images"),
        source_map.get("images"),
    )
    images_unchanged = images_before == images_after
    if not images_unchanged:
        needs_manual = needs_manual or "image URL bytes changed; edit refused"
        working = copy.deepcopy(dict(candidate or {}))
        diffs = []
        after = before
        images_unchanged = True

    if needs_manual and not diffs:
        status = "needs_manual"
    elif needs_manual:
        status = "needs_manual"
    elif diffs:
        status = "cleaned"
    else:
        status = "unchanged"
    result = ScrubResult(
        sku=str(sku or ""),
        status=status,
        before_blockers=_blocker_texts(before),
        after_blockers=_blocker_texts(after),
        diffs=diffs,
        needs_manual_reason=needs_manual,
        images_unchanged=images_unchanged,
    )
    return working, result


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        value = row[key]
        return default if value is None else value
    if isinstance(row, Mapping):
        return row.get(key, default)
    return default


def row_to_candidate_source(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    opt = _as_mapping(_row_value(row, "optimization"))
    attributes = _as_mapping(_row_value(row, "attributes"))
    specs = _as_mapping(_row_value(row, "specs"))
    images_raw = _row_value(row, "images")
    videos_raw = _row_value(row, "videos")
    candidate = {
        "title": str(opt.get("title") or ""),
        "description": str(opt.get("description") or ""),
        "aspects": dict(opt.get("aspects") or {}) if isinstance(opt.get("aspects"), Mapping) else {},
        "categoryId": str(opt.get("categoryId") or ""),
        "images": list(images_raw) if isinstance(images_raw, list) else list(_json_obj(images_raw) or []),
        "videos": list(videos_raw) if isinstance(videos_raw, list) else list(_json_obj(videos_raw) or []),
    }
    source = {
        "title": str(_row_value(row, "title") or ""),
        "description": str(_row_value(row, "description") or ""),
        "attributes": attributes,
        "specs": specs,
        "images": candidate["images"],
    }
    return candidate, source


def is_live_row(status: Any, listing_id: Any) -> bool:
    if str(status or "").strip().upper() in LIVE_STATUSES:
        return True
    return bool(str(listing_id or "").strip())


def is_ready_unpublished(status: Any, listing_id: Any) -> bool:
    return str(status or "").strip().upper() in READY_STATUSES and not is_live_row(status, listing_id)


def connect_collection_db(
    path: str,
    *,
    timeout: float = 30.0,
    readonly: bool = False,
) -> sqlite3.Connection:
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    if readonly:
        conn = sqlite3.connect(
            f"{target.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=timeout,
        )
    else:
        conn = sqlite3.connect(str(target), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def retry_locked(fn: Callable[[], Any], *, retries: int = 5, base_delay: float = 0.2) -> Any:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if not any(marker in str(exc).lower() for marker in _LOCKED_MARKERS):
                raise
            time.sleep(base_delay * (2 ** attempt))
    raise sqlite3.OperationalError(f"database locked after {retries} retries: {last}")


def backup_rows(rows: list[Mapping[str, Any]], destination: Any) -> None:
    payload = []
    for row in rows:
        if hasattr(row, "keys"):
            payload.append({key: row[key] for key in row.keys()})
        else:
            payload.append(dict(row))
    path = destination
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
        newline="\n",
    )


def apply_scrub_to_row(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    scrubbed: Mapping[str, Any],
    *,
    now: str | None = None,
) -> None:
    sku = str(row["sku"])
    status = str(row["status"] or "")
    listing_id = row["listing_id"] if "listing_id" in row.keys() else ""
    if not is_ready_unpublished(status, listing_id):
        raise PermissionError(f"refuse live/non-READY row {sku} status={status}")
    opt = _as_mapping(row["optimization"] if "optimization" in row.keys() else {})
    new_opt = dict(opt)
    new_opt["title"] = scrubbed.get("title", opt.get("title"))
    new_opt["description"] = scrubbed.get("description", opt.get("description"))
    new_opt["aspects"] = scrubbed.get("aspects", opt.get("aspects") or {})
    stamp = now or datetime.now(UTC).replace(tzinfo=None).isoformat()
    logs = append_log(row["logs"] if "logs" in row.keys() else None, f"ready_draft_claim_scrub applied at {stamp}")

    def _write() -> None:
        cursor = conn.execute(
            """
            UPDATE collected_products
            SET optimization = ?, logs = ?, updated_at = ?
            WHERE sku = ?
              AND UPPER(status) IN ('READY', 'READY_TO_PUBLISH')
              AND (listing_id IS NULL OR listing_id = '')
            """,
            (json.dumps(new_opt, ensure_ascii=False), logs, stamp, sku),
        )
        if cursor.rowcount < 1:
            raise PermissionError(f"refused write for {sku}: not a READY unpublished row")
        conn.commit()

    retry_locked(_write)


def format_diff_lines(result: ScrubResult, source: Mapping[str, Any] | None = None) -> list[str]:
    lines = [
        f"SKU {result.sku} status={result.status} row={result.row_status or '-'}",
    ]
    if source is not None:
        lines.append(f"  source: {(_source_blob(source) or '')[:240]}")
    if result.needs_manual_reason:
        lines.append(f"  needs_manual: {result.needs_manual_reason}")
    if not result.diffs:
        lines.append("  (no proposed edits)")
        return lines
    for diff in result.diffs:
        lines.append(f"  field={diff.field} action={diff.action} detector={diff.detector}")
        lines.append(f"    claim: {diff.claim_text}")
        lines.append(f"    source_evidence: {diff.source_text}")
        lines.append(f"    current: {diff.before[:300]}")
        lines.append(f"    proposed: {diff.after[:300]}")
    return lines
