"""Title sanitization helpers for supplier-brand prefix cleanup."""

from __future__ import annotations

import os
import re
from typing import Iterable, Tuple


# Keep AquaVerve untouched (explicit user requirement).
KEEP_BRAND_PREFIXES = ("aquaverve",)

LEADING_TITLE_MARKERS = (
    "[VIDEO]",
    "[ASSEMBLY VIDEO PROVIDED]",
    "[ASSEMBLY VIDEO]",
    "[VIDEO PROVIDED]",
    "ASSEMBLY VIDEO PROVIDED",
    "ASSEMBLY VIDEO",
    "VIDEO PROVIDED",
)

# Known supplier prefixes seen in DaJian/GigaCloud source titles.
DEFAULT_SUPPLIER_PREFIXES = (
    "U_STYLE",
    "U Style",
    "UStyle",
    "DEEPfurniture",
    "VIBE HAUS",
    "HAUS",
    "TOPMAX",
    "TREXM",
    "Mirod",
    "VIBE",
    "K&K",
    "A&A",
)

COMPLETE_SHORT_TITLE_WORDS = {
    "a",
    "ac",
    "ad",
    "air",
    "amp",
    "and",
    "bar",
    "bed",
    "box",
    "btu",
    "cm",
    "cup",
    "day",
    "diy",
    "dvd",
    "eva",
    "fan",
    "fit",
    "for",
    "ft",
    "gal",
    "gas",
    "gym",
    "hd",
    "hp",
    "in",
    "kit",
    "lcd",
    "led",
    "mat",
    "mdf",
    "new",
    "oak",
    "off",
    "on",
    "or",
    "ott",
    "out",
    "pc",
    "pet",
    "pro",
    "pu",
    "pvc",
    "rgb",
    "set",
    "sofa",
    "top",
    "toy",
    "tv",
    "up",
    "usb",
    "uv",
    "v",
    "w",
    "wifi",
    "wood",
    "xl",
}

TRAILING_CONNECTOR_WORDS = {
    "and",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "up",
    "with",
    "x",
    "×",
}


def _slug_prefix_to_pattern(prefix: str) -> str:
    """Convert a readable prefix into a safe, flexible regex fragment."""
    normalized = re.sub(r"[\s_-]+", " ", str(prefix or "").strip())
    if not normalized:
        return ""

    if "&" in normalized:
        parts = [re.escape(p.strip()) for p in normalized.split("&") if p.strip()]
        if len(parts) == 2:
            return rf"{parts[0]}\s*&\s*{parts[1]}"

    tokens = [re.escape(token) for token in normalized.split() if token]
    if not tokens:
        return ""
    return r"[\s_-]*".join(tokens)


def _get_supplier_prefixes(extra_prefixes: Iterable[str] | None = None) -> Tuple[str, ...]:
    env_raw = os.getenv("SUPPLIER_BRAND_PREFIXES", "")
    env_prefixes = [p.strip() for p in env_raw.split(",") if p.strip()]
    combined = list(DEFAULT_SUPPLIER_PREFIXES) + env_prefixes + list(extra_prefixes or [])
    # Preserve order while de-duplicating (case-insensitive)
    seen = set()
    final = []
    for p in combined:
        key = p.casefold()
        if key in seen:
            continue
        seen.add(key)
        final.append(p)
    return tuple(final)


def _strip_leading_title_markers(title: str) -> tuple[str, bool]:
    """Remove non-product markers like [VIDEO] from the start of a title."""
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip()
    changed = False

    while cleaned:
        updated = cleaned
        for marker in LEADING_TITLE_MARKERS:
            updated = re.sub(
                rf"^\s*{re.escape(marker)}\s*[:|\-]*\s*",
                "",
                updated,
                flags=re.IGNORECASE,
            )

        updated = re.sub(r"^\s*(\[(?:video|assembly video(?: provided)?)\])\s*", "", updated, flags=re.IGNORECASE)
        updated = re.sub(r"\s+", " ", updated).strip()
        if updated == cleaned:
            break
        cleaned = updated
        changed = True

    return cleaned, changed


