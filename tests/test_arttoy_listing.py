"""Tests for the art-toy listing path (B3): prompt, finalize, and qwen dispatch."""

import dataclasses
import json
from types import SimpleNamespace

import pytest

from src.utils.store_profile import StoreProfile
from src.utils.banned_terms_guard import BannedTermError
from src.services.arttoy_prompt import (
    build_arttoy_system_prompt,
    build_arttoy_user_prompt,
    finalize_arttoy_listing,
)


def _arttoy_profile(**over):
    base = dataclasses.replace(
        StoreProfile(),
        template_style="arttoy_hype",
        force_house_brand=False,  # art toys keep own IP; missing Brand -> Unbranded
        banned_terms=("POP MART", "Original", "Genuine"),
        footer_html="<div>SHIPPING &amp; LOGISTICS via SpeedPAK</div>",
        quality_footer_marker="shipping & logistics",
    )
    return dataclasses.replace(base, **over) if over else base


class TestPromptBuilders:
    def test_system_prompt_injects_banned_and_style(self):
        s = build_arttoy_system_prompt(_arttoy_profile())
        assert "POP MART" in s and "Original" in s
        assert "Hypebeast" in s and "INLINE CSS" in s
        assert "titleCN" in s and "descriptionCN" in s

    def test_system_prompt_falls_back_to_default_banned(self):
        p = dataclasses.replace(StoreProfile(), template_style="arttoy_hype", banned_terms=())
        s = build_arttoy_system_prompt(p)
        assert "POP MART" in s  # default art-toy banned list

    def test_user_prompt_includes_source_and_market(self):
        u = build_arttoy_user_prompt(
            title="Labubu Figure",
            description="vinyl toy",
            attributes={"Character": "Labubu"},
            market_intel={"top_keywords": ["art toy", "kawaii"], "price_stats": {"min": 10, "max": 40, "avg": 22}},
        )
        assert "Labubu Figure" in u
        assert "art toy" in u
        assert "$10-$40" in u


class TestFinalize:
    def test_appends_footer_once_even_with_escaped_amp(self):
        p = _arttoy_profile()
        r = finalize_arttoy_listing(
            {"title": "Cute Art Toy", "description": "<div>fig</div>", "aspects": {}}, p
        )
        assert r["description"].count("SHIPPING") == 1
        r2 = finalize_arttoy_listing(r, p)  # idempotent
        assert r2["description"].count("SHIPPING") == 1

    def test_guarantees_chinese_keys(self):
        r = finalize_arttoy_listing(
            {"title": "Toy", "description": "<div>x</div>", "aspects": {}}, _arttoy_profile()
        )
        assert "titleCN" in r and "descriptionCN" in r
        assert r["titleCN"] == "" and r["descriptionCN"] == ""

    def test_preserves_provided_chinese(self):
        r = finalize_arttoy_listing(
            {
                "title": "Toy",
                "titleCN": "玩具",
                "description": "<div>x</div>",
                "descriptionCN": "<p>描述</p>",
                "aspects": {},
            },
            _arttoy_profile(),
        )
        assert r["titleCN"] == "玩具"
        assert r["descriptionCN"] == "<p>描述</p>"

    def test_normalizes_aspects_to_lists(self):
        r = finalize_arttoy_listing(
            {"title": "Toy", "description": "<div>x</div>", "aspects": {"Type": "Blind Box", "Character": ["Molly"], "Empty": ""}},
            _arttoy_profile(),
        )
        assert r["aspects"]["Type"] == ["Blind Box"]
        assert r["aspects"]["Character"] == ["Molly"]
        assert "Empty" not in r["aspects"]
        assert r["aspects"]["Brand"] == ["Unbranded"]  # eBay-required Brand auto-added

    def test_strips_banned_from_title(self):
        r = finalize_arttoy_listing(
            {"title": "POP MART Labubu Figure", "description": "<div>x</div>", "aspects": {}},
            _arttoy_profile(),
        )
        assert "POP MART" not in r["title"]
        assert "Labubu" in r["title"]

    def test_strips_banned_brand_to_default(self):
        r = finalize_arttoy_listing(
            {"title": "Toy", "description": "<div>x</div>", "aspects": {"Brand": "Original"}},
            _arttoy_profile(),
        )
        # banned Brand value stripped -> falls back to default (Unbranded for art toys)
        assert r["aspects"].get("Brand") == ["Unbranded"]

    def test_output_is_clean_after_strip(self):
        r = finalize_arttoy_listing(
            {"title": "Genuine POP MART Toy", "description": "<div>Original design</div>",
             "aspects": {"Type": "Blind Box"}},
            _arttoy_profile(),
        )
        from src.utils.banned_terms_guard import scan_listing
        assert scan_listing(title=r["title"], description=r["description"],
                            aspects=r["aspects"], terms=["POP MART", "Original", "Genuine"]) == []


