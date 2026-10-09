from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


MI_DRAFT_ORIGIN = "mi_opportunity"
MI_DRAFT_LOG_MARKER = "[MI_OPPORTUNITY_DRAFT]"


def _normalize_optimization(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _normalize_logs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except Exception:
            return [value]
    return []


def apply_mi_draft_origin(optimization: Any, detected_at: str | None = None) -> dict:
    updated = _normalize_optimization(optimization)
    updated["draft_origin"] = MI_DRAFT_ORIGIN
    if detected_at:
        updated["draft_origin_detected_at"] = detected_at
    return updated


def append_mi_draft_log(logs: Any, detected_at: str | None = None) -> list[str]:
    entries = _normalize_logs(logs)
    if any(MI_DRAFT_LOG_MARKER in entry for entry in entries):
        return entries
    suffix = f" detected_at={detected_at}" if detected_at else ""
    entries.append(f"{MI_DRAFT_LOG_MARKER}{suffix}")
    return entries


def is_mi_draft_product(product: Mapping[str, Any] | None) -> bool:
    if not isinstance(product, Mapping):
        return False

    optimization = _normalize_optimization(product.get("optimization"))
    if optimization.get("draft_origin") == MI_DRAFT_ORIGIN:
        return True

    logs = _normalize_logs(product.get("logs"))
    return any(MI_DRAFT_LOG_MARKER in entry for entry in logs)