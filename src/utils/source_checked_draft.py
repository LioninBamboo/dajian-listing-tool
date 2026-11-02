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
from src.utils.claim_strip import claim_matches_text, strip_html_to_text
from src.utils.listing_fact_sheet import check_fact_sheet_violations
from src.utils.listing_quality_gate import _iter_source_entries

DEFAULT_TITLE_CONNECTORS = frozenset(
    {"with", "and", "for", "set", "of", "the", "a", "in", "&", "-", ","}
)
TITLE_MAX_LEN = 80
PUBLISHED_STATUSES = frozenset({"published", "ended"})
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


def source_entries_from_row(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    fields = source_fields_from_row(row)
    return _iter_source_entries(
        source_title=fields["title"],
        source_description=fields["description"],
        attributes=fields["attributes"],
        specs=fields["specs"],
    )


def source_text_from_row(row: Mapping[str, Any]) -> str:
    fields = source_fields_from_row(row)
    parts = [fields["title"], strip_html_to_text(fields["description"])]
    for mapping in (fields["attributes"], fields["specs"]):
        for key, value in mapping.items():
            parts.append(f"{key}: {value}")
    return " ".join(part for part in parts if str(part).strip())


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


def measurement_to_inches(num: float, unit: str) -> float | None:
    factor = _INCH_FACTORS.get(_unit_key(unit))
    if factor is None:
        return None
    return float(num) * factor


def extract_measurements(text: str) -> list[tuple[float, str, float]]:
    found: list[tuple[float, str, float]] = []
    for match in _MEASUREMENT_RE.finditer(str(text or "")):
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
    for match in _MEASUREMENT_RE.finditer(remaining):
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
    return any(
        measurements_match(item[2], src[2]) for item in wanted for src in source
    )


def _compound_suffix_hit(token: str, source_words: Iterable[str]) -> bool:
    folded = token.casefold()
    if len(folded) < 3:
        return False
    for word in source_words:
        if word == folded or len(word) <= len(folded):
            continue
        if word.endswith(folded) and (len(word) - len(folded)) >= 4:
            return True
    return False


def locate_token_evidence(
    token: str,
    kind: str,
    entries: Iterable[tuple[str, str]],
    *,
    connectors: Iterable[str] | None = None,
) -> TokenEvidence:
    allowed = set(connectors if connectors is not None else title_connectors())
    folded = token.casefold()
    if kind == "word" and folded in allowed:
        return TokenEvidence(token=token, sourced=True, evidence="connector", kind="word")
    for label, raw in entries:
        text = strip_html_to_text(raw)
        if kind == "measure":
            if _measurement_in_source(token, text):
                return TokenEvidence(token=token, sourced=True, evidence=f"{label}={text[:80]}", kind="measure")
            continue
        words = _word_set(text)
        if folded in words or simple_stem(folded) in {simple_stem(word) for word in words}:
            return TokenEvidence(token=token, sourced=True, evidence=f"{label}={text[:80]}", kind="word")
        if _compound_suffix_hit(folded, words):
            return TokenEvidence(token=token, sourced=True, evidence=f"{label}={text[:80]}", kind="word")
    return TokenEvidence(token=token, sourced=False, evidence="", kind=kind)


def check_title_tokens(title: str, row: Mapping[str, Any]) -> SourceCheckResult:
    entries = source_entries_from_row(row)
    connectors = title_connectors()
    evidence: list[TokenEvidence] = []
    unsourced: list[str] = []
    content_seen = False
    for token, kind in tokenize_content(title):
        hit = locate_token_evidence(token, kind, entries, connectors=connectors)
        evidence.append(hit)
        if kind == "word" and token.casefold() in connectors:
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


def _aspect_value_in_source(value: str, source_text: str) -> bool:
    if not str(value or "").strip():
        return False
    if claim_matches_text(value, source_text):
        return True
    if extract_measurements(value) and _measurement_in_source(value, source_text):
        return True
    folded = strip_html_to_text(value).casefold()
    source_folded = strip_html_to_text(source_text).casefold()
    if folded and re.search(rf"(?<![A-Za-z0-9]){re.escape(folded)}(?![A-Za-z0-9])", source_folded):
        return True
    return False


def locate_aspect_evidence(value: str, row: Mapping[str, Any]) -> TokenEvidence:
    for label, raw in source_entries_from_row(row):
        text = strip_html_to_text(raw)
        if _aspect_value_in_source(value, text):
            return TokenEvidence(token=value, sourced=True, evidence=f"{label}={text[:80]}", kind="aspect")
    return TokenEvidence(token=value, sourced=False, evidence="", kind="aspect")


def validate_aspect_value(value: str, row: Mapping[str, Any]) -> SourceCheckResult:
    cleaned = str(value or "").strip()
    if not cleaned:
        return SourceCheckResult(ok=False, reason="aspect_value_empty")
    hit = locate_aspect_evidence(cleaned, row)
    if not hit.sourced:
        return SourceCheckResult(ok=False, reason="aspect_unsourced", unsourced=[cleaned], evidence=[hit])
    return SourceCheckResult(ok=True, evidence=[hit])


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
    fact_status = str(fact.get("status") or "unavailable")
    return SourceCheckResult(
        ok=True,
        reason="pass" if fact_status in {"pass", "unavailable"} else fact_status,
    )
