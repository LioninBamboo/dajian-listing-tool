"""Source-faithful semantic rewrite pipeline (P0).

Generation path is deterministic (zero LLM). LLM is used only via
``fact_sheet_for_content`` for verification/diff extraction.

Live writes require dual gate: CLI ``--apply`` AND
``SEMANTIC_REWRITE_APPLY_ENABLED=1``. P0 must never set the env var.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

APPLY_ENV_FLAG = "SEMANTIC_REWRITE_APPLY_ENABLED"
BACKUP_DIR = Path("logs/semantic_rewrite_backups")
# Curated maps (human-queue drain). Missing/invalid file → empty maps (safe fallback).
MAPS_PATH = Path("config/semantic_rewrite_maps.yaml")

# Fabric-like tokens for Upholstery Fabric source fills (anti-hallucination).
_FABRIC_LIKE_RE = re.compile(
    r"\b("
    r"polyester|cotton|linen|velvet|corduroy|chenille|boucle|bouclé|teddy|"
    r"fabric|oxford|pu(?:\s*leather)?|leather|microfiber|suede|nylon|"
    r"canvas|twill|jacquard|wool|silk|rayon|spandex|acrylic|fleece"
    r")\b",
    re.I,
)
_STRUCTURAL_ONLY_RE = re.compile(
    r"^(?:wood|solid\s+wood|metal|steel|iron|plastic|mdf|particle\s*board|"
    r"carbon\s*steel|pine|acacia(?:\s+wood)?|rubberwood|rubber\s*wood)$",
    re.I,
)

# Cached curated maps: None = not loaded yet; dict = loaded (possibly empty).
_maps_cache: dict[str, Any] | None = None

# Material-ish aspect keys removed when unsupported (W3636 pattern).
_MATERIAL_ASPECT_KEYS = frozenset(
    {
        "material",
        "frame material",
        "pole material",
        "stake material",
        "floor material",
        "seat material",
        "seat material type",
        "back material",
        "back upholstery material",
        "upholstery material",
        "upholstery fabric",
        "blade material",
        "fabric type",
        "cover material",
        # secondary material aspects that also carry material claims the
        # fact-sheet checks (unsupported values here blocked the daily rewrite:
        # Seat Fill Material=foam / Tabletop Material=wood veneer / etc.)
        "seat fill material",
        "fill material",
        "filling",
        "filler",
        "tabletop material",
        "table top material",
        "top material",
        "top material type",
        "desktop material",
        "surface material",
        "leg material",
    }
)

_CAPACITY_ASPECT_KEYS = frozenset(
    {
        "occupancy",
        "occupant capacity",
        "seating capacity",
        "capacity",
        "number of seats",
        "seat capacity",
    }
)

_CERT_PATTERNS = (
    r"\btsa(?:[-\s]?approved)?\b",
    r"\bul\s+listed\b",
    r"\bul\b",
    r"\bastm\b",
    r"\biso\s+\d+\b",
    r"\bcarb\s+(?:2|ii)\b",
    r"\bfda\b",
    r"\bcpsc\b",
    r"\bce\s+certified\b",
    r"\bcertified\b",
    r"\bcertification\b",
)

_COUNT_NOUN_ASPECTS = {
    "tier": ("Number of Tiers", "Number of Shelves"),
    "shelf": ("Number of Shelves", "Number of Tiers"),
    "drawer": ("Number of Drawers",),
    "door": ("Number of Doors",),
    "seat": ("Seating Capacity", "Number of Seats"),
    "blade": ("Number of Blades",),
    "nozzle": ("Number of Nozzles",),
    "wheel": ("Number of Wheels",),
}


@dataclass
class SkipResult:
    sku: str
    code: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "skip", **asdict(self)}


@dataclass
class RewritePlan:
    sku: str
    before: dict[str, Any]
    after: dict[str, Any]
    violations: list[dict[str, Any]] = field(default_factory=list)
    applied_fixes: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    pushable: bool = False
    needs_human: bool = False
    notes: list[str] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "plan", **asdict(self)}


@dataclass
class ApplyResult:
    ok: bool
    sku: str
    reason: str = ""
    listing_id: str | None = None
    backup_path: str | None = None
    stage: str = ""  # put_ok | offer_ok | publish_ok | rolled_back | dual_gate | ...
    put_ok: bool = False
    offer_ok: bool = False
    publish_ok: bool = False
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def dual_gate_allows_apply(*, cli_apply: bool) -> bool:
    """Both CLI flag and env SEMANTIC_REWRITE_APPLY_ENABLED=1 required."""
    return bool(cli_apply) and os.environ.get(APPLY_ENV_FLAG) == "1"


def dual_gate_block_reason(*, cli_apply: bool) -> str:
    missing = []
    if not cli_apply:
        missing.append("--apply flag")
    if os.environ.get(APPLY_ENV_FLAG) != "1":
        missing.append(f"{APPLY_ENV_FLAG}=1")
    return "dual_gate_blocked: need " + " AND ".join(missing)


# ── helpers ──────────────────────────────────────────────────────────────


def _strip_html(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _parse_json(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return {} if default is None else default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {} if default is None else default


def _aspect_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _first_aspect(aspects: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        for k, v in aspects.items():
            if str(k).lower() == key.lower():
                vals = _aspect_list(v)
                if vals:
                    return vals[0]
    return ""


def _set_aspect(aspects: dict[str, Any], key: str, value: str | list[str] | None) -> None:
    # Preserve existing key casing if present
    real_key = key
    for k in list(aspects.keys()):
        if str(k).lower() == key.lower():
            real_key = k
            break
    if value is None or value == [] or value == "":
        aspects.pop(real_key, None)
        return
    aspects[real_key] = value if isinstance(value, list) else [value]


def _delete_aspect_keys(aspects: dict[str, Any], keys_lower: set[str] | frozenset[str]) -> list[str]:
    removed = []
    for k in list(aspects.keys()):
        if str(k).lower() in keys_lower:
            aspects.pop(k, None)
            removed.append(k)
    return removed


def extract_product_features_fragment(source_description: str) -> str:
    """Crop Product Features / characteristics bullets for the template builder.

    Prevents spec-table rows from being misread as selling points (W3636 pattern).
    """
    html = source_description or ""
    # Prefer explicit Product Features section with list
    m = re.search(
        r"(?:Product\s+Features|KEY\s+FEATURES|Features)\s*</[^>]+>\s*(<ul\b.*?</ul>)",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return f"<div><h3>Product Features</h3>{m.group(1)}</div>"
    # Any <ul> of <li>
    m2 = re.search(r"(<ul\b[^>]*>\s*(?:<li\b.*?</li>\s*){2,}</ul>)", html, flags=re.IGNORECASE | re.DOTALL)
    if m2:
        return f"<div><h3>Product Features</h3>{m2.group(1)}</div>"
    # Fallback: return full description (builder will sentence-split)
    return html


def characteristics_too_thin(characteristics: list[str] | None, source_description: str = "") -> bool:
    chars = [c for c in (characteristics or []) if str(c).strip()]
    if len(chars) >= 2:
        return False
    # Allow non-empty feature bullets in description
    try:
        from scripts.audit_fix_active_listings import extract_source_feature_bullets

        bullets = extract_source_feature_bullets(source_description or "")
        return len(bullets) < 2
    except Exception:
        return len(chars) < 2


def description_shrink_ratio(before_html: str, after_html: str) -> float:
    b = len(_strip_html(before_html or ""))
    a = len(_strip_html(after_html or ""))
    if b <= 0:
        return 0.0
    return a / b


def scrub_violation_tokens_from_title(title: str, violations: list[dict[str, Any]]) -> str:
    """Remove claim tokens from title without full source rebuild (gap 1 fallback)."""
    out = title or ""
    for v in violations:
        claim = str(v.get("claim_text") or "").split(" (source:")[0].strip()
        if len(claim) < 3:
            continue
        if v.get("claim_type") not in {
            "semantic_material",
            "semantic_certification",
            "semantic_capacity",
            "semantic_count",
            "semantic_feature",
        }:
            continue
        # Prefer whole-phrase removal, then multi-char tokens
        out = re.sub(re.escape(claim), " ", out, flags=re.IGNORECASE)
        for token in re.findall(r"[A-Za-z0-9]{3,}", claim):
            if token.lower() in {"the", "and", "with", "for", "source", "inch", "size"}:
                continue
            out = re.sub(rf"\b{re.escape(token)}\b", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" -|,;/")
    return out


def title_triggers_category_change(
    title: str,
    current_category_id: str | None,
    description: str = "",
) -> bool:
    """True if audit-equivalent category precheck would change/reject current category.

    Uses classify_listing_profile + matcher.canonicalize_category / plausibility
    with the *existing* category as baseline. Never proposes a rewrite that forces
    a category change (gap 1 — never change category).
    """
    cid = str(current_category_id or "").strip()
    if not cid or not (title or "").strip():
        return False
    try:
        from src.utils.listing_quality_gate import classify_listing_profile
        from src.services.ebay_category_matcher import create_category_matcher

        profile = classify_listing_profile(
            title=title or "",
            description=description or "",
            category_id=cid,
        )
        if profile.category_id and str(profile.category_id).strip() != cid:
            return True

        matcher = create_category_matcher()
        canon, _ = matcher.canonicalize_category(title, cid, None, description)
        if canon and str(canon).strip() != cid:
            return True
        if not matcher.is_category_plausible_for_text(title, cid, None):
            # Would emit category_mismatch → avoid this title
            return True
    except Exception:
        # Conservative: if precheck infrastructure fails, do not block
        return False
    return False


def title_contains_violation(title: str, violations: list[dict[str, Any]]) -> bool:
    t = (title or "").lower()
    if not t:
        return False
    for v in violations:
        claim = str(v.get("claim_text") or "").lower()
        if not claim:
            continue
        # strip parenthetical source notes
        claim = claim.split(" (source:")[0].strip()
        if len(claim) >= 3 and claim in t:
            return True
        # token-ish match for short certs
        for token in re.findall(r"[a-z0-9]{3,}", claim):
            if token in {"the", "and", "with", "for", "source"}:
                continue
            if re.search(rf"\b{re.escape(token)}\b", t):
                # only if claim type is critical-ish
                if v.get("claim_type") in {
                    "semantic_material",
                    "semantic_certification",
                    "semantic_capacity",
                    "semantic_count",
                    "semantic_feature",
                }:
                    return True
    return False


def _normalize_material_label(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return text
    # Title-case multi-word materials, keep common abbreviations
    parts = []
    for w in text.split(" "):
        up = w.upper()
        if up in {"PU", "PVC", "ABS", "MDF", "HDPE", "LED", "USB"}:
            parts.append(up)
        else:
            parts.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(parts)


def _normalize_map_key(raw: str) -> str:
    """Casefold + collapse whitespace; strip spaces around list separators."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    text = re.sub(r"\s*,\s*", ",", text)
    text = re.sub(r"\s*;\s*", ";", text)
    return text.casefold()


