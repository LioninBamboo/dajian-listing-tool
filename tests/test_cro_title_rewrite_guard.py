"""Guards for the scheduled CRO title rewrite: aspect-grounding + FactSheet backstop."""

import importlib

cro = importlib.import_module("scripts.cro_title_rewrite")


class TestFitmentPreservation:
    def test_protects_vehicle_make_model_year(self):
        prot = cro._protected_keywords(
            "Trailer Hitch 2 Inch for 2015 Ford Ranger Mazda B-Series", {})
        assert {"ford", "ranger", "mazda", "2015"} <= prot

    def test_no_protection_for_furniture(self):
        # No vehicle terms → nothing protected → full-rewrite is free to restructure.
        assert cro._protected_keywords("Modern Wooden Dining Table for 6 People", {}) == set()


class TestAspectGrounding:
    def test_only_appends_sku_own_aspect_values(self):
        # build_enriched_title never invents — every added token traces to an aspect.
        out = cro.build_enriched_title(
            "MGO Garden Planter",
            {"Material": ["Magnesium Oxide (MGO)"], "Color": ["Rust"], "Style": ["Modern"]},
        )
        assert out is not None
        new = out["new_title"].lower()
        assert "rust" in new                      # from Color aspect
        # nothing that isn't in an aspect
        assert "teak" not in new and "ceramic" not in new and "waterproof" not in new

    def test_no_aspects_no_rewrite(self):
        assert cro.build_enriched_title("Plain Title", {}) is None


class TestFactGuardDegradation:
    def test_qc_unavailable_does_not_block(self, monkeypatch):
        # With the FactSheet unavailable (no QWEN), the guard must degrade to PASS
        # rather than silently blocking every SKU — the aspect-grounding still holds.
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "0")
        cand = {
            "sku": "X", "title": "MGO Garden Planter",
            "aspects": {"Material": ["Magnesium Oxide (MGO)"]},
            "optimization": {"description": "A magnesium oxide planter."},
        }
        ok, violations = cro.title_fact_guard(cand, "MGO Garden Planter Rust Outdoor")
        assert ok is True
        assert violations == []
