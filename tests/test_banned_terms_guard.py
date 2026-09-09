"""Tests for the banned-terms hard guard (B1, blind-box instance)."""

import pytest

from src.utils.banned_terms_guard import (
    BannedTermError,
    assert_clean,
    clean_banned_aspects,
    scan_listing,
    strip_banned_terms,
)

TERMS = ["POP MART", "Original", "Genuine", "Authentic", "OEM", "Licensed"]


class TestScanning:
    def test_hits_title_description_and_aspect(self):
        hits = scan_listing(
            title="POP MART Labubu Figure",
            description="<p>100% <b>Original</b> design</p>",
            aspects={"Brand": ["POP MART"], "Type": ["Blind Box"]},
            terms=TERMS,
        )
        fields = sorted(h.field for h in hits)
        assert fields == ["aspect:Brand", "description", "title"]
        assert all(h.term in TERMS for h in hits)

    def test_multiword_term_matches_across_whitespace(self):
        hits = scan_listing(title="POP   MART set", terms=TERMS)
        assert len(hits) == 1
        assert hits[0].term == "POP MART"

    def test_html_tags_do_not_hide_terms(self):
        hits = scan_listing(description="<span>Gen</span>uine?", terms=TERMS)
        # 'Gen' + 'uine' split by a tag should NOT be reassembled into a match
        assert hits == []
        hits2 = scan_listing(description="<span>Genuine</span> article", terms=TERMS)
        assert len(hits2) == 1

    def test_case_insensitive(self):
        hits = scan_listing(title="pop mart / oem parts", terms=TERMS)
        assert {h.term for h in hits} == {"POP MART", "OEM"}


class TestFalsePositives:
    @pytest.mark.parametrize(
        "text",
        ["GOEM housing", "OEMs plural", "Originality matters", "Genuinely nice"],
    )
    def test_no_partial_word_matches(self, text):
        assert scan_listing(title=text, terms=TERMS) == []


class TestNoOp:
    def test_empty_terms_is_noop(self):
        assert scan_listing(title="POP MART", description="Original", terms=[]) == []

    def test_none_terms_reads_profile(self, monkeypatch, tmp_path):
        from src.utils import store_profile as sp

        prof = tmp_path / "p.yaml"
        prof.write_text(
            "listing:\n  banned_terms:\n    - POP MART\n", encoding="utf-8"
        )
        monkeypatch.setenv("STORE_PROFILE_PATH", str(prof))
        sp.reset_store_profile_cache()
        try:
            hits = scan_listing(title="A POP MART toy")  # terms=None -> profile
            assert len(hits) == 1 and hits[0].term == "POP MART"
        finally:
            sp.reset_store_profile_cache()

    def test_default_profile_has_no_banned_terms(self, monkeypatch):
        from src.utils import store_profile as sp

        monkeypatch.delenv("STORE_PROFILE_PATH", raising=False)
        monkeypatch.setattr(sp, "_LOCAL_PROFILE_PATH", sp._PROJECT_ROOT / "nope.yaml")
        sp.reset_store_profile_cache()
        try:
            # main-account default: guard must be a no-op even with obvious terms
            assert scan_listing(title="Original Genuine POP MART") == []
        finally:
            sp.reset_store_profile_cache()


class TestStrip:
    def test_removes_whole_terms_and_tidies(self):
        assert strip_banned_terms("Authentic Universal Monsters Figure", TERMS) == "Universal Monsters Figure"
        assert strip_banned_terms("100% Original design", TERMS) == "100% design"

    def test_case_insensitive_and_multiword(self):
        assert strip_banned_terms("a pop mart toy", TERMS) == "a toy"

    def test_does_not_touch_partial_words(self):
        assert strip_banned_terms("Originality and OEMs", TERMS) == "Originality and OEMs"

    def test_html_tags_preserved(self):
        out = strip_banned_terms("<p>Genuine <b>vinyl</b></p>", TERMS)
        assert "<p>" in out and "<b>vinyl</b>" in out and "Genuine" not in out

    def test_empty_terms_is_noop(self):
        assert strip_banned_terms("POP MART Original", []) == "POP MART Original"

    def test_clean_aspects_drops_emptied_values(self):
        out = clean_banned_aspects({"Brand": ["POP MART"], "Type": ["Blind Box"]}, TERMS)
        assert "Brand" not in out          # emptied -> dropped
        assert out["Type"] == ["Blind Box"]


class TestAssertClean:
    def test_raises_with_hit_details(self):
        with pytest.raises(BannedTermError) as exc:
            assert_clean(title="POP MART", aspects={"Brand": ["Original"]}, terms=TERMS)
        assert exc.value.hits
        assert "POP MART" in str(exc.value)

    def test_passes_when_clean(self):
        assert_clean(
            title="Designer Art Toy Blind Box Figure",
            description="<p>Collectible vinyl figure</p>",
            aspects={"Type": ["Blind Box"]},
            terms=TERMS,
        )
