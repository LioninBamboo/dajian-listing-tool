"""Quality gate for generated eBay listing drafts.

The AI optimizer is allowed to draft copy, but product identity, category,
measurements, item specifics, and image sufficiency must be deterministic.
"""

from __future__ import annotations

import copy
import html as html_lib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from src.utils.dimension_helpers import (
    extract_all_dimensions,
    extract_product_dimensions_from_text,
    extract_product_weight_from_text,
    replace_description_measurements,
)
from src.utils.product_context_signals import (
    has_outdoor_marker,
    outdoor_context as strict_outdoor_context,
)
from src.utils.publish_aspect_completion import infer_number_of_items_in_set
from src.utils.publish_autofix import sanitize_placeholder_aspects, sanitize_single_value_aspects
from src.utils.publish_validation import (
    REQUIRED_MEASUREMENT_ASPECT_KEYS,
    first_aspect_text,
    measurement_validation_errors,
)
from src.utils.title_sanitizer import normalize_listing_title_for_ebay


MIN_READY_IMAGE_COUNT = 2

# CJK Unified Ideographs — buyer-facing EN listings must not ship Chinese copy.
CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

BAD_TEXT_REPLACEMENTS = {
    "Sofafor": "Sofa for",
    "Boucl\u8c37": "Boucle",
    "Boucl\u0439": "Boucle",
    "Boucl\u0438\u0436": "Boucle",
    "Boucl\u00e9": "Boucle",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u6bcf": "-",
}

FURNITURE_FORBIDDEN_ASPECTS = {
    "US Shoe Size",
    "Shoe Width",
    "Shoe Size",
    "Sport",
    "Sport/Activity",
    "Performance/Activity",
    "Performance",
    "Occasion",
    "Upper Material",
    "Closure",
    "Style Code",
    "Character",
    "Pet Type",
    "Dog Size",
    "Cat Size",
}

GAME_TABLE_FORBIDDEN_ASPECTS = {
    "Game Type",
    "Game Title",
    "Min. Number of Players",
    "Recommended Age Range",
    "Gender",
    "Theme",
    "Year",
    "Tabletop Size",
    "Table Height",
    "Upholstery Material",
    "Upholstery Fabric",
}

CLAIM_PATTERNS: dict[str, tuple[str, ...]] = {
    "foldable": (
        r"\bfoldable\b",
        r"\bfolding\b",
        r"\bcollapsible\b",
        r"\bcollapse\b",
    ),
    "charging": (
        r"\busb(?:[-\s]?c)?\b",
        r"\bcharging\s+stations?\b",
        r"\bcharging\s+ports?\b",
        r"\bpower\s+outlets?\b",
        r"\bac\s+outlets?\b",
        r"\bwireless\s+charg(?:er|ing)\b",
    ),
}

# Single arbiter patterns for "does SOURCE support foldable?"
# Design (HANDOFF_FOLLOWUP): fold/folding/collapsible/折叠 = support;
# extendable / extending / drop leaf alone do NOT constitute foldable evidence.
FOLDABLE_SOURCE_EVIDENCE_PATTERNS: tuple[str, ...] = (
    # Inflection-aware: previous \bfold\b missed "folded"/"folds", so a
    # drop-leaf island whose source says "can be folded" was wrongly judged
    # unsupported and every rewrite of it stalled (2026-07-14 foldable regression).
    # Still excludes drop-leaf/extendable *alone* — those carry no fold verb.
    r"\bfold(?:s|ed|ing|able|away)?\b",
    r"\bcollaps(?:e|es|ed|ing|ible)\b",
    r"折叠",
)

NATURALLY_FOLDABLE_TITLE_KEYWORDS: tuple[str, ...] = (
    "umbrella",
    "camping chair",
    "canopy",
    "shade sail",
    "tent",
    "hammock",
)

CLAIM_LABELS = {
    "foldable": "foldable/collapsible",
    "charging": "USB/charging",
}

SUPPORTED_FEATURE_CLAIMS = {
    "foldable": "Foldable",
}

DIRECT_VIDEO_EXTENSIONS = frozenset({
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".avi",
    ".wmv",
    ".mkv",
})

BLOCKED_VIDEO_EXTENSIONS = {
    ".txt": "source_video_txt_manifest",
    ".m3u8": "source_video_stream_manifest",
}

BLOCKED_VIDEO_HOST_MARKERS = (
    "youku.com",
    "youtube.com",
    "youtu.be",
    "vimeo.com",
)


@dataclass(frozen=True)
class ListingQualityIssue:
    code: str
    message: str
    severity: str = "BLOCKER"
    field: str | None = None


@dataclass(frozen=True)
class ListingProfile:
    kind: str
    category_id: str | None = None
    category_name: str | None = None
    type_value: str | None = None
    set_includes: str | None = None
    room: str | None = None
    indoor_outdoor: str | None = None
    remove_aspects: frozenset[str] = frozenset()


def _clean_text(value: str) -> str:
    text = str(value or "")
    for bad, good in BAD_TEXT_REPLACEMENTS.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, str):
        return _clean_text(value)
    return value


def _text_context(*parts: Any) -> str:
    return _clean_text(" ".join(str(part or "") for part in parts)).lower()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _set_aspect(aspects: dict[str, Any], key: str, value: str | None) -> None:
    if value:
        aspects[key] = [_clean_text(value)]


def _append_unique_aspect_value(aspects: dict[str, Any], key: str, value: str) -> bool:
    cleaned_value = _clean_text(value)
    if not cleaned_value:
        return False
    existing = _as_list(aspects.get(key))
    if any(item.lower() == cleaned_value.lower() for item in existing):
        return False
    existing.append(cleaned_value)
    aspects[key] = existing
    return True


def _plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return _clean_text(text)


def _text_matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    normalized = _plain_text(text).lower()
    if not normalized:
        return False
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)


def _iter_source_entries(
    *,
    source_title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if source_title:
        entries.append(("title", source_title))
    if source_description:
        entries.append(("description", source_description))

    for source_name, source in (("attribute", attributes or {}), ("spec", specs or {})):
        for key, value in source.items():
            key_text = _clean_text(key)
            value_text = _clean_text(value)
            if not key_text and not value_text:
                continue
            entries.append((f"{source_name}:{key_text or 'unknown'}", f"{key_text}: {value_text}"))

    return entries


def _collect_claim_evidence(
    entries: list[tuple[str, str]],
    patterns: tuple[str, ...],
    *,
    limit: int = 5,
) -> list[str]:
    evidence: list[str] = []
    seen: set[tuple[str, str]] = set()

    for label, text in entries:
        normalized = _plain_text(text)
        if not normalized:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
                snippet = _clean_text(match.group(0))
                if not snippet:
                    continue
                key = (label, snippet.lower())
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(f"{label}={snippet}")
                if len(evidence) >= limit:
                    return evidence
    return evidence


def _generated_claim_locations(
    *,
    title: str,
    description: str,
    aspects: Mapping[str, Any],
    claim_name: str,
) -> list[str]:
    patterns = CLAIM_PATTERNS[claim_name]
    locations: list[str] = []

    if _text_matches_any(title, patterns):
        locations.append("title")
    if _text_matches_any(description, patterns):
        locations.append("description")

    for key, value in aspects.items():
        aspect_text = f"{key}: {' '.join(_as_list(value))}"
        if _text_matches_any(aspect_text, patterns):
            locations.append(f"aspects.{key}")
    return locations


def evaluate_source_video_urls(videos: list[str] | None = None) -> dict[str, Any]:
    """Classify supplier video URLs before READY/publish."""
    normalized_videos = _as_list(videos or [])
    result: dict[str, Any] = {
        "present": bool(normalized_videos),
        "url_count": len(normalized_videos),
        "primary_url": normalized_videos[0] if normalized_videos else None,
        "direct_url": None,
        "preflight_status": "missing",
        "issue_codes": [],
    }
    if not normalized_videos:
        return result

    blocked_codes: list[str] = []
    unknown_urls: list[str] = []

    for raw_url in normalized_videos:
        parsed = urlparse(str(raw_url).strip())
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        filename = path.rsplit("/", 1)[-1]
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1]

        if ext in DIRECT_VIDEO_EXTENSIONS:
            result["direct_url"] = raw_url
            result["preflight_status"] = "ready"
            result["issue_codes"] = []
            return result

        if ext in BLOCKED_VIDEO_EXTENSIONS:
            blocked_codes.append(BLOCKED_VIDEO_EXTENSIONS[ext])
            continue

        if ext in {".html", ".htm"} or any(marker in host for marker in BLOCKED_VIDEO_HOST_MARKERS):
            blocked_codes.append("source_video_page_url")
            continue

        unknown_urls.append(raw_url)

    result["issue_codes"] = sorted(set(blocked_codes))
    if unknown_urls:
        result["preflight_status"] = "unknown"
    else:
        result["preflight_status"] = "blocked"
    return result


