"""Deletion-only text gate: after must be an ordered subsequence of before."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from src.utils.claim_strip import (
    aspect_values,
    normalize_entities,
    strip_html_to_text,
)

# Word tokens, plus standalone unit glyphs the spec treats as their own tokens.
_TOKEN_RE = re.compile(r"[\w][\w'.-]*|[\"'°]")
_NUMBER_UNIT_RE = re.compile(r"\d+(?:\.\d+)?")


def tokenize_text(value: str) -> list[str]:
    """Normalize HTML/entities, then tokenize per section 6.2."""
    text = strip_html_to_text(normalize_entities(value or ""))
    folded = text.casefold()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(folded):
        tokens.append(match.group(0))
    return tokens


def ordered_subsequence(before: list[str], after: list[str]) -> tuple[bool, list[str]]:
    """True when ``after`` is an ordered subsequence of ``before`` with no extra counts."""
    if not after:
        return True, []
    before_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}
    for token in before:
        before_counts[token] = before_counts.get(token, 0) + 1
    for token in after:
        after_counts[token] = after_counts.get(token, 0) + 1
    extras: list[str] = []
    for token, count in after_counts.items():
        if count > before_counts.get(token, 0):
            extras.extend([token] * (count - before_counts.get(token, 0)))
    if extras:
        return False, extras[:10]

    index = 0
    for token in after:
        found = False
        while index < len(before):
            if before[index] == token:
                found = True
                index += 1
                break
            index += 1
        if not found:
            return False, extras[:10] if extras else [token]
    return True, []


def deletion_only_text(before: str, after: str) -> tuple[bool, list[str]]:
    """Return (ok, first 10 offending tokens)."""
    return ordered_subsequence(tokenize_text(before), tokenize_text(after))


def deletion_only_aspects(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """after_values ⊆ before_values per aspect name; no new aspect names."""
    before_map = {
        str(key): [str(item) for item in aspect_values(value)]
        for key, value in dict(before or {}).items()
    }
    after_map = {
        str(key): [str(item) for item in aspect_values(value)]
        for key, value in dict(after or {}).items()
    }
    offenders: list[str] = []
    before_folded = {key.casefold(): key for key in before_map}
    for name, values in after_map.items():
        original = before_folded.get(name.casefold())
        if original is None:
            offenders.append(name)
            continue
        allowed = list(before_map[original])
        for value in values:
            if value in allowed:
                allowed.remove(value)
            else:
                offenders.append(value)
    return (not offenders), offenders[:10]


def first_offending_tokens(tokens: Iterable[str], limit: int = 10) -> list[str]:
    out: list[str] = []
    for token in tokens:
        out.append(str(token))
        if len(out) >= limit:
            break
    return out
