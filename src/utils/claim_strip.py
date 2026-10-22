"""Manifest-driven, fail-closed claim strip for live Main listings.

Used by ``scripts/audit_fix_active_listings.py`` via
``__strip_claims_from_manifest__``. This module never talks to eBay or SQLite.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


STRIP_FIX_KEY = "__strip_claims_from_manifest__"
STRIP_NOW = "strip_now"
KEEP = "keep"
NEEDS_LOOK = "needs_look"

TOKEN_OFFER_DESC = "offer_listingDescription"
TOKEN_INV_DESC = "inventory_description"
TOKEN_TITLE = "title"
TOKEN_FEATURES = "aspect:Features"

LOCATION_DESCRIPTION = "description"
LOCATION_FEATURES = "features"
LOCATION_ASPECTS = "aspects"
LOCATION_ALL = "all"
VALID_LOCATIONS = (
    LOCATION_DESCRIPTION,
    LOCATION_FEATURES,
    LOCATION_ASPECTS,
    LOCATION_ALL,
)

STATUS_WILL_STRIP = "will_strip"
STATUS_ALREADY_ABSENT = "already_absent"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NO_SNIPPET = "no_snippet"
STATUS_PROTECTED_ZONE = "protected_zone"
STATUS_IN_TITLE = "in_title"
STATUS_CLAIM_NOT_IN_REPORT = "claim_not_in_report"
STATUS_LISTING_ID_MISMATCH = "listing_id_mismatch"
STATUS_REQUIRED_ASPECT = "required_aspect_would_empty"
STATUS_REQUIRED_ASPECTS_UNAVAILABLE = "required_aspects_unavailable"
STATUS_SECTION_EMPTY = "section_empty"
STATUS_LOCATION_FILTERED = "location_filtered"
STATUS_DRIFT = "drift"
STATUS_FAILED_RESTORED = "failed_restored"
STATUS_RESTORE_FAILED = "restore_failed"
STATUS_LIVE_CHANGED_BETWEEN_FETCHES = "live_changed_between_fetches"
STATUS_FETCH_FAILED = "fetch_failed"
STATUS_LIVE_LISTING_MISSING = "live_listing_missing"
STATUS_WRITE_FAILED = "write_failed"
STATUS_STRIPPED = "stripped"
STATUS_IGNORED = "ignored"

# GET /offer returns these; Inventory offer PUT rejects them.
OFFER_GET_ONLY_KEYS = (
    "offerId",
    "listing",
    "listingId",
    "status",
    "warnings",
    "errors",
    "href",
)

_WS_RE = re.compile(r"\s+")
_SEP_NORMALIZE_RE = re.compile(r"[\s\-_]+")
_CLAIM_WORD_SPLIT_RE = re.compile(r"[\s\-_]+")
_EMPTY_LI_RE = re.compile(r"<li\b[^>]*>\s*</li>", re.I)
_EMPTY_P_RE = re.compile(r"<p\b[^>]*>\s*</p>", re.I)
_EMPTY_STRONG_RE = re.compile(r"<strong\b[^>]*>\s*</strong>", re.I)
_DOUBLED_SEP_RE = re.compile(r"\s*\.\s+\.\s*|\s*,\s*,\s*|\s+-\s+-\s+")
_LEADING_SEP_RE = re.compile(r"^(?:\s|[,.;:\-–—|/])+")
_TRAILING_SEP_RE = re.compile(r"(?:\s|[,.;:\-–—|/])+$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_PACKAGE_HEADING_RE = re.compile(
    r"<\s*(?P<tag>h[1-6])\b[^>]*>\s*(?:<[^>]+>\s*)*"
    r"(?P<label>PACKAGE\s+INCLUDES|PERFECT\s+FOR)"
    r"\s*(?:<[^>]+>\s*)*</\s*(?P=tag)\s*>",
    re.I,
)
_ASSEMBLY_OPEN_RE = re.compile(
    r"<\s*(?P<tag>[a-zA-Z][\w:-]*)\b[^>]*\bdata-assembly-note\b[^>]*>",
    re.I,
)


# ---------------------------------------------------------------------------
# Normalization / matching
# ---------------------------------------------------------------------------


def normalize_entities(text: str) -> str:
    if not text:
        return ""
    decoded = html.unescape(text)
    return (
        decoded.replace("\xa0", " ")
        .replace("\u202f", " ")
        .replace("\u2009", " ")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2033", '"')
        .replace("\u2032", "'")
    )


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def strip_html_to_text(raw: str) -> str:
    if not raw:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", normalize_entities(raw))
    return collapse_ws(without_tags)


def normalize_claim_value(value: str) -> str:
    return collapse_ws(_SEP_NORMALIZE_RE.sub(" ", normalize_entities(value).casefold()))


def claim_pattern(claim: str) -> re.Pattern[str] | None:
    words = [re.escape(part) for part in _CLAIM_WORD_SPLIT_RE.split(claim.strip()) if part]
    if not words:
        return None
    inner = r"[\s\-_]+".join(words)
    return re.compile(rf"(?<![A-Za-z0-9-]){inner}(?![A-Za-z0-9])", re.IGNORECASE)


def claim_matches_text(claim: str, text: str) -> bool:
    pattern = claim_pattern(claim)
    if pattern is None:
        return False
    return bool(pattern.search(normalize_entities(text or "")))


def snippet_mid60(snippet: str) -> str:
    collapsed = strip_html_to_text(snippet)
    if len(collapsed) <= 60:
        return collapsed
    extra = len(collapsed) - 60
    start = extra // 2
    return collapsed[start : start + 60]


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# HTML index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HtmlIndex:
    html: str
    plain: str
    collapsed: str
    collapsed_to_html: list[tuple[int, int]]


def _decode_entity_at(raw: str, start: int) -> tuple[str, int]:
    end = raw.find(";", start, start + 12)
    if end < 0:
        return raw[start], start + 1
    entity = raw[start : end + 1]
    decoded = normalize_entities(entity)
    if decoded == entity:
        return raw[start], start + 1
    return decoded, end + 1


def build_html_index(raw: str) -> HtmlIndex:
    html_src = raw or ""
    plain_chars: list[str] = []
    plain_spans: list[tuple[int, int]] = []
    i = 0
    n = len(html_src)
    in_tag = False
    while i < n:
        ch = html_src[i]
        if not in_tag:
            if ch == "<":
                if html_src.startswith("<!--", i):
                    end = html_src.find("-->", i + 4)
                    i = n if end < 0 else end + 3
                    # A dropped comment is a whitespace boundary, same as a tag.
                    if plain_chars:
                        plain_chars.append(" ")
                        plain_spans.append((i, i))
                    continue
                # Tags become spaces so collapsed text matches strip_html_to_text
                # and live_snippet windows (e.g. "trailers.</li><li>220-Lb").
                if plain_chars:
                    plain_chars.append(" ")
                    plain_spans.append((i, i))
                in_tag = True
                i += 1
                continue
            if ch == "&":
                decoded, nxt = _decode_entity_at(html_src, i)
                decoded = normalize_entities(decoded)
                for offset, dch in enumerate(decoded):
                    plain_chars.append(dch)
                    # Map every decoded char to the whole entity span.
                    plain_spans.append((i, nxt))
                    _ = offset
                i = nxt
                continue
            plain_chars.append(normalize_entities(ch))
            plain_spans.append((i, i + 1))
            i += 1
        else:
            if ch == ">":
                in_tag = False
            i += 1

    plain = "".join(plain_chars)
    collapsed_chars: list[str] = []
    collapsed_spans: list[tuple[int, int]] = []
    pending_space = False
    started = False
    for ch, span in zip(plain_chars, plain_spans):
        if ch.isspace() or ch == "\xa0":
            if started:
                pending_space = True
            continue
        if pending_space:
            collapsed_chars.append(" ")
            collapsed_spans.append(span)
            pending_space = False
        collapsed_chars.append(ch)
        collapsed_spans.append(span)
        started = True
    return HtmlIndex(
        html=html_src,
        plain=plain,
        collapsed="".join(collapsed_chars),
        collapsed_to_html=collapsed_spans,
    )


def _match_html_span(index: HtmlIndex, collapsed_start: int, collapsed_end: int) -> tuple[int, int]:
    spans = index.collapsed_to_html[collapsed_start:collapsed_end]
    if not spans:
        return 0, 0
    return spans[0][0], spans[-1][1]


# ---------------------------------------------------------------------------
# Protected zones and containers
# ---------------------------------------------------------------------------


def _tag_ranges(raw: str, tag: str) -> list[tuple[int, int]]:
    open_re = re.compile(rf"<\s*{tag}\b[^>]*>", re.I)
    close_re = re.compile(rf"</\s*{tag}\s*>", re.I)
    ranges: list[tuple[int, int]] = []
    stack: list[int] = []
    tokens = []
    for match in open_re.finditer(raw):
        tokens.append(("open", match.start(), match.end()))
    for match in close_re.finditer(raw):
        tokens.append(("close", match.start(), match.end()))
    tokens.sort(key=lambda item: item[1])
    for kind, start, end in tokens:
        if kind == "open":
            stack.append(start)
        elif stack:
            open_start = stack.pop()
            if not stack:
                ranges.append((open_start, end))
    return ranges


def _innermost_containing(ranges: list[tuple[int, int]], start: int, end: int) -> tuple[int, int] | None:
    containing = [(a, b) for a, b in ranges if a <= start and end <= b]
    if not containing:
        return None
    return min(containing, key=lambda item: item[1] - item[0])


def _next_element_end(raw: str, start: int) -> int:
    i = start
    n = len(raw)
    while i < n and raw[i].isspace():
        i += 1
    if i >= n or raw[i] != "<":
        return start
    match = re.match(r"<\s*([a-zA-Z][\w:-]*)\b[^>]*>", raw[i:])
    if not match:
        return start
    tag = match.group(1)
    open_end = i + match.end()
    if raw[i:open_end].rstrip().endswith("/>"):
        return open_end
    close = re.search(rf"</\s*{re.escape(tag)}\s*>", raw[open_end:], re.I)
    if not close:
        return open_end
    return open_end + close.end()


def protected_ranges(raw: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    ranges.extend(_tag_ranges(raw, "table"))
    for level in range(1, 7):
        ranges.extend(_tag_ranges(raw, f"h{level}"))
    for match in _PACKAGE_HEADING_RE.finditer(raw):
        block_end = _next_element_end(raw, match.end())
        ranges.append((match.start(), max(block_end, match.end())))
    for match in _ASSEMBLY_OPEN_RE.finditer(raw):
        tag = match.group("tag")
        close = re.search(rf"</\s*{re.escape(tag)}\s*>", raw[match.end() :], re.I)
        end = match.end() + close.end() if close else match.end()
        ranges.append((match.start(), end))
    return ranges


def is_protected(raw: str, start: int, end: int, cache: list[tuple[int, int]] | None = None) -> bool:
    zones = cache if cache is not None else protected_ranges(raw)
    return any(a <= start and end <= b for a, b in zones)


def _li_heading_and_body(li_html: str) -> tuple[str, str, str, str]:
    """Return (open_tag, heading_html, body_html, close_tag)."""
    open_match = re.match(r"<li\b[^>]*>", li_html, re.I)
    close_match = re.search(r"</li\s*>\s*$", li_html, re.I)
    if not open_match or not close_match:
        return "", "", li_html, ""
    open_tag = open_match.group(0)
    close_tag = close_match.group(0)
    inner = li_html[open_match.end() : close_match.start()]
    heading = re.match(r"\s*(<strong\b[^>]*>.*?</strong\s*>)\s*", inner, re.I | re.S)
    if heading:
        return open_tag, heading.group(1), inner[heading.end() :], close_tag
    return open_tag, "", inner, close_tag


def _sentence_count(text: str) -> int:
    collapsed = collapse_ws(strip_html_to_text(text))
    if not collapsed:
        return 0
    return len([part for part in _SENTENCE_SPLIT_RE.split(collapsed) if part.strip()])


def _body_is_single_sentence(body_html: str) -> bool:
    return _sentence_count(body_html) <= 1


def _remove_sentence_from_html(container_html: str, claim: str) -> tuple[str, str] | None:
    """Remove the sentence containing *claim* from an <li> or <p>.

    Returns (new_container_html, removed_html) or None if the whole
    container should be deleted.
    """
    open_match = re.match(r"<(li|p)\b[^>]*>", container_html, re.I)
    close_match = re.search(r"</(li|p)\s*>\s*$", container_html, re.I)
    if not open_match or not close_match:
        return None
    open_tag = open_match.group(0)
    close_tag = close_match.group(0)
    inner = container_html[open_match.end() : close_match.start()]
    heading_match = re.match(r"(\s*<strong\b[^>]*>.*?</strong\s*>\s*)", inner, re.I | re.S)
    heading_html = heading_match.group(1) if heading_match else ""
    body_html = inner[len(heading_html) :]
    body_index = build_html_index(body_html)
    pattern = claim_pattern(claim)
    if pattern is None:
        return None
    match = pattern.search(body_index.collapsed)
    if not match:
        return None
    # Split the collapsed body into sentences and pick the one covering the match.
    sentences = []
    cursor = 0
    for split in _SENTENCE_SPLIT_RE.finditer(body_index.collapsed):
        sentences.append((cursor, split.start(), split.end()))
        cursor = split.end()
    sentences.append((cursor, len(body_index.collapsed), len(body_index.collapsed)))
    target = None
    for start, end, next_start in sentences:
        if start <= match.start() < max(end, next_start) or start <= match.start() < end or (
            start <= match.start() and match.end() <= max(end, 0) + 1
        ):
            if start <= match.start() and match.start() < (end if end > start else len(body_index.collapsed)):
                target = (start, end if end > start else len(body_index.collapsed))
                break
    if target is None:
        # Fallback: the remainder from the last split covering the match.
        for start, end, _next_start in sentences:
            sent_end = end if end > start else len(body_index.collapsed)
            if start <= match.start() < sent_end or (start <= match.start() <= sent_end):
                target = (start, sent_end)
                break
    if target is None:
        return None
    sent_start, sent_end = target
    html_start, html_end = _match_html_span(body_index, sent_start, max(sent_end, sent_start + 1))
    # Include a single leading whitespace that belongs to this sentence.
    while html_start > 0 and body_html[html_start - 1] in " \t\r\n":
        html_start -= 1
    removed = body_html[html_start:html_end]
    new_body = body_html[:html_start] + body_html[html_end:]
    new_body = _cleanup_fragment(new_body)
    if not strip_html_to_text(new_body):
        return None
    new_inner = _cleanup_fragment(heading_html + new_body)
    if not strip_html_to_text(new_inner):
        return None
    # No trailing space before the closing tag.
    new_inner = new_inner.rstrip(" \t")
    return f"{open_tag}{new_inner}{close_tag}", removed


def _cleanup_fragment(fragment: str) -> str:
    text = fragment
    text = _EMPTY_STRONG_RE.sub("", text)
    text = _DOUBLED_SEP_RE.sub(lambda m: m.group(0)[0] if m.group(0) else "", text)
    # Collapse doubled spaces in text nodes only.
    out: list[str] = []
    in_tag = False
    prev_space = False
    for ch in text:
        if ch == "<":
            in_tag = True
            prev_space = False
            out.append(ch)
            continue
        if ch == ">":
            in_tag = False
            prev_space = False
            out.append(ch)
            continue
        if in_tag:
            out.append(ch)
            continue
        if ch.isspace():
            if prev_space:
                continue
            prev_space = True
            out.append(" ")
            continue
        prev_space = False
        out.append(ch)
    cleaned = "".join(out)
    cleaned = _DOUBLED_SEP_RE.sub(". ", cleaned)
    return cleaned


def cleanup_html_document(raw: str) -> str:
    text = raw or ""
    text = _EMPTY_LI_RE.sub("", text)
    text = _EMPTY_P_RE.sub("", text)
    text = _EMPTY_STRONG_RE.sub("", text)
    return text


def ul_would_be_empty(html_src: str, removed_li_htmls: Iterable[str]) -> bool:
    working = html_src
    for unit in removed_li_htmls:
        if unit and unit in working:
            working = working.replace(unit, "", 1)
    working = cleanup_html_document(working)
    for start, end in _tag_ranges(working, "ul"):
        inner = working[start:end]
        if not re.search(r"<li\b", inner, re.I):
            return True
    return False


# ---------------------------------------------------------------------------
# Description strip
# ---------------------------------------------------------------------------


@dataclass
class DescriptionHit:
    status: str
    removed_unit_html: str = ""
    removed_unit_text: str = ""
    after_html: str = ""
    container_html: str = ""
    whole_container: bool = False


def _iter_claim_occurrences(index: HtmlIndex, claim: str) -> list[tuple[int, int]]:
    pattern = claim_pattern(claim)
    if pattern is None or not index.collapsed:
        return []
    return [(m.start(), m.end()) for m in pattern.finditer(index.collapsed)]


def _anchor_occurrences(
    index: HtmlIndex,
    occurrences: list[tuple[int, int]],
    snippet: str,
) -> list[tuple[int, int]]:
    mid = snippet_mid60(snippet)
    if not mid:
        return []
    anchored = []
    for start, end in occurrences:
        window_start = max(0, start - 160)
        window_end = min(len(index.collapsed), end + 160)
        window = index.collapsed[window_start:window_end]
        if mid in window:
            anchored.append((start, end))
    return anchored


def choose_description_unit(
    html_src: str,
    claim: str,
    html_start: int,
    html_end: int,
) -> tuple[str, str, bool]:
    """Return (unit_html, mode, is_whole_container).

    mode is 'li' or 'p'.
    """
    li_range = _innermost_containing(_tag_ranges(html_src, "li"), html_start, html_end)
    p_range = _innermost_containing(_tag_ranges(html_src, "p"), html_start, html_end)
    if li_range and (not p_range or (li_range[0] >= p_range[0] and li_range[1] <= p_range[1])):
        unit = html_src[li_range[0] : li_range[1]]
        _open, heading_html, body_html, _close = _li_heading_and_body(unit)
        heading_text = strip_html_to_text(heading_html)
        if heading_html and claim_matches_text(claim, heading_text):
            return unit, "li", True
        if _body_is_single_sentence(body_html):
            return unit, "li", True
        return unit, "li", False
    if p_range:
        unit = html_src[p_range[0] : p_range[1]]
        if _body_is_single_sentence(unit):
            return unit, "p", True
        return unit, "p", False
    # Bare text: refuse rather than delete an unbounded span.
    return "", "", True


def strip_claim_from_description(
    html_src: str,
    claim: str,
    live_snippet: str,
) -> DescriptionHit:
    source = html_src or ""
    if not claim_matches_text(claim, strip_html_to_text(source)):
        return DescriptionHit(status=STATUS_ALREADY_ABSENT, after_html=source)
    if not collapse_ws(live_snippet or ""):
        return DescriptionHit(status=STATUS_NO_SNIPPET, after_html=source)

    index = build_html_index(source)
    occurrences = _iter_claim_occurrences(index, claim)
    if not occurrences:
        return DescriptionHit(status=STATUS_ALREADY_ABSENT, after_html=source)
    anchored = _anchor_occurrences(index, occurrences, live_snippet)
    if not anchored:
        return DescriptionHit(status=STATUS_NOT_FOUND, after_html=source)
    if len(anchored) > 1:
        return DescriptionHit(status=STATUS_AMBIGUOUS, after_html=source)

    start, end = anchored[0]
    html_start, html_end = _match_html_span(index, start, end)
    zones = protected_ranges(source)
    if is_protected(source, html_start, html_end, zones):
        return DescriptionHit(status=STATUS_PROTECTED_ZONE, after_html=source)

    unit_html, _mode, whole = choose_description_unit(source, claim, html_start, html_end)
    if not unit_html:
        return DescriptionHit(status=STATUS_NOT_FOUND, after_html=source)
    if whole:
        after = source.replace(unit_html, "", 1)
        after = cleanup_html_document(after)
        return DescriptionHit(
            status=STATUS_WILL_STRIP,
            removed_unit_html=unit_html,
            removed_unit_text=strip_html_to_text(unit_html),
            after_html=after,
            container_html=unit_html,
            whole_container=True,
        )
    sentence_result = _remove_sentence_from_html(unit_html, claim)
    if sentence_result is None:
        after = source.replace(unit_html, "", 1)
        after = cleanup_html_document(after)
        return DescriptionHit(
            status=STATUS_WILL_STRIP,
            removed_unit_html=unit_html,
            removed_unit_text=strip_html_to_text(unit_html),
            after_html=after,
            container_html=unit_html,
            whole_container=True,
        )
    new_unit, removed = sentence_result
    after = source.replace(unit_html, new_unit, 1)
    after = cleanup_html_document(after)
    return DescriptionHit(
        status=STATUS_WILL_STRIP,
        removed_unit_html=removed,
        removed_unit_text=strip_html_to_text(removed),
        after_html=after,
        container_html=unit_html,
        whole_container=False,
    )


def apply_description_units(html_src: str, unit_htmls: list[str]) -> str:
    working = html_src or ""
    seen: set[str] = set()
    for unit in unit_htmls:
        if not unit or unit in seen or unit not in working:
            continue
        seen.add(unit)
        working = working.replace(unit, "", 1)
    return cleanup_html_document(working)


# ---------------------------------------------------------------------------
# Aspects
# ---------------------------------------------------------------------------


def find_aspect_key(aspects: Mapping[str, Any], name: str) -> str | None:
    want = str(name or "").strip().casefold()
    for key in aspects:
        if str(key).strip().casefold() == want:
            return str(key)
    return None


def aspect_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return [str(raw)]


def is_required_aspect(name: str, required_names: Iterable[str]) -> bool:
    want = str(name or "").strip().casefold()
    return any(str(item or "").strip().casefold() == want for item in required_names)


def is_features_aspect(name: str) -> bool:
    return str(name or "").strip().casefold() == "features"


@dataclass
class AspectHit:
    status: str
    after_aspects: dict[str, list[str]] = field(default_factory=dict)
    removed_values: list[str] = field(default_factory=list)


def strip_claim_from_aspects(
    aspects: Mapping[str, Any] | None,
    aspect_name: str,
    claim: str,
    required_names: Iterable[str] = (),
    *,
    required_aspects_available: bool = True,
) -> AspectHit:
    current = {
        str(key): aspect_values(value)
        for key, value in dict(aspects or {}).items()
    }
    key = find_aspect_key(current, aspect_name)
    if key is None:
        # Re-run after a successful strip: the key is already gone.
        return AspectHit(status=STATUS_ALREADY_ABSENT, after_aspects=current)
    values = current[key]
    target = normalize_claim_value(claim)
    kept = [value for value in values if normalize_claim_value(value) != target]
    removed = [value for value in values if normalize_claim_value(value) == target]
    if not removed:
        return AspectHit(status=STATUS_ALREADY_ABSENT, after_aspects=current)
    if not is_features_aspect(key) and not required_aspects_available:
        return AspectHit(status=STATUS_REQUIRED_ASPECTS_UNAVAILABLE, after_aspects=current)
    if not kept:
        if not is_features_aspect(key) and is_required_aspect(key, required_names):
            return AspectHit(status=STATUS_REQUIRED_ASPECT, after_aspects=current)
        current = dict(current)
        current.pop(key, None)
        return AspectHit(status=STATUS_WILL_STRIP, after_aspects=current, removed_values=removed)
    current = dict(current)
    current[key] = kept
    return AspectHit(status=STATUS_WILL_STRIP, after_aspects=current, removed_values=removed)


# ---------------------------------------------------------------------------
# Manifest / report
# ---------------------------------------------------------------------------


@dataclass
class ManifestRow:
    sku: str
    listing_id: str
    claim: str
    appears_in: list[str]
    live_snippet: str
    recommendation: str
    raw_appears_in: str = ""


def parse_appears_in(raw: str) -> list[str]:
    tokens = []
    for part in str(raw or "").split(";"):
        token = part.strip()
        if token:
            tokens.append(token)
    return tokens


def load_claims_manifest(path: str | Path) -> tuple[list[ManifestRow], dict[str, int]]:
    counts = {"strip_now": 0, "keep": 0, "needs_look": 0, "other": 0, "total": 0}
    rows: list[ManifestRow] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"claims manifest {path} has no header")
        fields = {name.strip().casefold(): name for name in reader.fieldnames if name}
        required = ("sku", "claim", "live_snippet", "recommendation")
        for col in required:
            if col not in fields:
                raise ValueError(f"claims manifest {path} missing column {col}")
        appears_key = fields.get("appears_in") or fields.get("location")
        if not appears_key:
            raise ValueError(f"claims manifest {path} missing appears_in/location")
        for raw in reader:
            counts["total"] += 1
            rec = str(raw.get(fields["recommendation"]) or "").strip()
            if rec == STRIP_NOW:
                counts["strip_now"] += 1
            elif rec == KEEP:
                counts["keep"] += 1
            elif rec == NEEDS_LOOK:
                counts["needs_look"] += 1
            else:
                counts["other"] += 1
            appears_raw = str(raw.get(appears_key) or "")
            listing_id = ""
            if "listing_id" in fields:
                listing_id = str(raw.get(fields["listing_id"]) or "").strip()
            rows.append(
                ManifestRow(
                    sku=str(raw.get(fields["sku"]) or "").strip(),
                    listing_id=listing_id,
                    claim=str(raw.get(fields["claim"]) or "").strip(),
                    appears_in=parse_appears_in(appears_raw),
                    live_snippet=str(raw.get(fields["live_snippet"]) or ""),
                    recommendation=rec,
                    raw_appears_in=appears_raw,
                )
            )
    return rows, counts


def filter_tokens_for_location(tokens: Iterable[str], location: str) -> list[str]:
    loc = (location or LOCATION_ALL).strip().casefold()
    kept = []
    for token in tokens:
        token = str(token).strip()
        if not token:
            continue
        if loc == LOCATION_ALL:
            kept.append(token)
            continue
        if loc == LOCATION_DESCRIPTION:
            if token in {TOKEN_OFFER_DESC, TOKEN_INV_DESC}:
                kept.append(token)
            continue
        if loc == LOCATION_FEATURES:
            if token.casefold() == TOKEN_FEATURES.casefold():
                kept.append(token)
            continue
        if loc == LOCATION_ASPECTS:
            if token.lower().startswith("aspect:") and token.casefold() != TOKEN_FEATURES.casefold():
                kept.append(token)
    return kept


def issues_for_sku(report: Mapping[str, Any] | list | None, sku: str) -> list[dict]:
    entries = []
    if isinstance(report, Mapping):
        entries = report.get("issues") or []
    elif isinstance(report, list):
        entries = report
    matched = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sku") or "").strip() != sku:
            continue
        issues = entry.get("issues") or []
        matched.extend(issue for issue in issues if isinstance(issue, dict))
    return matched


def claim_in_report(issues: Iterable[Mapping[str, Any]], claim: str) -> bool:
    needle = claim.casefold()
    for issue in issues:
        typ = str(issue.get("type") or "").strip()
        if not typ.startswith("semantic_"):
            continue
        detail = str(issue.get("detail") or "")
        if needle in detail.casefold():
            return True
    return False


def live_listing_id(offer: Mapping[str, Any] | None) -> str:
    offer = offer or {}
    listing = offer.get("listing") if isinstance(offer.get("listing"), dict) else {}
    return str(offer.get("listingId") or listing.get("listingId") or "").strip()


def live_title(inventory: Mapping[str, Any] | None, offer: Mapping[str, Any] | None = None) -> str:
    product = ((inventory or {}).get("product") or {}) if inventory else {}
    return str(product.get("title") or (offer or {}).get("title") or "")


def live_price(offer: Mapping[str, Any] | None) -> str:
    summary = ((offer or {}).get("pricingSummary") or {}) if offer else {}
    price = summary.get("price") or {}
    if isinstance(price, dict):
        return str(price.get("value") or "")
    return str(price or "")


def live_quantity(inventory: Mapping[str, Any] | None, offer: Mapping[str, Any] | None = None) -> Any:
    availability = ((inventory or {}).get("availability") or {}) if inventory else {}
    qty = (availability.get("shipToLocationAvailability") or {}).get("quantity")
    if qty is not None and str(qty).strip() != "":
        return qty
    if offer and offer.get("availableQuantity") is not None:
        return offer.get("availableQuantity")
    return None


def inventory_availability_quantity(inventory: Mapping[str, Any] | None) -> Any:
    availability = ((inventory or {}).get("availability") or {}) if inventory else {}
    return (availability.get("shipToLocationAvailability") or {}).get("quantity")


def offer_available_quantity(offer: Mapping[str, Any] | None) -> Any:
    if not offer:
        return None
    return offer.get("availableQuantity")


def fetches_price_or_qty_changed(
    first_inventory: Mapping[str, Any] | None,
    first_offer: Mapping[str, Any] | None,
    recheck_inventory: Mapping[str, Any] | None,
    recheck_offer: Mapping[str, Any] | None,
) -> bool:
    return (
        inventory_availability_quantity(first_inventory)
        != inventory_availability_quantity(recheck_inventory)
        or offer_available_quantity(first_offer) != offer_available_quantity(recheck_offer)
        or (first_offer or {}).get("pricingSummary") != (recheck_offer or {}).get("pricingSummary")
    )


def offer_description_only_put_payload(
    live_offer: Mapping[str, Any] | None,
    listing_description: str,
) -> dict[str, Any]:
    """Deep-copy a GET offer and change only listingDescription for PUT."""
    payload = deepcopy(dict(live_offer or {}))
    for key in OFFER_GET_ONLY_KEYS:
        payload.pop(key, None)
    payload["listingDescription"] = listing_description
    return payload


def live_image_urls(inventory: Mapping[str, Any] | None) -> list[str]:
    product = ((inventory or {}).get("product") or {}) if inventory else {}
    urls = product.get("imageUrls") or []
    return [str(item) for item in urls]


def live_category_id(offer: Mapping[str, Any] | None) -> str:
    offer = offer or {}
    category = offer.get("category") if isinstance(offer.get("category"), dict) else {}
    return str(offer.get("categoryId") or category.get("categoryId") or "").strip()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class TokenAction:
    sku: str
    claim: str
    token: str
    status: str
    removed_unit_text: str = ""
    removed_unit_html: str = ""
    field: str = ""


@dataclass
class ClaimResult:
    sku: str
    claim: str
    token: str
    status: str
    removed_unit_text: str = ""
    before_sha: str = ""
    after_sha: str = ""
    actions: list[TokenAction] = field(default_factory=list)
    readback: dict[str, bool] | None = None


@dataclass
class SkuPlan:
    sku: str
    listing_id: str = ""
    title: str = ""
    offer_description_before: str = ""
    offer_description_after: str = ""
    inventory_description_before: str = ""
    inventory_description_after: str = ""
    aspects_before: dict[str, list[str]] = field(default_factory=dict)
    aspects_after: dict[str, list[str]] = field(default_factory=dict)
    results: list[ClaimResult] = field(default_factory=list)
    skip_reason: str | None = None

    @property
    def has_changes(self) -> bool:
        return (
            self.offer_description_after != self.offer_description_before
            or self.inventory_description_after != self.inventory_description_before
            or self.aspects_after != self.aspects_before
        )

    def field_payload(self, which: str) -> str:
        if which == "after":
            return json.dumps(
                {
                    "offer": self.offer_description_after,
                    "inventory": self.inventory_description_after,
                    "aspects": self.aspects_after,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(
            {
                "offer": self.offer_description_before,
                "inventory": self.inventory_description_before,
                "aspects": self.aspects_before,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _aspect_name_from_token(token: str) -> str | None:
    if token.lower().startswith("aspect:"):
        return token.split(":", 1)[1].strip()
    return None


def _aggregate_status(actions: list[TokenAction]) -> str:
    if not actions:
        return STATUS_LOCATION_FILTERED
    if any(item.status == STATUS_IN_TITLE for item in actions):
        return STATUS_IN_TITLE
    if any(item.status == STATUS_WILL_STRIP for item in actions):
        return STATUS_WILL_STRIP
    if any(item.status == STATUS_STRIPPED for item in actions):
        return STATUS_STRIPPED
    order = [
        STATUS_SECTION_EMPTY,
        STATUS_LISTING_ID_MISMATCH,
        STATUS_DRIFT,
        STATUS_RESTORE_FAILED,
        STATUS_FAILED_RESTORED,
        STATUS_LIVE_CHANGED_BETWEEN_FETCHES,
        STATUS_WRITE_FAILED,
        STATUS_CLAIM_NOT_IN_REPORT,
        STATUS_PROTECTED_ZONE,
        STATUS_REQUIRED_ASPECTS_UNAVAILABLE,
        STATUS_REQUIRED_ASPECT,
        STATUS_AMBIGUOUS,
        STATUS_NO_SNIPPET,
        STATUS_NOT_FOUND,
        STATUS_ALREADY_ABSENT,
        STATUS_LOCATION_FILTERED,
    ]
    statuses = {item.status for item in actions}
    for status in order:
        if status in statuses:
            return status
    return actions[0].status


def plan_sku_claim_strip(
    sku: str,
    rows: list[ManifestRow],
    inventory: Mapping[str, Any] | None,
    offer: Mapping[str, Any] | None,
    report_issues: Iterable[Mapping[str, Any]],
    *,
    location: str = LOCATION_ALL,
    required_aspect_names: Iterable[str] = (),
    required_aspects_available: bool = True,
) -> SkuPlan:
    product = ((inventory or {}).get("product") or {}) if inventory else {}
    offer_desc = str((offer or {}).get("listingDescription") or "")
    inv_desc = str(product.get("description") or "")
    aspects_before = {
        str(key): aspect_values(value)
        for key, value in dict(product.get("aspects") or {}).items()
    }
    title = live_title(inventory, offer)
    listing_id = live_listing_id(offer)
    plan = SkuPlan(
        sku=sku,
        listing_id=listing_id,
        title=title,
        offer_description_before=offer_desc,
        offer_description_after=offer_desc,
        inventory_description_before=inv_desc,
        inventory_description_after=inv_desc,
        aspects_before=deepcopy(aspects_before),
        aspects_after=deepcopy(aspects_before),
    )

    sku_rows = [row for row in rows if row.sku == sku]
    for row in sku_rows:
        if row.listing_id and listing_id and row.listing_id != listing_id:
            plan.skip_reason = STATUS_LISTING_ID_MISMATCH
            for item in sku_rows:
                if item.recommendation != STRIP_NOW:
                    continue
                plan.results.append(
                    ClaimResult(
                        sku=sku,
                        claim=item.claim,
                        token=item.raw_appears_in,
                        status=STATUS_LISTING_ID_MISMATCH,
                    )
                )
            return plan

    working_aspects = deepcopy(aspects_before)
    offer_hits: list[tuple[TokenAction, DescriptionHit]] = []
    inv_hits: list[tuple[TokenAction, DescriptionHit]] = []
    pending_results: list[tuple[ClaimResult, list[str]]] = []

    for row in sku_rows:
        if row.recommendation != STRIP_NOW:
            continue
        tokens = filter_tokens_for_location(row.appears_in, location)
        actions: list[TokenAction] = []
        if claim_matches_text(row.claim, title):
            actions.append(
                TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=row.raw_appears_in,
                    status=STATUS_IN_TITLE,
                    field="title",
                )
            )
            plan.results.append(
                ClaimResult(
                    sku=sku,
                    claim=row.claim,
                    token=row.raw_appears_in,
                    status=STATUS_IN_TITLE,
                    before_sha=sha256_text(plan.field_payload("before")),
                    after_sha=sha256_text(plan.field_payload("before")),
                    actions=actions,
                )
            )
            continue
        if not claim_in_report(report_issues, row.claim):
            actions.append(
                TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=row.raw_appears_in,
                    status=STATUS_CLAIM_NOT_IN_REPORT,
                )
            )
            plan.results.append(
                ClaimResult(
                    sku=sku,
                    claim=row.claim,
                    token=row.raw_appears_in,
                    status=STATUS_CLAIM_NOT_IN_REPORT,
                    actions=actions,
                )
            )
            continue
        if not tokens:
            actions.append(
                TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=row.raw_appears_in,
                    status=STATUS_LOCATION_FILTERED,
                )
            )
            plan.results.append(
                ClaimResult(
                    sku=sku,
                    claim=row.claim,
                    token=row.raw_appears_in,
                    status=STATUS_LOCATION_FILTERED,
                    actions=actions,
                )
            )
            continue

        removed_texts: list[str] = []
        for token in tokens:
            if token == TOKEN_TITLE:
                continue
            if token == TOKEN_OFFER_DESC:
                hit = strip_claim_from_description(offer_desc, row.claim, row.live_snippet)
                action = TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=token,
                    status=hit.status,
                    removed_unit_text=hit.removed_unit_text,
                    removed_unit_html=hit.removed_unit_html,
                    field=TOKEN_OFFER_DESC,
                )
                actions.append(action)
                if hit.status == STATUS_WILL_STRIP:
                    offer_hits.append((action, hit))
                    removed_texts.append(hit.removed_unit_text)
                continue
            if token == TOKEN_INV_DESC:
                hit = strip_claim_from_description(inv_desc, row.claim, row.live_snippet)
                action = TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=token,
                    status=hit.status,
                    removed_unit_text=hit.removed_unit_text,
                    removed_unit_html=hit.removed_unit_html,
                    field=TOKEN_INV_DESC,
                )
                actions.append(action)
                if hit.status == STATUS_WILL_STRIP:
                    inv_hits.append((action, hit))
                    removed_texts.append(hit.removed_unit_text)
                continue
            aspect_name = _aspect_name_from_token(token)
            if aspect_name:
                hit = strip_claim_from_aspects(
                    working_aspects,
                    aspect_name,
                    row.claim,
                    required_aspect_names,
                    required_aspects_available=required_aspects_available,
                )
                action = TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=token,
                    status=hit.status,
                    removed_unit_text="; ".join(hit.removed_values),
                    field=token,
                )
                actions.append(action)
                if hit.status == STATUS_WILL_STRIP:
                    working_aspects = hit.after_aspects
                    plan.aspects_after = deepcopy(working_aspects)
                    removed_texts.extend(hit.removed_values)
                continue
            actions.append(
                TokenAction(
                    sku=sku,
                    claim=row.claim,
                    token=token,
                    status=STATUS_NOT_FOUND,
                    field=token,
                )
            )

        result = ClaimResult(
            sku=sku,
            claim=row.claim,
            token=row.raw_appears_in,
            status=_aggregate_status(actions),
            removed_unit_text=" | ".join(text for text in removed_texts if text),
            before_sha=sha256_text(plan.field_payload("before")),
            after_sha="",
            actions=actions,
        )
        pending_results.append((result, removed_texts))
        plan.results.append(result)

    def _apply_description_hits(
        original: str,
        hits: list[tuple[TokenAction, DescriptionHit]],
    ) -> str:
        if not hits:
            return original
        whole_containers = [
            hit.container_html
            for _action, hit in hits
            if hit.whole_container and hit.container_html
        ]
        working = original
        seen_whole: set[str] = set()
        for container in whole_containers:
            if container in seen_whole or container not in working:
                continue
            seen_whole.add(container)
            working = working.replace(container, "", 1)
        for _action, hit in hits:
            if hit.whole_container:
                continue
            if hit.container_html and any(hit.container_html == container for container in seen_whole):
                continue
            if hit.removed_unit_html and hit.removed_unit_html in working:
                working = working.replace(hit.removed_unit_html, "", 1)
        return cleanup_html_document(working)

    plan.offer_description_after = _apply_description_hits(offer_desc, offer_hits)
    plan.inventory_description_after = _apply_description_hits(inv_desc, inv_hits)
    for result, _texts in pending_results:
        result.after_sha = sha256_text(plan.field_payload("after"))

    offer_units = [
        hit.container_html if hit.whole_container else hit.removed_unit_html
        for _action, hit in offer_hits
    ]
    inv_units = [
        hit.container_html if hit.whole_container else hit.removed_unit_html
        for _action, hit in inv_hits
    ]
    if (
        ul_would_be_empty(offer_desc, [u for u in offer_units if (u or "").lower().find("<li") >= 0])
        or ul_would_be_empty(inv_desc, [u for u in inv_units if (u or "").lower().find("<li") >= 0])
    ):
        plan.skip_reason = STATUS_SECTION_EMPTY
        plan.offer_description_after = offer_desc
        plan.inventory_description_after = inv_desc
        plan.aspects_after = deepcopy(aspects_before)
        for result in plan.results:
            if result.status == STATUS_WILL_STRIP:
                result.status = STATUS_SECTION_EMPTY
                result.after_sha = result.before_sha
                result.removed_unit_text = ""
                for action in result.actions:
                    if action.status == STATUS_WILL_STRIP:
                        action.status = STATUS_SECTION_EMPTY
    return plan


def plans_equivalent(left: SkuPlan, right: SkuPlan) -> bool:
    return (
        left.offer_description_after == right.offer_description_after
        and left.inventory_description_after == right.inventory_description_after
        and left.aspects_after == right.aspects_after
        and left.skip_reason == right.skip_reason
    )


def targeted_aspect_names(plan: SkuPlan) -> set[str]:
    names = set()
    for result in plan.results:
        for action in result.actions:
            aspect_name = _aspect_name_from_token(action.token)
            if aspect_name:
                names.add(aspect_name.casefold())
    return names


def claim_still_in_field(claim: str, field_text: str) -> bool:
    return claim_matches_text(claim, strip_html_to_text(field_text) if "<" in (field_text or "") else field_text)


def claim_matches_unprotected(claim: str, html_src: str) -> bool:
    """True when *claim* appears outside tables, headings, PACKAGE INCLUDES, etc."""
    source = html_src or ""
    if "<" not in source:
        return claim_matches_text(claim, source)
    index = build_html_index(source)
    occurrences = _iter_claim_occurrences(index, claim)
    if not occurrences:
        return False
    zones = protected_ranges(source)
    for start, end in occurrences:
        html_start, html_end = _match_html_span(index, start, end)
        if not is_protected(source, html_start, html_end, zones):
            return True
    return False


def claim_still_in_unprotected(claim: str, field_text: str) -> bool:
    if "<" in (field_text or ""):
        return claim_matches_unprotected(claim, field_text)
    return claim_matches_text(claim, field_text)


def verify_readback(
    plan: SkuPlan,
    inventory: Mapping[str, Any] | None,
    offer: Mapping[str, Any] | None,
    before_inventory: Mapping[str, Any] | None,
    before_offer: Mapping[str, Any] | None,
) -> dict[str, bool]:
    product = ((inventory or {}).get("product") or {}) if inventory else {}
    before_product = ((before_inventory or {}).get("product") or {}) if before_inventory else {}
    offer_desc = str((offer or {}).get("listingDescription") or "")
    inv_desc = str(product.get("description") or "")
    aspects = {
        str(key): aspect_values(value)
        for key, value in dict(product.get("aspects") or {}).items()
    }
    targeted = targeted_aspect_names(plan)
    before_aspects = {
        str(key): aspect_values(value)
        for key, value in dict(before_product.get("aspects") or {}).items()
    }
    nontarget_equal = True
    for key, values in before_aspects.items():
        if key.casefold() in targeted:
            continue
        if aspects.get(key) != values:
            nontarget_equal = False
    for key in aspects:
        if key.casefold() in targeted:
            continue
        if key not in before_aspects:
            nontarget_equal = False

    units_gone = True
    claims_gone = True
    for result in plan.results:
        if result.status not in {STATUS_WILL_STRIP, STATUS_STRIPPED}:
            continue
        for action in result.actions:
            if action.status not in {STATUS_WILL_STRIP, STATUS_STRIPPED}:
                continue
            if action.field == TOKEN_OFFER_DESC:
                if action.removed_unit_html and action.removed_unit_html in offer_desc:
                    units_gone = False
                if claim_still_in_unprotected(result.claim, offer_desc):
                    claims_gone = False
            elif action.field == TOKEN_INV_DESC:
                if action.removed_unit_html and action.removed_unit_html in inv_desc:
                    units_gone = False
                if claim_still_in_unprotected(result.claim, inv_desc):
                    claims_gone = False
            else:
                aspect_name = _aspect_name_from_token(action.token)
                if aspect_name:
                    key = find_aspect_key(aspects, aspect_name)
                    values = aspects.get(key or "", [])
                    if any(normalize_claim_value(value) == normalize_claim_value(result.claim) for value in values):
                        units_gone = False
                        claims_gone = False

    description_matches = True
    if plan.offer_description_after != plan.offer_description_before:
        description_matches = offer_desc == plan.offer_description_after
    if plan.inventory_description_after != plan.inventory_description_before:
        description_matches = description_matches and inv_desc == plan.inventory_description_after

    offer_invariants = (
        offer_available_quantity(offer) == offer_available_quantity(before_offer)
        and (offer or {}).get("pricingSummary") == (before_offer or {}).get("pricingSummary")
        and (offer or {}).get("listingPolicies") == (before_offer or {}).get("listingPolicies")
        and (offer or {}).get("merchantLocationKey") == (before_offer or {}).get("merchantLocationKey")
        and (offer or {}).get("storeCategoryNames") == (before_offer or {}).get("storeCategoryNames")
    )
    invariants = (
        offer_invariants
        and inventory_availability_quantity(inventory)
        == inventory_availability_quantity(before_inventory)
        and live_category_id(offer) == live_category_id(before_offer)
        and live_title(inventory, offer) == live_title(before_inventory, before_offer)
        and live_image_urls(inventory) == live_image_urls(before_inventory)
        and nontarget_equal
    )
    return {
        "units_gone": units_gone,
        "description_matches": description_matches,
        "invariants_hold": invariants,
        "claims_gone": claims_gone,
    }


def snapshot_fields(inventory: Mapping[str, Any] | None, offer: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "inventory": deepcopy(inventory or {}),
        "offer": deepcopy(offer or {}),
    }


def replace_inventory_product_fields(
    inventory: Mapping[str, Any],
    *,
    description: str | None = None,
    aspects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = deepcopy(dict(inventory or {}))
    product = dict(payload.get("product") or {})
    if description is not None:
        product["description"] = description
    if aspects is not None:
        product["aspects"] = deepcopy(dict(aspects))
    payload["product"] = product
    return payload


def unified_field_diffs(plan: SkuPlan) -> list[str]:
    chunks: list[str] = []
    import difflib

    def _add(label: str, before: str, after: str) -> None:
        if before == after:
            return
        diff = difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            fromfile=f"{plan.sku} {label} before",
            tofile=f"{plan.sku} {label} after",
            lineterm="",
        )
        chunks.append("\n".join(diff))

    _add("offer_listingDescription.html", plan.offer_description_before, plan.offer_description_after)
    _add(
        "offer_listingDescription.txt",
        strip_html_to_text(plan.offer_description_before),
        strip_html_to_text(plan.offer_description_after),
    )
    _add("inventory_description.html", plan.inventory_description_before, plan.inventory_description_after)
    _add(
        "inventory_description.txt",
        strip_html_to_text(plan.inventory_description_before),
        strip_html_to_text(plan.inventory_description_after),
    )
    if plan.aspects_before != plan.aspects_after:
        chunks.append(
            f"{plan.sku} aspects before: {json.dumps(plan.aspects_before, ensure_ascii=False)}\n"
            f"{plan.sku} aspects after:  {json.dumps(plan.aspects_after, ensure_ascii=False)}"
        )
    return chunks


def result_entry(result: ClaimResult, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "sku": result.sku,
        "claim": result.claim,
        "token": result.token,
        "status": result.status,
        "removed_unit_text": result.removed_unit_text,
        "before_sha": result.before_sha,
        "after_sha": result.after_sha,
    }
    if result.readback is not None:
        payload["readback"] = result.readback
    if extra:
        payload.update(dict(extra))
    return payload


def sku_event_entry(
    sku: str,
    status: str,
    *,
    error: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "sku": sku,
        "claim": "",
        "token": "",
        "status": status,
        "removed_unit_text": "",
        "before_sha": "",
        "after_sha": "",
    }
    if error:
        payload["error"] = error
    if extra:
        payload.update(dict(extra))
    return payload


RequiredAspectLoader = Callable[[str | None], Iterable[str]]