def source_supports_foldable(
    *,
    source_title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
) -> bool:
    """Unique arbiter: whether the GIGA/source product supports foldable claims.

    Both listing_quality_gate (missing_foldable / source_facts) and
    claim_diff_engine (unsupported foldable claim) MUST use this function so
    the two detectors cannot disagree on the same inputs.

    Semantic boundary:
    - fold / folding / collapsible / collapse / 折叠 → supported
    - extendable / extending / drop leaf alone → NOT supported
    - naturally foldable product titles (umbrella, tent, …) → supported
    """
    title_l = _clean_text(source_title).lower()
    if any(kw in title_l for kw in NATURALLY_FOLDABLE_TITLE_KEYWORDS):
        return True

    entries = _iter_source_entries(
        source_title=source_title,
        source_description=source_description,
        attributes=attributes,
        specs=specs,
    )
    combined = " ".join(text for _, text in entries)
    combined = re.sub(r"<[^>]+>", " ", combined)
    combined = re.sub(r"\s+", " ", combined)
    # Cushion durability copy such as "will not collapse after long-term
    # sitting" describes resistance to deformation, not a foldable product.
    combined = re.sub(
        r"\b(?:will|does|do|can)\s+not\s+collaps(?:e|es|ed|ing|ible)\b|"
        r"\bwon['’]?t\s+collaps(?:e|es|ed|ing|ible)\b",
        " ",
        combined,
        flags=re.IGNORECASE,
    )
    return any(
        re.search(pattern, combined, flags=re.IGNORECASE)
        for pattern in FOLDABLE_SOURCE_EVIDENCE_PATTERNS
    )


def build_source_facts(
    *,
    source_title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    videos: list[str] | None = None,
) -> dict[str, Any]:
    """Extract deterministic source facts used to block hallucinated claims."""
    entries = _iter_source_entries(
        source_title=source_title,
        source_description=source_description,
        attributes=attributes,
        specs=specs,
    )
    claims = {
        claim_name: {
            "supported": bool(evidence := _collect_claim_evidence(entries, patterns)),
            "evidence": evidence,
        }
        for claim_name, patterns in CLAIM_PATTERNS.items()
    }
    # Foldable uses the single arbiter so title-only evidence ("Folding Mattress")
    # and description evidence stay consistent with claim_diff_engine.
    foldable_supported = source_supports_foldable(
        source_title=source_title,
        source_description=source_description,
        attributes=attributes,
        specs=specs,
    )
    foldable_evidence = _collect_claim_evidence(entries, FOLDABLE_SOURCE_EVIDENCE_PATTERNS)
    claims["foldable"] = {
        "supported": foldable_supported,
        "evidence": foldable_evidence if foldable_supported else [],
    }
    return {
        "assembly_required": infer_source_assembly_required(attributes, specs, source_description),
        "assembly_status": infer_source_assembly_status(attributes, specs, source_description),
        "claims": claims,
        "video": evaluate_source_video_urls(videos),
    }