def _normalize_title_text(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip()


def _normalize_title_token(token: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(token or "").casefold())


def _strip_dangling_trailing_connectors(title: str) -> str:
    words = [word for word in _normalize_title_text(title).split(" ") if word]
    while words and words[-1].casefold() in TRAILING_CONNECTOR_WORDS:
        words.pop()
    return " ".join(words).strip(" -:|_/.,")


def _drop_last_title_token(title: str) -> str:
    normalized = _normalize_title_text(title)
    if " " not in normalized:
        return normalized
    return normalized.rsplit(" ", 1)[0].strip(" -:|_/.,")


def strip_supplier_brand_prefix(
    title: str,
    extra_prefixes: Iterable[str] | None = None,
) -> tuple[str, bool, str | None]:
    """
    Remove known supplier-brand prefix at the start of a title.

    Returns: (cleaned_title, changed, removed_prefix)
    """
    raw = re.sub(r"\s+", " ", str(title or "")).strip()
    if not raw:
        return "", False, None

    # Never remove our own storefront brand when it is a leading token.
    for keep in KEEP_BRAND_PREFIXES:
        if re.match(rf"^{re.escape(keep)}(?:\b|[\s:_\-|])", raw, flags=re.IGNORECASE):
            return raw, False, None

    for prefix in _get_supplier_prefixes(extra_prefixes):
        pattern = _slug_prefix_to_pattern(prefix)
        if not pattern:
            continue
        match = re.match(
            rf"^\s*((?:\[[^\]]+\]\s*)*)(?:{pattern})(?:\b|(?=\s|[_\-:|]))\s*(?:[-:|/_]+\s*)?",
            raw,
            flags=re.IGNORECASE,
        )
        if not match:
            continue

        leading_tags = (match.group(1) or "").strip()
        cleaned = raw[match.end() :]
        cleaned = re.sub(r"^[\s\-:|/_]+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) < 5:
            return raw, False, None
        if leading_tags:
            cleaned = f"{leading_tags} {cleaned}".strip()
        return cleaned, True, prefix

    return raw, False, None


def sanitize_listing_title(
    title: str,
    extra_prefixes: Iterable[str] | None = None,
) -> tuple[str, bool]:
    """
    Remove non-product markers and supplier prefixes from a listing title.

    Returns: (cleaned_title, changed)
    """
    raw = re.sub(r"\s+", " ", str(title or "")).strip()
    if not raw:
        return "", False

    cleaned, marker_changed = _strip_leading_title_markers(raw)
    cleaned, prefix_changed, _ = strip_supplier_brand_prefix(cleaned, extra_prefixes=extra_prefixes)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|_/")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, bool(marker_changed or prefix_changed or cleaned != raw)


def title_has_incomplete_trailing_fragment(
    title: str,
    *,
    source_title: str = "",
    max_length: int = 80,
) -> bool:
    """Return True when the title ends with a likely truncation fragment."""
    cleaned = _normalize_title_text(title)
    if not cleaned:
        return False

    words = [word for word in cleaned.split(" ") if word]
    if not words:
        return False

    last_word = words[-1]
    normalized_last = _normalize_title_token(last_word)
    next_source_char = ""
    source_prefix_match = False
    normalized_source = _normalize_title_text(source_title)
    if normalized_source.startswith(cleaned) and len(normalized_source) > len(cleaned):
        source_prefix_match = True
        next_source_char = normalized_source[len(cleaned) : len(cleaned) + 1]
        if next_source_char.isalnum():
            return True

    if len(last_word) == 1 and normalized_last not in {"a"}:
        return True

    if (
        re.search(r"\b\d+(?:\.\d+)?$", cleaned)
        and (source_prefix_match or len(cleaned) >= max_length - 5)
    ):
        return True

    if not normalized_last or normalized_last in COMPLETE_SHORT_TITLE_WORDS:
        return False

    if source_title:
        source_tokens = [
            _normalize_title_token(token)
            for token in re.split(r"[\s,\.\-\(\)/\+]+", normalized_source)
            if _normalize_title_token(token)
        ]
        if any(
            token.startswith(normalized_last) and token != normalized_last
            for token in source_tokens
        ):
            return True

    if len(cleaned) >= max_length and len(normalized_last) <= 3:
        return True

    return False


def normalize_listing_title_for_ebay(
    title: str,
    *,
    source_title: str = "",
    max_length: int = 80,
    extra_prefixes: Iterable[str] | None = None,
) -> tuple[str, bool]:
    """
    Remove non-product markers and trim titles to a word-safe eBay length.

    Returns: (cleaned_title, changed)
    """
    sanitized, sanitized_changed = sanitize_listing_title(title, extra_prefixes=extra_prefixes)
    cleaned = _normalize_title_text(sanitized)

    if len(cleaned) > max_length:
        truncated = cleaned[:max_length].rstrip(" -:|_/.,")
        if (
            max_length < len(cleaned)
            and truncated
            and truncated[-1].isalnum()
            and cleaned[max_length : max_length + 1].isalnum()
        ):
            truncated = _drop_last_title_token(truncated)
        cleaned = _strip_dangling_trailing_connectors(truncated)

    if title_has_incomplete_trailing_fragment(cleaned, source_title=source_title, max_length=max_length):
        candidate = _strip_dangling_trailing_connectors(_drop_last_title_token(cleaned))
        if candidate:
            cleaned = candidate

    cleaned = _normalize_title_text(cleaned).strip(" -:|_/.,")
    return cleaned, bool(sanitized_changed or cleaned != _normalize_title_text(title))
