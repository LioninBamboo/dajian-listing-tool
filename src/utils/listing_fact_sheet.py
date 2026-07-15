"""Fact-sheet extraction and comparison for general semantic hallucination detection.

Design: the LLM is used ONLY as an extractor — it turns free-form listing copy
into a structured fact sheet (materials / features / counts / capacity /
certifications / dimensions). The hallucination judgment itself is a
deterministic diff between the source fact sheet and the live fact sheet, so
results are reproducible, auditable, and cheap to cache.

This closes the long tail that per-pattern rule tables cannot enumerate
(e.g. source "600D Oxford" published as "Canvas" — no rule chain existed).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Mapping

FACT_SHEET_VERSION = 3

# Deterministic material lexicon — backstop for LLM extraction misses.
# qwen-plus reliably extracts materials from spec fields but can read a
# title material as a product-type word ("Canvas Bell Tent" → no canvas
# claim). Multi-word terms must come before their single-word suffixes.
_MATERIAL_LEXICON: tuple[str, ...] = (
    "stainless steel", "tempered glass", "solid wood", "memory foam",
    "genuine leather", "real leather", "top grain leather", "full grain leather",
    "pu leather", "faux leather", "bonded leather", "particle board",
    "engineered wood", "canvas", "oxford", "polyester", "nylon", "cotton",
    "linen", "velvet", "boucle", "suede", "oak", "pine", "teak", "walnut",
    "acacia", "bamboo", "eucalyptus", "rubberwood", "mdf", "plywood",
    "rattan", "wicker", "aluminum", "aluminium", "iron", "brass", "copper",
    "marble", "granite", "ceramic", "abs", "pvc", "acrylic", "resin",
    "concrete", "cement", "steel", "glass", "foam", "leather",
)

_STOPWORDS = {
    "a", "an", "and", "de", "for", "in", "of", "or", "the", "to", "with",
    "high", "quality", "premium", "durable", "new", "style", "design",
}

_DIMENSION_TOLERANCE = {"length": 1.0, "width": 1.0, "height": 1.0, "weight": 2.0}

# Claims that mean the same thing but share no tokens. When a live claim
# matches a group, it is supported if the source sheet matches the same group.
_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"4 season", "four season", "all season", "year round", "year-round", "all year"}),
    frozenset({"foldable", "folding", "collapsible"}),
    frozenset({"water resistant", "water-resistant", "withstand rain", "rain resistant"}),
    frozenset({"assembly required", "setup required", "needs assembly"}),
)

# Generalization is safe, specialization is the hallucination direction:
# live "fabric" backed by source "corduroy" is fine; live "polyester" backed
# by source "corduroy" is a violation. Leather is deliberately absent — the
# PU-leather→leather upgrade family belongs to the rule engine's chains.
_GENERIC_MATERIAL_MEMBERS: dict[str, frozenset[str]] = {
    "fabric": frozenset({
        "chenille", "corduroy", "velvet", "polyester", "cotton", "linen",
        "oxford", "canvas", "boucle", "suede", "mesh", "nylon", "upholstery",
    }),
    "wood": frozenset({
        "oak", "pine", "teak", "walnut", "acacia", "bamboo", "eucalyptus",
        "rubberwood", "mdf", "plywood", "hardwood", "solid wood",
    }),
    "metal": frozenset({
        "steel", "iron", "aluminum", "aluminium", "alloy", "brass", "copper", "chrome",
    }),
    "plastic": frozenset({
        "abs", "pvc", "acrylic", "polypropylene", "hdpe", "resin",
    }),
}

_CAPACITY_UNIT_WORDS = ("person", "people", "seat", "seater", "occupant")

# Unsupported feature claims in these families are refund/safety-grade (HIGH);
# everything else (use-case phrasing, comfort words) reports as MEDIUM.
_HIGH_RISK_FEATURE_PATTERN = re.compile(
    r"usb|charg|power|electric|outlet|heat|warm|cool|massage|vibrat|"
    r"waterproof|water[\s-]*resist|weather|fireproof|fire[\s-]*resist|"
    r"lock|safety|anti[\s-]*tip|certified|certification|tsa|ul\b|astm|"
    r"reclin|swivel|fold|convert|adjust",
    re.IGNORECASE,
)

_EXTRACTION_PROMPT = """You are a strict information extractor. Read the product listing content below and output ONLY a JSON object with the factual claims it makes. Do not infer, do not normalize beyond lowercasing, do not add claims that are not explicitly stated.

