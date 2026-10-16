"""Shared publish-time validation helpers for READY drafts and live publish flows."""

from __future__ import annotations

import re


REQUIRED_MEASUREMENT_ASPECT_KEYS = ('Item Length', 'Item Width', 'Item Height')
OPTIONAL_MEASUREMENT_ASPECT_KEYS = ('Item Weight',)
MEASUREMENT_ASPECT_KEYS = REQUIRED_MEASUREMENT_ASPECT_KEYS + OPTIONAL_MEASUREMENT_ASPECT_KEYS
PLACEHOLDER_ASPECT_VALUES = {
    'see description',
    'refer to description',
    'refer to product images',
    'refer to photos',
    'not specified',
    'n/a',
    'na',
    'unknown',
}


def first_aspect_text(aspects: dict, key: str) -> str:
    value = (aspects or {}).get(key)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def measurement_issue(value: str, max_value: float | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return "missing"
    value_lower = text.lower()
    if value_lower in PLACEHOLDER_ASPECT_VALUES or 'see description' in value_lower or 'refer to' in value_lower:
        return "placeholder"
    match = re.search(r'(\d+(?:\.\d+)?)', text)
    if match:
        number = float(match.group(1))
        if number <= 0:
            return "non-positive"
        if max_value is not None and number > max_value:
            return "implausible"
    return None


def measurement_validation_errors(aspects: dict) -> list[str]:
    errors: list[str] = []

    for key in REQUIRED_MEASUREMENT_ASPECT_KEYS:
        value = first_aspect_text(aspects, key)
        issue = measurement_issue(value, max_value=500)
        if issue == "missing":
            errors.append(f"missing measurement aspect: {key}")
            continue
        if issue == "placeholder":
            errors.append(f"placeholder measurement aspect: {key}={value}")
            continue
        if issue == "non-positive":
            errors.append(f"non-positive measurement aspect: {key}={value}")
            continue
        if issue == "implausible":
            errors.append(f"implausible measurement aspect: {key}={value}")

    for key in OPTIONAL_MEASUREMENT_ASPECT_KEYS:
        value = first_aspect_text(aspects, key)
        if not value:
            continue
        issue = measurement_issue(value, max_value=2000)
        if issue == "placeholder":
            errors.append(f"placeholder measurement aspect: {key}={value}")
            continue
        if issue == "non-positive":
            errors.append(f"non-positive measurement aspect: {key}={value}")
            continue
        if issue == "implausible":
            errors.append(f"implausible measurement aspect: {key}={value}")

    return errors


def category_validation_errors(
    category_matcher,
    title_context: str,
    category_id: str,
    category_name: str = None,
) -> list[str]:
    normalized_category_id = str(category_id or '').strip()
    if not normalized_category_id:
        return ["missing categoryId"]

    if category_matcher:
        normalized_category_id, category_name = category_matcher.canonicalize_category(
            title_context,
            normalized_category_id,
            category_name,
        )

    if category_matcher and not category_matcher.is_category_plausible_for_text(
        title_context,
        normalized_category_id,
        category_name,
    ):
        return [f"implausible category {normalized_category_id} for title '{title_context[:120]}'"]

    return []


def get_publish_blockers(
    category_matcher,
    title_context: str,
    aspects: dict,
    category_id: str,
    category_name: str = None,
) -> list[str]:
    blockers = measurement_validation_errors(aspects)
    blockers.extend(category_validation_errors(category_matcher, title_context, category_id, category_name))
    return blockers
