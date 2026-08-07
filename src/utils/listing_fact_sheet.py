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

from src.utils.dimension_helpers import normalize_dimension_orientation

FACT_SHEET_VERSION = 4
FACT_SHEET_RULESET_VERSION = "fact-sheet-rules-v1"
FACT_SHEET_CACHE_SCHEMA_VERSION = 1

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
    frozenset({"adjustable height", "height adjustable", "hydraulic adjustment", "hydraulic lift"}),
    # Surface treatment wording (trellis W1586*: source says "powder coating",
    # live/extractor often says "powder coated" — not a material upgrade).
    frozenset({
        "powder coated",
        "powder coating",
        "powder-coat",
        "powder-coated",
        "powder-coating",
        "powder coat",
    }),
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
    # 2026-07-26: "engineered wood" is the US retail umbrella term for exactly
    # these panel products — a source listing "MDF / particle board / melamine"
    # genuinely IS engineered wood, so flagging it wasted 12 CRITICAL slots.
    # Membership is still required: a chenille-and-foam sofa whose copy claims
    # an "engineered wood frame" has no member here and keeps flagging, which
    # is correct — the source never states a frame material at all.
    "engineered wood": frozenset({
        "mdf", "particle board", "particleboard", "melamine", "plywood",
        "chipboard", "fiberboard", "osb", "veneer", "composite wood",
    }),
    "engineered wood frame": frozenset({
        "mdf", "particle board", "particleboard", "melamine", "plywood",
        "chipboard", "fiberboard", "osb", "veneer", "composite wood",
    }),
    # HDPE/polyethylene is what "PE rattan" is made of. Requiring a PE member
    # keeps a polyester+PU tent claiming HDPE flagged (W2505P427760).
    "hdpe": frozenset({
        "pe rattan", "polyethylene", "pe wicker", "hdpe",
    }),
    "polyethylene": frozenset({
        "pe rattan", "polyethylene", "pe wicker", "hdpe",
    }),
}

_CAPACITY_UNIT_WORDS = ("person", "people", "seat", "seater", "occupant")

# Unsupported feature claims in these families are refund/safety-grade (HIGH);
# everything else (use-case phrasing, comfort words) reports as MEDIUM.
_HIGH_RISK_FEATURE_PATTERN = re.compile(
    r"usb|charg|power|electric|outlet|heat|warm|cool|massage|vibrat|"
    r"waterproof|water[\s-]*resist|weather|fireproof|fire[\s-]*resist|"
    # any "-proof" / "-resistant" property claim (rust/corrosion/uv/frost/scratch…)
    r"[a-z]{3,}[\s-]*proof|[a-z]{3,}[\s-]*resist\w*|corros\w*|rust\b|uv[\s-]|frost|"
    r"food[\s-]*grade|oe[\s-]*grade|heavy[\s-]*duty|"
    r"lock|safety|anti[\s-]*tip|certified|certification|tsa|ul\b|astm|"
    r"reclin|swivel|fold|convert|adjust",
    re.IGNORECASE,
)

# Subjective/aesthetic claims that are not falsifiable against a source spec.
# 2026-07-26: these produced ~2.9k MEDIUM rows per audit ("comfortable",
# "modern", "sturdy", "ergonomic"), burying the ~200 real CRITICAL findings
# and making every report read as "still thousands of problems". A buyer
# cannot be refunded for a sofa that is insufficiently "modern"; a claim only
# belongs in the fact sheet if the source could contradict it.
#
# Deliberately NOT listed here (they stay checked because they are verifiable
# and refund-relevant): waterproof, foldable, adjustable, reclining, locking,
# portable, expandable, reversible, and anything _HIGH_RISK_FEATURE_PATTERN
# matches — that pattern is applied first and always wins.
_SUBJECTIVE_FEATURE_PATTERN = re.compile(
    r"^(?:"
    r"comfort\w*|cozy|cosy|relax\w*|soft|plush|luxur\w*|elegant|stylish|"
    r"modern|contemporary|classic|traditional|rustic|minimalist|chic|sleek|"
    r"beautiful|attractive|premium|high[\s-]*quality|quality|durable|sturdy|"
    r"robust|strong|reliable|versatile|multi[\s-]*functional|practical|"
    r"convenient|ergonomic|spacious|roomy|generous|compact|space[\s-]*saving|"
    r"lightweight|easy[\s-]*(?:to[\s-]*)?(?:clean|assemble|use|move|maintain)|"
    r"easy[\s-]*assembly|hassle[\s-]*free\s*\w*|simple\s*\w*|"
    r"perfect\s*\w*|ideal\s*\w*|great\s*\w*|excellent\s*\w*|"
    # descriptive / aesthetic framing (HIGH_RISK is checked first, so real
    # property claims like "corrosion-resistant finish" still get blocked):
    r"decorative|decor|ornamental|natural(?:[\s-]*look\w*)?|inviting|charming|"
    r"aesthetic|timeless|refined|tasteful|understated|vibrant|bold|airy|fresh|"
    r"effortless|eye[\s-]*catching|statement|accent|"
    r"freestanding|free[\s-]*standing|floor[\s-]*standing|standalone|tabletop|"
    r"ready[\s-]*to[\s-]*use|use[\s-]*ready|no[\s-]*assembly|pre[\s-]*assembled|"
    r"assembly[\s-]*free|no[\s-]*tools|"
    r"[a-z]+[\s-]*(?:design|shape|style|finish|look|tone|profile|silhouette|feel|accent)|"
    r"indoor|outdoor|indoor\s*/?\s*outdoor|home|office|living\s*room|bedroom"
    r")$",
    re.IGNORECASE,
)


