"""通用幻觉检测引擎。

用静态规则表替代 audit_fix_active_listings.py 中的 8 条手工正则。
所有检测逻辑基于 source vs generated 文本比对。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ClaimViolation:
    """一条幻觉违规记录。"""
    claim_type: str          # "material_upgrade" | "unsupported_feature" | "unsupported_quantity" | ...
    claim_text: str          # 具体的幻觉文本，如 "genuine leather"
    location: str            # "title" | "description" | "aspects.Material" | "aspects.Features"
    source_evidence: str     # source 中最接近的原文，如 "PU Leather"；或 "NOT_FOUND"
    severity: str            # "CRITICAL" | "HIGH"
    auto_fixable: bool       # 是否能自动修复


# ──────────────── 规则表 ────────────────

MATERIAL_UPGRADE_CHAINS: dict[str, set[str]] = {
    # source 材料 (lowercase) → 禁止升级到的材料集合
    "glass":           {"tempered glass", "borosilicate glass"},
    "pu leather":      {"genuine leather", "real leather", "top grain leather", "full grain leather"},
    "faux leather":    {"genuine leather", "real leather", "top grain leather"},
    "leather":         {"genuine leather", "real leather"},
    "mdf":             {"solid wood", "hardwood"},
    "particle board":  {"solid wood", "hardwood"},
    "engineered wood": {"solid wood", "hardwood"},
    "density board":   {"solid wood", "hardwood"},
    "wood":            set(),  # generic wood 不限制，但 species 检测会拦截
    "metal":           {"stainless steel", "titanium"},
    "steel":           {"stainless steel"},
    "iron":            {"aluminum", "aluminium", "stainless steel"},
    "fabric":          {"velvet", "silk", "cashmere"},
    "polyester":       {"velvet", "silk"},
    "foam":            {"memory foam", "latex foam"},
    "plastic":         {"abs plastic", "polycarbonate"},
}

WOOD_SPECIES_PATTERNS: dict[str, tuple[str, ...]] = {
    # 与 audit_fix_active_listings.py L116-L125 的 WOOD_SPECIES_PATTERNS 保持一致
    "Acacia Wood": (r"\bacacia\b",),
    "Teak Wood": (r"\bteak\b",),
    "Oak Wood": (r"\boak\b",),
    "Pine Wood": (r"\bpine\b",),
    "Rubberwood": (r"\brubber\s*wood\b", r"\brubberwood\b"),
    "Walnut Wood": (r"\bwalnut\b",),
    "Bamboo": (r"\bbamboo\b",),
    "Eucalyptus Wood": (r"\beucalyptus\b",),
}

FEATURE_CLAIM_PATTERNS: dict[str, tuple[str, ...]] = {
    # feature_name → 检测正则列表
    # source 中如果没有对应的 feature_name 关键词支持，则为违规
    "foldable":        (r"\bfoldable\b", r"\bfolding\b", r"\bcollapsible\b", r"\bcollapse\b", r"\bfold\b"),
    "reclining":       (r"\breclining\b", r"\brecline\b", r"\breclinable\b"),
    "swivel":          (r"\bswivel\b", r"\b360.?degree\b", r"\brotating\b"),
    "charging":        (r"\busb\b", r"\bcharging\s+(?:port|station)\b", r"\bpower\s+outlet\b", r"\bbuilt-in\s+outlet\b", r"\bcharger\b"),
    "bluetooth":       (r"\bbluetooth\b", r"\bspeaker\b", r"\baudio\s+system\b", r"\bsound\s+system\b"),
    "waterproof":      (r"\bwaterproof\b",),
    "water_resistant": (r"\bwater[\s-]*resistant\b",),
    "uv_resistant":    (r"\buv[\s-]*resistant\b",),
    "weather_resistant": (r"\bweather[\s-]*resistant\b", r"\ball[\s-]*weather\b"),
    "fade_resistant":  (r"\bfade[\s-]*resistant\b",),
    "heated":          (r"\bheated\b", r"\bheating\b", r"\bwarmer\b"),
    "massage":         (r"\bmassage\b", r"\bvibrat(?:ion|ing)\b"),
    "led_lighting":    (r"\bled\s+light\b", r"\bled\s+strip\b", r"\bbacklit\b"),
    "soft_close":      (r"\bsoft[\s-]*close\b",),
    "self_closing":    (r"\bself[\s-]*closing\b",),
    "anti_tip":        (r"\banti[\s-]*tip\b", r"\btip[\s-]*over\s+restraint\b"),
    "lockable":        (r"\blockable\b", r"\bwith\s+lock\b"),
    "convertible":     (r"\bconvertible\b", r"\b(?:2|3|two|three)[\s-]*in[\s-]*(?:1|one)\b"),
    "removable_cover": (r"\bremovable\s+cover\b", r"\bwashable\s+cover\b"),
    "tsa_approved":    (r"\btsa\b", r"\btransportation\s+security\b"),
    "cushion":         (r"\bcushions?\b", r"\bcushioned\b", r"\bupholstered\b"),
}

# 对应 source 的检测正则——用于判断 source 是否支持某 feature
# 这与 FEATURE_CLAIM_PATTERNS 中的正则不同：source 可能含中文
FEATURE_SOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "foldable":        (r"\bfold", r"\bcollapse", r"折叠"),
    "charging":        (r"\busb\b", r"\boutlet\b", r"\bcharging\b", r"\bpower\s+strip\b", r"插座", r"充电", r"\bcharger\b"),
    "bluetooth":       (r"\bbluetooth\b", r"\bspeaker\b", r"\baudio\b", r"\bsound\b", r"蓝牙", r"音响", r"喇叭"),
    "cushion":         (r"\bcushions?\b", r"\bcushioned\b", r"\bpillow", r"\bupholster", r"\bfoam\b", r"\bsponge\b", r"\bsofa\b", r"\bsectional\b", r"垫"),
    "water_resistant": (r"\bwater[\s-]*resistant\b", r"防水"),
    "uv_resistant":    (r"\buv[\s-]*resistant\b", r"防紫外线"),
    "weather_resistant": (r"\bweather[\s-]*resistant\b", r"\ball[\s-]*weather\b", r"耐候"),
    "fade_resistant":  (r"\bfade[\s-]*resistant\b", r"防褪色"),
    "waterproof":      (r"\bwaterproof\b", r"防水"),
    # 其余 feature 默认用 FEATURE_CLAIM_PATTERNS 同名正则检测 source
}

COUNTABLE_CLAIMS: dict[str, tuple[str, ...]] = {
    "position": (r"\b(\d+)[\s-]*position\b", r"\b(\d+)[\s-]*level\b"),
    "tier":     (r"\b(\d+)[\s-]*tier\b",),
    "shelf":    (r"\b(\d+)\s*(?:shelf|shelves)\b",),
    "drawer":   (r"\b(\d+)\s*drawer\b",),
    "door":     (r"\b(\d+)\s*door\b",),
    "seat":     (r"\b(\d+)[\s-]*seat\b",),
}

CERTIFICATION_PATTERNS: tuple[str, ...] = (
    r"\btsa\b",
    r"\bul\s+listed\b",
    r"\bastm\b",
    r"\biso\s+\d+\b",
    r"\bcarb\s+(?:2|ii)\b",
    r"\bfda\b",
    r"\bcpsc\b",
)

# 自然就是 foldable 的产品（不需要 source 证据）
NATURALLY_FOLDABLE_KEYWORDS: tuple[str, ...] = (
    "umbrella", "camping chair", "canopy", "shade sail", "tent", "hammock",
)


def _strip_html_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_source_material_hint(attrs: Mapping[str, Any] | None, description: str) -> list[str]:
    attrs = attrs or {}
    materials = []
    
    # Check common material fields in attributes
    for key in ("Material", "Frame Material", "Main Material", "材质"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            materials.append(re.sub(r"\s+", " ", value).strip())

    if materials:
        return materials

    # Fallback to description
    text = _strip_html_text(description)
    patterns = (
        r"(?:材质|Material)\s*[:：]\s*([A-Za-z0-9+/\-& ,]+)",
        r"(?:Made of|Constructed of)\s+([A-Za-z0-9+/\-& ,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            material = re.sub(r"\s+", " ", match.group(1)).strip(" -,:;.")
            if material:
                materials.append(material)
                
    return materials


def build_source_constraints(
    attrs: Mapping[str, Any] | None,
    specs: Mapping[str, Any] | None,
    source_description: str,
    source_title: str = "",
) -> dict[str, Any]:
    """从 source 数据提取约束边界。"""
    attrs = attrs or {}
    specs = specs or {}
    
    materials = _extract_source_material_hint(attrs, source_description)
    
    source_text_parts = [source_description]
    for m in (attrs, specs):
        for k, v in m.items():
            source_text_parts.append(f"{k}: {v}")
    source_text = " ".join(source_text_parts)
    
    supported_features = set()
    for feature_name, fallback_patterns in FEATURE_CLAIM_PATTERNS.items():
        patterns = FEATURE_SOURCE_PATTERNS.get(feature_name, fallback_patterns)
        if any(re.search(p, source_text, re.IGNORECASE) for p in patterns):
            supported_features.add(feature_name)
            
    # For quantities, we simply extract numbers associated with keywords if possible,
    # but for this basic engine, we can leave counts mostly empty, relying on the source text
    # when detecting generated claims.
    counts: dict[str, int] = {}
    for claim_name, patterns in COUNTABLE_CLAIMS.items():
        for pattern in patterns:
            match = re.search(pattern, source_text, re.IGNORECASE)
            if match:
                counts[claim_name] = int(match.group(1))
                break

    return {
        "materials": materials,
        "supported_features": supported_features,
        "source_text": source_text,
        "counts": counts,
        "naturally_foldable": any(w in source_title.lower() for w in NATURALLY_FOLDABLE_KEYWORDS),
    }


def detect_claim_violations(
    source_constraints: dict[str, Any],
    generated_title: str,
    generated_description: str,
    generated_aspects: dict[str, Any],
) -> list[ClaimViolation]:
    """主入口：检测 generated 中所有不被 source 支持的声明。"""
    violations = []
    
    violations.extend(_check_material_claims(source_constraints, generated_aspects, generated_title, generated_description))
    violations.extend(_check_wood_species_claims(source_constraints, generated_aspects, generated_title, generated_description))
    violations.extend(_check_feature_claims(source_constraints, generated_aspects, generated_title, generated_description))
    violations.extend(_check_quantity_claims(source_constraints, generated_aspects, generated_title, generated_description))
    violations.extend(_check_certification_claims(source_constraints, generated_title, generated_description))
    
    return violations


def _get_generated_text(title: str, description: str, aspects: dict[str, Any]) -> str:
    parts = [title, _strip_html_text(description)]
    for k, v in aspects.items():
        if isinstance(v, list):
            parts.append(f"{k}: {' '.join(str(i) for i in v)}")
        else:
            parts.append(f"{k}: {v}")
    # Keep a non-whitespace delimiter between fields so regexes do not
    # accidentally stitch the end of one aspect value to the next aspect key
    # (for example "Seating Capacity: Up to 2" + "Seat Depth").
    return " || ".join(part for part in parts if part).lower()


def _check_material_claims(
    source_constraints: dict[str, Any],
    generated_aspects: dict[str, Any],
    generated_title: str,
    generated_description: str,
) -> list[ClaimViolation]:
    violations = []
    source_materials = source_constraints.get("materials", [])
    generated_text = _get_generated_text(generated_title, generated_description, generated_aspects)
    
    # To check material upgrades, we look at each source material, find its base types, 
    # and then ensure NO forbidden upgrades are present in the generated text.
    for source_mat in source_materials:
        source_lower = source_mat.lower()
        # Find which rule applies to this source material
        forbidden_upgrades = set()
        for base_mat, upgrades in MATERIAL_UPGRADE_CHAINS.items():
            if base_mat in source_lower:
                forbidden_upgrades.update(upgrades)
                
        # Also, if a material is completely absent in source, and is an upgrade of something...
        # We need a robust way: check if ANY upgrade is in generated, but NOT in source.
        
    source_text_lower = source_constraints.get("source_text", "").lower()
    
    checked_upgrades = set()
    
    # Add a synonym map to handle cases where the source uses a shorthand (like ABS)
    # but the AI generates the full term (like ABS plastic)
    upgrade_source_synonyms = {
        "abs": r"\b(?:abs|abs plastic|abs material)\b",
        "genuine leather": r"\b(?:genuine leather|real leather|top grain leather)\b",
        "real leather": r"\b(?:genuine leather|real leather|top grain leather)\b",
        "solid wood": r"\b(?:solid wood|hardwood)\b",
        "hardwood": r"\b(?:solid wood|hardwood)\b",
        "rubberwood": r"\b(?:rubberwood|rubber wood)\b",
        "walnut": r"\b(?:walnut|walnut wood)\b",
    }
    
    for base_mat, upgrades in MATERIAL_UPGRADE_CHAINS.items():
        for upgrade in upgrades:
            if upgrade in checked_upgrades:
                continue
            checked_upgrades.add(upgrade)
            
            if upgrade in generated_text:
                # Check if the upgrade string actually matches as a full word
                if re.search(rf"\b{re.escape(upgrade)}\b", generated_text):
                    # Determine the regex pattern to check against the source text
                    source_pattern = upgrade_source_synonyms.get(upgrade, rf"\b{re.escape(upgrade)}\b")
                    if not re.search(source_pattern, source_text_lower):
                        violations.append(ClaimViolation(
                            claim_type="material_upgrade",
                            claim_text=upgrade,
                            location="generated content",
                            source_evidence=source_materials[0] if source_materials else "NOT_FOUND",
                            severity="CRITICAL",
                            auto_fixable=True,
                        ))
                        
    return violations


def _check_wood_species_claims(
    source_constraints: dict[str, Any],
    generated_aspects: dict[str, Any],
    generated_title: str,
    generated_description: str,
) -> list[ClaimViolation]:
    violations = []
    generated_text = _get_generated_text(generated_title, generated_description, generated_aspects)
    source_text_lower = source_constraints.get("source_text", "").lower()
    
    for species, patterns in WOOD_SPECIES_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, generated_text):
                if not re.search(pattern, source_text_lower):
                    violations.append(ClaimViolation(
                        claim_type="unsupported_wood_species",
                        claim_text=species,
                        location="generated content",
                        source_evidence="NOT_FOUND",
                        severity="CRITICAL",
                        auto_fixable=True,
                    ))
                    break # Break patterns loop to avoid duplicate violations for the same species
    return violations


def _check_feature_claims(
    source_constraints: dict[str, Any],
    generated_aspects: dict[str, Any],
    generated_title: str,
    generated_description: str,
) -> list[ClaimViolation]:
    violations = []
    generated_text = _get_generated_text(generated_title, generated_description, generated_aspects)
    supported_features = source_constraints.get("supported_features", set())
    
    for feature_name, patterns in FEATURE_CLAIM_PATTERNS.items():
        if feature_name == "foldable" and source_constraints.get("naturally_foldable"):
            continue
            
        generated_has = any(re.search(p, generated_text) for p in patterns)
        source_has = feature_name in supported_features
        
        if generated_has and not source_has:
            violations.append(ClaimViolation(
                claim_type="unsupported_feature",
                claim_text=feature_name,
                location="generated content",
                source_evidence="NOT_FOUND",
                severity="CRITICAL",
                auto_fixable=True,
            ))
            
    return violations


def _check_quantity_claims(
    source_constraints: dict[str, Any],
    generated_aspects: dict[str, Any],
    generated_title: str,
    generated_description: str,
) -> list[ClaimViolation]:
    violations = []
    generated_text = _get_generated_text(generated_title, generated_description, generated_aspects)
    counts = source_constraints.get("counts", {})
    
    for claim_name, patterns in COUNTABLE_CLAIMS.items():
        for pattern in patterns:
            match = re.search(pattern, generated_text)
            if match:
                claimed_count = int(match.group(1))
                source_count = counts.get(claim_name)
                
                if source_count is None:
                    # Sometimes, words like "3-tier" are used but it's not extracted.
                    # As a safe measure, we can flag this as HIGH for unsupported quantity.
                    violations.append(ClaimViolation(
                        claim_type="unsupported_quantity",
                        claim_text=f"{claimed_count}-{claim_name}",
                        location="generated content",
                        source_evidence="NOT_FOUND",
                        severity="HIGH",
                        auto_fixable=True,
                    ))
                elif claimed_count != source_count:
                    violations.append(ClaimViolation(
                        claim_type="unsupported_quantity",
                        claim_text=f"claimed {claimed_count}-{claim_name}, source has {source_count}",
                        location="generated content",
                        source_evidence=str(source_count),
                        severity="CRITICAL",
                        auto_fixable=True,
                    ))
                break # Only check the first match for a specific countable claim type
                
    return violations


def _check_certification_claims(
    source_constraints: dict[str, Any],
    generated_title: str,
    generated_description: str,
) -> list[ClaimViolation]:
    violations = []
    generated_text = f"{generated_title} {generated_description}".lower()
    source_text_lower = source_constraints.get("source_text", "").lower()
    
    for pattern in CERTIFICATION_PATTERNS:
        match = re.search(pattern, generated_text)
        if match:
            cert_text = match.group(0)
            if not re.search(pattern, source_text_lower):
                violations.append(ClaimViolation(
                    claim_type="unsupported_feature",
                    claim_text="tsa_approved" if "tsa" in cert_text else cert_text,
                    location="title/description",
                    source_evidence="NOT_FOUND",
                    severity="CRITICAL",
                    auto_fixable=True,
                ))
                
    return violations
