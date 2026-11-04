"""Shared GIGA-source paraphrase coverage for unsupported-claim checks.

Owner policy 2026-11-02: a reasonable paraphrase of the GIGA source is
supported. Only claims with no basis in the source are flagged.

A draft phrase is SUPPORTED when, after one identical normalization on both
sides, every substantive content word is covered by the source and at least
one substantive content word matches. Coverage uses lowercase, hyphen / space
/ compound folding of *adjacent* source tokens, simple lemma / plural
folding, number words, and the single reviewed synonym table below.

Hard claims are never relaxed by the synonym table. Number words are hard
*before* digit conversion. Count claims (seat/person/tier/piece) need an
exact same-segment number match. Materials and resist/LED/battery words
need exact token coverage; measurements, certs, assembly, and armrests
stay with the existing detectors. Multi-word claims must land in one
source segment / small ordered window. Source negation and
faux/-look/-grain qualifiers block support.

This module is read-only. It does not write DB rows, files, or eBay.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable

# Support is OR across these fields. No field has exclusive priority; an empty
# or missing field is skipped and never counts as support by itself.
SOURCE_FIELD_PRECEDENCE = (
    "title, description (including bullets), attributes, specs — any field "
    "may support; empty/None fields are ignored. Multi-word claims must be "
    "covered inside one sentence/attribute/comma segment."
)

FEATURE_CLAIM_TYPES = frozenset({
    "semantic_feature",
    "unsupported_feature",
    "claim_unsupported_feature",
})

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
    }),
    # GIGA W2105P595057 pairs "cushioning" with "filling" next to rebound.
    # Kept out of the padded/seat group so "filling" cannot support a seat.
    frozenset({
        "cushioning",
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
    "single": "1",
    "double": "2",
    "dozen": "12",
    "pair": "2",
}

COUNT_NOUNS: dict[str, str] = {
    "seat": "seat",
    "seats": "seat",
    "seater": "seat",
    "seaters": "seat",
    "person": "person",
    "persons": "person",
    "people": "person",
    "tier": "tier",
    "tiers": "tier",
    "piece": "piece",
    "pieces": "piece",
    "pack": "pack",
    "packs": "pack",
}

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "with", "on",
    "at", "by", "from", "as", "is", "are", "be", "its", "your", "our",
    "this", "that", "will", "not", "no", "all", "up",
})

_MARKETING_ADJECTIVES = frozenset({
    "smart",
    "design",
    "versatile",
    "versatility",
    "organization",
    "organisational",
    "organizational",
})

_NEGATION_WORDS = frozenset({"not", "no", "without", "except", "excluding"})
_TRAILING_ABSENT = frozenset({"none", "na"})
_TRAILING_NOT_WORDS = frozenset({
    "included", "include", "available", "required", "removable",
    "washable", "rechargeable",
})
_QUALIFIER_WORDS = frozenset({
    "faux", "look", "grain", "imitation", "style", "fake", "artificial",
})
_ALLOWED_NON_PAIRS = frozenset({("non", "slip")})

_FW_TABLE = str.maketrans({
    "＆": "&",
    "，": ",",
    "．": ".",
    "　": " ",
    **{chr(0xFF10 + i): str(i) for i in range(10)},
})

_NUMBER_WORD_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|single|double|dozen|pair)\b",
    re.IGNORECASE,
)

# Weight / cert / assembly / armrest / power families stay on the existing
# detectors. This module never marks them supported.
_DETECTOR_OWNED_RE = re.compile(
    r"(?:"
    r"\b(?:lbs?|pounds?|kgs?|kilograms?|oz|ounces?|inch(?:es)?|cm|mm)\b|"
    r"\b(?:load|capacit(?:y|ies)|weight|weighs)\b|"
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

_HARD_CLAIM_RE = re.compile(
    r"(?:"
    r"\d|"
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|single|double|dozen|pair)\b|"
    r"\b(?:lbs?|pounds?|kgs?|kilograms?|oz|ounces?|inch(?:es)?|cm|mm)\b|"
    r"\b(?:load|capacit(?:y|ies)|weight|weighs)\b|"
    r"\b(?:solid[\s-]+wood|mdf|particle(?:[\s-]+)?board|plywood|metal|steel)\b|"
    r"\b(?:wood(?:en)?|marble|velvet|iron|alumin(?:i)?um|rattan|glass|stone)\b|"
    r"\b(?:leather)\b|"
    r"\b(?:certified|certification|ul[\s-]+listed|astm|tsa|iso|carb|fda|cpsc)\b|"
    r"\b(?:assembl(?:y|e|ed|ing)?|hardware|install(?:ation|ing)?|"
    r"mins?|minutes?|warranty|warranties)\b|"
    r"\b(?:waterproof|weatherproof|water[\s-]*resist\w*|fire[\s-]*resist\w*|"
    r"fireproof|warp[\s-]*resist\w*|uv[\s-]*resist\w*|rust[\s-]*resist\w*|"
    r"stain[\s-]*resist\w*|scratch[\s-]*resist\w*)\b|"
    r"\b(?:armrests?|arm[\s-]*rests?|armchair|arm[\s-]+style)\b|"
    r"\b(?:heated|heating|electric|usb|charg(?:e|ing|er)?|battery|led|"
    r"remote|rechargeable)\b"
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
_SEGMENT_SPLIT_RE = re.compile(r"[.!?|;,\n\r]+")
_FRACTION_OR_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?",
)
_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?|[a-z]+",
)
_MIN_COMPOUND_PART = 3
_NEGATION_WINDOW = 3
_WINDOW_SLACK = 1


@dataclass
class _SegmentIndex:
    tokens: list[str]
    variants: list[frozenset[str]]
    compounds: set[str]
    negated: set[int]
    qualified: set[int]
    counts: list[tuple[str, str, int]]


@dataclass
class _SourceIndex:
    segments: list[_SegmentIndex] = field(default_factory=list)


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
    raw = re.sub(r"\bn\s*/\s*a\b", " na ", raw)
    raw = re.sub(r"[–—_/]+", "-", raw)
    raw = re.sub(r"[^a-z0-9./-]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def is_hard_claim_phrase(claim: Any) -> bool:
    """True for families this module must not relax.

    Number words are inspected on the raw normalized claim *before* one→1.
    """
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


def _raw_field_strings(*fields: Any) -> list[str]:
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
                    parts.extend(_raw_field_strings(item))
                else:
                    parts.append(str(item))
        else:
            parts.append(str(field))
    return parts


def join_source_fields(*fields: Any) -> str:
    """Join title / description / bullets / attributes / specs.

    Any non-empty field may support a claim. None and empty values are
    skipped. A completely empty join never supports a claim.
    """
    return normalize_for_coverage(" ".join(_raw_field_strings(*fields)))


def _segment_texts(*fields: Any) -> list[str]:
    segments: list[str] = []
    for raw in _raw_field_strings(*fields):
        cleaned = _HTML_RE.sub(" ", str(raw))
        for part in _SEGMENT_SPLIT_RE.split(cleaned):
            norm = normalize_for_coverage(part)
            if norm:
                segments.append(norm)
    return segments


def _lex_tokens(text: str, *, convert_numbers: bool) -> list[str]:
    norm = normalize_for_coverage(text)
    if not norm:
        return []
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(norm.replace("-", " ")):
        token = match.group(0).replace(" ", "")
        if not token:
            continue
        if convert_numbers:
            token = NUMBER_WORDS.get(token, token)
        tokens.append(token)
    return tokens


def _raw_tokens(text: str) -> list[str]:
    return _lex_tokens(text, convert_numbers=True)


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
        out.add(raw[:-1])
        out.add(raw[:-2])
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


def _adjacent_compounds(tokens: list[str]) -> set[str]:
    compounds: set[str] = set()
    for index in range(len(tokens) - 1):
        left, right = tokens[index], tokens[index + 1]
        if len(left) < _MIN_COMPOUND_PART or len(right) < _MIN_COMPOUND_PART:
            continue
        compounds.add(left + right)
    return compounds


def _mark_negated(tokens: list[str]) -> set[int]:
    negated: set[int] = set()
    length = len(tokens)
    for index, token in enumerate(tokens):
        if token.endswith("less") and len(token) > 4:
            negated.add(index)
        if token.startswith("non") and token != "nonslip":
            if token == "non":
                nxt = tokens[index + 1] if index + 1 < length else ""
                if (token, nxt) not in _ALLOWED_NON_PAIRS and nxt:
                    negated.add(index + 1)
            else:
                negated.add(index)
        prev = tokens[max(0, index - _NEGATION_WINDOW):index]
        if token not in _NEGATION_WORDS and any(item in _NEGATION_WORDS for item in prev):
            negated.add(index)
        nxt = tokens[index + 1:index + 4]
        if len(nxt) >= 2 and nxt[0] == "not" and nxt[1] in {"included", "include", "required"}:
            negated.add(index)
        if len(nxt) >= 2 and nxt[0] == "sold" and nxt[1] == "separately":
            negated.add(index)
    return negated


def _mark_qualified(tokens: list[str]) -> set[int]:
    qualified: set[int] = set()
    length = len(tokens)
    for index, token in enumerate(tokens):
        neighbors = []
        if index:
            neighbors.append(tokens[index - 1])
        if index + 1 < length:
            neighbors.append(tokens[index + 1])
        if token in _QUALIFIER_WORDS or token.endswith("look"):
            if index:
                qualified.add(index - 1)
            if index + 1 < length:
                qualified.add(index + 1)
        if any(item in _QUALIFIER_WORDS or item.endswith("look") for item in neighbors):
            qualified.add(index)
    return qualified


def _build_segment(norm_text: str) -> _SegmentIndex:
    tokens = _lex_tokens(norm_text, convert_numbers=True)
    return _SegmentIndex(
        tokens=tokens,
        variants=[frozenset(token_variants(token)) for token in tokens],
        compounds=_adjacent_compounds(tokens),
        negated=_mark_negated(tokens),
        qualified=_mark_qualified(tokens),
        counts=_counts_from_tokens(tokens),
    )


def _index_source(*fields: Any) -> _SourceIndex:
    segments = [_build_segment(text) for text in _segment_texts(*fields)]
    return _SourceIndex(segments=[item for item in segments if item.tokens])


def _suffix_has_trailing_negation(tokens: list[str], start: int) -> bool:
    """True when the match is undone by later absence wording in the segment."""
    suffix = tokens[start:]
    if not suffix:
        return False
    if any(token in _TRAILING_ABSENT for token in suffix):
        return True
    length = len(suffix)
    for index, token in enumerate(suffix):
        nxt = suffix[index + 1] if index + 1 < length else ""
        if token == "sold" and nxt == "separately":
            return True
        if token != "not" or not nxt or nxt in _STOPWORDS or nxt in _NEGATION_WORDS:
            continue
        if nxt in _TRAILING_NOT_WORDS or nxt not in _MARKETING_ADJECTIVES:
            return True
    return False


def _is_count_number(token: str) -> bool:
    return bool(token and _FRACTION_OR_NUMBER_RE.fullmatch(token))


def _range_at(tokens: list[str], index: int) -> tuple[int, str, str] | None:
    """NUM NUM NOUN or NUM to NUM NOUN. The range string stays '4-6', not 6."""
    if index >= len(tokens) or not _is_count_number(tokens[index]):
        return None
    low = tokens[index]
    nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
    nxt2 = tokens[index + 2] if index + 2 < len(tokens) else ""
    nxt3 = tokens[index + 3] if index + 3 < len(tokens) else ""
    if _is_count_number(nxt):
        noun = COUNT_NOUNS.get(nxt2)
        if noun:
            return (index + 3, f"{low}-{nxt}", noun)
        return None
    if nxt == "to" and _is_count_number(nxt2):
        noun = COUNT_NOUNS.get(nxt3)
        if noun:
            return (index + 4, f"{low}-{nxt2}", noun)
    return None


def _counts_from_tokens(tokens: list[str]) -> list[tuple[str, str, int]]:
    """Exact (number_or_range, count_noun, end) triples.

    Ranges are parsed before noun-before-number binding so 'Seats 4-6
    person' stays ('4-6', 'person'), never ('4', 'seat') plus ('6',
    'person'). A range never becomes an exact endpoint count.
    """
    found: list[tuple[str, str, int]] = []
    index = 0
    while index < len(tokens):
        ranged = _range_at(tokens, index)
        if ranged:
            end, number, noun = ranged
            found.append((number, noun, end))
            index = end
            continue
        token = tokens[index]
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        noun = COUNT_NOUNS.get(nxt)
        if _is_count_number(token) and noun:
            found.append((token, noun, index + 2))
            index += 2
            continue
        noun = COUNT_NOUNS.get(token)
        if noun and _is_count_number(nxt) and _range_at(tokens, index + 1) is None:
            found.append((nxt, noun, index + 2))
            index += 2
            continue
        index += 1
    return found


def _extract_counts(text: Any) -> list[tuple[str, str]]:
    return [(number, noun) for number, noun, _end in _counts_from_tokens(_lex_tokens(text, convert_numbers=True))]


def _exact_count_supported(claim_counts: list[tuple[str, str]], index: _SourceIndex) -> bool:
    if not claim_counts:
        return False
    for number, noun in claim_counts:
        matched = False
        for segment in index.segments:
            for src_number, src_noun, end in segment.counts:
                if (src_number, src_noun) != (number, noun):
                    continue
                if "-" in src_number and "-" not in number:
                    continue
                if _suffix_has_trailing_negation(segment.tokens, end):
                    continue
                matched = True
                break
            if matched:
                break
        if not matched:
            return False
    return True


def _phrase_in_segment(
    phrase_tokens: list[str],
    segment: _SegmentIndex,
    *,
    honor_negation: bool = True,
) -> bool:
    if not phrase_tokens or not segment.tokens:
        return False
    n = len(phrase_tokens)
    phrase_vars = [token_variants(token) for token in phrase_tokens]
    if n == 1:
        target = phrase_vars[0]
        for index, variants in enumerate(segment.variants):
            if honor_negation and index in segment.negated:
                continue
            if target & variants and not _suffix_has_trailing_negation(segment.tokens, index + 1):
                return True
        if not (target & segment.compounds):
            return False
        for index in range(len(segment.tokens) - 1):
            if honor_negation and (index in segment.negated or index + 1 in segment.negated):
                continue
            joined = segment.tokens[index] + segment.tokens[index + 1]
            if joined in target or token_variants(joined) & target:
                if not _suffix_has_trailing_negation(segment.tokens, index + 2):
                    return True
        return False
    for start in range(len(segment.tokens) - n + 1):
        if honor_negation and any(start + offset in segment.negated for offset in range(n)):
            continue
        if all(phrase_vars[offset] & segment.variants[start + offset] for offset in range(n)):
            if not _suffix_has_trailing_negation(segment.tokens, start + n):
                return True
    return False


def phrase_in_text(phrase: Any, text: Any) -> bool:
    """Whole-phrase / whole-word match. ``table`` does not match ``stable``."""
    phrase_tokens = _raw_tokens(phrase)
    if not phrase_tokens:
        return False
    index = _index_source(text)
    return any(_phrase_in_segment(phrase_tokens, segment) for segment in index.segments)


def _usable_position(segment: _SegmentIndex, index: int) -> bool:
    return index not in segment.negated and index not in segment.qualified


def _token_in_segment(token: str, segment: _SegmentIndex) -> bool:
    variants = token_variants(token)
    for index, item_vars in enumerate(segment.variants):
        if not _usable_position(segment, index):
            continue
        if variants & item_vars and not _suffix_has_trailing_negation(segment.tokens, index + 1):
            return True
    if not (variants & segment.compounds):
        return False
    for index in range(len(segment.tokens) - 1):
        if not _usable_position(segment, index) or not _usable_position(segment, index + 1):
            continue
        joined = segment.tokens[index] + segment.tokens[index + 1]
        if joined in variants or token_variants(joined) & variants:
            if not _suffix_has_trailing_negation(segment.tokens, index + 2):
                return True
    return False


def _window_covers(claim_tokens: list[str], segment: _SegmentIndex, start: int, end: int) -> int | None:
    pos = start
    last_end = start
    for token in claim_tokens:
        variants = token_variants(token)
        found = False
        while pos < end:
            if _usable_position(segment, pos) and variants & segment.variants[pos]:
                pos += 1
                last_end = pos
                found = True
                break
            if (
                pos + 1 < end
                and _usable_position(segment, pos)
                and _usable_position(segment, pos + 1)
            ):
                joined = segment.tokens[pos] + segment.tokens[pos + 1]
                if joined in variants or token_variants(joined) & variants:
                    pos += 2
                    last_end = pos
                    found = True
                    break
            pos += 1
        if not found:
            return None
    return last_end


def _segment_covers_tokens(claim_tokens: list[str], segment: _SegmentIndex) -> bool:
    if not claim_tokens or not segment.tokens:
        return False
    if len(claim_tokens) == 1:
        return _token_in_segment(claim_tokens[0], segment)
    window = len(claim_tokens) + _WINDOW_SLACK
    for start in range(len(segment.tokens)):
        end = min(len(segment.tokens), start + window)
        if end - start < len(claim_tokens):
            continue
        hit = _window_covers(claim_tokens, segment, start, end)
        if hit is not None and not _suffix_has_trailing_negation(segment.tokens, hit):
            return True
    return False


def _group_matches_segment(group: frozenset[str], segment: _SegmentIndex) -> bool:
    for phrase in group:
        tokens = _raw_tokens(phrase)
        honor = not any(token in _NEGATION_WORDS for token in tokens)
        if _phrase_in_segment(tokens, segment, honor_negation=honor):
            return True
    return False


def _soft_support(claim_norm: str, index: _SourceIndex) -> bool:
    claim_segment = _build_segment(claim_norm)
    for segment in index.segments:
        remaining = claim_norm
        consumed = False
        for group in CLAIM_PARAPHRASE_SYNONYMS:
            claim_hits = [
                phrase
                for phrase in group
                if _phrase_in_segment(_raw_tokens(phrase), claim_segment, honor_negation=False)
            ]
            if not claim_hits or not _group_matches_segment(group, segment):
                continue
            consumed = True
            for phrase in sorted(claim_hits, key=lambda item: len(normalize_for_coverage(item)), reverse=True):
                remaining = _remove_phrase(remaining, phrase)
        remaining_tokens = content_tokens(remaining)
        if remaining_tokens:
            if _segment_covers_tokens(remaining_tokens, segment):
                return True
        elif consumed:
            return True
    return False


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


def source_supports_claim_phrase(
    claim: Any,
    *source_fields: Any,
    _index: _SourceIndex | None = None,
) -> bool:
    """True when every substantive claim word is covered by the source.

    Empty / None sources never support a claim and never raise.
    Bare Leather vs PU Leather is never supported here (whole-value).
    Count claims need an exact same-segment number. Other hard phrases need
    exact token coverage (no synonym table). Detector-owned families stay
    flagged so existing detectors remain the last word.
    Unknown words are not guessed by edit distance.
    """
    claim_norm = normalize_for_coverage(claim)
    if not claim_norm:
        return False
    if is_bare_leather_claim(claim_norm):
        return False
    index = _index if _index is not None else _index_source(*source_fields)
    if not index.segments:
        return False
    if _DETECTOR_OWNED_RE.search(claim_norm):
        return False
    if is_hard_claim_phrase(claim_norm):
        return _hard_support(claim_norm, index)
    return _soft_support(claim_norm, index)


def _hard_support(claim_norm: str, index: _SourceIndex) -> bool:
    claim_counts = _extract_counts(claim_norm)
    if claim_counts:
        return _exact_count_supported(claim_counts, index)
    tokens = content_tokens(claim_norm)
    if not tokens:
        return False
    return any(_segment_covers_tokens(tokens, segment) for segment in index.segments)


def filter_supported_paraphrase_violations(
    violations: Iterable[Mapping[str, Any]] | None,
    *source_fields: Any,
    claim_text_key: str = "claim_text",
    claim_type_key: str = "claim_type",
) -> list[dict[str, Any]]:
    """Drop non-hard feature phrases the GIGA source already covers.

    Materials, capacity, certifications, empty types, and other families
    are kept so the existing strict detectors remain the last word.
    """
    kept: list[dict[str, Any]] = []
    index = _index_source(*source_fields)
    for item in violations or []:
        payload = dict(item)
        claim_type = str(payload.get(claim_type_key) or "")
        claim_text = payload.get(claim_text_key)
        if claim_type in FEATURE_CLAIM_TYPES and claim_text:
            if source_supports_claim_phrase(claim_text, _index=index):
                continue
        kept.append(payload)
    return kept