JSON schema:
{
  "materials": ["lowercase material claims, e.g. \\"600d oxford fabric\\", \\"solid wood\\", \\"pvc\\""],
  "features": ["lowercase functional feature claims, e.g. \\"stove jack\\", \\"foldable\\", \\"usb charging port\\", \\"mesh windows\\""],
  "counts": {"lowercase noun": integer, "e.g. doors": 2, "poles": 8},
  "capacity": "person/seat capacity claim like \\"8 person\\" or null",
  "certifications": ["lowercase certification claims, e.g. \\"tsa approved\\", \\"ul listed\\""],
  "dimensions": {"length": number or null, "width": number or null, "height": number or null, "weight": number or null}
}

Rules:
1. materials: every material named for the product or its parts, INCLUDING material words inside the TITLE (a "Canvas Bell Tent" title claims "canvas"; a "Solid Wood Desk" title claims "solid wood"). Fabric names (canvas, oxford, polyester, velvet), wood species, metals, leather types, and plastics are all materials.
2. features: functional capabilities only (not colors, not style words). INCLUDE usage-capability claims such as "4 season", "year-round use", "waterproof", "foldable".
3. counts: only explicit "N <noun>" claims (2 doors, 8 poles, 3 tiers, 12 stakes).
4. capacity: only explicit occupancy/seating claims.
5. dimensions: overall product dimensions in inches/lbs if stated.
6. Output raw JSON only, no markdown fences.