def is_subjective_feature(feature: str) -> bool:
    """Aesthetic/comfort wording a source spec can never contradict.

    High-risk (safety/function) wording always wins, so "easy to clean
    waterproof cover" is still checked.
    """
    text = re.sub(r"[\s-]+", " ", str(feature or "").strip().lower())
    if not text:
        return True
    if _HIGH_RISK_FEATURE_PATTERN.search(text):
        return False
    return bool(_SUBJECTIVE_FEATURE_PATTERN.match(text))

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
            fact_sheet_version INTEGER NOT NULL DEFAULT 0,
            ruleset_version TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'listing_content',
            model_status TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(fact_sheet_cache)").fetchall()
    }
    migrations = {
        "fact_sheet_version": "INTEGER NOT NULL DEFAULT 0",
        "ruleset_version": "TEXT NOT NULL DEFAULT ''",
        "source_kind": "TEXT NOT NULL DEFAULT 'listing_content'",
        "model_status": "TEXT NOT NULL DEFAULT 'unknown'",
    }
    for column, definition in migrations.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE fact_sheet_cache ADD COLUMN {column} {definition}")
    conn.commit()


def get_fact_sheet_cache_record(conn, content_hash: str) -> dict | None:
    """Return a current-version cache record, including provenance."""
    ensure_fact_sheet_cache(conn)
    row = conn.execute(
        """
        SELECT content_hash, sheet_json, created_at, fact_sheet_version,
               ruleset_version, source_kind, model_status
        FROM fact_sheet_cache
        WHERE content_hash = ?
          AND fact_sheet_version = ?
          AND ruleset_version = ?
        """,
        (content_hash, FACT_SHEET_VERSION, FACT_SHEET_RULESET_VERSION),
    ).fetchone()
    if not row:
        return None
    try:
        return {
            "content_hash": row[0],
            "sheet": json.loads(row[1]),
            "created_at": row[2],
            "fact_sheet_version": row[3],
            "ruleset_version": row[4],
            "source_kind": row[5],
            "model_status": row[6],
        }
    except (TypeError, ValueError):
        return None


def get_cached_fact_sheet(conn, content_hash: str) -> dict | None:
    record = get_fact_sheet_cache_record(conn, content_hash)
    return record["sheet"] if record else None


def store_fact_sheet(
    conn,
    content_hash: str,
    sheet: Mapping[str, Any],
    *,
    source_kind: str = "listing_content",
    model_status: str = "extracted",
) -> None:
    ensure_fact_sheet_cache(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO fact_sheet_cache (
            content_hash, sheet_json, fact_sheet_version, ruleset_version,
            source_kind, model_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            content_hash,
            json.dumps(sheet, ensure_ascii=False),
            FACT_SHEET_VERSION,
            FACT_SHEET_RULESET_VERSION,
            str(source_kind or "listing_content"),
            str(model_status or "unknown"),
        ),
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


# Spelled-out counts: "two-seater" carries the same claim as "2 person" but has
# no digit, so it used to fall through to token equality and report a conflict.
_WORD_NUMBERS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "twelve": 12,
}