def source_preflight_issue_codes(source_facts: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(source_facts, Mapping):
        return []
    video = source_facts.get("video") if isinstance(source_facts.get("video"), Mapping) else {}
    if video.get("present") and video.get("preflight_status") == "blocked":
        return ["source_video_not_publishable"]
    return []


def _normalize_yes_no(value: Any) -> str | None:
    text = _clean_text(value).lower()
    if not text:
        return None
    if text in {"yes", "y", "true", "1", "required", "requires assembly", "assembly required"}:
        return "Yes"
    if text in {"no", "n", "false", "0", "not required", "no assembly required"}:
        return "No"
    if any(marker in text for marker in ("\u662f", "\u9700\u8981", "\u9700\u5b89\u88c5", "\u9700\u7ec4\u88c5")):
        return "Yes"
    if any(marker in text for marker in ("\u5426", "\u4e0d\u9700", "\u65e0\u9700")):
        return "No"
    return None


def infer_source_assembly_required(
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    source_description: str = "",
) -> str | None:
    """Return Yes/No only when the supplier source explicitly states assembly."""
    for source in (attributes or {}, specs or {}):
        for key, value in source.items():
            normalized_key = _clean_text(key).lower()
            if normalized_key in {"assembly required", "requires assembly"}:
                parsed = _normalize_yes_no(value)
                if parsed:
                    return parsed

    text = _plain_text(source_description)
    patterns = (
        r"\bassembly\s+required\s*[:：]?\s*(yes|no)\b",
        r"\brequires\s+assembly\s*[:：]?\s*(yes|no)\b",
        (
            r"(?:\u662f\u5426)?(?:\u9700\u8981\u5b89\u88c5|\u9700\u5b89\u88c5|"
            r"\u9700\u8981\u7ec4\u88c5|\u9700\u7ec4\u88c5|\u5b89\u88c5\u8981\u6c42|"
            r"\u7ec4\u88c5\u8981\u6c42)\s*[:：]?\s*"
            r"(\u662f|\u5426|\u9700\u8981|\u4e0d\u9700\u8981|\u65e0\u9700|yes|no)"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        parsed = _normalize_yes_no(match.group(1))
        if parsed:
            return parsed
    return None


ASSEMBLY_NO_REQUIRED_PATTERNS = (
    r"\bno\s+assembly\s+required\b",
    r"\bassembly[-\s]?free\b",
    r"\bships?\s+fully\s+assembled\b",
    r"\bfully\s+assembled\b",
    r"\bready\s+to\s+use\s+right\s+out\s+of\s+the\s+box\b",
)

ASSEMBLY_YES_REQUIRED_PATTERNS = (
    r"\brequires\s+assembly\b",
    r"\bassembly\s+(?:is\s+)?needed\b",
    r"\bmust\s+be\s+assembled\b",
)

ASSEMBLY_STATUS_PATTERNS = (
    (r"\bpart(?:ially)?\s+assembled\b", "Part Assembled"),
    (r"\bpre[-\s]?assembled\b", "Part Assembled"),
    (r"\bfully\s+assembled\b", "Fully Assembled"),
    (r"\bready\s+to\s+assemble\b", "Ready to Assemble"),
    (r"\bunassembled\b", "Ready to Assemble"),
)


def infer_source_assembly_status(
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    source_description: str = "",
) -> str | None:
    """Return an assembly status only when the supplier source states it explicitly."""
    for source in (attributes or {}, specs or {}):
        for key, value in source.items():
            normalized_key = _clean_text(key).lower()
            if normalized_key != "assembly status":
                continue
            value_text = _clean_text(value).lower()
            for pattern, normalized_status in ASSEMBLY_STATUS_PATTERNS:
                if re.search(pattern, value_text, flags=re.IGNORECASE):
                    return normalized_status

    text = _plain_text(source_description)
    for pattern, normalized_status in ASSEMBLY_STATUS_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return normalized_status
    return None


def has_expected_assembly_copy(description: str, expected: str | None) -> bool:
    """Return True when the description explicitly matches the expected assembly requirement."""
    if not description or not expected:
        return False

    text = _plain_text(description).lower()
    if expected == "Yes":
        positive_patterns = (
            r"\bassembly\s+required\s*[:：]?\s*yes\b",
            r"\bsome\s+assembly\s+required\b",
            r"\brequires\s+assembly\b",
            r"\bassembly\s+(?:is\s+)?required\b",
            r"\bassembly\s+(?:is\s+)?needed\b",
            r"\bmust\s+be\s+assembled\b",
            r"\bready\s+to\s+use\s+after\s+assembly\b",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in positive_patterns)

    negative_patterns = (
        r"\bassembly\s+required\s*[:：]?\s*no\b",
        r"\bno\s+assembly\s+required\b",
        r"\bdoes\s+not\s+require\s+assembly\b",
        r"\bassembly[-\s]?free\b",
        r"\bships?\s+fully\s+assembled\b",
        r"\bfully\s+assembled\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in negative_patterns)


SPECIFICATIONS_TABLE_RE = re.compile(
    r"(<h3[^>]*>\s*SPECIFICATIONS\s*</h3>\s*<table[^>]*>)(.*?)(</table>)",
    flags=re.IGNORECASE | re.DOTALL,
)
COMPLETE_DETAILS_RE = re.compile(
    r'<div[^>]*>\s*<p[^>]*>\s*<strong>\s*(?:📋\s*)?Complete Details:\s*</strong>.*?</div>',
    flags=re.IGNORECASE | re.DOTALL,
)
ASSEMBLY_SPEC_ROW_RE = re.compile(
    r"<tr\b[^>]*>\s*<td\b[^>]*>\s*Assembly(?:\s+Required)?\s*</td>\s*<td\b[^>]*>.*?</td>\s*</tr>",
    flags=re.IGNORECASE | re.DOTALL,
)
DESCRIPTION_LITERAL_ESCAPE_RE = re.compile(r"\\[nrt]")
DESCRIPTION_BETWEEN_TAG_QUOTE_GT_RE = re.compile(
    r'>\s*(?:&quot;|&#34;|")\s*>\s*(?=<)',
    flags=re.IGNORECASE,
)
DESCRIPTION_BETWEEN_TAG_QUOTES_RE = re.compile(
    r'>\s*(?:&quot;|&#34;|")\s*(?=<)',
    flags=re.IGNORECASE,
)


def _assembly_copy(expected: str) -> str:
    if expected == "Yes":
        return "Yes - Assembly is required; setup required before use with included hardware and instructions."
    return "No - Ready for use without assembly."


def _build_spec_row(label: str, value: str, *, shaded: bool, mark_assembly: bool = False) -> str:
    row_style = ' style="background:#fafafa"' if shaded else ""
    marker = ' data-assembly-note="true"' if mark_assembly else ""
    return (
        f"<tr{row_style}{marker}>"
        f'<td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72;width:40%">{label}</td>'
        f'<td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#2d3436">{value}</td>'
        f"</tr>"
    )


def _sanitize_specifications_table_body(table_body: str) -> str:
    cleaned = re.sub(r"</?div\b[^>]*>", "", table_body or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def sanitize_generated_description_html(description: str) -> str:
    if not description:
        return description or ""
    cleaned = DESCRIPTION_LITERAL_ESCAPE_RE.sub(" ", description)
    cleaned = DESCRIPTION_BETWEEN_TAG_QUOTE_GT_RE.sub(">", cleaned)
    cleaned = DESCRIPTION_BETWEEN_TAG_QUOTES_RE.sub(">", cleaned)
    cleaned = re.sub(r">\s+(?=<)", ">", cleaned)
    return cleaned.strip()


def _sanitize_specifications_table_html(description: str) -> str:
    match = SPECIFICATIONS_TABLE_RE.search(description or "")
    if not match:
        return description or ""
    table_start, table_body, table_end = match.groups()
    sanitized_body = _sanitize_specifications_table_body(table_body)
    return f"{description[:match.start()]}{table_start}{sanitized_body}{table_end}{description[match.end():]}"


def _strip_existing_assembly_note(description: str) -> str:
    updated = re.sub(
        r'<div[^>]*data-assembly-note="true"[^>]*>.*?</div>',
        "",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    updated = re.sub(
        r'<p[^>]*>\s*<strong>\s*Assembly(?:\s+Required)?\s*:?\s*</strong>.*?</p>',
        "",
        updated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    updated = COMPLETE_DETAILS_RE.sub("", updated)
    return updated


def _upsert_assembly_specs_row(description: str, expected: str) -> str:
    updated = _strip_existing_assembly_note(description)
    match = SPECIFICATIONS_TABLE_RE.search(updated)
    if not match:
        fallback = (
            f'<p data-assembly-note="true"><strong>Assembly Required:</strong> {_assembly_copy(expected)}</p>'
        )
        if "</div>" in updated:
            return updated.replace("</div>", f"{fallback}</div>", 1)
        return f"{updated}{fallback}"

    table_start, table_body, table_end = match.groups()
    sanitized_body = _sanitize_specifications_table_body(table_body)
    body_without_assembly = ASSEMBLY_SPEC_ROW_RE.sub("", sanitized_body)
    row_count = len(re.findall(r"<tr\b", body_without_assembly, flags=re.IGNORECASE))
    assembly_row = _build_spec_row(
        "Assembly Required",
        _assembly_copy(expected),
        shaded=bool(row_count % 2),
        mark_assembly=True,
    )
    new_body = f"{body_without_assembly.rstrip()}{assembly_row}"
    return f"{updated[:match.start()]}{table_start}{new_body}{table_end}{updated[match.end():]}"


def find_assembly_description_contradictions(description: str, expected: str | None) -> list[str]:
    if not expected:
        return []
    text = _plain_text(description).lower()
    if expected == "No":
        found: list[str] = []
        for match in re.finditer(r"\bassembly\s+required\b", text, flags=re.IGNORECASE):
            before = text[max(0, match.start() - 30): match.start()]
            after = text[match.end(): match.end() + 16]
            if re.search(r"\b(?:no|not)\b", before):
                continue
            if re.match(r"\s*[:：]?\s*(?:no|not\s+required|false)\b", after):
                continue
            found.append(r"\bassembly\s+required\b")
            break
        for pattern in ASSEMBLY_YES_REQUIRED_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                found.append(pattern)
        return list(dict.fromkeys(found))

    patterns = ASSEMBLY_NO_REQUIRED_PATTERNS
    found: list[str] = []
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.append(pattern)
    return found


def rewrite_assembly_copy(description: str, expected: str | None) -> str:
    if not description or not expected:
        return description or ""

    if expected == "Yes":
        replacements = (
            (
                r"Space-Smart\s*&\s*Assembly-Free\s*:\s*Ships?\s+fully\s+assembled\s*[-\u2013\u2014]\s*"
                r"ready\s+to\s+use\s+right\s+out\s+of\s+the\s+box\.\s*"
                r"Saves\s+time,\s*eliminates\s+frustration,\s*and\s*guarantees\s+structural\s+integrity\.?",
                "Assembly Required: Assembly is required before use. Follow the included instructions and hardware guide.",
            ),
            (
                r"\bno\s+assembly\s+required\b",
                "assembly required",
            ),
            (
                r"\bassembly[-\s]?free\b",
                "assembly required",
            ),
            (
                r"\bships?\s+fully\s+assembled\b",
                "requires assembly before use",
            ),
            (
                r"\bfully\s+assembled\b",
                "requires assembly",
            ),
            (
                r"\bready\s+to\s+use\s+right\s+out\s+of\s+the\s+box\b",
                "ready to use after assembly",
            ),
        )
    else:
        replacements = (
            (r"\bassembly\s+required\s*[:：]?\s*yes\b", "no assembly required"),
            (r"\bsome\s+assembly\s+required\b", "no assembly required"),
            (r"\brequires\s+assembly\b", "does not require assembly"),
            (r"\bassembly\s+(?:is\s+)?needed\b", "no assembly required"),
            (r"\bmust\s+be\s+assembled\b", "does not require assembly"),
        )

    updated = description
    for pattern, replacement in replacements:
        updated = re.sub(pattern, replacement, updated, flags=re.IGNORECASE)

    return _upsert_assembly_specs_row(updated, expected)


def apply_source_assembly_requirement(
    opt: dict[str, Any],
    *,
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
) -> str | None:
    aspects = opt.setdefault("aspects", {})
    if "Assembly Status" in aspects and not infer_source_assembly_status(attributes, specs, source_description):
        aspects.pop("Assembly Status", None)

    expected = infer_source_assembly_required(attributes, specs, source_description)
    if not expected:
        return None

    aspects["Assembly Required"] = [expected]

    packaging = _as_list(aspects.get("Packaging"))
    if expected == "Yes" and any("fully assembled" in value.lower() for value in packaging):
        aspects.pop("Packaging", None)

    opt["description"] = rewrite_assembly_copy(opt.get("description", ""), expected)
    return expected


def apply_aspect_assembly_description_requirement(opt: dict[str, Any]) -> str | None:
    """Keep description copy aligned when the generated aspect already declares assembly."""
    aspects = opt.get("aspects") if isinstance(opt.get("aspects"), dict) else {}
    current = first_aspect_text(aspects, "Assembly Required")
    expected = _normalize_yes_no(current)
    if not expected:
        return None

    description = opt.get("description", "")
    if not has_expected_assembly_copy(description, expected):
        opt["description"] = rewrite_assembly_copy(description, expected)
    return expected


def _remove_aspects(aspects: dict[str, Any], names: set[str] | frozenset[str]) -> None:
    for name in names:
        aspects.pop(name, None)


def _format_measurement(value: float | int | str, unit: str) -> str:
    number = float(value)
    normalized = str(round(number, 2))
    return f"{normalized} {unit}"


def _infer_color_from_text(text: str) -> str | None:
    color_map = (
        ("black", "Black"),
        ("blue", "Blue"),
        ("green", "Green"),
        ("brown", "Brown"),
        ("camel", "Beige"),
        ("beige", "Beige"),
        ("cream", "Beige"),
        ("white", "White"),
        ("gray", "Gray"),
        ("grey", "Gray"),
        ("red", "Red"),
    )
    normalized = _text_context(text)
    for keyword, color in color_map:
        if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", normalized):
            return color
    return None


def _fill_measurement_aspects(aspects: dict[str, Any], attributes: Mapping[str, Any]) -> dict[str, str]:
    dims = extract_all_dimensions(dict(attributes or {}))
    updates: dict[str, str] = {}
    mapping = {
        "Item Length": ("length", "in"),
        "Item Width": ("width", "in"),
        "Item Height": ("height", "in"),
        "Item Weight": ("weight", "lbs"),
    }
    for aspect_key, (dim_key, unit) in mapping.items():
        value = dims.get(dim_key)
        if value in (None, ""):
            continue
        formatted = _format_measurement(value, unit)
        aspects[aspect_key] = [formatted]
        updates[aspect_key] = formatted
    return updates


def _seed_attributes_from_description(
    attributes: Mapping[str, Any] | None,
    description: str,
) -> dict[str, Any]:
    seeded = dict(attributes or {})
    desc_dims = extract_product_dimensions_from_text(description or "")
    desc_weight = extract_product_weight_from_text(description or "")
    for key, value in {
        "Assembled Length (in.)": desc_dims.get("length"),
        "Assembled Width (in.)": desc_dims.get("width"),
        "Assembled Height (in.)": desc_dims.get("height"),
        "Product Weight (lbs.)": desc_weight,
    }.items():
        if value not in (None, "") and not seeded.get(key):
            seeded[key] = str(value)
    return seeded


def classify_listing_profile(title: str, description: str = "", category_id: str = "") -> ListingProfile:
    title_text = _text_context(title)
    text = _text_context(title, description)
    outdoor_context = has_outdoor_marker(text)
    # Shared with ebay_category_matcher via product_context_signals: one passing
    # mention of a balcony must not outweigh four named indoor rooms. Both
    # engines classify categories, so a guard fixed in only one leaves live wrong.
    outdoor_context_strict = strict_outdoor_context(title_text, text)
    sofa_context = any(
        marker in title_text
        for marker in (
            "sectional sofa",
            "l-shaped sofa",
            "u-shaped sofa",
            "modular sectional",
            "single sofa",
            "sofa",
            "couch",
            "loveseat",
            "armchair",
            "reading chair",
            "chaise lounge",
            "club chair",
        )
    )
    dining_set_markers = (
        "dining set",
        "dining table set",
        "piece dining set",
        "5-piece dining",
        "5 piece dining",
        "6-piece dining",
        "6 piece dining",
        "7-piece dining",
        "7 piece dining",
        "table set",
        "kitchen table set",
    )
    kids_table_set_markers = (
        "kids activity table",
        "kids table and chair",
        "kids table & chair",
        "children table and chair",
        "children's table and chair",
        "childrens table and chair",
        "play table and chair",
        "play table & chair",
        "activity table and chair",
        "toddler table and chair",
        "table and chair set for toddlers",
    )
    bed_markers = (
        "bunk bed",
        "loft bed",
        "platform bed",
        "bed frame",
        "house bed",
        "floor bed",
        "montessori bed",
    )
    bed_stack_markers = (
        "queen over queen",
        "twin over twin",
        "twin over full",
        "full over full",
        "full over queen",
        "queen over full",
    )

    if any(marker in title_text for marker in ("treadmill", "walking pad", "running machine")) and not any(
        marker in title_text for marker in ("dog treadmill", "pet treadmill")
    ):
        return ListingProfile(
            kind="treadmill",
            category_id="15280",
            category_name="Treadmills",
            type_value="Treadmill",
        )

    if any(marker in title_text for marker in ("potting bench", "garden workstation")):
        return ListingProfile(
            kind="potting_bench",
            category_id="139939",
            category_name="Greenhouses",
            type_value="Potting Bench",
        )

    if any(marker in title_text for marker in ("table tennis", "ping pong")):
        return ListingProfile(
            kind="table_tennis",
            category_id="97075",
            category_name="Tables",
            type_value="Table Tennis Table",
            set_includes="Table",
        )

    if any(marker in title_text for marker in kids_table_set_markers) or (
        "kids table" in title_text and "chair" in title_text
    ):
        return ListingProfile(
            kind="kids_table_set",
            category_id="66743",
            category_name="Play Table & Chair Sets",
            type_value="Play Table & Chair Set",
            set_includes="Table & Chairs",
            room="Playroom",
        )

    non_dining_table_set_markers = (
        "coffee table",
        "cocktail table",
        "side table",
        "end table",
        "console table",
        "nesting table",
    )
    if any(marker in title_text for marker in dining_set_markers) and not any(
        marker in title_text for marker in kids_table_set_markers + non_dining_table_set_markers
    ):
        return ListingProfile(
            kind="dining_set",
            category_id="107578",
            category_name="Dining Sets",
            type_value="Dining Set",
            set_includes="Dining Table & Stools" if "stool" in title_text else "Dining Table & Chairs",
        )

    if any(marker in title_text for marker in ("computer desk", "writing desk", "l-shaped desk", "l shaped desk", "corner desk", "home office desk")):
        return ListingProfile(
            kind="desk",
            category_id="88057",
            category_name="Desks & Tables",
        )

    if any(marker in title_text for marker in bed_markers + bed_stack_markers):
        return ListingProfile(
            kind="bed",
            category_id="175758",
            category_name="Beds & Bed Frames",
            type_value=(
                "Loft Bed"
                if "loft bed" in title_text
                else ("Bunk Bed" if any(marker in title_text for marker in ("bunk bed",) + bed_stack_markers) else "Bed Frame")
            ),
            room="Bedroom",
        )

    pantry_markers = (
        "kitchen pantry",
        "pantry cabinet",
        "freestanding pantry",
        "pantry storage",
        "storage cabinet",
        "cupboard",
        "kitchen hutch",
        "hutch cabinet",
        "microwave shelf",
    )
    has_pantry = (
        any(marker in title_text for marker in pantry_markers)
        or ("hutch" in title_text and any(marker in title_text for marker in ("kitchen", "pantry", "microwave")))
    )
    if has_pantry and not any(marker in title_text for marker in ("wine rack", "wine cabinet", "bar cabinet")):
        return ListingProfile(
            kind="pantry_cabinet",
            category_id="20487",
            category_name="Cabinets & Cupboards",
            type_value="Cabinet",
        )

    if "golf" in title_text and ("golf putting green" in title_text or "putting green" in title_text):
        return ListingProfile(
            kind="golf_putting_green",
            category_id="36234",
            category_name="Putting Greens & Aids",
        )

    if "golf" in title_text and any(marker in title_text for marker in ("golf hitting mat", "golf practice mat", "golf training mat", "golf swing mat")):
        return ListingProfile(
            kind="golf_mat",
            category_id="50876",
            category_name="Nets, Cages & Mats",
        )

    if (
        "porch swing" in title_text
        or "porch swing bed" in title_text
        or "patio swing bed" in title_text
        or "garden swing bed" in title_text
        or (
            "swing bed" in title_text
            and outdoor_context
            and "rope" in title_text
            and not any(marker in title_text for marker in ("cat ", "dog ", "pet ", "kitten", "puppy"))
        )
    ):
        return ListingProfile(
            kind="porch_swing",
            category_id="79694",
            category_name="Porch Swings",
            indoor_outdoor="Outdoor",
        )

    if any(marker in title_text for marker in ("outdoor daybed", "patio daybed", "sunbed")) or (
        "daybed" in title_text
        and outdoor_context_strict
        and "porch swing" not in title_text
        and "swing bed" not in title_text
        and not any(marker in title_text for marker in ("sofa", "couch", "loveseat", "sectional"))
    ):
        return ListingProfile(
            kind="outdoor_daybed",
            category_id="138996",
            category_name="Outdoor Daybeds",
            indoor_outdoor="Outdoor",
        )

    if (
        outdoor_context_strict
        and not any(marker in title_text for marker in ("sofa", "couch", "loveseat", "sectional"))
        and re.search(r"\btable\b", title_text) is None
        and any(
            marker in title_text
            for marker in (
                "outdoor chair",
                "patio chair",
                "club chair",
                "club chairs",
                "armchair",
                "armchairs",
                "patio armchair",
                "patio armchairs",
                "outdoor dining chair",
                "outdoor dining chairs",
                "patio dining chair",
                "patio dining chairs",
                "outdoor lounge chair",
                "outdoor lounge chairs",
                "patio lounge",
                "sun lounger",
                "camping chair",
                "camping chairs",
            )
        )
    ):
        return ListingProfile(
            kind="outdoor_chair",
            category_id="79684",
            category_name="Outdoor Chairs",
            type_value="Outdoor Chair",
            set_includes="Chairs" if any(marker in title_text for marker in ("set of 2", "set of two", "chairs")) else "Chair",
            indoor_outdoor="Outdoor",
        )

    if any(marker in title_text for marker in ("bar stool", "bar stools", "counter stool", "counter stools", "barstool", "barstools")):
        return ListingProfile(
            kind="bar_stool",
            category_id="103431",
            category_name="Bar Stools & Stools",
            type_value="Bar Stool",
            set_includes="Stools" if ("set of" in text or "stools" in text) else "Stool",
        )

    patio_set_markers = (
        "patio furniture set",
        "patio set",
        "patio conversation",
        "outdoor conversation set",
        "outdoor furniture set",
        "outdoor sectional",
    )
    if any(marker in title_text for marker in patio_set_markers):
        return ListingProfile(
            kind="patio_furniture_set",
            category_id="139849",
            category_name="Patio & Garden Furniture Sets",
            type_value="Patio Furniture Set",
            set_includes="Sofa Set",
            room="Patio",
            indoor_outdoor="Outdoor",
        )

    if (
        not sofa_context
        and any(marker in title_text for marker in ("storage ottoman", "lift top ottoman", "storage footstool"))
    ) or (
        not sofa_context
        and "ottoman" in title_text and "storage" in title_text
    ):
        return ListingProfile(
            kind="storage_ottoman",
            category_id="20490",
            category_name="Ottomans, Footstools & Poufs",
            type_value="Storage Ottoman",
            set_includes="Ottoman",
            room="Living Room",
            remove_aspects=frozenset({"Top Material", "Tabletop Material"}),
        )

    if not sofa_context and "ottoman bench" in title_text:
        return ListingProfile(
            kind="ottoman_bench",
            category_id="20490",
            category_name="Ottomans, Footstools & Poufs",
            type_value="Ottoman",
            set_includes="Ottoman",
            room="Living Room",
            remove_aspects=frozenset({"Top Material", "Tabletop Material"}),
        )

    excluded_bench = any(
        marker in title_text
        for marker in (
            "garden bench",
            "outdoor bench",
            "park bench",
            "shower bench",
            "bath stool",
            "potting bench",
            "hall tree",
            "coat rack bench",
            "shoe bench",
            "storage ottoman",
            "ottoman bench",
        )
    )
    bench_markers = (
        "dining bench",
        "long bench",
        "upholstered bench",
        "mid century bench",
        "bench with",
        "end of bed",
        "footrest stool",
    )
    if not excluded_bench and any(marker in title_text for marker in bench_markers):
        return ListingProfile(
            kind="indoor_bench",
            category_id="262980",
            category_name="Benches",
            type_value="Bench",
            set_includes="Bench",
        )

    if any(marker in title_text for marker in ("board game table", "gaming table", "game table")):
        return ListingProfile(
            kind="game_table",
            category_id="38204",
            category_name="Tables",
            type_value="Game Table",
            set_includes="Table",
            room="Game Room",
            remove_aspects=frozenset(GAME_TABLE_FORBIDDEN_ASPECTS),
        )

    if "dining chair" in title_text or ("chair" in title_text and "dining" in title_text and ("set of" in title_text or "chairs" in title_text)):
        return ListingProfile(
            kind="dining_chair",
            category_id="54235",
            category_name="Chairs",
            type_value="Dining Chair",
            set_includes="Chairs" if ("set of" in text or "chairs" in text) else None,
            room="Dining Room",
        )

    if "bean bag" in title_text:
        return ListingProfile(
            kind="bean_bag",
            category_id="48319",
            category_name="Bean Bags & Inflatables",
            type_value="Beanbag",
            room="Living Room",
            remove_aspects=frozenset({"Set Includes"}),
        )

    if "dining table" in title_text and "chair" not in title_text and "bench" not in title_text:
        return ListingProfile(
            kind="dining_table",
            category_id="38204",
            category_name="Tables",
            type_value="Dining Table",
            set_includes="Table",
            room="Dining Room",
            remove_aspects=frozenset({"Upholstery Material", "Upholstery Fabric"}),
        )

    if any(marker in title_text for marker in ("coffee table", "cocktail table")):
        return ListingProfile(
            kind="coffee_table",
            category_id="38204",
            category_name="Coffee Tables",
            type_value="Coffee Table",
            set_includes="Table",
            room="Living Room",
        )

    if any(marker in title_text for marker in ("nightstand", "bedside table")):
        return ListingProfile(
            kind="nightstand",
            category_id="38199",
            category_name="Nightstands",
            type_value="Nightstand",
            room="Bedroom",
            remove_aspects=frozenset({"Set Includes", "Upholstery Material", "Upholstery Fabric"}),
        )

    if any(marker in title_text for marker in ("side table", "end table", "accent table", "lamp table")):
        # End/side/accent tables are eBay End Tables (54235), not Coffee/Dining
        # Tables (38204). Mapping to 38204 caused live CRITICAL false pressure
        # to re-categorize 15–20" end tables as coffee tables (2026-08-06).
        return ListingProfile(
            kind="side_table",
            category_id="54235",
            category_name="End Tables",
            type_value="End & Side Tables",
            room="Living Room",
            remove_aspects=frozenset({"Set Includes", "Upholstery Material", "Upholstery Fabric"}),
        )

    if any(marker in title_text for marker in ("armchair", "reading chair", "single seat sofa", "accent chair")) and "dining" not in title_text:
        return ListingProfile(
            kind="armchair",
            category_id="38208",
            category_name="Sofas, Armchairs & Couches",
            type_value="Armchair",
            set_includes="Chair",
            room="Living Room",
        )

    # "Sofa Table" / "Sofa Side Table" / "Console Table ... Behind Couch" are
    # tables that merely mention a sofa — never classify them as sofas
    # (live incident 2026-07-13: a nightstand and a console table were both
    # recategorized into 38208 by this branch).
    sofa_accessory_context = (
        re.search(r"\b(?:sofa|couch)\s+(?:side\s+)?table\b", title_text)
        or "console table" in title_text
        or re.search(r"\bbehind\s+(?:the\s+)?(?:couch|sofa)\b", title_text)
    )
    if not sofa_accessory_context and any(
        marker in title_text
        for marker in ("loveseat", "sectional sofa", "l-shaped sofa", "u-shaped sofa", "modular sectional", "sofa", "couch")
    ):
        is_sectional = any(
            marker in title_text
            for marker in ("sectional", "modular", "l-shaped", "u-shaped", "u shaped", "l shaped")
        )
        return ListingProfile(
            kind="sofa",
            category_id="38208",
            category_name="Sofas, Armchairs & Couches",
            type_value="Loveseat" if "loveseat" in text else ("Sectional" if is_sectional else "Sofa"),
            set_includes="Sofa Set" if is_sectional else "Sofa",
            room="Living Room",
        )

    return ListingProfile(kind="unknown")


def _apply_profile_rules(opt: dict[str, Any], profile: ListingProfile) -> None:
    aspects = opt.setdefault("aspects", {})
    if profile.category_id:
        opt["categoryId"] = profile.category_id
        opt["categoryName"] = profile.category_name
    if profile.type_value:
        _set_aspect(aspects, "Type", profile.type_value)
    if profile.set_includes:
        _set_aspect(aspects, "Set Includes", profile.set_includes)
    if profile.room and not aspects.get("Room"):
        _set_aspect(aspects, "Room", profile.room)
    if profile.indoor_outdoor:
        _set_aspect(aspects, "Indoor/Outdoor", profile.indoor_outdoor)
    _remove_aspects(aspects, FURNITURE_FORBIDDEN_ASPECTS | profile.remove_aspects)

    if profile.kind == "dining_chair":
        title_text = _text_context(opt.get("title", ""))
        match = re.search(r"set\s+of\s+(\d+)", title_text)
        if match:
            _set_aspect(aspects, "Number of Items in Set", match.group(1))
        aspects.setdefault("Department", ["Adults"])
    if profile.kind == "dining_set":
        item_count = infer_number_of_items_in_set(opt.get("title", ""), category_id=profile.category_id or "")
        if item_count:
            _set_aspect(aspects, "Number of Items in Set", item_count)
            _set_aspect(aspects, "Number of Pieces", item_count)
    if profile.kind == "storage_ottoman":
        for material_key in ("Upholstery Material", "Material"):
            values = _as_list(aspects.get(material_key))
            if any("boucle" in value.lower() for value in values):
                _set_aspect(aspects, material_key, "Boucle" if material_key == "Upholstery Material" else "Fabric")

    inferred_color = _infer_color_from_text(opt.get("title", ""))
    if inferred_color:
        _set_aspect(aspects, "Color", inferred_color)


def _numbers_present(text: str, *values: str) -> bool:
    normalized = re.sub(r"<[^>]+>", " ", text or "")
    normalized = re.sub(r"\s+", " ", normalized).lower()
    for value in values:
        match = re.search(r"(\d+(?:\.\d+)?)", str(value or ""))
        if not match:
            return False
        number = float(match.group(1))
        variants = {
            f"{number:g}",
            f"{number:.1f}".rstrip("0").rstrip("."),
            f"{number:.2f}".rstrip("0").rstrip("."),
        }
        if not any(variant and variant in normalized for variant in variants):
            return False
    return True


def _first_present_aspect(aspects: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = first_aspect_text(aspects, key)
        if value:
            return value
    return ""


def _build_fallback_feature_bullets(
    *,
    title: str,
    description: str,
    aspects: Mapping[str, Any],
    source_facts: Mapping[str, Any] | None = None,
) -> list[str]:
    bullets: list[str] = []

    feature_type = _first_present_aspect(aspects, "Type", "Set Includes")
    if feature_type:
        bullets.append(f"Product Type: {feature_type}.")

    length = first_aspect_text(aspects, "Item Length")
    width = first_aspect_text(aspects, "Item Width")
    height = first_aspect_text(aspects, "Item Height")
    if length and width and height:
        bullets.append(f"Overall Dimensions (L x W x H): {length} x {width} x {height}.")

    weight = first_aspect_text(aspects, "Item Weight")
    if weight:
        bullets.append(f"Item Weight: {weight}.")

    material = _first_present_aspect(
        aspects,
        "Material",
        "Upholstery Material",
        "Upholstery Fabric",
        "Frame Material",
        "Base Material",
    )
    if material:
        bullets.append(f"Material: {material}.")

    color = first_aspect_text(aspects, "Color")
    if color:
        bullets.append(f"Color: {color}.")

    assembly_required = ""
    if isinstance(source_facts, Mapping):
        assembly_required = str(source_facts.get("assembly_required") or "").strip()
    if assembly_required and "assembly required" not in description.lower():
        if assembly_required == "Yes":
            bullets.append("Assembly Required: Yes - setup is required before use.")
        else:
            bullets.append("Assembly Required: No - ready for use without assembly.")

    if not bullets and title:
        bullets.append(_clean_text(title))

    return bullets[:6]


def _ensure_key_features_block(
    description: str,
    *,
    title: str,
    aspects: Mapping[str, Any],
    source_facts: Mapping[str, Any] | None = None,
) -> str:
    if not description:
        return description or ""

    lowered = description.lower()
    has_key_features = "key features" in lowered
    has_bullets = "<li" in lowered
    if has_key_features and has_bullets:
        return description

    if has_bullets and not has_key_features:
        inserted = re.sub(
            r"(<ul\b[^>]*>|<li\b)",
            r'<h3 style="margin:16px 0 8px 0;font-size:18px;">KEY FEATURES</h3>\1',
            description,
            count=1,
            flags=re.IGNORECASE,
        )
        if inserted != description:
            return inserted
        return '<h3 style="margin:16px 0 8px 0;font-size:18px;">KEY FEATURES</h3>' + description

    bullets = _build_fallback_feature_bullets(
        title=title,
        description=description,
        aspects=aspects,
        source_facts=source_facts,
    )
    if not bullets:
        return description

    bullet_html = "".join(f"<li>{_clean_text(bullet)}</li>" for bullet in bullets)
    if has_key_features and not has_bullets:
        return description + f"<ul>{bullet_html}</ul>"

    intro = f"<p>{_clean_text(title)}</p>" if title else ""
    return (
        f"{intro}<h3 style=\"margin:16px 0 8px 0;font-size:18px;\">KEY FEATURES</h3>"
        f"<ul>{bullet_html}</ul>{description}"
    )


def normalize_generated_listing(
    optimization: Mapping[str, Any] | None,
    *,
    source_title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    images: list[str] | None = None,
    videos: list[str] | None = None,
    category_matcher: Any = None,
) -> dict[str, Any]:
    """Normalize an AI-generated draft before it can become READY or publish."""
    opt: dict[str, Any] = copy.deepcopy(dict(optimization or {}))
    sanitized_title, _ = normalize_listing_title_for_ebay(
        opt.get("title") or source_title or "",
        source_title=source_title or "",
    )
    opt["title"] = _clean_text(sanitized_title)
    opt["description"] = _clean_text(opt.get("description") or source_description or "")
    opt["aspects"] = _normalize_value(opt.get("aspects") or {})

    if not isinstance(opt["aspects"], dict):
        opt["aspects"] = {}

    title_context = " ".join(part for part in (source_title, opt.get("title")) if part).strip()
    profile = classify_listing_profile(title_context or opt.get("title", ""), opt.get("description", ""), opt.get("categoryId", ""))

    if category_matcher and opt.get("categoryId"):
        category_id, category_name = category_matcher.canonicalize_category(
            title_context or opt.get("title", ""),
            str(opt.get("categoryId") or ""),
            opt.get("categoryName"),
            opt.get("description", ""),
        )
        opt["categoryId"] = category_id
        if category_name:
            opt["categoryName"] = category_name

    seeded_attributes = _seed_attributes_from_description(attributes, opt.get("description", ""))
    updates = _fill_measurement_aspects(opt["aspects"], seeded_attributes)
    _apply_profile_rules(opt, profile)
    sanitize_placeholder_aspects(opt["aspects"], title=opt["title"], category_id=str(opt.get("categoryId") or ""))
    _apply_profile_rules(opt, profile)
    opt["description"] = sanitize_generated_description_html(opt.get("description", ""))
    opt["description"] = _sanitize_specifications_table_html(opt.get("description", ""))
    # Hard requirement: English buyer copy + store banner/footer shell.
    # Rebuilds Chinese / keyword-soup KEY FEATURES into conversion copy + template.
    opt["description"] = ensure_store_description_template(
        opt.get("description", ""),
        title=opt.get("title") or source_title or "",
        source_description=source_description or "",
        attributes=attributes or {},
        specs=specs or {},
        aspects=opt.get("aspects") or {},
    )
    # Assembly note must land AFTER template ensure so rebuild/wrap cannot wipe it.
    apply_source_assembly_requirement(
        opt,
        source_description=source_description,
        attributes=attributes,
        specs=specs,
    )
    apply_aspect_assembly_description_requirement(opt)
    sanitize_single_value_aspects(opt["aspects"])

    length = updates.get("Item Length") or first_aspect_text(opt["aspects"], "Item Length")
    width = updates.get("Item Width") or first_aspect_text(opt["aspects"], "Item Width")
    height = updates.get("Item Height") or first_aspect_text(opt["aspects"], "Item Height")
    weight = updates.get("Item Weight") or first_aspect_text(opt["aspects"], "Item Weight")
    try:
        opt["description"] = replace_description_measurements(
            opt["description"],
            length=float(re.search(r"(\d+(?:\.\d+)?)", length).group(1)) if length else None,
            width=float(re.search(r"(\d+(?:\.\d+)?)", width).group(1)) if width else None,
            height=float(re.search(r"(\d+(?:\.\d+)?)", height).group(1)) if height else None,
            weight=float(re.search(r"(\d+(?:\.\d+)?)", weight).group(1)) if weight else None,
        )
    except Exception:
        pass

    opt["source_facts"] = build_source_facts(
        source_title=source_title,
        source_description=source_description,
        attributes=attributes,
        specs=specs,
        videos=videos,
    )
    claim_facts = opt["source_facts"].get("claims") if isinstance(opt["source_facts"], dict) else {}
    for claim_name, feature_value in SUPPORTED_FEATURE_CLAIMS.items():
        claim_state = claim_facts.get(claim_name) if isinstance(claim_facts, Mapping) else {}
        if isinstance(claim_state, Mapping) and claim_state.get("supported"):
            _append_unique_aspect_value(opt["aspects"], "Features", feature_value)
    opt["description"] = _ensure_key_features_block(
        opt.get("description", ""),
        title=opt.get("title", ""),
        aspects=opt.get("aspects", {}),
        source_facts=opt["source_facts"],
    )

    # ── Layer 2: Deterministic claim violation detection ──
    from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations
    source_constraints = build_source_constraints(
        attrs=attributes,
        specs=specs,
        source_description=source_description,
        source_title=source_title,
    )
    claim_violations = detect_claim_violations(
        source_constraints=source_constraints,
        generated_title=opt.get("title", ""),
        generated_description=opt.get("description", ""),
        generated_aspects=opt.get("aspects", {}),
    )
    # 记录违规到 source_facts 供下游使用
    opt["source_facts"]["claim_violations"] = [
        {
            "claim_type": v.claim_type,
            "claim_text": v.claim_text,
            "location": v.location,
            "severity": v.severity,
        }
        for v in claim_violations
    ]

    # ── Layer 3: LLM Fact Checker (runs conditionally) ──
    # Skip when buyer copy already uses the English store template shell.
    # Deterministic FactSheet (run_listing_qc) remains the semantic gate for
    # those listings; LLM was double-blocking compliant template rebuilds.
    enable_llm_fact_check = True  # Feature flag
    critical_layer2_violations = [v for v in claim_violations if v.severity == "CRITICAL"]
    desc_now = opt.get("description") or ""
    store_template_en = description_uses_store_template(desc_now) and not description_contains_cjk(
        desc_now
    )
    if enable_llm_fact_check and not critical_layer2_violations and not store_template_en:
        from src.utils.llm_fact_checker import llm_fact_check
        llm_violations = llm_fact_check(
            source_title=source_title,
            source_description=source_description,
            source_specs=specs or {},
            generated_title=opt.get("title", ""),
            generated_description=opt.get("description", "")
        )
        if llm_violations:
            opt["source_facts"]["llm_fact_check_results"] = llm_violations
    elif store_template_en:
        opt["source_facts"]["llm_fact_check_results"] = []
        opt["source_facts"]["llm_fact_check_skipped"] = "store_template_en"

    return opt


def description_contains_cjk(text: str | None) -> bool:
    """True when buyer-facing copy contains CJK ideographs."""
    return bool(CJK_CHAR_RE.search(text or ""))


def _store_profile_or_none():
    try:
        from src.utils.store_profile import get_store_profile

        return get_store_profile()
    except Exception:
        return None


def description_has_store_banner(text: str | None, profile: Any = None) -> bool:
    profile = profile if profile is not None else _store_profile_or_none()
    marker = str(getattr(profile, "quality_banner_marker", "") or "").strip().lower()
    if not marker:
        return True
    return marker in html_lib.unescape(text or "").lower()


def description_has_store_footer(text: str | None, profile: Any = None) -> bool:
    profile = profile if profile is not None else _store_profile_or_none()
    marker = str(getattr(profile, "quality_footer_marker", "") or "").strip().lower()
    if not marker:
        return True
    return marker in html_lib.unescape(text or "").lower()


def description_uses_store_template(text: str | None, profile: Any = None) -> bool:
    """Buyer-facing description carries the configured store banner + footer markers."""
    profile = profile if profile is not None else _store_profile_or_none()
    return description_has_store_banner(text, profile) and description_has_store_footer(text, profile)


def build_store_description_shell(
    *,
    title: str,
    body_html: str = "",
    profile: Any = None,
) -> str:
    """Minimal brand shell: banner + optional body + footer (idempotent markers)."""
    profile = profile if profile is not None else _store_profile_or_none()
    brand = html_lib.escape(str(getattr(profile, "brand_name", "AquaVerve") or "AquaVerve").upper())
    tagline = html_lib.escape(str(getattr(profile, "brand_tagline", "") or ""))
    footer_html = str(getattr(profile, "footer_html", "") or "").strip()
    if not footer_html:
        l1 = html_lib.escape(str(getattr(profile, "description_footer_line1", "") or ""))
        l2 = html_lib.escape(str(getattr(profile, "description_footer_line2", "") or ""))
        footer_html = (
            '<div style="text-align:center;padding:20px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">'
            f'<p style="margin:0;font-size:12px;color:#d4af37;letter-spacing:1px">{l1}</p>'
            f'<p style="margin:8px 0 0;font-size:11px;color:#808080">{l2}</p>'
            "</div>"
        )
    safe_title = html_lib.escape(_clean_text(title) or brand)
    body = body_html or ""
    return (
        '<div style="max-width:900px;margin:0 auto;font-family:Arial,sans-serif;color:#1a1a1a;line-height:1.7">'
        '<div style="text-align:center;padding:30px 15px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">'
        f'<h1 style="margin:0;font-size:28px;font-weight:300;letter-spacing:6px;color:#d4af37">{brand}</h1>'
        f'<p style="margin:8px 0 0;font-size:12px;color:#a0a0a0;letter-spacing:2px">{tagline}</p>'
        "</div>"
        '<div style="background:#f8f9fa;padding:25px;text-align:center;border-bottom:2px solid #d4af37">'
        f'<h2 style="margin:0;font-size:20px;color:#2d3436;font-weight:500">{safe_title}</h2>'
        "</div>"
        f"{body}"
        f"{footer_html}"
        "</div>"
    )


def ensure_store_description_template(
    description: str | None,
    *,
    title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    aspects: Mapping[str, Any] | None = None,
    profile: Any = None,
) -> str:
    """Ensure buyer-facing description is English and uses the store template shell.

    - CJK / empty / keyword-soup KEY FEATURES → full conversion rebuild
    - English body missing only banner/footer → wrap (preserve assembly/spec copy)
    - Already good store template → unchanged
    """
    profile = profile if profile is not None else _store_profile_or_none()
    desc = description or ""

    thin = False
    try:
        from src.utils.conversion_copy import is_thin_key_features_description

        thin = is_thin_key_features_description(desc)
    except Exception:
        thin = False

    has_shell = description_uses_store_template(desc, profile)
    has_cjk = description_contains_cjk(desc)

    # Healthy English store template with real bullets — keep as-is.
    if desc.strip() and has_shell and not has_cjk and not thin:
        return desc

    needs_full_rebuild = (not desc.strip()) or has_cjk or thin

    if needs_full_rebuild:
        rebuilt = ""
        try:
            from src.services.semantic_rewrite import build_description_from_source

            rebuilt = build_description_from_source(
                title=title or "",
                source_description=source_description or "",
                attrs=dict(attributes or {}),
                specs=dict(specs or {}),
                aspects=dict(aspects or {}),
            ) or ""
        except Exception:
            try:
                from scripts.audit_fix_active_listings import build_structured_description_from_source

                rebuilt = build_structured_description_from_source(
                    title or "",
                    source_description or "",
                    dict(attributes or {}),
                    dict(specs or {}),
                    dict(aspects or {}),
                ) or ""
            except Exception:
                rebuilt = ""

        if (
            rebuilt
            and not description_contains_cjk(rebuilt)
            and description_uses_store_template(rebuilt, profile)
        ):
            return rebuilt

    # English body without shell (or rebuild failed): wrap existing copy so
    # assembly notes / specs rows from normalize are not discarded.
    if description_contains_cjk(desc):
        body = ""
    else:
        body = desc
    wrapped = build_store_description_shell(title=title or "", body_html=body, profile=profile)
    if not description_contains_cjk(wrapped) and description_uses_store_template(wrapped, profile):
        return wrapped
    return wrapped or desc


def validate_listing_quality(
    optimization: Mapping[str, Any] | None,
    *,
    source_title: str = "",
    source_description: str = "",
    attributes: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    images: list[str] | None = None,
    videos: list[str] | None = None,
    category_matcher: Any = None,
) -> list[ListingQualityIssue]:
    """Return quality issues that should block READY/publish."""
    opt = dict(optimization or {})
    aspects = opt.get("aspects") if isinstance(opt.get("aspects"), dict) else {}
    title = _clean_text(opt.get("title") or source_title or "")
    description = _clean_text(opt.get("description") or source_description or "")
    image_count = len(images or [])
    issues: list[ListingQualityIssue] = []
    source_facts = opt.get("source_facts") if isinstance(opt.get("source_facts"), dict) else build_source_facts(
        source_title=source_title,
        source_description=source_description,
        attributes=attributes,
        specs=specs,
        videos=videos,
    )

    if not title:
        issues.append(ListingQualityIssue("missing_title", "missing listing title", field="title"))
    if not description:
        issues.append(ListingQualityIssue("missing_description", "missing listing description", field="description"))
    else:
        has_key_features = "key features" in description.lower()
        has_bullets = "<li" in description.lower()
        if has_key_features != has_bullets:
            issues.append(ListingQualityIssue("description_incomplete", "description is missing KEY FEATURES or bullet points", field="description"))
        if description_contains_cjk(description) or description_contains_cjk(title):
            issues.append(
                ListingQualityIssue(
                    "description_contains_cjk",
                    "buyer-facing title/description contains Chinese (CJK) characters; English store template required",
                    field="description",
                )
            )
        store_profile = _store_profile_or_none()
        if not description_has_store_banner(description, store_profile):
            issues.append(
                ListingQualityIssue(
                    "missing_store_banner",
                    "description missing store brand banner/template marker",
                    field="description",
                )
            )
        if not description_has_store_footer(description, store_profile):
            issues.append(
                ListingQualityIssue(
                    "missing_store_footer",
                    "description missing store footer template marker",
                    field="description",
                )
            )
    if image_count < MIN_READY_IMAGE_COUNT:
        issues.append(
            ListingQualityIssue(
                "insufficient_images",
                f"only {image_count} image(s); at least {MIN_READY_IMAGE_COUNT} required",
                field="images",
            )
        )

    blob = "\n".join([title, description, str(aspects)])
    bad_tokens = [bad for bad in BAD_TEXT_REPLACEMENTS if bad in blob]
    if bad_tokens:
        issues.append(
            ListingQualityIssue(
                "bad_text_artifact",
                f"contains mojibake text: {', '.join(sorted(set(bad_tokens)))}",
            )
        )

    category_id = str(opt.get("categoryId") or "").strip()
    profile = classify_listing_profile(" ".join(part for part in (source_title, title) if part), description, category_id)
    if profile.category_id and category_id != profile.category_id:
        issues.append(
            ListingQualityIssue(
                "category_mismatch",
                f"{profile.kind} should use category {profile.category_id}, got {category_id or 'missing'}",
                field="categoryId",
            )
        )
    elif not category_id:
        issues.append(ListingQualityIssue("missing_category", "missing categoryId", field="categoryId"))

    if category_matcher and category_id:
        if not category_matcher.is_category_plausible_for_text(title, category_id, opt.get("categoryName")):
            issues.append(
                ListingQualityIssue(
                    "category_mismatch",
                    f"category {category_id} is implausible for title '{title[:120]}'",
                    field="categoryId",
                )
            )

    for error in measurement_validation_errors(aspects):
        code = "missing_measurement" if "missing measurement" in error else "invalid_measurement"
        field_match = re.search(r"Item (?:Length|Width|Height|Weight)", error)
        issues.append(ListingQualityIssue(code, error, field=field_match.group(0) if field_match else None))

    expected_assembly = source_facts.get("assembly_required") or infer_source_assembly_required(
        attributes,
        specs,
        source_description,
    )
    if expected_assembly:
        current_assembly = first_aspect_text(aspects, "Assembly Required")
        if current_assembly.lower() != expected_assembly.lower():
            issues.append(
                ListingQualityIssue(
                    "assembly_required_mismatch",
                    f"Assembly Required should be {expected_assembly}, got {current_assembly or 'missing'}",
                    field="Assembly Required",
                )
            )
        contradictions = find_assembly_description_contradictions(description, expected_assembly)
        if contradictions:
            issues.append(
                ListingQualityIssue(
                    "assembly_description_contradiction",
                    f"description contradicts source Assembly Required={expected_assembly}",
                    field="description",
                )
            )
        packaging = _as_list(aspects.get("Packaging"))
        if expected_assembly == "Yes" and any("fully assembled" in value.lower() for value in packaging):
            issues.append(
                ListingQualityIssue(
                    "assembly_packaging_contradiction",
                    "Packaging must not say Fully Assembled when source says assembly is required",
                    field="Packaging",
                )
            )

    current_assembly = first_aspect_text(aspects, "Assembly Required")
    normalized_current_assembly = _normalize_yes_no(current_assembly)
    if normalized_current_assembly and not has_expected_assembly_copy(description, normalized_current_assembly):
        issues.append(
            ListingQualityIssue(
                "assembly_description_missing",
                f"description must explicitly state Assembly Required={normalized_current_assembly}",
                field="description",
            )
        )

    claim_facts = source_facts.get("claims") if isinstance(source_facts.get("claims"), Mapping) else {}
    for claim_name, label in CLAIM_LABELS.items():
        claim_state = claim_facts.get(claim_name) if isinstance(claim_facts.get(claim_name), Mapping) else {}
        if claim_state.get("supported"):
            continue
        locations = _generated_claim_locations(
            title=title,
            description=description,
            aspects=aspects,
            claim_name=claim_name,
        )
        if locations:
            issues.append(
                ListingQualityIssue(
                    f"unsupported_{claim_name}_claim",
                    f"listing mentions {label} in {', '.join(locations)} but source does not support it",
                    field=locations[0],
                )
            )

    for claim_name, feature_value in SUPPORTED_FEATURE_CLAIMS.items():
        claim_state = claim_facts.get(claim_name) if isinstance(claim_facts.get(claim_name), Mapping) else {}
        if not claim_state.get("supported"):
            continue
        feature_values = _as_list(aspects.get("Features"))
        if not any(value.lower() == feature_value.lower() for value in feature_values):
            issues.append(
                ListingQualityIssue(
                    f"missing_supported_{claim_name}_feature",
                    f"source supports {CLAIM_LABELS.get(claim_name, claim_name)} but Features is missing {feature_value}",
                    field="Features",
                )
            )

    video_info = source_facts.get("video") if isinstance(source_facts.get("video"), Mapping) else {}
    if video_info.get("present") and video_info.get("preflight_status") == "blocked":
        detail = ", ".join(video_info.get("issue_codes") or []) or "unknown reason"
        issues.append(
            ListingQualityIssue(
                "source_video_not_publishable",
                f"source video exists but is not directly publishable: {detail}",
                field="videos",
            )
        )

    for forbidden in sorted(FURNITURE_FORBIDDEN_ASPECTS):
        if forbidden in aspects:
            issues.append(
                ListingQualityIssue(
                    "forbidden_aspect",
                    f"{forbidden} is not applicable to this furniture listing",
                    field=forbidden,
                )
            )

    if profile.kind != "unknown":
        expected_type = profile.type_value
        if expected_type and first_aspect_text(aspects, "Type") != expected_type:
            issues.append(
                ListingQualityIssue(
                    "wrong_type_aspect",
                    f"Type should be {expected_type}",
                    field="Type",
                )
            )
        if profile.set_includes and first_aspect_text(aspects, "Set Includes") != profile.set_includes:
            issues.append(
                ListingQualityIssue(
                    "wrong_set_includes",
                    f"Set Includes should be {profile.set_includes}",
                    field="Set Includes",
                )
            )
        if profile.kind == "dining_set":
            expected_count = infer_number_of_items_in_set(title, category_id=category_id or profile.category_id or "")
            current_count = first_aspect_text(aspects, "Number of Items in Set")
            if expected_count and current_count != expected_count:
                issues.append(
                    ListingQualityIssue(
                        "wrong_set_count",
                        f"Number of Items in Set should be {expected_count}",
                        field="Number of Items in Set",
                    )
                )

    measurement_values = [
        first_aspect_text(aspects, key)
        for key in REQUIRED_MEASUREMENT_ASPECT_KEYS
        if first_aspect_text(aspects, key)
    ]
    if len(measurement_values) == 3 and not _numbers_present(description, *measurement_values):
        issues.append(
            ListingQualityIssue(
                "description_measurement_mismatch",
                "description does not contain the item length/width/height numbers",
                field="description",
            )
        )

    # ── Layer 2 claim violations as quality issues ──
    stored_violations = (source_facts.get("claim_violations") or [])
    for cv in stored_violations:
        if cv.get("severity") in ("CRITICAL", "HIGH"):
            issues.append(
                ListingQualityIssue(
                    f"claim_violation_{cv['claim_type']}",
                    f"Unsupported claim: {cv['claim_text']} in {cv['location']}",
                    field=cv.get("location"),
                )
            )

    # ── Layer 3 LLM fact check violations as quality issues ──
    llm_violations = (source_facts.get("llm_fact_check_results") or [])
    for llm_v in llm_violations:
        if llm_v.get("severity") == "HIGH":
            issues.append(
                ListingQualityIssue(
                    "llm_fact_violation",
                    f"LLM Fact Check Failed: {llm_v.get('quote')} - {llm_v.get('reason')}",
                    field="description",
                )
            )

    return issues


def blocking_issue_messages(issues: list[ListingQualityIssue]) -> list[str]:
    return [issue.message for issue in issues if issue.severity == "BLOCKER"]
