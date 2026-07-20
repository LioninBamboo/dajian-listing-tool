"""Hard guard against IP/brand-restricted terms in generated listings.

Some eBay categories (notably designer toys / blind boxes) have restricted
keywords — "POP MART" is a restricted search term and a VeRO-active brand, and
IP-claim words like "Original"/"Genuine"/"Authentic" invite takedowns. The
LLM is told to avoid them, but "told to" is not a guarantee; this module is the
deterministic backstop that scans the *generated* output and fails closed.

The banned-term list is per-instance (``StoreProfile.banned_terms``). It is
empty by default, so this guard is a no-op for the furniture / auto-parts
instances and only bites where an instance opts in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class BannedTermHit:
    term: str          # the banned term as configured
    field: str         # where it was found: "title" | "description" | "aspect:<name>"
    matched: str       # the exact substring that matched (original casing)
    context: str       # short surrounding text for the report


def _strip_html(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()


def _compile_term(term: str) -> Optional[re.Pattern]:
    """Whole-token, case-insensitive matcher; internal spaces match any run."""
    tokens = [re.escape(t) for t in str(term).split()]
    if not tokens:
        return None
    core = r"\s+".join(tokens)
    # (?<!\w)/(?!\w) so "OEM" does not fire inside "GOEM" or "OEMs".
    return re.compile(rf"(?<!\w){core}(?!\w)", re.IGNORECASE)


def _context(text: str, start: int, end: int, width: int = 24) -> str:
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    snippet = text[lo:hi].strip()
    return f"…{snippet}…" if (lo > 0 or hi < len(text)) else snippet


def _scan_field(field: str, text: str, patterns: Sequence[tuple]) -> List[BannedTermHit]:
    hits: List[BannedTermHit] = []
    if not text:
        return hits
    for term, pat in patterns:
        for m in pat.finditer(text):
            hits.append(
                BannedTermHit(
                    term=term,
                    field=field,
                    matched=m.group(0),
                    context=_context(text, m.start(), m.end()),
                )
            )
    return hits


def _resolve_terms(terms: Optional[Iterable[str]]) -> List[str]:
    if terms is not None:
        return [t for t in terms if str(t).strip()]
    from src.utils.store_profile import get_store_profile

    return [t for t in get_store_profile().banned_terms if str(t).strip()]


def scan_listing(
    *,
    title: str = "",
    description: str = "",
    aspects: Optional[Mapping[str, Any]] = None,
    terms: Optional[Iterable[str]] = None,
) -> List[BannedTermHit]:
    """Return every banned-term hit across title, description, and aspects.

    ``terms=None`` pulls the list from the active StoreProfile; pass an explicit
    list in tests. An empty term list yields no hits (guard disabled).
    Description is HTML-stripped before matching so tag noise never hides a term.
    """
    resolved = _resolve_terms(terms)
    if not resolved:
        return []
    patterns = [(t, p) for t in resolved if (p := _compile_term(t)) is not None]
    if not patterns:
        return []

    hits: List[BannedTermHit] = []
    hits += _scan_field("title", title or "", patterns)
    hits += _scan_field("description", _strip_html(description or ""), patterns)

    for name, value in (aspects or {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        joined = " ".join(str(v) for v in values if v is not None)
        # scan the aspect name too — a banned brand can hide in a key
        hits += _scan_field(f"aspect:{name}", f"{name} {joined}", patterns)

    return hits


def assert_clean(
    *,
    title: str = "",
    description: str = "",
    aspects: Optional[Mapping[str, Any]] = None,
    terms: Optional[Iterable[str]] = None,
) -> None:
    """Raise BannedTermError if any banned term is present."""
    hits = scan_listing(title=title, description=description, aspects=aspects, terms=terms)
    if hits:
        raise BannedTermError(hits)


class BannedTermError(ValueError):
    def __init__(self, hits: Sequence[BannedTermHit]):
        self.hits = list(hits)
        summary = ", ".join(sorted({f"{h.term} @ {h.field}" for h in hits}))
        super().__init__(f"banned terms present: {summary}")
