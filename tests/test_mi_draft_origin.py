from src.utils.mi_draft_origin import (
    MI_DRAFT_LOG_MARKER,
    MI_DRAFT_ORIGIN,
    apply_mi_draft_origin,
    append_mi_draft_log,
    is_mi_draft_product,
)


def test_apply_mi_draft_origin_preserves_existing_optimization_fields():
    updated = apply_mi_draft_origin(
        {"title": "Modern Sofa", "aspects": {"Brand": "AquaVerve"}},
        detected_at="2026-05-04T09:30:00",
    )

    assert updated["title"] == "Modern Sofa"
    assert updated["aspects"] == {"Brand": "AquaVerve"}
    assert updated["draft_origin"] == MI_DRAFT_ORIGIN
    assert updated["draft_origin_detected_at"] == "2026-05-04T09:30:00"


def test_is_mi_draft_product_detects_log_marker_when_optimization_was_replaced():
    logs = append_mi_draft_log([], detected_at="2026-05-04T09:30:00")

    assert any(MI_DRAFT_LOG_MARKER in entry for entry in logs)
    assert is_mi_draft_product(
        {
            "optimization": {"title": "Regenerated Title"},
            "logs": logs,
        }
    )