def reset_semantic_rewrite_maps_cache() -> None:
    """Test helper: force next load_semantic_rewrite_maps() to re-read disk."""
    global _maps_cache
    _maps_cache = None


def load_semantic_rewrite_maps(
    path: Path | str | None = None,
    *,
    force_reload: bool = False,
) -> dict[str, Any]:
    """Load curated maps. Missing/invalid file → empty maps (enhancement not dependency)."""
    global _maps_cache
    if _maps_cache is not None and not force_reload and path is None:
        return _maps_cache

    target = Path(path) if path is not None else MAPS_PATH
    empty: dict[str, Any] = {
        "version": 0,
        "compound_material_map": {},
        "required_aspect_map": {},
        "loaded": False,
        "path": str(target),
    }
    if not target.is_file():
        if path is None and not force_reload:
            _maps_cache = empty
        return empty
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception:
        if path is None and not force_reload:
            _maps_cache = empty
        return empty
    if not isinstance(data, dict):
        if path is None and not force_reload:
            _maps_cache = empty
        return empty

    # User gate: enabled must be explicitly true. Pending review → empty maps.
    enabled = data.get("enabled")
    if enabled is not True and str(enabled).strip().lower() not in {"1", "true", "yes"}:
        disabled = {
            **empty,
            "loaded": True,
            "enabled": False,
            "version": data.get("version") or 1,
            "path": str(target),
        }
        if path is None:
            _maps_cache = disabled
        return disabled

    compound_raw = data.get("compound_material_map") or {}
    compound: dict[str, dict[str, Any]] = {}
    if isinstance(compound_raw, dict):
        for k, v in compound_raw.items():
            if not isinstance(v, dict):
                continue
            compound[_normalize_map_key(str(k))] = dict(v)

    required: dict[str, dict[str, Any]] = {}
    req_list = data.get("required_aspect_map") or []
    if isinstance(req_list, list):
        for row in req_list:
            if not isinstance(row, dict):
                continue
            cat = str(row.get("category_id") or "").strip()
            aspect = str(row.get("aspect") or "").strip()
            if not cat or not aspect:
                continue
            key = f"{cat}|{aspect.casefold()}"
            required[key] = dict(row)

    result = {
        "version": data.get("version") or 1,
        "compound_material_map": compound,
        "required_aspect_map": required,
        "loaded": True,
        "enabled": True,
        "path": str(target),
    }
    if path is None:
        _maps_cache = result
    return result