# --- Dispatch tests (mock the LLM client) --------------------------------


class _FakeCompletions:
    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _optimizer(contents):
    from qwen_optimizer import QwenOptimizer

    opt = QwenOptimizer.__new__(QwenOptimizer)  # bypass __init__/API key
    opt.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(contents)))
    opt.model = "test-model"
    return opt


@pytest.fixture
def _use_profile(monkeypatch, tmp_path):
    # Keep the shared category matcher offline: it's best-effort in the art-toy
    # path (wrapped in try/except) and a live api.ebay.com call has no place in
    # a unit test.
    import src.services.ebay_category_matcher as ecm

    monkeypatch.setattr(
        ecm, "create_category_matcher",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline in tests")),
    )

    def _apply(profile_yaml):
        from src.utils import store_profile as sp

        f = tmp_path / "profile.yaml"
        f.write_text(profile_yaml, encoding="utf-8")
        monkeypatch.setenv("STORE_PROFILE_PATH", str(f))
        sp.reset_store_profile_cache()

    yield _apply
    from src.utils import store_profile as sp

    sp.reset_store_profile_cache()


class TestDispatch:
    ARTTOY_YAML = (
        "listing:\n"
        "  template_style: arttoy_hype\n"
        "  footer_html: '<div>SHIPPING &amp; LOGISTICS</div>'\n"
        "  banned_terms:\n    - POP MART\n    - Original\n"
        "quality_gate:\n  footer_marker: 'shipping & logistics'\n"
    )

    def test_furniture_style_does_not_dispatch_to_arttoy(self, _use_profile, monkeypatch):
        _use_profile("store:\n  brand_name: AquaVerve\n")  # template_style defaults to furniture_classic
        opt = _optimizer([])
        called = {"arttoy": False}
        monkeypatch.setattr(
            opt, "optimize_arttoy_listing",
            lambda *a, **k: called.__setitem__("arttoy", True) or {},
        )
        # Force the furniture branch to stop right after dispatch check by making
        # extract_dimensions raise — we only care that arttoy was NOT called.
        monkeypatch.setattr(opt, "extract_dimensions", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
        with pytest.raises(RuntimeError):
            opt.optimize_product_full("Wooden Coffee Table", "oak table")
        assert called["arttoy"] is False

    def test_arttoy_style_dispatches_and_finalizes(self, _use_profile):
        _use_profile(self.ARTTOY_YAML)
        clean = json.dumps({
            "title": "Kawaii Designer Vinyl Figure Blind Box",
            "titleCN": "可爱设计师玩具",
            "description": "<div style='color:#000'>Collectible vinyl art toy</div>",
            "descriptionCN": "<p>可爱</p>",
            "aspects": {"Type": "Blind Box", "Character": "Labubu"},
        })
        opt = _optimizer([clean])
        result = opt.optimize_product_full("Labubu toy", "vinyl figure")
        assert result["title"].startswith("Kawaii")
        assert result["titleCN"] == "可爱设计师玩具"
        assert "SHIPPING" in result["description"]  # footer appended
        assert result["aspects"]["Type"] == ["Blind Box"]

    def test_strips_banned_in_single_call_no_retry(self, _use_profile):
        _use_profile(self.ARTTOY_YAML)
        dirty = json.dumps({
            "title": "POP MART Labubu Figure",
            "description": "<div>clean art toy</div>",
            "aspects": {"Type": "Blind Box"},
        })
        opt = _optimizer([dirty])
        result = opt.optimize_arttoy_listing("Labubu", "vinyl")
        assert "POP MART" not in result["title"]
        assert "Labubu" in result["title"]
        # deterministic strip => clean on the first call, no retry
        assert len(opt.client.chat.completions.calls) == 1

    def test_long_description_truncated_and_footer_preserved(self, _use_profile):
        _use_profile(self.ARTTOY_YAML)
        big = "<div>" + ("<p>Collectible vinyl art toy detail sentence here.</p>" * 120) + "</div>"
        assert len(big) > 3300
        payload = json.dumps({
            "title": "Clean Designer Art Toy",
            "description": big,
            "aspects": {"Type": "Blind Box"},
        })
        opt = _optimizer([payload])
        result = opt.optimize_arttoy_listing("toy", "vinyl")
        assert len(result["description"]) <= 4000  # under eBay limit
        assert "SHIPPING" in result["description"]  # footer survived truncation

    def test_falls_back_on_unparseable_output(self, _use_profile):
        _use_profile(self.ARTTOY_YAML)
        opt = _optimizer(["this is not json", "still not json"])
        result = opt.optimize_arttoy_listing("Labubu", "vinyl")
        assert result.get("error") == "generation failed"
