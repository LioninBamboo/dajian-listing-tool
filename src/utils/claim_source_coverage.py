"""Shared GIGA-source paraphrase coverage for unsupported-claim checks.

Owner policy 2026-11-02: a reasonable paraphrase of the GIGA source is
supported. Only claims with no basis in the source are flagged.

A draft phrase is SUPPORTED when, after one identical normalization on both
sides, every substantive content word is covered by the source and at least
one substantive content word matches. Coverage uses lowercase, hyphen / space
/ compound folding, simple lemma / plural folding, number words, and the
single reviewed synonym table below.

Hard claims are never relaxed by the synonym table: numbers / measurements /
load or weight capacity, materials, certifications, assembly / hardware /
install time, warranty, waterproof / fire-resistant, armrests / arm style,
heated / electric / USB. Existing material, leather, capacity, and
certification detectors stay the sole arbiters for those families.

This module is read-only. It does not write DB rows, files, or eBay.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Iterable

# Support is OR across these fields. No field has exclusive priority; an empty
# or missing field is skipped and never counts as support by itself.
SOURCE_FIELD_PRECEDENCE = (
    "title, description (including bullets), attributes, specs — any field "
    "may support; empty/None fields are ignored"
)

# One reviewed table. Phrase groups, not fuzzy nearest-guess.
# A claim phrase in a group is covered when the source contains any other
# (or the same) phrase in that group, after normalization.
CLAIM_PARAPHRASE_SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({
        "anti-slip",
        "anti slip",
        "antislip",
        "non-slip",
        "non slip",
        "nonslip",
    }),
    frozenset({
        "rubber",
        "rubberized",
    }),
    frozenset({
        "resilient",
        "rebound",
    }),
    frozenset({
        "padded",
        "cushion",
        "cushioned",
        "cushioning",
        "foam-padded",
        "foam padded",
        "foampadded",
        # GIGA W2105P595057 writes "filling" for the same pad as "cushioning".
        "filling",
    }),
    frozenset({
        "upholstery",
        "upholstered",
    }),
    frozenset({
        "open sections",
        "open section",
        "open shelves",
        "open shelf",
        "open shelving",
        "open storage",
    }),
    frozenset({
        "compact size",
        "compact footprint",
    }),
    frozenset({
        "will not take up extra space",
        "wont take up extra space",
        "space-saving",
        "space saving",
        "spacesaving",
        "space-smart",
        "space smart",
        "spacesmart",
    }),
    frozenset({
        "countertop",
        "countertop friendly",
        "countertop-friendly",
    }),
)

NUMBER_WORDS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "with", "on",
    "at", "by", "from", "as", "is", "are", "be", "its", "your", "our",
    "this", "that", "will", "not", "all", "up",
})

# Ignored for coverage. Alone they never make a claim supported.
_MARKETING_ADJECTIVES = frozenset({
    "smart",
    "design",
    "versatile",
    "versatility",
    "organization",
    "organisational",
    "organizational",
})

_FW_TABLE = str.maketrans({
    "＆": "&",
    "，": ",",
    "．": ".",
    "　": " ",
    **{chr(0xFF10 + i): str(i) for i in range(10)},
})

# Hard families stay on the existing strict detectors. The synonym table is
# never applied to these phrases.
_HARD_CLAIM_RE = re.compile(
    r"(?:"
    r"\d|"
    r"\b(?:lbs?|pounds?|kgs?|kilograms?|oz|ounces?|inch(?:es)?|cm|mm)\b|"
    r"\b(?:load|capacit(?:y|ies)|weight|weighs)\b|"
    r"\b(?:solid[\s-]+wood|mdf|particle(?:[\s-]+)?board|plywood|metal|steel)\b|"
    r"\b(?:leather)\b|"
    r"\b(?:certified|certification|ul[\s-]+listed|astm|tsa|iso|carb|fda|cpsc)\b|"
    r"\b(?:assembl(?:y|e|ed|ing)?|hardware|install(?:ation|ing)?|"
    r"mins?|minutes?|warranty|warranties)\b|"
    r"\b(?:waterproof|water[\s-]*resist\w*|fire[\s-]*resist\w*|fireproof|"
    r"warp[\s-]*resist\w*)\b|"
    r"\b(?:armrests?|arm[\s-]*rests?|armchair|arm[\s-]+style)\b|"
    r"\b(?:heated|heating|electric|usb|charg(?:e|ing|er)?)\b"
    r")",
    re.IGNORECASE,
)

_BARE_LEATHER_RE = re.compile(r"(?<![a-z0-9])leather(?![a-z0-9])", re.IGNORECASE)
_LEATHER_PREFIX_RE = re.compile(
    r"(?:pu|p\.u\.|p\s+u|faux|vegan|bonded|synthetic|artificial|imitation"
    r"|genuine|real|top[\s-]+grain|full[\s-]+grain)[\s-]*$",
    re.IGNORECASE,
)

_HTML_RE = re.compile(r"<[^>]+>")
_FRACTION_OR_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?",
)
_WORD_RE = re.compile(r"[a-z]+")
_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?|[a-z]+",
)
_MIN_COMPOUND_PART = 3


def normalize_for_coverage(text: Any) -> str:
    """Normalize once. Used identically on draft and source sides."""
    raw = "" if text is None else str(text)
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.translate(_FW_TABLE)
    raw = (
        raw.replace("\xa0", " ")
        .replace("\u3000", " ")
        .replace("\u202f", " ")
        .replace("\u2009", " ")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = _HTML_RE.sub(" ", raw)
    raw = re.sub(r"\s*&\s*", " and ", raw)
    raw = raw.replace("won't", "will not").replace("Won't", "will not")
    raw = raw.replace("can't", "cannot")
    raw = raw.casefold()
    raw = raw.replace("'", "")
    raw = re.sub(r"[–—_/]+", "-", raw)
    raw = re.sub(r"[^a-z0-9./-]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def is_hard_claim_phrase(claim: Any) -> bool:
    """True for families this PR must not relax."""
    text = normalize_for_coverage(claim)
    if not text:
        return False
    return bool(_HARD_CLAIM_RE.search(text))


def is_bare_leather_claim(claim: Any) -> bool:
    text = normalize_for_coverage(claim)
    if not text:
        return False
    for match in _BARE_LEATHER_RE.finditer(text):
        prefix = text[max(0, match.start() - 16):match.start()]
        if not _LEATHER_PREFIX_RE.search(prefix):
            return True
    return False


def join_source_fields(*fields: Any) -> str:
    """Join title / description / bullets / attributes / specs.

    Any non-empty field may support a claim. None and empty values are
    skipped. A completely empty join never supports a claim.
    """
    parts: list[str] = []
    for field in fields:
        if field is None or field == "":
            continue
        if isinstance(field, Mapping):
            for key, value in field.items():
                raw_values = value if isinstance(value, (list, tuple, set)) else [value]
                key_text = str(key or "").strip()
                for raw in raw_values:
                    if raw is None or raw == "":
                        continue
                    if key_text:
                        parts.append(f"{key_text} {raw}")
                    else:
                        parts.append(str(raw))
        elif isinstance(field, (list, tuple, set)):
            for item in field:
                if item is None or item == "":
                    continue
                if isinstance(item, Mapping):
                    parts.append(join_source_fields(item))
                else:
                    parts.append(str(item))
        else:
            parts.append(str(field))
    return normalize_for_coverage(" ".join(part for part in parts if part))


def _raw_tokens(text: str) -> list[str]:
    norm = normalize_for_coverage(text)
    if not norm:
        return []
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(norm.replace("-", " ")):
        token = match.group(0).replace(" ", "")
        if token:
            tokens.append(NUMBER_WORDS.get(token, token))
    return tokens


def token_variants(token: str) -> set[str]:
    """Simple lemma / plural / number-word variants. No edit-distance."""
    raw = NUMBER_WORDS.get(str(token or "").casefold(), str(token or "").casefold())
    if not raw:
        return set()
    out = {raw}
    if raw in NUMBER_WORDS:
        out.add(NUMBER_WORDS[raw])
    for word, digit in NUMBER_WORDS.items():
        if raw == digit:
            out.add(word)
    if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss"):
        out.add(raw[:-1])
        if raw.endswith("es") and len(raw) > 4:
            out.add(raw[:-2])
        if raw.endswith("ies") and len(raw) > 4:
            out.add(raw[:-3] + "y")
    else:
        if not raw.endswith("s"):
            out.add(raw + "s")
        if raw.endswith("y") and len(raw) > 2:
            out.add(raw[:-1] + "ies")
        if not raw.endswith("es"):
            out.add(raw + "es")
    if len(raw) > 4 and raw.endswith("ed"):
        out.add(raw[:-1])       # shaped → shape
        out.add(raw[:-2])       # cushioned → cushion
        out.add(raw[:-2] + "e")
    if len(raw) > 5 and raw.endswith("ing"):
        out.add(raw[:-3])
        out.add(raw[:-3] + "e")
    return {item for item in out if item}


def _is_ignored_token(token: str) -> bool:
    variants = token_variants(token)
    return bool(variants & (_STOPWORDS | _MARKETING_ADJECTIVES))


def content_tokens(text: Any) -> list[str]:
    """Substantive tokens after stopword / marketing-adjective removal."""
    return [token for token in _raw_tokens(text) if not _is_ignored_token(token)]


def phrase_in_text(phrase: Any, text: Any) -> bool:
    """Whole-phrase / whole-word match. ``table`` does not match ``stable``."""
    phrase_tokens = _raw_tokens(phrase)
    text_tokens = _raw_tokens(text)
    if not phrase_tokens or not text_tokens:
        return False
    phrase_vars = [token_variants(token) for token in phrase_tokens]
    n = len(phrase_tokens)
    for index in range(len(text_tokens) - n + 1):
        window = text_tokens[index:index + n]
        if all(token_variants(window[pos]) & phrase_vars[pos] for pos in range(n)):
            return True
    compacted_phrase = "".join(phrase_tokens)
    compacted_text_parts = ["".join(text_tokens[i:j]) for i in range(len(text_tokens)) for j in range(i + 1, len(text_tokens) + 1)]
    if compacted_phrase in compacted_text_parts and (
        n > 1 or compacted_phrase in text_tokens or any(
            compacted_phrase in token_variants(token) for token in text_tokens
        )
    ):
        # Single-token compacted equality must still be a whole token, not a
        # substring of a longer word (table vs stable already handled above).
        if n == 1:
            return any(token_variants(token) & token_variants(phrase_tokens[0]) for token in text_tokens)
        return True
    return False


def _source_token_universe(source_tokens: list[str]) -> set[str]:
    universe: set[str] = set()
    for token in source_tokens:
        universe |= token_variants(token)
    # Adjacent and any-pair concatenations so barrel + back covers barrelback
    # even when other words sit between them.
    for left in source_tokens:
        for right in source_tokens:
            if left == right and source_tokens.count(left) < 2:
                continue
            if len(left) < _MIN_COMPOUND_PART or len(right) < _MIN_COMPOUND_PART:
                continue
            universe.add(left + right)
            universe.add(right + left)
    return universe


def _token_covered(token: str, source_universe: set[str], source_tokens: list[str]) -> bool:
    if token_variants(token) & source_universe:
        return True
    raw = NUMBER_WORDS.get(token, token)
    if _FRACTION_OR_NUMBER_RE.fullmatch(raw):
        return raw in source_universe or raw in source_tokens
    if len(raw) < 2 * _MIN_COMPOUND_PART:
        return False
    for index in range(_MIN_COMPOUND_PART, len(raw) - _MIN_COMPOUND_PART + 1):
        left, right = raw[:index], raw[index:]
        if (token_variants(left) & source_universe) and (token_variants(right) & source_universe):
            return True
    return False


def _group_matches_text(group: frozenset[str], text: str) -> bool:
    return any(phrase_in_text(phrase, text) for phrase in group)


def source_supports_claim_phrase(claim: Any, *source_fields: Any) -> bool:
    """True when every substantive claim word is covered by the source.

    Empty / None sources never support a claim and never raise.
    Bare Leather vs PU Leather is never supported here (whole-value).
    Hard claims skip the synonym table; numeric tokens must match exactly.
    Unknown words are not guessed by edit distance.
    """
    claim_norm = normalize_for_coverage(claim)
    if not claim_norm:
        return False
    if is_bare_leather_claim(claim_norm):
        return False
    source_norm = join_source_fields(*source_fields)
    if not source_norm:
        return False

    allow_synonyms = not is_hard_claim_phrase(claim_norm)
    remaining = claim_norm
    consumed_phrase = False
    if allow_synonyms:
        for group in CLAIM_PARAPHRASE_SYNONYMS:
            claim_hits = [phrase for phrase in group if phrase_in_text(phrase, claim_norm)]
            if not claim_hits:
                continue
            if not _group_matches_text(group, source_norm):
                continue
            consumed_phrase = True
            for phrase in sorted(claim_hits, key=lambda item: len(normalize_for_coverage(item)), reverse=True):
                remaining = _remove_phrase(remaining, phrase)

    remaining_tokens = content_tokens(remaining)
    source_tokens = _raw_tokens(source_norm)
    source_universe = _source_token_universe(source_tokens)
    if remaining_tokens:
        if not all(
            _token_covered(token, source_universe, source_tokens)
            for token in remaining_tokens
        ):
            return False
        return True
    return consumed_phrase


def _remove_phrase(text: str, phrase: str) -> str:
    phrase_tokens = _raw_tokens(phrase)
    text_tokens = _raw_tokens(text)
    if not phrase_tokens or not text_tokens:
        return text
    phrase_vars = [token_variants(token) for token in phrase_tokens]
    n = len(phrase_tokens)
    kept: list[str] = []
    index = 0
    while index < len(text_tokens):
        window = text_tokens[index:index + n]
        if len(window) == n and all(
            token_variants(window[pos]) & phrase_vars[pos] for pos in range(n)
        ):
            index += n
            continue
        kept.append(text_tokens[index])
        index += 1
    return " ".join(kept)


def filter_supported_paraphrase_violations(
    violations: Iterable[Mapping[str, Any]] | None,
    *source_fields: Any,
    claim_text_key: str = "claim_text",
    claim_type_key: str = "claim_type",
) -> list[dict[str, Any]]:
    """Drop non-hard feature phrases the GIGA source already covers.

    Materials, capacity, certifications, and other hard families are kept
    so the existing strict detectors remain the last word.
    """
    kept: list[dict[str, Any]] = []
    source_norm = join_source_fields(*source_fields)
    for item in violations or []:
        payload = dict(item)
        claim_type = str(payload.get(claim_type_key) or "")
        claim_text = payload.get(claim_text_key)
        if claim_type in {"semantic_feature", "unsupported_feature"} or claim_type == "":
            if (
                claim_text
                and not is_hard_claim_phrase(claim_text)
                and not is_bare_leather_claim(claim_text)
                and source_supports_claim_phrase(claim_text, source_norm)
            ):
                continue
        kept.append(payload)
    return kept