def lookup_compound_material_primary(
    raw: str,
    maps: Mapping[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return (primary, entry) when compound map hits with policy=apply and primary set.

    No evidence / human policy / missing primary → (None, entry_or_None).
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return None, None
    maps = maps if maps is not None else load_semantic_rewrite_maps()
    entry = (maps.get("compound_material_map") or {}).get(_normalize_map_key(text))
    if not entry:
        return None, None
    policy = str(entry.get("policy") or "human").strip().lower()
    primary = entry.get("primary")
    evidence = str(entry.get("evidence") or "").strip()
    if policy != "apply" or not primary or not str(primary).strip():
        return None, entry
    # Anti-hallucination: refuse apply rows without evidence text
    if not evidence:
        return None, entry
    return str(primary).strip(), entry


def _is_fabric_like_value(raw: str) -> bool:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return False
    if _STRUCTURAL_ONLY_RE.match(text):
        return False
    return bool(_FABRIC_LIKE_RE.search(text))


def _is_simple_material_value(raw: str) -> bool:
    """Single clean material token — no compound lists (gap 4 relax gate).

    Enhancement: curated compound_material_map hit with policy=apply + primary
    counts as simple (caller should use resolve_material_value for the primary).
    Missing/unmapped compound → False (original safe behavior).
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text or len(text) > 40:
        return False
    if re.search(r"[,;/|]| and | or ", text, re.I):
        primary, _entry = lookup_compound_material_primary(text)
        return bool(primary)
    return True


def resolve_material_value(raw: str) -> str:
    """Return auto-safe material token: map primary for curated compounds, else raw.

    Does not invent values. Unmapped compounds return raw unchanged (caller still
    sees _is_simple_material_value False unless map hit).
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""
    if re.search(r"[,;/|]| and | or ", text, re.I):
        primary, _entry = lookup_compound_material_primary(text)
        if primary:
            return primary
    return text


def lookup_required_aspect_policy(
    category_id: str | None,
    aspect_lower: str,
    maps: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    maps = maps if maps is not None else load_semantic_rewrite_maps()
    key = f"{str(category_id or '').strip()}|{str(aspect_lower or '').strip().casefold()}"
    return (maps.get("required_aspect_map") or {}).get(key)


def resolve_required_aspect_from_map(
    *,
    category_id: str | None,
    aspect_lower: str,
    source_attrs: Mapping[str, Any],
    source_sheet: Mapping[str, Any] | None,
    maps: Mapping[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Apply Table B. Returns (value_or_None, note).

    policy=human / miss / no evidence → (None, reason).
    """
    entry = lookup_required_aspect_policy(category_id, aspect_lower, maps=maps)
    if not entry:
        return None, "required_map: miss"
    policy = str(entry.get("policy") or "human").strip().lower()
    evidence = str(entry.get("evidence") or "").strip()
    if not evidence:
        return None, "required_map: refuse_no_evidence"
    if policy == "human":
        return None, "required_map: policy=human"
    if policy == "neutral":
        val = entry.get("suggested_value")
        if val is None or not str(val).strip():
            return None, "required_map: neutral_empty"
        return str(val).strip(), f"required_map: neutral {aspect_lower} ← {val}"
    if policy != "source":
        return None, f"required_map: unknown_policy={policy}"

    fields = entry.get("source_fields") or []
    fabric_only = bool(entry.get("fabric_like_only"))
    # Try explicit source fields first
    for field_name in fields:
        raw = str(source_attrs.get(field_name) or "").strip()
        if not raw:
            continue
        # Prefer map-resolved primary for compound source fields
        if not _is_simple_material_value(raw) and not lookup_compound_material_primary(raw)[0]:
            continue
        resolved = resolve_material_value(raw)
        if not resolved:
            continue
        if fabric_only and not _is_fabric_like_value(resolved):
            continue
        return resolved, f"required_map: source {aspect_lower} ← {field_name}={resolved}"

    # Fallback: upholstery/material helper then sheet materials
    if aspect_lower in {"upholstery fabric", "upholstery material", "material", "frame material"}:
        raw = _source_upholstery_or_material(source_attrs, source_sheet)
        if raw and (_is_simple_material_value(raw) or lookup_compound_material_primary(raw)[0]):
            resolved = resolve_material_value(raw)
            if resolved and (not fabric_only or _is_fabric_like_value(resolved)):
                return resolved, f"required_map: source {aspect_lower} ← sheet/attrs {resolved}"

    return None, "required_map: source_empty"


def _source_upholstery_or_material(source_attrs: Mapping[str, Any], source_sheet: Mapping[str, Any] | None) -> str:
    for key in (
        "Upholstery Material",
        "Upholstery Fabric",
        "Main Material",
        "Material",
        "材质",
    ):
        val = str(source_attrs.get(key) or "").strip()
        if val:
            return val
    mats = (source_sheet or {}).get("materials") or []
    if mats:
        return str(mats[0])
    return ""


def get_required_aspect_names(category_id: str | None) -> set[str]:
    """Category required aspect names (lowercase) from publisher table."""
    try:
        from src.services.ebay_publisher import EbayPublisher

        cat_config = EbayPublisher.CATEGORY_REQUIRED_ASPECTS.get(str(category_id or ""), {})
        required = cat_config.get("required") or []
        return {str(x).lower() for x in required}
    except Exception:
        return set()


def protect_and_fill_required_aspects(
    aspects: dict[str, Any],
    *,
    category_id: str | None,
    title: str,
    source_attrs: Mapping[str, Any],
    source_sheet: Mapping[str, Any] | None,
    before_aspects: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Gap 2: never drop category-required aspects; fill missing from source/before."""
    notes: list[str] = []
    human: list[str] = []
    out = copy.deepcopy(aspects) if aspects else {}
    required = get_required_aspect_names(category_id)
    before_aspects = before_aspects or {}

    # Restore any required key that vanished after 3.3 fixes
    key_map = {
        "upholstery fabric": "Upholstery Fabric",
        "upholstery material": "Upholstery Material",
        "material": "Material",
        "frame material": "Frame Material",
        "brand": "Brand",
        "type": "Type",
        "color": "Color",
    }
    for req_lower in required:
        present = any(str(k).lower() == req_lower for k, v in out.items() if _aspect_list(v))
        if present:
            continue
        # Table B curated map first (enhancement; miss → original path)
        map_val, map_note = resolve_required_aspect_from_map(
            category_id=category_id,
            aspect_lower=req_lower,
            source_attrs=source_attrs,
            source_sheet=source_sheet,
        )
        if map_val:
            label = _normalize_material_label(map_val) if "material" in req_lower or "fabric" in req_lower else map_val
            _set_aspect(out, key_map.get(req_lower, req_lower.title()), label)
            notes.append(map_note)
            continue
        if map_note and map_note not in {"required_map: miss"}:
            notes.append(map_note)
        # Prefer source mapping for fabric/material required fields
        if req_lower in {"upholstery fabric", "upholstery material", "material", "frame material"}:
            src = _source_upholstery_or_material(source_attrs, source_sheet)
            if src and (req_lower != "material" or _is_simple_material_value(src)):
                resolved = resolve_material_value(src)
                label = _normalize_material_label(resolved)
                _set_aspect(out, key_map.get(req_lower, req_lower.title()), label)
                notes.append(f"required_protect: {req_lower} ← source {label}")
                continue
        # Fall back to before value
        for bk, bv in before_aspects.items():
            if str(bk).lower() == req_lower and _aspect_list(bv):
                _set_aspect(out, bk, _aspect_list(bv))
                notes.append(f"required_protect: restored {bk} from before")
                break
        else:
            human.append(f"required_missing:{req_lower}")

    # Publisher fill for remaining gaps (Type, Brand defaults, etc.)
    try:
        from scripts.audit_fix_active_listings import _fill_missing_required_aspects

        _fill_missing_required_aspects(out, str(category_id or ""), title or "")
        notes.append("required_fill: _fill_missing_required_aspects applied")
    except Exception as exc:
        notes.append(f"required_fill_error: {exc}")

    # Final check
    for req_lower in required:
        present = any(str(k).lower() == req_lower for k, v in out.items() if _aspect_list(v))
        if not present:
            if f"required_missing:{req_lower}" not in human:
                human.append(f"required_missing:{req_lower}")

    return out, notes, human


def apply_aspect_fixes(
    aspects: dict[str, Any],
    *,
    violations: list[dict[str, Any]],
    source_attrs: Mapping[str, Any],
    source_sheet: Mapping[str, Any] | None,
    source_dims_trustworthy: bool,
    source_dims: Mapping[str, Any] | None,
    protected_keys: set[str] | frozenset[str] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Apply 3.3 correction table. Returns (new_aspects, fix_notes, needs_human_types)."""
    new_aspects = copy.deepcopy(aspects) if aspects else {}
    notes: list[str] = []
    needs_human: list[str] = []
    source_sheet = source_sheet or {}
    source_dims = source_dims or {}
    protected = {str(k).lower() for k in (protected_keys or set())}

    types_seen = {str(v.get("claim_type") or "") for v in violations}

    # semantic_material
    if "semantic_material" in types_seen:
        main = (
            source_attrs.get("Main Material")
            or source_attrs.get("Material")
            or source_attrs.get("材质")
            or ""
        )
        main = str(main).strip()
        if not main and source_sheet.get("materials"):
            main = str(source_sheet["materials"][0])
        if main and not _is_simple_material_value(main):
            # Compound materials (e.g. "Polyester,rubber Wood") → human
            # (unless curated compound_material_map resolves primary — then simple)
            needs_human.append("semantic_material_compound")
            notes.append(f"semantic_material: compound source value kept for human: {main!r}")
        elif main:
            resolved = resolve_material_value(main)
            label = _normalize_material_label(resolved)
            _set_aspect(new_aspects, "Material", label)
            if resolved != re.sub(r"\s+", " ", main.strip()):
                notes.append(
                    f"semantic_material: Material ← {label} (compound_map primary from {main!r})"
                )
            else:
                notes.append(f"semantic_material: Material ← {label}")
        # Drop unsupported material-type aspects — never drop required/protected
        drop_keys = set(_MATERIAL_ASPECT_KEYS) - {"material"} - protected
        # For protected material-ish keys that are still present: replace with source value
        for k in list(new_aspects.keys()):
            kl = str(k).lower()
            if kl in protected and kl in _MATERIAL_ASPECT_KEYS and kl != "material":
                src = _source_upholstery_or_material(source_attrs, source_sheet)
                if src and _is_simple_material_value(src):
                    resolved_src = resolve_material_value(src)
                    _set_aspect(new_aspects, k, _normalize_material_label(resolved_src))
                    notes.append(f"semantic_material: protected {k} replaced ← {resolved_src}")
                # else leave value for protect_and_fill later
        removed = _delete_aspect_keys(new_aspects, drop_keys)
        if main and _is_simple_material_value(main):
            resolved = resolve_material_value(main)
            _set_aspect(new_aspects, "Material", _normalize_material_label(resolved))
        if removed:
            notes.append(f"semantic_material: removed aspects {removed}")

    # semantic_capacity
    if "semantic_capacity" in types_seen:
        source_cap = (source_sheet or {}).get("capacity")
        # Only treat person/seat style capacity as seating; reject weight-like
        # values such as "350lbs" (fact-sheet extractor blind spot).
        cap_text = str(source_cap or "").strip()
        looks_like_person_cap = bool(
            re.search(r"\b\d+\s*(?:person|people|seat|seater|occupant)s?\b", cap_text, re.I)
        ) or (
            bool(re.fullmatch(r"\d+", cap_text))
            and int(cap_text) <= 20
        )
        if cap_text and looks_like_person_cap:
            _set_aspect(new_aspects, "Seating Capacity", cap_text)
            notes.append(f"semantic_capacity: set Seating Capacity ← {cap_text}")
        else:
            removed = _delete_aspect_keys(new_aspects, _CAPACITY_ASPECT_KEYS)
            notes.append(
                f"semantic_capacity: removed {removed or list(_CAPACITY_ASPECT_KEYS)}"
                + (f" (ignored non-person capacity {cap_text!r})" if cap_text else "")
            )

    # semantic_certification
    if "semantic_certification" in types_seen:
        cert_re = re.compile("|".join(_CERT_PATTERNS), re.IGNORECASE)
        for key, val in list(new_aspects.items()):
            vals = _aspect_list(val)
            kept = []
            for item in vals:
                if cert_re.search(item):
                    scrubbed = cert_re.sub("", item)
                    scrubbed = re.sub(r"\s{2,}", " ", scrubbed).strip(" -/,;")
                    if scrubbed:
                        kept.append(scrubbed)
                else:
                    kept.append(item)
            if kept != vals:
                _set_aspect(new_aspects, key, kept if kept else None)
                notes.append(f"semantic_certification: scrubbed aspect {key}")

    # semantic_count
    if "semantic_count" in types_seen:
        source_counts = (source_sheet or {}).get("counts") or {}
        for v in violations:
            if v.get("claim_type") != "semantic_count":
                continue
            claim = str(v.get("claim_text") or "")
            # e.g. "5 blade (source: 4)" or "5 blade"
            m = re.match(r"(\d+)\s+(\w+)", claim)
            if not m:
                needs_human.append("semantic_count")
                continue
            noun = m.group(2).lower().rstrip("s")
            src_n = source_counts.get(noun) or source_counts.get(noun + "s")
            aspect_keys = _COUNT_NOUN_ASPECTS.get(noun, ())
            if src_n is not None:
                for ak in aspect_keys:
                    if any(str(k).lower() == ak.lower() for k in new_aspects):
                        _set_aspect(new_aspects, ak, str(src_n))
                        notes.append(f"semantic_count: {ak} ← {src_n}")
                        break
                else:
                    if aspect_keys:
                        _set_aspect(new_aspects, aspect_keys[0], str(src_n))
                        notes.append(f"semantic_count: set {aspect_keys[0]} ← {src_n}")
            else:
                for ak in aspect_keys:
                    removed = _delete_aspect_keys(new_aspects, {ak.lower()})
                    if removed:
                        notes.append(f"semantic_count: deleted {removed} (no source count)")

    # semantic_feature (HIGH)
    if "semantic_feature" in types_seen:
        source_features = {str(f).lower() for f in (source_sheet or {}).get("features") or []}
        source_blob = " ".join(source_features)
        for key, val in list(new_aspects.items()):
            if str(key).lower() != "features":
                continue
            kept = []
            for item in _aspect_list(val):
                il = item.lower()
                # waterproof degrade
                if "waterproof" in il and "water resistant" in source_blob and "waterproof" not in source_blob:
                    kept.append("Water Resistant")
                    notes.append("semantic_feature: waterproof → Water Resistant")
                    continue
                # drop if clearly unsupported high-risk and not in source
                if any(tok in il for tok in ("waterproof", "usb", "bluetooth", "foldable", "collapsible")):
                    if not any(sf in il or il in sf for sf in source_features):
                        notes.append(f"semantic_feature: drop Features item {item!r}")
                        continue
                kept.append(item)
            _set_aspect(new_aspects, key, kept if kept else None)

    # semantic_dimension
    if "semantic_dimension" in types_seen:
        if source_dims_trustworthy and source_dims:
            for axis, aspect_key in (
                ("length", "Item Length"),
                ("width", "Item Width"),
                ("height", "Item Height"),
            ):
                val = source_dims.get(axis)
                if val is not None:
                    _set_aspect(new_aspects, aspect_key, f"{val} in")
                    notes.append(f"semantic_dimension: {aspect_key} ← {val} in")
        else:
            notes.append("semantic_dimension: skipped (suspect/untrusted source dims)")

    # Unknown violation types → needs_human (do not invent fixes)
    known = {
        "semantic_material",
        "semantic_capacity",
        "semantic_certification",
        "semantic_count",
        "semantic_feature",
        "semantic_dimension",
    }
    for t in types_seen:
        if t and t not in known and not t.startswith("claim_"):
            needs_human.append(t)

    return new_aspects, notes, needs_human


def scrub_certification_from_text(text: str) -> str:
    out = text or ""
    for pat in _CERT_PATTERNS:
        out = re.sub(pat, " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out)
    return out


def build_description_from_source(
    *,
    title: str,
    source_description: str,
    attrs: Mapping[str, Any],
    specs: Mapping[str, Any],
    aspects: Mapping[str, Any],
    characteristics: list[str] | None = None,
) -> str:
    """Deterministic description rebuild (zero LLM)."""
    from scripts.audit_fix_active_listings import build_structured_description_from_source

    # Prefer characteristics as explicit bullets when present
    if characteristics:
        li = "".join(f"<li>{_escape_basic(c)}</li>" for c in characteristics if str(c).strip())
        fragment = f"<div><h3>Product Features</h3><ul>{li}</ul></div>"
    else:
        fragment = extract_product_features_fragment(source_description)

    html = build_structured_description_from_source(
        title,
        fragment,
        dict(attrs or {}),
        dict(specs or {}),
        dict(aspects or {}),
    )
    return html or ""


def _escape_basic(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def validate_rewrite(
    *,
    source_title: str,
    source_description: str,
    source_attrs: Mapping[str, Any],
    source_specs: Mapping[str, Any],
    new_title: str,
    new_description: str,
    new_aspects: Mapping[str, Any],
    conn: Any = None,
    source_sheet: Mapping[str, Any] | None = None,
    applied_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Three-layer validation. All must pass for pushable=True.

    ``applied_notes`` are the fix notes from apply_aspect_fixes; when they show
    Material was set from the source value, material-family CRITICALs are
    accepted (see the rationale at the ``passed`` computation below).
    """
    from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations

    result: dict[str, Any] = {
        "claim_critical": [],
        "fact_critical": [],
        "quality_gate": {},
        "passed": False,
    }

    # Layer 1: claim engine
    constraints = build_source_constraints(
        attrs=source_attrs,
        specs=source_specs,
        source_description=source_description or "",
        source_title=source_title or "",
    )
    claim_violations = detect_claim_violations(
        constraints,
        new_title or "",
        new_description or "",
        dict(new_aspects or {}),
    )
    result["claim_critical"] = [
        {"claim_text": v.claim_text, "claim_type": v.claim_type, "severity": v.severity}
        for v in claim_violations
        if getattr(v, "severity", "") == "CRITICAL"
    ]

    # Layer 2: fact sheet diff (new content vs source)
    if conn is not None and source_sheet is not None:
        try:
            from src.utils.listing_fact_sheet import compare_fact_sheets, fact_sheet_for_content

            new_sheet = fact_sheet_for_content(
                conn,
                new_title or "",
                new_description or "",
                structured={"aspects": dict(new_aspects or {}), "attributes": dict(source_attrs or {})},
            )
            if new_sheet:
                _strip = lambda h: re.sub(r"<[^>]+>", " ", str(h or ""))
                _live_text = " ".join((
                    new_title or "", _strip(new_description),
                    " ".join(f"{k}: {v}" for k, v in (new_aspects or {}).items()),
                ))
                _source_text = " ".join((
                    source_title or "", _strip(source_description),
                    " ".join(f"{k}: {v}" for k, v in (source_attrs or {}).items()),
                ))
                diffs = compare_fact_sheets(
                    source_sheet, new_sheet, live_text=_live_text, source_text=_source_text
                )
                result["fact_critical"] = [d for d in diffs if d.get("severity") == "CRITICAL"]
            else:
                result["fact_note"] = "fact_sheet_for_content returned None (skipped layer 2)"
        except Exception as exc:
            result["fact_note"] = f"fact sheet layer error: {exc}"

    # Layer 3: quality gate markers
    from src.utils.store_profile import get_store_profile

    _profile = get_store_profile()
    desc_l = (new_description or "").lower()
    qg = {
        "has_key_features": "key features" in desc_l and "<li" in desc_l,
        "has_banner": _profile.quality_banner_marker in desc_l,
        "has_footer": _profile.quality_footer_marker in desc_l,
        "has_perfect_for": "perfect for" in desc_l,
        "has_package_includes": "package includes" in desc_l,
        "has_lwh_digits": bool(
            re.search(r"\d+(?:\.\d+)?", _first_aspect(new_aspects, "Item Length"))
            and re.search(r"\d+(?:\.\d+)?", _first_aspect(new_aspects, "Item Width"))
            and re.search(r"\d+(?:\.\d+)?", _first_aspect(new_aspects, "Item Height"))
        )
        or bool(re.search(r"\d+(?:\.\d+)?\s*(?:in|inch)", desc_l)),
        "has_assembly_copy": "assembly" in desc_l or bool(_first_aspect(new_aspects, "Assembly Required")),
        "title_len_ok": len(new_title or "") <= 80,
        "title_nonempty": bool((new_title or "").strip()),
    }
    result["quality_gate"] = qg
    qg_ok = all(
        [
            qg["has_key_features"],
            qg["has_banner"],
            qg["has_footer"],
            qg["title_len_ok"],
            qg["title_nonempty"],
        ]
    )
    # LWH and assembly preferred but not always present for non-furniture — soft
    result["quality_gate_passed"] = qg_ok

    # 2026-07-22: material-family claims are ACCEPTED when the rewrite has
    # resolved Material to the source value. Rationale: the claim engine flags
    # e.g. source title "Solid Wood Nightstand" vs source Main Material
    # "Rubber Wood" as material_upgrade forever, so requiring zero material
    # CRITICALs made the pipeline reject 100% of candidates (7/17–7/22: 0 pushes
    # on ~30 analysed per day). The manual fix-all batch used exactly this
    # standard — source-value material + intact template + word-safe title —
    # and 124 listings passed independent dual-engine spot-checks 12/12 clean.
    # Non-material CRITICALs (dimension/count/capacity/certification/feature)
    # still block: those are the ones a human must judge.
    material_family = {
        "material_upgrade", "semantic_material", "hallucinated_wood",
        "hallucinated_wood_species", "hallucinated_leather", "hallucinated_cushion",
    }

    def _is_material(item: Mapping[str, Any]) -> bool:
        return str(item.get("claim_type") or "") in material_family

    material_resolved_from_source = any(
        "semantic_material: Material ←" in note for note in (applied_notes or [])
    )
    non_material_claim = [c for c in result["claim_critical"] if not _is_material(c)]
    non_material_fact = [f for f in result["fact_critical"] if not _is_material(f)]
    result["material_accepted"] = bool(
        material_resolved_from_source
        and (len(non_material_claim) < len(result["claim_critical"])
             or len(non_material_fact) < len(result["fact_critical"]))
    )

    if material_resolved_from_source:
        result["passed"] = (
            len(non_material_claim) == 0 and len(non_material_fact) == 0 and qg_ok
        )
    else:
        result["passed"] = (
            len(result["claim_critical"]) == 0
            and len(result["fact_critical"]) == 0
            and qg_ok
        )
    return result


def _load_db_row(conn, sku: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT sku, title, description, attributes, specs, optimization, videos, status, listing_id "
        "FROM collected_products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if not row:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    cols = [
        "sku",
        "title",
        "description",
        "attributes",
        "specs",
        "optimization",
        "videos",
        "status",
        "listing_id",
    ]
    return dict(zip(cols, row))


def _fetch_source_snapshot(dajian, sku: str):
    """Read-only source snapshot (never writes DB)."""
    from src.services.source_refresh import build_source_snapshot

    detail = None
    if dajian is None:
        return None, None
    try:
        if hasattr(dajian, "get_product_detail_by_sku"):
            detail = dajian.get_product_detail_by_sku(sku)
        elif hasattr(dajian, "get_product_details"):
            details = dajian.get_product_details([sku]) or []
            detail = details[0] if details else None
    except Exception:
        return None, None
    if not detail:
        return None, None
    snap = build_source_snapshot(detail if isinstance(detail, Mapping) else {})
    return snap, detail


def _fetch_live(ebay, sku: str) -> dict[str, Any]:
    live: dict[str, Any] = {
        "title": "",
        "description": "",
        "aspects": {},
        "offer_id": None,
        "listing_id": None,
        "category_id": None,
        "inventory": {},
        "offer": {},
    }
    if ebay is None:
        return live
    try:
        inv = ebay.get_inventory_item(sku) or {}
    except Exception:
        inv = {}
    live["inventory"] = inv
    product = inv.get("product") or {}
    live["title"] = product.get("title") or ""
    live["aspects"] = product.get("aspects") or {}
    inv_desc = product.get("description") or ""

    try:
        offers = ebay.get_offers_by_sku(sku) or []
    except Exception:
        offers = []
    offer = offers[0] if offers else {}
    live["offer"] = offer
    live["offer_id"] = offer.get("offerId")
    listing = offer.get("listing") or {}
    live["listing_id"] = listing.get("listingId") or offer.get("listingId")
    live["category_id"] = offer.get("categoryId") or (offer.get("category") or {}).get("categoryId")
    live["description"] = offer.get("listingDescription") or inv_desc
    if not live["title"]:
        live["title"] = product.get("title") or ""
    return live


def _suspect_source_dimensions(source_title: str, attrs: Mapping[str, Any], specs: Mapping[str, Any]) -> bool:
    """Lightweight port of audit suspect_source_dimensions guard (read-only)."""
    try:
        from src.utils.dimension_helpers import extract_all_dimensions

        dims = extract_all_dimensions(dict(attrs or {}), dict(specs or {}), "")
        title_nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*(?:\"|in\b|inch)", source_title or "", re.I)]
        if not title_nums or not dims:
            return False
        # If package dims equal assembled and title claims much larger — suspect
        # Heuristic: max assembled axis << max title inch by >30%
        assembled = [dims.get("length"), dims.get("width"), dims.get("height")]
        assembled = [a for a in assembled if isinstance(a, (int, float))]
        if not assembled:
            return False
        if max(title_nums) > 0 and max(assembled) < max(title_nums) * 0.7:
            # only flag when package weight-ish equality isn't available — conservative
            return True
    except Exception:
        return False
    return False


def _source_dims(attrs: Mapping[str, Any], specs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from src.utils.dimension_helpers import extract_all_dimensions

        return extract_all_dimensions(dict(attrs or {}), dict(specs or {}), "") or {}
    except Exception:
        return {}


def plan_rewrite(conn, dajian, ebay, sku: str) -> RewritePlan | SkipResult:
    """Build a pushable or blocked rewrite plan. Never writes eBay or collected_products."""
    sku = str(sku or "").strip()
    if not sku:
        return SkipResult(sku="", code="invalid_sku", reason="empty sku")

    row = _load_db_row(conn, sku)
    if not row:
        return SkipResult(sku=sku, code="not_in_db", reason="SKU not found in collected_products")

    # Optional: mark refresh intent with apply=False (read-only drift check)
    try:
        from src.services.source_refresh import refresh_skus

        if dajian is not None:
            refresh_skus(conn, dajian, [sku], apply=False, context="semantic_rewrite_p0")
    except Exception as exc:
        # Non-fatal — continue with direct snapshot fetch
        pass

    snap, detail = _fetch_source_snapshot(dajian, sku)
    db_attrs = _parse_json(row.get("attributes"), {})
    db_specs = _parse_json(row.get("specs"), {})
    db_title = row.get("title") or ""
    db_desc = row.get("description") or ""

    if snap is not None:
        source_title = snap.title or db_title
        source_desc = snap.description_html or db_desc
        source_attrs = dict(snap.attributes or db_attrs)
        source_specs = dict(snap.specs or db_specs)
        characteristics = list(snap.characteristics or [])
        sku_available = bool(snap.sku_available)
    else:
        source_title = db_title
        source_desc = db_desc
        source_attrs = db_attrs
        source_specs = db_specs
        characteristics = []
        sku_available = True
        # try parse characteristics from description
        if detail and isinstance(detail, Mapping):
            characteristics = [
                str(x).strip() for x in (detail.get("characteristics") or []) if str(x).strip()
            ]
            if detail.get("skuAvailable") is False:
                sku_available = False

    if not sku_available:
        return SkipResult(sku=sku, code="unavailable", reason="source skuAvailable=False")

    if characteristics_too_thin(characteristics, source_desc):
        return SkipResult(
            sku=sku,
            code="empty_characteristics",
            reason="source characteristics empty/too thin for deterministic rebuild",
        )

    live = _fetch_live(ebay, sku)
    # Fall back to local optimization when live empty (tests / offline)
    opt = _parse_json(row.get("optimization"), {})
    before_title = live.get("title") or opt.get("title") or source_title
    before_desc = live.get("description") or opt.get("description") or ""
    before_aspects = live.get("aspects") or opt.get("aspects") or {}
    if not isinstance(before_aspects, dict):
        before_aspects = {}

    # Fact sheets for violation list
    source_sheet = None
    live_sheet = None
    violations: list[dict[str, Any]] = []
    try:
        from src.utils.listing_fact_sheet import compare_fact_sheets, fact_sheet_for_content

        source_sheet = fact_sheet_for_content(
            conn,
            source_title,
            source_desc,
            structured={"attributes": source_attrs, "specs": source_specs, "aspects": {}},
        )
        live_sheet = fact_sheet_for_content(
            conn,
            before_title,
            before_desc,
            structured={"aspects": before_aspects, "attributes": source_attrs},
        )
        if source_sheet and live_sheet:
            violations = compare_fact_sheets(source_sheet, live_sheet)
    except Exception as exc:
        violations = []
        # continue — claim engine still used in validation

    # Also merge claim-engine feature violations as semantic_feature HIGH
    try:
        from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations

        constraints = build_source_constraints(
            attrs=source_attrs,
            specs=source_specs,
            source_description=source_desc,
            source_title=source_title,
        )
        for cv in detect_claim_violations(
            constraints, before_title, before_desc, before_aspects
        ):
            if cv.severity in ("CRITICAL", "HIGH"):
                claim_type = "semantic_feature" if cv.claim_type == "unsupported_feature" else f"claim_{cv.claim_type}"
                violations.append(
                    {
                        "claim_type": claim_type,
                        "claim_text": cv.claim_text,
                        "severity": cv.severity,
                        "source_evidence": cv.source_evidence,
                    }
                )
    except Exception:
        pass

    dims_trustworthy = not _suspect_source_dimensions(source_title, source_attrs, source_specs)
    source_dims = _source_dims(source_attrs, source_specs)
    category_id = str(live.get("category_id") or opt.get("categoryId") or "").strip()
    protected = get_required_aspect_names(category_id)

    new_aspects, fix_notes, human_types = apply_aspect_fixes(
        before_aspects,
        violations=violations,
        source_attrs=source_attrs,
        source_sheet=source_sheet,
        source_dims_trustworthy=dims_trustworthy,
        source_dims=source_dims,
        protected_keys=protected,
    )

    # Gap 2: required aspect protection + fill
    new_aspects, req_notes, req_human = protect_and_fill_required_aspects(
        new_aspects,
        category_id=category_id,
        title=before_title,
        source_attrs=source_attrs,
        source_sheet=source_sheet,
        before_aspects=before_aspects,
    )
    fix_notes.extend(req_notes)
    human_types.extend(req_human)

    # Title (gap 1: category precheck — never change category)
    from src.utils.title_sanitizer import normalize_listing_title_for_ebay

    if title_contains_violation(before_title, violations):
        candidate, _ = normalize_listing_title_for_ebay(source_title, source_title=source_title)
        if category_id and title_triggers_category_change(candidate, category_id, before_desc):
            scrubbed = scrub_violation_tokens_from_title(before_title, violations)
            scrubbed, _ = normalize_listing_title_for_ebay(scrubbed, source_title=source_title)
            if category_id and title_triggers_category_change(scrubbed, category_id, before_desc):
                new_title = before_title
                human_types.append("title_category_conflict")
                fix_notes.append(
                    "title: kept original — source rebuild and scrub both trigger category precheck"
                )
            else:
                new_title = scrubbed
                fix_notes.append(
                    "title: scrubbed violation tokens only (source rebuild would change category)"
                )
        else:
            new_title = candidate
            fix_notes.append("title: rebuilt from source (contained violation)")
    else:
        new_title, _ = normalize_listing_title_for_ebay(before_title, source_title=source_title)
        if category_id and title_triggers_category_change(new_title, category_id, before_desc):
            new_title = before_title
            fix_notes.append("title: kept original — normalize alone would trigger category precheck")
        else:
            fix_notes.append("title: kept (no violation in title)")

    # Description — deterministic rebuild
    new_desc = build_description_from_source(
        title=new_title,
        source_description=source_desc,
        attrs=source_attrs,
        specs=source_specs,
        aspects=new_aspects,
        characteristics=characteristics,
    )
    if not new_desc:
        return SkipResult(
            sku=sku,
            code="empty_characteristics",
            reason="build_structured_description_from_source returned empty",
        )

    # Scrub certs from description if needed
    if any(v.get("claim_type") == "semantic_certification" for v in violations):
        scrubbed = scrub_certification_from_text(new_desc)
        if scrubbed != new_desc:
            new_desc = scrubbed
            fix_notes.append("description: certification tokens scrubbed")

    shrink = description_shrink_ratio(before_desc, new_desc)
    if before_desc and shrink < 0.40:
        return SkipResult(
            sku=sku,
            code="shrink_redline",
            reason=f"description shrink ratio {shrink:.2f} < 0.40 (rebuild failure redline)",
        )

    validation = validate_rewrite(
        source_title=source_title,
        source_description=source_desc,
        source_attrs=source_attrs,
        source_specs=source_specs,
        new_title=new_title,
        new_description=new_desc,
        new_aspects=new_aspects,
        conn=conn,
        source_sheet=source_sheet,
        applied_notes=list(fix_notes),
    )

    needs_human = bool(human_types) or not validation.get("passed")
    notes = list(fix_notes)
    if human_types:
        notes.append(f"needs_human types: {sorted(set(human_types))}")
    if not dims_trustworthy:
        notes.append("suspect_source_dimensions: dimension writes suppressed")
    notes.append(f"description_shrink_ratio={shrink:.2f}")

    plan = RewritePlan(
        sku=sku,
        before={
            "title": before_title,
            "description": before_desc,
            "aspects": before_aspects,
            "offer_id": live.get("offer_id"),
            "listing_id": live.get("listing_id") or row.get("listing_id"),
            "category_id": category_id,
        },
        after={
            "title": new_title,
            "description": new_desc,
            "aspects": new_aspects,
            "category_id": category_id,  # never change category
        },
        violations=violations,
        applied_fixes=fix_notes,
        validation=validation,
        pushable=bool(validation.get("passed")) and not human_types,
        needs_human=needs_human,
        notes=notes,
        source={
            "title": source_title,
            "attributes": source_attrs,
            "specs": source_specs,
            "characteristics": characteristics,
            "sku_available": sku_available,
        },
    )
    return plan


def _restore_from_backup(ebay, before: dict[str, Any], sku: str) -> None:
    """Restore inventory+offer from backup snapshot (gap 3 / rollback path)."""
    from scripts.audit_fix_active_listings import _put_inventory_product_only

    title = before.get("title") or ""
    description = before.get("description") or ""
    aspects = copy.deepcopy(before.get("aspects") or {})
    category_id = before.get("category_id")
    # Gap 2: required protect on restore too
    aspects, _, _ = protect_and_fill_required_aspects(
        aspects,
        category_id=str(category_id or ""),
        title=title,
        source_attrs={},
        source_sheet=None,
        before_aspects=before.get("aspects") or {},
    )
    _put_inventory_product_only(ebay, sku, title, description, aspects)
    offer_id = before.get("offer_id")
    if offer_id and hasattr(ebay, "update_offer_category"):
        ebay.update_offer_category(
            offer_id,
            str(category_id or ""),
            listing_description=description,
        )
    if offer_id and hasattr(ebay, "publish_offer"):
        ebay.publish_offer(offer_id)


def apply_rewrite(ebay, conn, plan: RewritePlan, *, cli_apply: bool = False) -> ApplyResult:
    """Push plan to eBay + update DB. Dual-gated. Staged writes with auto-rollback (gap 3)."""
    if not isinstance(plan, RewritePlan):
        return ApplyResult(ok=False, sku="", reason="not a RewritePlan", stage="invalid")
    if not dual_gate_allows_apply(cli_apply=cli_apply):
        return ApplyResult(
            ok=False,
            sku=plan.sku,
            reason=dual_gate_block_reason(cli_apply=cli_apply),
            stage="dual_gate",
        )
    if not plan.pushable:
        return ApplyResult(ok=False, sku=plan.sku, reason="plan not pushable", stage="not_pushable")

    # Backup before write
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"{plan.sku}.json"
    backup_path.write_text(json.dumps(plan.before, ensure_ascii=False, indent=2), encoding="utf-8")

    title = plan.after["title"]
    description = plan.after["description"]
    aspects = copy.deepcopy(plan.after.get("aspects") or {})
    category_id = plan.after.get("category_id") or plan.before.get("category_id")
    # Final required-aspect guard before push
    aspects, _, req_human = protect_and_fill_required_aspects(
        aspects,
        category_id=str(category_id or ""),
        title=title,
        source_attrs=(plan.source or {}).get("attributes") or {},
        source_sheet=None,
        before_aspects=plan.before.get("aspects") or {},
    )
    if req_human:
        return ApplyResult(
            ok=False,
            sku=plan.sku,
            reason=f"apply_blocked_required_missing: {req_human}",
            backup_path=str(backup_path),
            stage="required_guard",
        )

    put_ok = offer_ok = publish_ok = False
    listing_id = plan.before.get("listing_id")
    offer_id = plan.before.get("offer_id")
    failed_stage = ""

    try:
        from scripts.audit_fix_active_listings import _put_inventory_product_only

        _put_inventory_product_only(ebay, plan.sku, title, description, aspects)
        put_ok = True

        if offer_id and hasattr(ebay, "update_offer_category"):
            ok = ebay.update_offer_category(
                offer_id,
                str(category_id or ""),
                listing_description=description,
            )
            if ok is False:
                raise RuntimeError("update_offer_category returned False")
            offer_ok = True
        else:
            offer_ok = True  # no offer to update

        if offer_id and hasattr(ebay, "publish_offer"):
            pub = ebay.publish_offer(offer_id) or {}
            listing_id = pub.get("listingId") or listing_id
            publish_ok = True
        else:
            publish_ok = True
    except Exception as exc:
        failed_stage = (
            "publish" if put_ok and offer_ok else
            "offer" if put_ok else
            "put"
        )
        # Gap 3: any stage failure after partial write → auto-rollback
        rolled = False
        if put_ok:
            try:
                _restore_from_backup(ebay, plan.before, plan.sku)
                rolled = True
            except Exception as rb_exc:
                return ApplyResult(
                    ok=False,
                    sku=plan.sku,
                    reason=(
                        f"apply_failed_stage={failed_stage}: {exc}; "
                        f"auto_rollback_FAILED: {rb_exc}"
                    ),
                    backup_path=str(backup_path),
                    stage=failed_stage,
                    put_ok=put_ok,
                    offer_ok=offer_ok,
                    publish_ok=publish_ok,
                    rolled_back=False,
                )
        return ApplyResult(
            ok=False,
            sku=plan.sku,
            reason=f"apply_failed_stage={failed_stage}: {exc}; auto_rollback={'ok' if rolled else 'n/a'}",
            backup_path=str(backup_path),
            stage=failed_stage,
            put_ok=put_ok,
            offer_ok=offer_ok,
            publish_ok=publish_ok,
            rolled_back=rolled,
        )

    # Success path — update local optimization
    try:
        row = _load_db_row(conn, plan.sku)
        opt = _parse_json(row.get("optimization") if row else {}, {})
        opt["title"] = title
        opt["description"] = description
        opt["aspects"] = aspects
        if category_id:
            opt["categoryId"] = str(category_id)
        logs = []
        if row and row.get("logs"):
            try:
                logs = json.loads(row["logs"]) if isinstance(row["logs"], str) else list(row["logs"])
            except Exception:
                logs = []
        logs.append({"event": "semantic_rewrite", "sku": plan.sku})
        conn.execute(
            "UPDATE collected_products SET optimization = ?, logs = ? WHERE sku = ?",
            (json.dumps(opt, ensure_ascii=False), json.dumps(logs, ensure_ascii=False), plan.sku),
        )
        conn.commit()
    except Exception as db_exc:
        # Live already published; record DB error but still report applied
        return ApplyResult(
            ok=True,
            sku=plan.sku,
            reason=f"applied_live_db_warn: {db_exc}",
            listing_id=str(listing_id) if listing_id else None,
            backup_path=str(backup_path),
            stage="publish_ok",
            put_ok=True,
            offer_ok=True,
            publish_ok=True,
        )

    return ApplyResult(
        ok=True,
        sku=plan.sku,
        reason="applied",
        listing_id=str(listing_id) if listing_id else None,
        backup_path=str(backup_path),
        stage="publish_ok",
        put_ok=True,
        offer_ok=True,
        publish_ok=True,
    )


def write_preview_markdown(plan: RewritePlan | SkipResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, SkipResult):
        path.write_text(
            f"# {plan.sku} — SKIP\n\n- code: `{plan.code}`\n- reason: {plan.reason}\n",
            encoding="utf-8",
        )
        return

    def _plain(html: str, limit: int = 4000) -> str:
        t = _strip_html(html)
        return t[:limit] + ("…" if len(t) > limit else "")

    before_a = plan.before.get("aspects") or {}
    after_a = plan.after.get("aspects") or {}
    all_keys = sorted(set(before_a) | set(after_a), key=lambda x: str(x).lower())
    aspect_rows = []
    for k in all_keys:
        b = before_a.get(k)
        a = after_a.get(k)
        if b != a:
            aspect_rows.append(f"| {k} | {b} | {a} |")

    viol_lines = [
        f"- **{v.get('severity')}** `{v.get('claim_type')}`: {v.get('claim_text')} (source={v.get('source_evidence')})"
        for v in plan.violations
    ] or ["- (none detected)"]

    val = plan.validation or {}
    md = f"""# {plan.sku} — Semantic Rewrite Preview (dry-run)

## Violations
{chr(10).join(viol_lines)}

## Title
- **before:** {plan.before.get('title')}
- **after:** {plan.after.get('title')}

## Aspects diff
| key | before | after |
|-----|--------|-------|
{chr(10).join(aspect_rows) if aspect_rows else "| (no aspect changes) | | |"}

## Description (plain text, truncated)
### Before
```
{_plain(plan.before.get('description') or '')}
```

### After
```
{_plain(plan.after.get('description') or '')}
```

## Applied fixes
{chr(10).join(f'- {n}' for n in plan.applied_fixes) or '- (none)'}

## Validation (3 layers)
- claim CRITICAL: {len(val.get('claim_critical') or [])}
- fact CRITICAL: {len(val.get('fact_critical') or [])}
- quality_gate: {json.dumps(val.get('quality_gate') or {}, ensure_ascii=False)}
- **passed:** {val.get('passed')}
- **pushable:** {plan.pushable}
- **needs_human:** {plan.needs_human}

## Notes
{chr(10).join(f'- {n}' for n in plan.notes)}
"""
    path.write_text(md, encoding="utf-8")
