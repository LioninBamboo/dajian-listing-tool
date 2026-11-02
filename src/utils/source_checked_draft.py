"""Source-checked title/aspect writes for local draft SKUs.

Used by ``sku_field_edit`` so a draft whose title states a wrong fact can be
replaced when every content token (or aspect value) is grounded in the stored
GIGA source. No eBay calls live here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.utils.banned_terms_guard import scan_listing
from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations
from src.utils.claim_strip import strip_html_to_text
from src.utils.listing_fact_sheet import check_fact_sheet_violations
from src.utils.listing_quality_gate import _iter_source_entries

DEFAULT_TITLE_CONNECTORS = frozenset(
    {"with", "and", "for", "of", "the", "a", "in", "&", "-", ","}
)
TITLE_MAX_LEN = 80
PUBLISHED_STATUSES = frozenset({"published", "ended"})
PROTECTED_ASPECTS = frozenset(
    {
        "brand",
        "mpn",
        "upc",
        "ean",
        "isbn",
        "manufacturer part number",
    }
)
ASPECT_NAME_FAMILIES = {
    "material": "material",
    "frame material": "material",
    "main material": "material",
    "upholstery material": "material",
    "upholstery": "material",
    "color": "color",
    "colour": "color",
    # GIGA attributes use "Main Color" (e.g. "Beige,Natural Wood"), not "Color".
    # _named_source_fields still requires the whitespace-only key to be exactly
    # "main color", so "Main-Color" / "Main_Color" / "Main Color Family" do not match.
    "main color": "color",
    "type": "type",
    "product type": "type",
    "item type": "type",
}
DIMENSION_ASPECT_FAMILIES = {
    "length": "length",
    "item length": "length",
    "assembled length": "length",
    "overall length": "length",
    "width": "width",
    "item width": "width",
    "assembled width": "width",
    "overall width": "width",
    "height": "height",
    "item height": "height",
    "assembled height": "height",
    "overall height": "height",
    "depth": "depth",
    "item depth": "depth",
    "assembled depth": "depth",
    "seat height": "seat_height",
    "seat width": "seat_width",
    "seat depth": "seat_depth",
}
_COMPOUND_ALLOWLIST: dict[str, frozenset[str]] = {
    "countertop": frozenset({"top"}),
    "tabletop": frozenset({"top"}),
    "bookshelf": frozenset({"shelf", "shelves"}),
    "bookshelves": frozenset({"shelf", "shelves"}),
}
_NEGATIVE_ATTR_VALUES = frozenset(
    {
        "no",
        "none",
        "false",
        "n/a",
        "na",
        "n.a",
        "n.a.",
        "",
        "0",
        "0.0",
        "-",
        "not applicable",
        "not specified",
    }
)
_COUNT_PHRASE_WORDS = frozenset({"set", "piece", "pieces", "pack", "pcs"})
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MEASUREMENT_RE = re.compile(
    r"""
    (?P<num>\d+(?:\.\d+)?)
    \s*-?\s*
    (?P<unit>
        ["”]|
        inches?\b|
        inch\b|
        in\b|
        cm\b|
        mm\b|
        feet\b|
        foot\b|
        ft\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_IN_FOLLOWED_BY_NUMBER = re.compile(r"^\s*-?\s*\d")
_IN_UNIT_TAIL = re.compile(
    r"""
    ^(?:
        \s*$ |
        \s*[|,;:)\]}./] |
        \s*[x×](?:\s|$) |
        \s*(?:w|d|h|wide|width|deep|depth|high|height|tall|long|length)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_COUNT_PHRASE_RE = re.compile(
    r"""
    (?:
        \bset\s+of\s+(?P<n1>\d+)\b
        |
        \b(?P<n2>\d+)\s*-?\s*(?:pieces?|pcs|pack)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BARE_COUNT_NOUN_RE = re.compile(
    r"""
    \b(?P<n>\d+)\s+
    (?:(?P<adj>[A-Za-z]+)\s+){0,3}
    (?P<noun>[A-Za-z]+(?:ies|ves|ches|shes|sses|xes|s))\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_COUNT_NOUN_SKIP = frozenset(
    {
        "inches",
        "inch",
        "cms",
        "mms",
        "feet",
        "pcs",
        "pieces",
        "piece",
        "packs",
        "pack",
        "this",
        "its",
        "as",
        "is",
        "has",
        "was",
    }
)
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|/|&|\band\b)\s*", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")
_INCH_FACTORS = {
    '"': 1.0,
    "”": 1.0,
    "in": 1.0,
    "inch": 1.0,
    "inches": 1.0,
    "cm": 1.0 / 2.54,
    "mm": 1.0 / 25.4,
    "ft": 12.0,
    "foot": 12.0,
    "feet": 12.0,
}
_MEASUREMENT_TOLERANCE_INCH = 0.06


@dataclass(frozen=True)
class TokenEvidence:
    token: str
    sourced: bool
    evidence: str = ""
    kind: str = "word"

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "sourced": self.sourced,
            "evidence": self.evidence,
            "kind": self.kind,
        }


@dataclass
class SourceCheckResult:
    ok: bool
    reason: str = ""
    unsourced: list[str] = field(default_factory=list)
    evidence: list[TokenEvidence] = field(default_factory=list)
    claim_violations: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "unsourced": list(self.unsourced),
            "evidence": [item.as_dict() for item in self.evidence],
        }


def title_connectors() -> frozenset[str]:
    raw = os.environ.get("SKU_FIELD_EDIT_TITLE_CONNECTORS")
    if raw is None:
        return DEFAULT_TITLE_CONNECTORS
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


def _as_mapping(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        import json

        try:
            loaded = json.loads(text)
        except Exception:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def source_fields_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": str(row.get("title") or ""),
        "description": str(row.get("description") or ""),
        "attributes": _as_mapping(row.get("attributes")),
        "specs": _as_mapping(row.get("specs")),
    }


def _attr_value_is_negative(value: Any) -> bool:
    text = strip_html_to_text(str(value if value is not None else "")).casefold().strip(" .:")
    return text in _NEGATIVE_ATTR_VALUES


def _filter_negative_source_entries(entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    kept: list[tuple[str, str]] = []
    for label, raw in entries:
        if str(label).startswith(("attribute:", "spec:")) and ":" in str(raw):
            _key, value = str(raw).split(":", 1)
            if _attr_value_is_negative(value):
                continue
        kept.append((label, raw))
    return kept


def source_entries_from_row(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    fields = source_fields_from_row(row)
    return _filter_negative_source_entries(
        _iter_source_entries(
            source_title=fields["title"],
            source_description=fields["description"],
            attributes=fields["attributes"],
            specs=fields["specs"],
        )
    )


def source_text_from_row(row: Mapping[str, Any]) -> str:
    return " ".join(text for _label, text in source_entries_from_row(row) if str(text).strip())


def simple_stem(token: str) -> str:
    text = str(token or "").casefold()
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("ves") and len(text) > 4:
        return text[:-3] + "f"
    if text.endswith(("ches", "shes", "sses", "xes")) and len(text) > 4:
        return text[:-2]
    if text.endswith("s") and not text.endswith("ss") and len(text) > 3:
        return text[:-1]
    return text


def _unit_key(unit: str) -> str:
    cleaned = str(unit or "").strip().casefold().rstrip(".")
    if cleaned in {'"', "”"}:
        return '"'
    return cleaned


def _inch_unit_allowed(text: str, match: re.Match[str]) -> bool:
    unit = _unit_key(match.group("unit"))
    if unit != "in":
        return True
    rest = text[match.end() :]
    if _IN_FOLLOWED_BY_NUMBER.match(rest):
        return False
    return bool(_IN_UNIT_TAIL.match(rest))


def iter_measurement_matches(text: str) -> list[re.Match[str]]:
    return [match for match in _MEASUREMENT_RE.finditer(str(text or "")) if _inch_unit_allowed(text, match)]


def measurement_to_inches(num: float, unit: str) -> float | None:
    factor = _INCH_FACTORS.get(_unit_key(unit))
    if factor is None:
        return None
    return float(num) * factor


def extract_measurements(text: str) -> list[tuple[float, str, float]]:
    found: list[tuple[float, str, float]] = []
    for match in iter_measurement_matches(text):
        inches = measurement_to_inches(float(match.group("num")), match.group("unit"))
        if inches is None:
            continue
        found.append((float(match.group("num")), match.group("unit"), inches))
    return found


def tokenize_content(text: str) -> list[tuple[str, str]]:
    """Return ``(display_token, kind)`` pairs. kind is ``measure`` or ``word``."""
    remaining = str(text or "")
    tokens: list[tuple[str, str]] = []
    cursor = 0
    for match in iter_measurement_matches(remaining):
        prefix = remaining[cursor : match.start()]
        tokens.extend((word, "word") for word in _WORD_RE.findall(prefix))
        tokens.append((match.group(0).strip(), "measure"))
        cursor = match.end()
    tokens.extend((word, "word") for word in _WORD_RE.findall(remaining[cursor:]))
    return tokens


def _word_set(text: str) -> set[str]:
    return {word.casefold() for word in _WORD_RE.findall(strip_html_to_text(text or ""))}


def measurements_match(left_inches: float, right_inches: float) -> bool:
    return abs(left_inches - right_inches) <= _MEASUREMENT_TOLERANCE_INCH


def _measurement_in_source(token: str, source_text: str) -> bool:
    wanted = extract_measurements(token)
    if not wanted:
        return False
    source = extract_measurements(source_text)
    return any(measurements_match(item[2], src[2]) for item in wanted for src in source)


def _compound_allowlist_hit(token: str, source_words: Iterable[str]) -> bool:
    folded = token.casefold()
    stemmed = simple_stem(folded)
    for word in source_words:
        allowed = _COMPOUND_ALLOWLIST.get(word) or _COMPOUND_ALLOWLIST.get(simple_stem(word))
        if not allowed:
            continue
        allowed_stems = {simple_stem(item) for item in allowed}
        if folded in allowed or stemmed in allowed or stemmed in allowed_stems:
            return True
    return False


def extract_count_phrase_numbers(text: str) -> set[int]:
    numbers: set[int] = set()
    for match in _COUNT_PHRASE_RE.finditer(str(text or "")):
        raw = match.group("n1") or match.group("n2")
        if raw:
            numbers.add(int(raw))
    return numbers


def extract_noun_count_claims(text: str) -> list[tuple[int, str]]:
    """Bare ``N <adj>* <plural-noun>`` claims, e.g. ``4 Dining Chairs``."""
    claims: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for match in _BARE_COUNT_NOUN_RE.finditer(str(text or "")):
        noun = str(match.group("noun") or "").casefold()
        if noun in _COUNT_NOUN_SKIP or noun in title_connectors():
            continue
        if not _looks_plural_noun(noun):
            continue
        item = (int(match.group("n")), simple_stem(noun))
        if item in seen:
            continue
        seen.add(item)
        claims.append(item)
    return claims


def _looks_plural_noun(word: str) -> bool:
    folded = str(word or "").casefold()
    if len(folded) <= 3 or folded.endswith("ss"):
        return False
    return folded.endswith(("ies", "ves", "ches", "shes", "sses", "xes", "s"))


def locate_token_evidence(
    token: str,
    kind: str,
    entries: Iterable[tuple[str, str]],
    *,
    connectors: Iterable[str] | None = None,
    count_phrase_ok: bool = False,
) -> TokenEvidence:
    allowed = set(connectors if connectors is not None else title_connectors())
    folded = token.casefold()
    if kind == "word" and folded in allowed:
        return TokenEvidence(token=token, sourced=True, evidence="connector", kind="word")
    if kind == "word" and count_phrase_ok and folded in _COUNT_PHRASE_WORDS:
        return TokenEvidence(token=token, sourced=True, evidence="count_phrase", kind="word")
    for label, raw in entries:
        text = strip_html_to_text(raw)
        if kind == "measure":
            if _measurement_in_source(token, text):
                return TokenEvidence(token=token, sourced=True, evidence=f"{label}={text[:80]}", kind="measure")
            continue
        words = _word_set(text)
        if folded in words or simple_stem(folded) in {simple_stem(word) for word in words}:
            return TokenEvidence(token=token, sourced=True, evidence=f"{label}={text[:80]}", kind="word")
        if _compound_allowlist_hit(folded, words):
            return TokenEvidence(token=token, sourced=True, evidence=f"{label}={text[:80]}", kind="word")
    return TokenEvidence(token=token, sourced=False, evidence="", kind=kind)


def check_title_tokens(title: str, row: Mapping[str, Any]) -> SourceCheckResult:
    entries = source_entries_from_row(row)
    connectors = title_connectors()
    source_blob = " ".join(text for _label, text in entries)
    title_counts = extract_count_phrase_numbers(title)
    source_counts = extract_count_phrase_numbers(source_blob)
    missing_counts = sorted(title_counts - source_counts)
    if missing_counts:
        return SourceCheckResult(
            ok=False,
            reason="title_count_phrase_unsourced",
            unsourced=[f"set/piece/pack {n}" for n in missing_counts],
        )
    source_nouns = set(extract_noun_count_claims(source_blob))
    missing_nouns = [
        f"{number} {noun}"
        for number, noun in extract_noun_count_claims(title)
        if (number, noun) not in source_nouns and number not in source_counts
    ]
    if missing_nouns:
        return SourceCheckResult(
            ok=False,
            reason="title_count_phrase_unsourced",
            unsourced=missing_nouns,
        )
    count_phrase_ok = bool(title_counts) and not missing_counts
    evidence: list[TokenEvidence] = []
    unsourced: list[str] = []
    content_seen = False
    for token, kind in tokenize_content(title):
        hit = locate_token_evidence(
            token,
            kind,
            entries,
            connectors=connectors,
            count_phrase_ok=count_phrase_ok,
        )
        evidence.append(hit)
        if kind == "word" and token.casefold() in connectors:
            continue
        if kind == "word" and count_phrase_ok and token.casefold() in _COUNT_PHRASE_WORDS:
            content_seen = True
            continue
        content_seen = True
        if not hit.sourced:
            unsourced.append(token)
    if not content_seen:
        return SourceCheckResult(
            ok=False,
            reason="title_no_content_tokens",
            unsourced=unsourced,
            evidence=evidence,
        )
    if unsourced:
        return SourceCheckResult(
            ok=False,
            reason="title_unsourced_tokens",
            unsourced=unsourced,
            evidence=evidence,
        )
    return SourceCheckResult(ok=True, evidence=evidence)


def validate_draft_title(title: str, row: Mapping[str, Any], *, banned_terms: Iterable[str] | None = None) -> SourceCheckResult:
    cleaned = str(title or "")
    if not cleaned.strip():
        return SourceCheckResult(ok=False, reason="title_empty")
    if len(cleaned) > TITLE_MAX_LEN:
        return SourceCheckResult(ok=False, reason="title_too_long")
    if _HTML_TAG_RE.search(cleaned) or "<" in cleaned or ">" in cleaned:
        return SourceCheckResult(ok=False, reason="title_has_html")
    hits = scan_listing(title=cleaned, terms=banned_terms)
    if hits:
        return SourceCheckResult(
            ok=False,
            reason="title_banned_term",
            unsourced=[hit.term for hit in hits],
        )
    return check_title_tokens(cleaned, row)


def _normalize_aspect_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(name or "").casefold()).strip()


def _whitespace_key(name: str) -> str:
    """Casefold and trim/collapse whitespace only. Hyphens and underscores stay."""
    return re.sub(r"\s+", " ", str(name or "").casefold()).strip()


def _aspect_family(name: str) -> str:
    return ASPECT_NAME_FAMILIES.get(_normalize_aspect_name(name), "")


def _dimension_family(name: str) -> str:
    return DIMENSION_ASPECT_FAMILIES.get(_normalize_aspect_name(name), "")


def _normalize_aspect_value(value: str) -> str:
    text = strip_html_to_text(str(value or "")).casefold()
    text = re.sub(r"[\s_\-]+", " ", text)
    return text.strip(" .:")


def _value_as_text(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(item).strip() for item in raw if str(item).strip())
    return str(raw if raw is not None else "")


def _named_value_items(source_value: str) -> list[str]:
    text = strip_html_to_text(source_value).strip()
    if not text:
        return []
    items = [part.strip() for part in _LIST_SPLIT_RE.split(text) if part.strip()]
    return items or [text]


def named_values_equal(wanted: str, source_value: str) -> bool:
    """Exact value match after case/whitespace/hyphen normalize, or one whole list item."""
    want = _normalize_aspect_value(wanted)
    if not want:
        return False
    if want == _normalize_aspect_value(source_value):
        return True
    return any(want == _normalize_aspect_value(item) for item in _named_value_items(source_value))


def _blank_main_color(raw: Any) -> bool:
    """None, ``""``, and whitespace-only ``Main Color`` are not a color source."""
    if raw is None:
        return True
    return not _value_as_text(raw).strip()


def _named_source_fields(row: Mapping[str, Any], name: str) -> list[tuple[str, str]]:
    fields = source_fields_from_row(row)
    family = _aspect_family(name)
    dimension = _dimension_family(name)
    wanted = _normalize_aspect_name(name)
    hits: list[tuple[str, str]] = []
    for source_name, mapping in (("attribute", fields["attributes"]), ("spec", fields["specs"])):
        for key, raw in mapping.items():
            text = _value_as_text(raw)
            key_n = _normalize_aspect_name(str(key))
            # Family lookup folds "_" and "-". Main Color does not: only case and
            # whitespace on the key, so Main-Color / Main_Color are different keys.
            if key_n == "main color" and _whitespace_key(key) != "main color":
                continue
            if key_n == "main color" and _blank_main_color(raw):
                continue
            if family and _aspect_family(key_n) == family:
                hits.append((f"{source_name}:{key}", text))
            elif dimension and _dimension_family(key_n) == dimension:
                hits.append((f"{source_name}:{key}", text))
            elif not family and not dimension and key_n == wanted:
                hits.append((f"{source_name}:{key}", text))
    return hits


def _aspect_fail(reason: str, value: str) -> SourceCheckResult:
    return SourceCheckResult(
        ok=False,
        reason=reason,
        unsourced=[value],
        evidence=[TokenEvidence(token=value, sourced=False, evidence="", kind="aspect")],
    )


def validate_aspect_value(name: str, value: str, row: Mapping[str, Any]) -> SourceCheckResult:
    """Validate ``NAME=VALUE`` against the named GIGA attribute/spec (or dimension)."""
    cleaned_name = str(name or "").strip()
    cleaned = str(value or "").strip()
    if not cleaned_name:
        return SourceCheckResult(ok=False, reason="field_not_editable")
    if _normalize_aspect_name(cleaned_name) in PROTECTED_ASPECTS:
        return SourceCheckResult(ok=False, reason="aspect_protected", unsourced=[cleaned_name])
    if not cleaned:
        return SourceCheckResult(ok=False, reason="aspect_value_empty")

    dimension = _dimension_family(cleaned_name)
    named = _named_source_fields(row, cleaned_name)
    if named:
        for label, raw in named:
            if named_values_equal(cleaned, raw):
                return SourceCheckResult(
                    ok=True,
                    evidence=[TokenEvidence(token=cleaned, sourced=True, evidence=f"{label}={raw[:80]}", kind="aspect")],
                )
            if dimension and extract_measurements(cleaned) and _measurement_in_source(cleaned, raw):
                return SourceCheckResult(
                    ok=True,
                    evidence=[TokenEvidence(token=cleaned, sourced=True, evidence=f"{label}={raw[:80]}", kind="aspect")],
                )
        return _aspect_fail("aspect_unsourced", cleaned)

    if dimension:
        source_blob = source_text_from_row(row)
        if extract_measurements(cleaned) and _measurement_in_source(cleaned, source_blob):
            return SourceCheckResult(
                ok=True,
                evidence=[TokenEvidence(token=cleaned, sourced=True, evidence="dimension", kind="aspect")],
            )
        return _aspect_fail("aspect_unsourced", cleaned)

    return _aspect_fail("aspect_no_named_source", cleaned)


def local_draft_refusal(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "").strip()
    listing_id = str(row.get("listing_id") or "").strip()
    if status.casefold() in PUBLISHED_STATUSES:
        return "published_not_editable"
    if listing_id:
        return "listing_id_present"
    return ""


def run_draft_claim_guards(
    conn: Any,
    row: Mapping[str, Any],
    *,
    candidate_title: str,
    candidate_description: str,
    candidate_aspects: Mapping[str, Any],
) -> SourceCheckResult:
    fields = source_fields_from_row(row)
    constraints = build_source_constraints(
        attrs=fields["attributes"],
        specs=fields["specs"],
        source_description=fields["description"],
        source_title=fields["title"],
    )
    claim_hits = detect_claim_violations(
        source_constraints=constraints,
        generated_title=candidate_title,
        generated_description=candidate_description,
        generated_aspects=dict(candidate_aspects or {}),
    )
    if claim_hits:
        return SourceCheckResult(
            ok=False,
            reason="unsourced_claim",
            unsourced=[str(item.claim_text) for item in claim_hits],
            claim_violations=[
                {
                    "claim_type": item.claim_type,
                    "claim_text": item.claim_text,
                    "severity": item.severity,
                    "source_evidence": item.source_evidence,
                }
                for item in claim_hits
            ],
        )
    fact = check_fact_sheet_violations(
        conn,
        source_title=fields["title"],
        source_description=fields["description"],
        source_attributes=fields["attributes"],
        source_specs=fields["specs"],
        candidate_title=candidate_title,
        candidate_description=candidate_description,
        candidate_aspects=dict(candidate_aspects or {}),
    ) or {}
    if str(fact.get("status") or "").lower() == "violations" and fact.get("violations"):
        return SourceCheckResult(
            ok=False,
            reason="fact_sheet_violation",
            unsourced=[str(item.get("claim_text") or "") for item in fact.get("violations") or []],
            claim_violations=list(fact.get("violations") or []),
        )
    fact_status = str(fact.get("status") or "unavailable").lower()
    if fact_status == "unavailable":
        return SourceCheckResult(ok=True, reason="fact_sheet_unavailable")
    if fact_status != "pass":
        return SourceCheckResult(ok=False, reason="fact_sheet_violation")
    return SourceCheckResult(ok=True, reason="pass")
