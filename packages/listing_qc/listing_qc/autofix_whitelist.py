"""Scheduled autofix whitelists shared across furniture-store daemons.

categoryId is intentionally never listed here.
"""

from __future__ import annotations

SCHEDULED_SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES = frozenset({
    "source_aspect_mismatch",
    "desc_dimension_mismatch",
    "desc_weight_mismatch",
    "wrong_dimension",
    "description_structure_missing_key_features",
    "wrong_weight",
    "missing_weight",
    "missing_dimension",
    "description_raw_source_dump",
    "assembly_description_missing",
    "assembly_status_unsupported",
    "assembly_required_mismatch",
    "non_applicable_aspect",
    "incomplete_title",
})

SCHEDULED_SOURCE_ASPECT_AUTOFIX_FIX_KEYS = frozenset({
    "Color",
    "Material",
    "__source_parameter_rebuild__",
    "Item Length",
    "Item Width",
    "Item Height",
    "Item Weight",
    "__desc_needs_update__",
    "__restore_live_description_from_local__",
    "__rebuild_description_from_source__",
    "__assembly_desc_update__",
    "__remove__Assembly Status",
    "Assembly Required",
    "__title__",
})

MISSING_VIDEO_AUTOFIX_ISSUE_TYPES = frozenset({"missing_video"})
MISSING_VIDEO_AUTOFIX_FIX_KEYS = frozenset({"__sync_video__"})
MISSING_VIDEO_DAILY_LIMIT_DEFAULT = 30