# Standard US mattress occupancy. Only sizes with an unambiguous count are
# listed: "king" is deliberately mapped to 2 sleepers, so a live "king size"
# against a source "4 person" still reports — sofa-bed occupancy is a judgment
# call that belongs to a human, not to this table.
_BED_SIZE_OCCUPANCY: dict[str, int] = {
    "twin": 1, "single": 1, "twin xl": 1,
    "full": 2, "double": 2, "queen": 2, "king": 2, "california king": 2,
}

# Noise words that carry no capacity information ("queen size" == "queen").
_CAPACITY_NOISE = re.compile(r"\b(?:size|sized|bed|mattress)\b")


def _capacity_numbers(text: str) -> tuple[int, int] | None:
    """Parse a capacity claim into an inclusive (low, high) range.

    "8 person" → (8, 8); "4-8 person" → (4, 8); "seats 6" → (6, 6);
    "two-seater" → (2, 2); "queen size" → (2, 2).
    Unit words (person/seat/seater/occupant) are treated as equivalent.
    Returns None when no count can be derived.
    """
    normalized = str(text or "").lower()
    if not normalized:
        return None
    normalized = _CAPACITY_NOISE.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
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
    # Spelled-out count, e.g. "two-seater".
    for word, value in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", normalized):
            return (value, value)
    # Mattress size implies occupancy; longest name first so "california king"
    # and "twin xl" win over the bare "king"/"twin" they contain.
    for name in sorted(_BED_SIZE_OCCUPANCY, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", normalized):
            return (_BED_SIZE_OCCUPANCY[name],) * 2
    return None


def _capacity_supported(live_capacity: str, source_capacity: str) -> bool:
    live_range = _capacity_numbers(live_capacity)
    source_range = _capacity_numbers(source_capacity)
    if live_range is None or source_range is None:
        return _tokens(live_capacity) == _tokens(source_capacity)
    return source_range[0] <= live_range[0] and live_range[1] <= source_range[1]


def _explicit_structured_capacity(structured: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """Read a trustworthy seat/capacity count from structured source fields.

    Supplier descriptions can disagree with structured fields. Only fields
    that explicitly describe seats/capacity participate; weight/load capacity
    and dimensional seat fields are excluded.
    """
    for key, raw in _structured_entries(structured):
        key_text = str(key or "").strip().lower()
        rendered = ", ".join(str(value) for value in raw) if isinstance(raw, (list, tuple)) else str(raw or "")
        if any(marker in key_text for marker in ("weight", "load", "depth", "height", "width")):
            continue
        if key_text == "variant":
            if not re.search(r"\b\d+\s*seat(?:s|er)?\b", rendered, re.IGNORECASE):
                continue
        elif not any(
            marker in key_text
            for marker in ("seat", "seating capacity", "occupancy", "capacity")
        ):
            continue
        parsed = _capacity_numbers(rendered)
        if parsed is not None:
            return parsed
    return None


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
    # Synonym groups: powder coated ↔ powder coating, foldable ↔ folding, etc.
    group = _matches_synonym_group(claim_norm)
    if group is not None:
        normalized = re.sub(r"[\s-]+", " ", text_norm)
        if any(phrase in normalized for phrase in group):
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


def _count_claim_in_text(count: int, noun: str, text: str) -> bool:
    """Recognize an explicit source count, including tier/shelf wording."""
    raw_noun = str(noun or "").lower()
    noun_key = {
        "shelves": "shelf",
        "tiers": "tier",
        "levels": "level",
    }.get(raw_noun, raw_noun.rstrip("s"))
    synonyms = {
        "shelf": ("shelf", "shelves", "tier", "tiers", "level", "levels"),
        "tier": ("tier", "tiers", "shelf", "shelves", "level", "levels"),
    }.get(noun_key, (noun_key, noun_key + "s"))
    terms = "|".join(re.escape(term) for term in synonyms)
    text = str(text or "")
    if re.search(
        rf"(?<!\d){int(count)}\s*[-\s]*(?:{terms})(?![a-z])",
        text,
        re.IGNORECASE,
    ):
        return True
    # Source copy often qualifies a component count (``1 main net`` or
    # ``2 side triangular nets``). Keep this bounded to two words so a
    # broader aggregate such as ``1 of 2 nets`` is not treated as the same
    # claim.
    return bool(
        re.search(
            rf"(?<!\d){int(count)}\s+(?:[a-z]+\s+){{1,2}}(?:{terms})(?![a-z])",
            text,
            re.IGNORECASE,
        )
    )


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
        if is_subjective_feature(feature):
            continue
        # Grounding A: feature literally in source copy -> supported
        if source_text is not None and _claim_in_text(feature, source_text):
            continue
        # Grounding C: the extractor inferred a feature from the product
        # context, but the live copy/aspects do not actually claim it.
        if live_text is not None and not _claim_in_text(feature, live_text):
            continue
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
        source_count_is_trusted = source_count is not None and (
            source_count > 0
            or (
                source_text is not None
                and _count_claim_in_text(source_count, noun, source_text)
            )
        )
        if source_count_is_trusted and source_count != live_count:
            # If the source copy itself explicitly makes the live count claim,
            # the discrepancy is an internal source-attribute conflict (for
            # example, ``3-tier`` alongside a structured six-shelf field), not
            # a hallucination introduced by the listing rewrite.
            if source_text is not None and _count_claim_in_text(live_count, noun, source_text):
                continue
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
    elif (
        live["capacity"]
        and source["capacity"]
        and not _capacity_supported(live["capacity"], source["capacity"])
    ):
        # Bed-size wording (for example, ``twin size``) is often stored in
        # the source copy while the structured source field stores occupancy
        # (for example, ``2 person``).  Treat that as a source-internal
        # representation conflict, not a fabricated live claim.
        if not (source_text is not None and _claim_in_text(live["capacity"], source_text)):
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
        sheet = cached
    else:
        sheet = extract_fact_sheet(title, description, structured)
        if sheet is not None:
            store_fact_sheet(conn, content_hash, sheet)

    if sheet is None:
        return None

    # Explicit eBay item aspects are more reliable than an LLM choosing an
    # axis from an unlabelled ``L x W x H`` sentence. Apply them to both cache
    # hits and fresh extractions so dimension orientation remains stable.
    explicit_dimensions = _explicit_fact_sheet_dimensions(structured, title)
    if explicit_dimensions:
        sheet = dict(sheet)
        dimensions = dict(sheet.get("dimensions") or {})
        dimensions.update(explicit_dimensions)
        sheet["dimensions"] = dimensions
    return sheet


def _structured_entries(structured: Mapping[str, Any] | None):
    for key, raw in (structured or {}).items():
        if isinstance(raw, Mapping) and str(key).lower() in {"attributes", "specs", "aspects"}:
            yield from raw.items()
        else:
            yield key, raw


def _numeric_structured_value(raw: Any) -> float | None:
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text or re.search(
            r"\b(?:see\s+description|not\s+applicable|n/?a)\b",
            text,
            re.IGNORECASE,
        ):
            continue
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            continue
        value = float(match.group(0))
        if value > 0:
            return value
    return None


def _title_uses_size_class_for_furniture_product(title: str) -> bool:
    """Return whether a numeric title size is likely a furniture class label.

    Sofa titles commonly use values such as ``100-inch sofa`` as a product
    size class, while the source's labeled assembled length is the actual
    item dimension. Keep the title value for other product types so a genuine
    title/spec conflict remains visible for review.
    """
    return bool(
        re.search(
            r"\b(?:sofa|sofas|sectional|sectionals|sleeper|sleepers|"
            r"couch|couches|loveseat|loveseats|futon|futons|daybed|daybeds)\b",
            str(title or ""),
            re.IGNORECASE,
        )
    )


def _explicit_fact_sheet_dimensions(
    structured: Mapping[str, Any] | None,
    title: str,
) -> dict[str, float]:
    """Read labeled item/source dimensions without trusting axis order in prose.

    ``Item *`` keys are live eBay aspects and take precedence. Source snapshots
    use ``Assembled *``/``Product Weight`` keys; a conflicting numeric title
    length remains visible for human review instead of being silently replaced.
    """
    axis_patterns = {
        "length": r"\bitem\s+length\b",
        "width": r"\bitem\s+width\b",
        "height": r"\bitem\s+height\b",
        "weight": r"\bitem\s+weight\b",
    }
    source_patterns = {
        "length": r"\b(?:assembled|overall|product)\s+length\b",
        "width": r"\b(?:assembled|overall|product)\s+width\b",
        "height": r"\b(?:assembled|overall|product)\s+height\b",
        "weight": r"\bproduct\s+weight\b",
    }
    item_values: dict[str, float] = {}
    source_values: dict[str, float] = {}
    for key, raw in _structured_entries(structured):
        key_text = str(key)
        if re.search(r"\b(package|shipping|carton|box)\b", key_text, re.IGNORECASE):
            continue
        axis = next(
            (
                name
                for name, pattern in axis_patterns.items()
                if re.search(pattern, key_text, re.IGNORECASE)
            ),
            None,
        )
        if axis is not None:
            value = _numeric_structured_value(raw)
            if value is not None:
                item_values[axis] = value
            continue
        if re.search(r"\boverall\s+product\s+weight\b", key_text, re.IGNORECASE):
            continue
        axis = next(
            (
                name
                for name, pattern in source_patterns.items()
                if re.search(pattern, key_text, re.IGNORECASE)
            ),
            None,
        )
        if axis is not None:
            value = _numeric_structured_value(raw)
            if value is not None:
                source_values[axis] = value

    # Supplier feeds for hall trees can label the vertical dimension as
    # ``Assembled Length`` and the depth as ``Assembled Height``. Reuse the
    # canonical orientation rule used when building eBay Item aspects, but
    # apply it only to source values: an explicit live ``Item *`` aspect must
    # remain authoritative and must not be swapped a second time.
    source_descriptor = {
        key: raw
        for key, raw in _structured_entries(structured)
        if str(key).lower() in {"product type", "type", "category", "product name", "title"}
    }
    source_values = normalize_dimension_orientation(source_descriptor, source_values)

    values = dict(source_values)
    values.update(item_values)
    title_match = re.search(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:\"|in(?:ch(?:es)?)?\b)",
        str(title or ""),
        re.IGNORECASE,
    )
    if title_match and "length" in source_values and "length" not in item_values:
        if abs(float(title_match.group(1)) - source_values["length"]) > _DIMENSION_TOLERANCE["length"]:
            source_descriptor_text = " ".join(str(value) for value in source_descriptor.values())
            is_hall_tree = "hall tree" in f"{title} {source_descriptor_text}".lower()
            if not _title_uses_size_class_for_furniture_product(title) and not is_hall_tree:
                values.pop("length", None)
    return values


def check_fact_sheet_violations(
    conn,
    source_title: str,
    source_description: str,
    source_attributes: Mapping[str, Any],
    source_specs: Mapping[str, Any],
    candidate_title: str,
    candidate_description: str,
    candidate_aspects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Unified FactSheet validation gate for READY, publish, and live audits.
    
    Returns:
        {"status": "pass" | "violations" | "unavailable",
         "violations": [...],
         "source_fingerprint": str,
         "candidate_fingerprint": str}
    """
    import os
    if os.getenv("AUDIT_SEMANTIC_FACT_SHEET", "1") == "0" or not os.getenv("QWEN_API_KEY"):
        return {"status": "unavailable", "violations": [], "source_fingerprint": "", "candidate_fingerprint": ""}

    candidate_aspects = candidate_aspects or {}

    def _is_package_measurement_key(key: object) -> bool:
        text = str(key or "")
        return bool(
            re.search(r"\b(package|shipping|carton|box)\b", text, re.IGNORECASE)
            and re.search(r"\b(length|width|height|weight|dimension|size)\b", text, re.IGNORECASE)
        )

    # Package/shipping measurements describe the carton, not the item. They
    # must not become source item dimensions in the semantic comparison.
    source_attributes_for_fact_sheet = {
        key: value
        for key, value in (source_attributes or {}).items()
        if not _is_package_measurement_key(key)
    }
    source_specs_for_fact_sheet = {
        key: value
        for key, value in (source_specs or {}).items()
        if not _is_package_measurement_key(key)
    }
    source_structured = {
        **source_attributes_for_fact_sheet,
        **source_specs_for_fact_sheet,
    }
    candidate_structured = {
        key: value
        for key, value in candidate_aspects.items()
        if not _is_package_measurement_key(key)
    }

    source_description_for_fact_sheet = re.sub(
        r"<li\b[^>]*>\s*(?:package|shipping|carton|box)\s+"
        r"(?:length|width|height|weight|dimensions?|size)\b.*?</li>",
        "",
        str(source_description or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    source_description_for_fact_sheet = re.sub(
        r"(?:^|[;\n|])\s*(?:package|shipping|carton|box)\s+"
        r"(?:length|width|height|weight|dimensions?|size)\s*[:=][^;\n|<]+",
        "",
        source_description_for_fact_sheet,
        flags=re.IGNORECASE,
    )

    # Seat/counter/bar height is a labeled product attribute, not the
    # listing's overall item height. Remove the label and its value before
    # dimension extraction so a seat height cannot create a false mismatch
    # against an overall-height aspect.
    non_item_height_pattern = (
        r"(?:\b\d+(?:\.\d+)?\s*(?:(?:-\s*)?"
        r"(?:in(?:ch(?:es)?)?\.?|\"))?\s*)?"
        r"\b(?:seat|counter|bar)\s+height\b"
        r"(?:\s*[:=-]?\s*\d+(?:\.\d+)?\s*"
        r"(?:in(?:ch(?:es)?)?\.?|\"))?"
    )
    source_title_for_fact_sheet = re.sub(
        non_item_height_pattern,
        "",
        str(source_title or ""),
        flags=re.IGNORECASE,
    )
    source_description_for_fact_sheet = re.sub(
        non_item_height_pattern,
        "",
        source_description_for_fact_sheet,
        flags=re.IGNORECASE,
    )
    
    source_fingerprint = fact_sheet_content_hash(
        source_title_for_fact_sheet,
        source_description_for_fact_sheet,
        source_structured,
    )
    candidate_fingerprint = fact_sheet_content_hash(
        candidate_title,
        candidate_description,
        candidate_structured,
    )

    try:
        source_sheet = fact_sheet_for_content(
            conn,
            source_title_for_fact_sheet,
            source_description_for_fact_sheet,
            source_structured,
        )
        live_sheet = fact_sheet_for_content(
            conn,
            candidate_title,
            candidate_description,
            candidate_structured,
        )
    except Exception as e:
        print(f"FactSheet extraction failed: {e}")
        return {"status": "unavailable", "violations": [], "source_fingerprint": source_fingerprint, "candidate_fingerprint": candidate_fingerprint}

    if not source_sheet or not live_sheet:
        return {"status": "unavailable", "violations": [], "source_fingerprint": source_fingerprint, "candidate_fingerprint": candidate_fingerprint}

    _strip_fs = lambda h: re.sub(r"<[^>]+>", " ", str(h or ""))
    source_txt = " ".join([source_title_for_fact_sheet or "", _strip_fs(source_description_for_fact_sheet),
                            " ".join(f"{k}: {v}" for k, v in source_structured.items())])
    live_txt = " ".join([candidate_title or "", _strip_fs(candidate_description),
                          " ".join(f"{k}: {v}" for k, v in candidate_aspects.items())])

    violations = compare_fact_sheets(source_sheet, live_sheet, live_text=live_txt, source_text=source_txt)

    # Some supplier feeds expose a structured seat count (for example
    # Seats=4 Seat) while retaining stale copy that says 5 people. If the live
    # structured capacity agrees with the supplier's structured field, treat
    # the description discrepancy as source-internal rather than a live
    # listing hallucination. A live capacity that disagrees with the
    # structured field remains reportable.
    source_structured_capacity = _explicit_structured_capacity(source_structured)
    candidate_structured_capacity = _explicit_structured_capacity(candidate_structured)
    if (
        source_structured_capacity is not None
        and candidate_structured_capacity is not None
        and source_structured_capacity == candidate_structured_capacity
    ):
        filtered_capacity_violations = []
        for violation in violations:
            if violation["claim_type"] == "semantic_capacity":
                live_claim = str(violation.get("claim_text") or "").split("(source:", 1)[0]
                if _capacity_numbers(live_claim) == candidate_structured_capacity:
                    continue
            filtered_capacity_violations.append(violation)
        violations = filtered_capacity_violations
    
    filtered_violations = []
    for v in violations:
        if v["severity"] in ("CRITICAL", "HIGH", "MEDIUM"):
            filtered_violations.append(v)
            
    if filtered_violations:
        return {"status": "violations", "violations": filtered_violations, "source_fingerprint": source_fingerprint, "candidate_fingerprint": candidate_fingerprint}
        
    return {"status": "pass", "violations": [], "source_fingerprint": source_fingerprint, "candidate_fingerprint": candidate_fingerprint}
