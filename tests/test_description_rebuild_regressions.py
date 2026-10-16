"""Regression coverage for source-grounded description rebuilding."""

from __future__ import annotations


def test_smart_truncate_html_keeps_the_store_footer_at_the_end():
    from src.utils.html_truncator import smart_truncate_html

    body = "<div class='description'>" + "".join(
        f"<p>Source-backed product detail {i}: durable construction and useful storage.</p>"
        for i in range(40)
    ) + "</div>"
    footer = (
        '<div class="store-footer">Ships from our warehouse with tracked delivery.</div>'
    )

    result = smart_truncate_html(body + footer, max_length=1200)

    assert len(result) <= 1200
    assert result.endswith(footer)
    assert "Ships from our warehouse" in result


def test_build_description_uses_full_source_and_conversion_bullets(monkeypatch):
    import scripts.audit_fix_active_listings as audit
    import src.services.semantic_rewrite as semantic_rewrite
    import src.utils.conversion_copy as conversion_copy

    captured = {}

    def fake_feature_builder(**kwargs):
        captured["feature_builder"] = kwargs
        return ["Grounded source-backed feature."]

    def fake_structured_builder(title, source_description, attrs, specs, aspects, **kwargs):
        captured["structured_builder"] = {
            "title": title,
            "source_description": source_description,
            "attrs": attrs,
            "specs": specs,
            "aspects": aspects,
            "kwargs": kwargs,
        }
        return "<div>rebuilt</div>"

    monkeypatch.setattr(conversion_copy, "build_conversion_feature_bullets", fake_feature_builder)
    monkeypatch.setattr(audit, "build_structured_description_from_source", fake_structured_builder)

    result = semantic_rewrite.build_description_from_source(
        title="Storage Ottoman",
        source_description="The source description contains the full product details.",
        attrs={"Material": "Fabric"},
        specs={"Item Width": "20 in"},
        aspects={},
        characteristics=["Removable lid"],
    )

    assert result == "<div>rebuilt</div>"
    assert "full product details" in captured["structured_builder"]["source_description"]
    assert "Removable lid" in captured["structured_builder"]["source_description"]
    assert captured["feature_builder"]["limit"] == 6
    assert captured["structured_builder"]["kwargs"]["feature_bullets"] == [
        "Grounded source-backed feature."
    ]