LISTING CONTENT:
"""


def fact_sheet_content_hash(title: str, description: str, structured: Mapping[str, Any] | None) -> str:
    """Stable hash of the exact inputs an extraction would see."""
    normalized = json.dumps(
        {
            "v": FACT_SHEET_VERSION,
            "title": title or "",
            "description": re.sub(r"\s+", " ", str(description or "")).strip(),
            "structured": {str(k): str(v) for k, v in sorted((structured or {}).items())},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_fact_sheet_cache(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_sheet_cache (
            content_hash TEXT PRIMARY KEY,
            sheet_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def get_cached_fact_sheet(conn, content_hash: str) -> dict | None:
    ensure_fact_sheet_cache(conn)
    row = conn.execute(
        "SELECT sheet_json FROM fact_sheet_cache WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def store_fact_sheet(conn, content_hash: str, sheet: Mapping[str, Any]) -> None:
    ensure_fact_sheet_cache(conn)
    conn.execute(
        "INSERT OR REPLACE INTO fact_sheet_cache (content_hash, sheet_json) VALUES (?, ?)",
        (content_hash, json.dumps(sheet, ensure_ascii=False)),
    )
    conn.commit()


def _strip_html(value: str) -> str:
    import html as _html

    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def lexical_materials(text: str) -> set[str]:
    """Deterministic material-term scan (longest match wins per position)."""
    found: set[str] = set()
    haystack = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    consumed = haystack
    for term in _MATERIAL_LEXICON:
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        if re.search(pattern, consumed):
            found.add(term)
            consumed = re.sub(pattern, " ", consumed)
    return found


def _normalize_sheet(raw: Mapping[str, Any]) -> dict:
    def _str_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return sorted({re.sub(r"\s+", " ", str(v)).strip().lower() for v in value if str(v).strip()})

    counts: dict[str, int] = {}
    for key, value in (raw.get("counts") or {}).items() if isinstance(raw.get("counts"), Mapping) else []:
        try:
            counts[str(key).strip().lower()] = int(value)
        except (TypeError, ValueError):
            continue

    dimensions: dict[str, float | None] = {}
    raw_dims = raw.get("dimensions") if isinstance(raw.get("dimensions"), Mapping) else {}
    for axis in ("length", "width", "height", "weight"):
        try:
            dimensions[axis] = float(raw_dims.get(axis)) if raw_dims.get(axis) is not None else None
        except (TypeError, ValueError):
            dimensions[axis] = None

    capacity = raw.get("capacity")
    capacity = re.sub(r"\s+", " ", str(capacity)).strip().lower() if capacity else None

    return {
        "materials": _str_list(raw.get("materials")),
        "features": _str_list(raw.get("features")),
        "counts": counts,
        "capacity": capacity or None,
        "certifications": _str_list(raw.get("certifications")),
        "dimensions": dimensions,
    }


def extract_fact_sheet(
    title: str,
    description: str,
    structured: Mapping[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> dict | None:
    """LLM extraction of a fact sheet. Returns None on any failure so callers
    can fall back to rules-only checking (never block the audit on the LLM)."""
    api_key = os.getenv("QWEN_API_KEY")
    if not api_key:
        return None

    parts = [f"TITLE: {title or ''}"]
    for key, value in (structured or {}).items():
        rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        parts.append(f"{key}: {rendered}")
    parts.append(f"DESCRIPTION: {_strip_html(description)[:6000]}")
    content = "\n".join(parts)
    backstop_materials = lexical_materials(content)

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout=timeout,
            max_retries=1,
        )
        response = client.chat.completions.create(
            model=os.getenv("FACT_SHEET_MODEL", "qwen-plus"),
            messages=[
                {"role": "system", "content": "You output only raw JSON."},
                {"role": "user", "content": _EXTRACTION_PROMPT + content},
            ],
            temperature=0.0,
            max_tokens=900,
        )
        text = (response.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        raw = json.loads(text)
        if not isinstance(raw, Mapping):
            return None
        sheet = _normalize_sheet(raw)
        # Deterministic backstop: material words the LLM read as product-type
        # nouns ("Canvas Bell Tent") are still material claims.
        sheet["materials"] = sorted(set(sheet["materials"]) | backstop_materials)
        return sheet
    except Exception as exc:
        print(f"[FactSheet] extraction failed: {exc}")
        return None


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {w.rstrip("s") if len(w) > 3 else w for w in words if w not in _STOPWORDS}


def _capacity_numbers(text: str) -> tuple[int, int] | None:
    """Parse a capacity claim into an inclusive (low, high) range.

    "8 person" → (8, 8); "4-8 person" → (4, 8); "seats 6" → (6, 6).
    Unit words (person/seat/seater/occupant) are treated as equivalent.
    Returns None when no number is present.
    """
    normalized = str(text or "").lower()
    if not normalized:
        return None
    range_match = re.search(r"(\d+)\s*(?:-|to|~)\s*(\d+)", normalized)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        return (min(low, high), max(low, high))
    single = re.search(r"(\d+)", normalized)
    if single:
        value = int(single.group(1))
        return (value, value)
    return None


def _capacity_supported(live_capacity: str, source_capacity: str) -> bool:
    live_range = _capacity_numbers(live_capacity)
    source_range = _capacity_numbers(source_capacity)
    if live_range is None or source_range is None:
        return _tokens(live_capacity) == _tokens(source_capacity)
    return source_range[0] <= live_range[0] and live_range[1] <= source_range[1]


def _generic_material_supported(material: str, source_materials: list[str]) -> bool:
    """live generic term (fabric/wood/metal/plastic) backed by a specific
    source member of that category is not a hallucination."""
    normalized = re.sub(r"\s+", " ", str(material or "").lower()).strip()
    members = _GENERIC_MATERIAL_MEMBERS.get(normalized)
    if not members:
        return False
    source_text = " ".join(source_materials).lower()
    return any(member in source_text for member in members) or normalized in source_text


def _matches_synonym_group(text: str) -> frozenset[str] | None:
    normalized = re.sub(r"[\s-]+", " ", str(text or "").lower())
    for group in _SYNONYM_GROUPS:
        if any(phrase in normalized for phrase in group):
            return group
    return None


def _claim_in_text(claim: str, text: str) -> bool:
    """Is a material/certification claim textually present in `text`?

    True when the claim appears as a substring, when all its significant
    tokens appear, or when it matches via the material lexicon. Used to
    decide whether a fact-sheet violation is *grounded* — a listing cannot
    be "claiming" a material its copy never mentions (the LLM extractor
    sometimes infers foam/veneer from soft-furnishing wording).
    """
    if not text:
        return False
    claim_norm = re.sub(r"\s+", " ", str(claim or "").lower()).strip()
    if not claim_norm:
        return False
    text_norm = re.sub(r"\s+", " ", str(text).lower())
    if claim_norm in text_norm:
        return True
    claim_tokens = _tokens(claim_norm)
    if claim_tokens and claim_tokens <= _tokens(text_norm):
        return True
    # Lexicon catches variant spellings (rubber wood/rubberwood)
    if claim_norm in lexical_materials(text_norm):
        return True
    return False


def _supported_by_any(claim: str, source_items: list[str], source_blob_tokens: set[str], source_blob_text: str = "") -> bool:
    """A live claim is supported when any source item shares a token, when the
    claim's tokens all appear in the source sheet, or when claim and source
    express the same thing through a known synonym group (4 season ≈ year-round)."""
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return True
    for item in source_items:
        if claim_tokens & _tokens(item):
            return True
    if claim_tokens <= source_blob_tokens:
        return True
    group = _matches_synonym_group(claim)
    if group is not None:
        normalized_blob = re.sub(r"[\s-]+", " ", source_blob_text.lower())
        return any(phrase in normalized_blob for phrase in group)
    return False


def compare_fact_sheets(
    source: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    live_text: str | None = None,
    source_text: str | None = None,
) -> list[dict]:
    """Deterministic diff: every live claim must be supported by the source sheet.

    Optional grounding (both default None → unchanged legacy behavior):
    - ``source_text``: raw source copy. A material literally present in the
      source text is supported even if the source *sheet* extraction missed
      it (kills "melamine/sponge is in source" false positives).
    - ``live_text``: raw live/after copy. A material NOT textually present in
      the live copy is an extractor inference, not a claim the listing makes,
      so it is suppressed (kills foam-from-"cushioned" false positives). A
      material that IS present but unsupported by source still flags (canvas
      on a rubberwood item stays a real hallucination).
    """
    violations: list[dict] = []
    source = _normalize_sheet(source)
    live = _normalize_sheet(live)

    source_blob_tokens: set[str] = set()
    source_blob_parts: list[str] = []
    for bucket in ("materials", "features", "certifications"):
        for item in source[bucket]:
            source_blob_tokens |= _tokens(item)
            source_blob_parts.append(item)
    if source["capacity"]:
        source_blob_tokens |= _tokens(source["capacity"])
        source_blob_parts.append(source["capacity"])
    source_blob_text = " | ".join(source_blob_parts)

    generic_terms = set(_GENERIC_MATERIAL_MEMBERS)
    for material in live["materials"]:
        if _generic_material_supported(material, source["materials"]):
            continue
        # A bare generic term (wood/metal/fabric/plastic/foam) with NO source
        # material info at all is unverifiable, not a hallucination — flagging
        # it buries the real material upgrades in noise.
        normalized_material = re.sub(r"\s+", " ", material).strip()
        if not source["materials"] and normalized_material in (generic_terms | {"foam"}):
            continue
        # Grounding A: material literally in source copy → supported even if
        # the source sheet extraction missed it.
        if source_text is not None and _claim_in_text(material, source_text):
            continue
        # Grounding C: material not textually present in live copy → the
        # extractor inferred it; the listing does not claim it.
        if live_text is not None and not _claim_in_text(material, live_text):
            continue
        if not _supported_by_any(material, source["materials"], source_blob_tokens, source_blob_text):
            violations.append(
                {
                    "claim_type": "semantic_material",
                    "claim_text": material,
                    "severity": "CRITICAL",
                    "source_evidence": ", ".join(source["materials"]) or "NOT_FOUND",
                }
            )

    for feature in live["features"]:
        if not _supported_by_any(feature, source["features"], source_blob_tokens, source_blob_text):
            violations.append(
                {
                    "claim_type": "semantic_feature",
                    "claim_text": feature,
                    "severity": "HIGH" if _HIGH_RISK_FEATURE_PATTERN.search(feature) else "MEDIUM",
                    "source_evidence": ", ".join(source["features"]) or "NOT_FOUND",
                }
            )

    for noun, live_count in live["counts"].items():
        source_count = source["counts"].get(noun)
        if source_count is not None and source_count != live_count:
            violations.append(
                {
                    "claim_type": "semantic_count",
                    "claim_text": f"{live_count} {noun} (source: {source_count})",
                    "severity": "CRITICAL",
                    "source_evidence": str(source_count),
                }
            )

    if live["capacity"] and not source["capacity"]:
        # "1 person" on a chair / "2 person" on a loveseat is the product type
        # itself, not a fabricated spec — only flag unsourced claims of 3+.
        live_range = _capacity_numbers(live["capacity"])
        if live_range is None or live_range[1] >= 3:
            violations.append(
                {
                    "claim_type": "semantic_capacity",
                    "claim_text": live["capacity"],
                    "severity": "HIGH",
                    "source_evidence": "NOT_FOUND",
                }
            )
    elif live["capacity"] and source["capacity"] and not _capacity_supported(live["capacity"], source["capacity"]):
        violations.append(
            {
                "claim_type": "semantic_capacity",
                "claim_text": f"{live['capacity']} (source: {source['capacity']})",
                "severity": "CRITICAL",
                "source_evidence": source["capacity"],
            }
        )

    for cert in live["certifications"]:
        # A certification the live copy does not textually state is an
        # extractor inference (never suppress a cert that IS in the copy —
        # a fake UL/TSA claim in the description must still flag).
        if live_text is not None and not _claim_in_text(cert, live_text):
            continue
        if not _supported_by_any(cert, source["certifications"], source_blob_tokens, source_blob_text):
            violations.append(
                {
                    "claim_type": "semantic_certification",
                    "claim_text": cert,
                    "severity": "CRITICAL",
                    "source_evidence": ", ".join(source["certifications"]) or "NOT_FOUND",
                }
            )

    for axis, tolerance in _DIMENSION_TOLERANCE.items():
        live_value = live["dimensions"].get(axis)
        source_value = source["dimensions"].get(axis)
        if live_value is not None and source_value is not None and abs(live_value - source_value) > tolerance:
            violations.append(
                {
                    "claim_type": "semantic_dimension",
                    "claim_text": f"{axis}: live={live_value} vs source={source_value}",
                    "severity": "CRITICAL",
                    "source_evidence": str(source_value),
                }
            )

    # One claim can land in two buckets (e.g. "ul certified" as feature AND
    # certification) — keep the most severe report per claim text.
    severity_rank = {"CRITICAL": 0, "HIGH": 1}
    deduped: dict[str, dict] = {}
    for violation in sorted(violations, key=lambda v: severity_rank.get(v["severity"], 9)):
        deduped.setdefault(violation["claim_text"].split(" (source:")[0], violation)
    return list(deduped.values())


def fact_sheet_for_content(
    conn,
    title: str,
    description: str,
    structured: Mapping[str, Any] | None = None,
) -> dict | None:
    """Cache-aware extraction: only calls the LLM when content actually changed."""
    content_hash = fact_sheet_content_hash(title, description, structured)
    cached = get_cached_fact_sheet(conn, content_hash)
    if cached is not None:
        return cached
    sheet = extract_fact_sheet(title, description, structured)
    if sheet is not None:
        store_fact_sheet(conn, content_hash, sheet)
    return sheet
