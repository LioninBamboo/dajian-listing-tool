"""Tests for the per-instance store profile config layer (M1, multi-account plan)."""

import textwrap

import pytest

from src.utils.store_profile import (
    StoreProfile,
    StoreProfileError,
    get_store_profile,
    load_store_profile,
    reset_store_profile_cache,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_store_profile_cache()
    yield
    reset_store_profile_cache()


class TestDefaults:
    """Missing file / missing keys must reproduce current main-account values."""

    def test_missing_file_returns_main_account_defaults(self, tmp_path):
        profile = load_store_profile(tmp_path / "nope.yaml")
        assert profile.brand_name == "AquaVerve"
        assert profile.brand_tagline == "PREMIUM HOME FURNISHINGS"
        assert profile.merchant_location_key == "DAJIAN_LA_WAREHOUSE"
        assert profile.fallback_listing_policies() == {
            "fulfillmentPolicyId": "321897899021",
            "returnPolicyId": "321896608021",
            "paymentPolicyId": "321896606021",
        }
        assert profile.quality_banner_marker == "aquaverve"
        assert profile.quality_footer_marker == "california"
        assert profile.server_port == 8000
        assert profile.server_base_url == "http://localhost:8000"
        assert profile.brand_name_lower == "aquaverve"

    def test_malformed_yaml_raises_not_silent_default(self, tmp_path):
        # A present-but-broken profile must fail loud: on a sub-account the
        # defaults are another instance's brand, so silent fallback is unsafe.
        bad = tmp_path / "bad.yaml"
        bad.write_text("store: [unclosed", encoding="utf-8")
        with pytest.raises(StoreProfileError):
            load_store_profile(bad)

    def test_non_dict_yaml_raises_not_silent_default(self, tmp_path):
        weird = tmp_path / "list.yaml"
        weird.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(StoreProfileError):
            load_store_profile(weird)


class TestOverrides:
    def test_sub_account_profile_overrides(self, tmp_path):
        yaml_file = tmp_path / "autoparts.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                store:
                  brand_name: "MotorNest"
                  brand_tagline: "PERFORMANCE AUTO PARTS"
                  promotion_prefix: "MotorNest Auto Sale"
                ebay:
                  merchant_location_key: "AUTOPARTS_TX_WAREHOUSE"
                  fallback_fulfillment_policy_id: "111"
                  fallback_return_policy_id: "222"
                  fallback_payment_policy_id: "333"
                quality_gate:
                  banner_marker: "motornest"
                  footer_marker: "texas"
                server:
                  port: 8001
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.brand_name == "MotorNest"
        assert profile.brand_name_lower == "motornest"
        assert profile.merchant_location_key == "AUTOPARTS_TX_WAREHOUSE"
        assert profile.fallback_listing_policies() == {
            "fulfillmentPolicyId": "111",
            "returnPolicyId": "222",
            "paymentPolicyId": "333",
        }
        assert profile.quality_banner_marker == "motornest"
        assert profile.quality_footer_marker == "texas"
        assert profile.server_port == 8001
        assert profile.server_base_url == "http://localhost:8001"

    def test_partial_yaml_keeps_other_defaults(self, tmp_path):
        yaml_file = tmp_path / "partial.yaml"
        yaml_file.write_text('store:\n  brand_name: "MotorNest"\n', encoding="utf-8")
        profile = load_store_profile(yaml_file)
        assert profile.brand_name == "MotorNest"
        assert profile.merchant_location_key == "DAJIAN_LA_WAREHOUSE"
        assert profile.server_port == 8000

    def test_unknown_keys_ignored(self, tmp_path):
        yaml_file = tmp_path / "extra.yaml"
        yaml_file.write_text(
            "store:\n  brand_name: X\n  bogus_key: y\nnew_section:\n  a: 1\n",
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.brand_name == "X"

    def test_local_override_beats_tracked_default(self, tmp_path, monkeypatch):
        from src.utils import store_profile as sp

        local = tmp_path / "store_profile.local.yaml"
        local.write_text('store:\n  brand_name: "LocalBrand"\n', encoding="utf-8")
        monkeypatch.delenv("STORE_PROFILE_PATH", raising=False)
        monkeypatch.setattr(sp, "_LOCAL_PROFILE_PATH", local)
        assert sp.load_store_profile().brand_name == "LocalBrand"

    def test_env_var_beats_local_override(self, tmp_path, monkeypatch):
        from src.utils import store_profile as sp

        local = tmp_path / "store_profile.local.yaml"
        local.write_text('store:\n  brand_name: "LocalBrand"\n', encoding="utf-8")
        env_file = tmp_path / "env.yaml"
        env_file.write_text('store:\n  brand_name: "EnvBrand"\n', encoding="utf-8")
        monkeypatch.setattr(sp, "_LOCAL_PROFILE_PATH", local)
        monkeypatch.setenv("STORE_PROFILE_PATH", str(env_file))
        assert sp.load_store_profile().brand_name == "EnvBrand"

    def test_env_var_path_override(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "env.yaml"
        yaml_file.write_text('store:\n  brand_name: "EnvBrand"\n', encoding="utf-8")
        monkeypatch.setenv("STORE_PROFILE_PATH", str(yaml_file))
        reset_store_profile_cache()
        assert get_store_profile().brand_name == "EnvBrand"


class TestCache:
    def test_get_store_profile_is_cached(self):
        assert get_store_profile() is get_store_profile()

    def test_reset_forces_reload(self, tmp_path, monkeypatch):
        first = get_store_profile()
        yaml_file = tmp_path / "second.yaml"
        yaml_file.write_text('store:\n  brand_name: "Reloaded"\n', encoding="utf-8")
        monkeypatch.setenv("STORE_PROFILE_PATH", str(yaml_file))
        reset_store_profile_cache()
        second = get_store_profile()
        assert second.brand_name == "Reloaded"
        assert first.brand_name != second.brand_name


class TestConsumerWiring:
    """The refactored call sites must follow the profile, not hardcodes."""

    def test_description_template_uses_profile_brand(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "brand.yaml"
        yaml_file.write_text(
            'store:\n  brand_name: "MotorNest"\n  brand_tagline: "AUTO PARTS"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("STORE_PROFILE_PATH", str(yaml_file))
        reset_store_profile_cache()

        from scripts.audit_fix_active_listings import build_structured_description_from_source

        html = build_structured_description_from_source(
            "Test Product",
            "<div><h3>Product Features</h3><ul>"
            "<li>Durable steel frame supports up to 150 lbs of cargo weight.</li>"
            "<li>Weather resistant powder coating protects against rust and corrosion.</li>"
            "</ul></div>",
            {},
            {"Weight": "10 lbs"},
            {"Assembly Required": ["No"]},
        )
        assert "MOTORNEST" in html
        assert "AUTO PARTS" in html
        assert "AQUAVERVE" not in html

    def test_title_sanitizer_keeps_profile_brand(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "keep.yaml"
        yaml_file.write_text('store:\n  brand_name: "MotorNest"\n', encoding="utf-8")
        monkeypatch.setenv("STORE_PROFILE_PATH", str(yaml_file))
        reset_store_profile_cache()

        from src.utils.title_sanitizer import strip_supplier_brand_prefix

        cleaned, changed, removed = strip_supplier_brand_prefix("MotorNest Cargo Rack for Truck")
        assert cleaned == "MotorNest Cargo Rack for Truck"
        assert changed is False
        assert removed is None
