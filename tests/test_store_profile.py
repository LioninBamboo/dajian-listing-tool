"""Tests for the per-instance store profile config layer (M1, multi-account plan)."""

import textwrap

import pytest

from src.utils.store_profile import (
    StoreProfile,
    StoreProfileError,
    get_store_profile,
    load_store_profile,
    peek_yaml_says_ops,
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
        # 2026-07-22: 原断言为 "california"。7/15 footer 文案改成
        # "✦ Ships from US Warehouse ✦" 后该默认值未同步,导致重建描述一律
        # has_footer=False、改写管线 100% 拒推(7/17-7/22 零推送)。默认值已改为
        # footer 中稳定不变的片段,断言随之更新。
        assert profile.quality_footer_marker == "ships from"
        assert profile.server_port == 8000
        assert profile.server_base_url == "http://localhost:8000"
        assert profile.brand_name_lower == "aquaverve"
        assert profile.sku_edit_backup_dir == ""

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
        assert profile.qc_profile == "furniture"
        assert profile.server_port == 8001
        assert profile.server_base_url == "http://localhost:8001"

    def test_sku_edit_backup_dir_yaml_key(self, tmp_path):
        yaml_file = tmp_path / "backup.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                server:
                  sku_edit_backup_dir: D:/deploy_backup
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.sku_edit_backup_dir == "D:/deploy_backup"

    def test_qc_profile_override(self, tmp_path):
        yaml_file = tmp_path / "motors.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                quality_gate:
                  qc_profile: motors
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.qc_profile == "motors"

    def test_scheduler_profile_ops_override(self, tmp_path):
        yaml_file = tmp_path / "ops.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                server:
                  scheduler_profile: ops
                  scheduler_mutex_name: Global\\GrovePopSchedulerDaemonMutex
                store:
                  brand_name: GrovePop
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.scheduler_profile == "ops"
        assert profile.is_ops_scheduler is True
        assert profile.resolved_scheduler_mutex_name() == "Global\\GrovePopSchedulerDaemonMutex"
        assert profile.scheduler_enable_qc_autofix is False
        assert profile.scheduler_enable_ads is False
        assert profile.scheduler_enable_cro_promote is False
        assert profile.ops_qc_autofix_enabled is False
        assert profile.ops_ads_enabled is False
        assert profile.cro_promote_scheduler_enabled is False
        assert profile.resolved_scheduler_allowed_jobs == frozenset({"ops_daily", "order_recheck"})
        assert profile.job_is_allowed("ops_daily") is True
        assert profile.job_is_allowed("smart_reprice") is False

    def test_ops_allowlist_unknown_jobs_fail_closed(self, tmp_path):
        yaml_file = tmp_path / "ops.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                server:
                  scheduler_profile: ops
                  scheduler_allowed_jobs:
                    - not-a-real-job
                    - ops_daily
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.resolved_scheduler_allowed_jobs == frozenset({"ops_daily", "order_recheck"})

    def test_full_profile_allowlist_is_unrestricted(self):
        profile = load_store_profile()
        assert profile.scheduler_profile == "full"
        assert profile.resolved_scheduler_allowed_jobs is None
        assert profile.job_is_allowed("smart_reprice") is True

    def test_full_profile_ignores_explicit_allowlist(self):
        profile = StoreProfile(
            scheduler_profile="full",
            scheduler_allowed_jobs=("ops_daily", "order_recheck"),
        )
        assert profile.resolved_scheduler_allowed_jobs is None
        assert profile.job_is_allowed("listing_audit") is True
        assert profile.job_is_allowed("smart_reprice") is True

    def test_unknown_non_ops_profile_stays_unrestricted(self):
        profile = StoreProfile(scheduler_profile="fullish", scheduler_allowed_jobs=("ops_daily",))
        assert profile.is_ops_scheduler is False
        assert profile.resolved_scheduler_allowed_jobs is None
        assert profile.job_is_allowed("daily_tasks") is True

    def test_ops_rejects_blank_job_names(self):
        profile = StoreProfile(scheduler_profile="ops")
        assert profile.job_is_allowed("") is False
        assert profile.job_is_allowed(None) is False

    def test_ops_allowlist_from_comma_string(self, tmp_path):
        yaml_file = tmp_path / "ops.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                server:
                  scheduler_profile: ops
                  scheduler_allowed_jobs: "ops_daily, order_recheck"
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.resolved_scheduler_allowed_jobs == frozenset({"ops_daily", "order_recheck"})

    def test_peek_yaml_says_ops(self, tmp_path):
        ops = tmp_path / "ops.yaml"
        ops.write_text("server:\n  scheduler_profile: ops\n", encoding="utf-8")
        broken = tmp_path / "broken.yaml"
        broken.write_text("server: [\n  scheduler_profile: ops\n", encoding="utf-8")
        full = tmp_path / "full.yaml"
        full.write_text("server:\n  scheduler_profile: full\n", encoding="utf-8")
        assert peek_yaml_says_ops(ops) is True
        assert peek_yaml_says_ops(broken) is True
        assert peek_yaml_says_ops(full) is False

    def test_peek_yaml_says_ops_quoted_whitespace_and_flow(self, tmp_path):
        quoted_space = tmp_path / "quoted_space.yaml"
        quoted_space.write_text(
            'server:\n  scheduler_profile: "OPS "\nstore: [unclosed\n',
            encoding="utf-8",
        )
        single = tmp_path / "single.yaml"
        single.write_text(
            "server:\n  scheduler_profile: 'ops'\nstore: [unclosed\n",
            encoding="utf-8",
        )
        flow = tmp_path / "flow.yaml"
        flow.write_text(
            "server: {scheduler_profile: ops, scheduler_enable_ads: true\n",
            encoding="utf-8",
        )
        flow_quoted = tmp_path / "flow_quoted.yaml"
        flow_quoted.write_text(
            'server: {scheduler_profile: "OPS "}\nstore: [unclosed\n',
            encoding="utf-8",
        )
        comment_only = tmp_path / "comment.yaml"
        comment_only.write_text(
            "server:\n  scheduler_profile: full  # scheduler_profile: ops\n",
            encoding="utf-8",
        )
        ops_daily = tmp_path / "ops_daily.yaml"
        ops_daily.write_text(
            "server:\n  scheduler_allowed_jobs: [ops_daily]\n",
            encoding="utf-8",
        )
        assert peek_yaml_says_ops(quoted_space) is True
        assert peek_yaml_says_ops(single) is True
        assert peek_yaml_says_ops(flow) is True
        assert peek_yaml_says_ops(flow_quoted) is True
        assert peek_yaml_says_ops(comment_only) is False
        assert peek_yaml_says_ops(ops_daily) is False

    def test_ops_optional_slices_default_off_and_can_opt_in(self, tmp_path):
        yaml_file = tmp_path / "ops_slices.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                server:
                  scheduler_profile: ops
                  scheduler_enable_qc_autofix: true
                  scheduler_enable_ads: true
                store:
                  brand_name: GrovePop
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.scheduler_enable_qc_autofix is True
        assert profile.scheduler_enable_ads is True
        assert profile.ops_qc_autofix_enabled is True
        assert profile.ops_ads_enabled is True

    def test_full_profile_ignores_ops_slice_flags(self, tmp_path):
        yaml_file = tmp_path / "full_slices.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                server:
                  scheduler_profile: full
                  scheduler_enable_qc_autofix: true
                  scheduler_enable_ads: true
                """
            ),
            encoding="utf-8",
        )
        profile = load_store_profile(yaml_file)
        assert profile.scheduler_enable_qc_autofix is True
        assert profile.is_ops_scheduler is False
        assert profile.ops_qc_autofix_enabled is False
        assert profile.ops_ads_enabled is False

    def test_v2_listing_and_pricing_sections(self, tmp_path):
        yaml_file = tmp_path / "blindbox.yaml"
        yaml_file.write_text(
            textwrap.dedent(
                """
                listing:
                  template_style: "arttoy_hype"
                  banned_terms:
                    - POP MART
                    - Original
                    - Genuine
                pricing:
                  pricing_strategy: "undercut"
                  undercut_pct: 0.05
                  undercut_min_abs: 0.5
                  price_ends_99: true
                  shipping_model: "free"
                  fixed_shipping_amount: 8.99
                """
            ),
            encoding="utf-8",
        )
        p = load_store_profile(yaml_file)
        assert p.template_style == "arttoy_hype"
        assert p.banned_terms == ("POP MART", "Original", "Genuine")
        assert p.banned_terms_lower == ("pop mart", "original", "genuine")
        assert p.pricing_strategy == "undercut"
        assert p.undercut_pct == 0.05
        assert p.undercut_min_abs == 0.5
        assert p.price_ends_99 is True
        assert p.shipping_model == "free"
        assert p.fixed_shipping_amount == 8.99

    def test_v2_defaults_keep_existing_instances_unchanged(self):
        p = StoreProfile()
        assert p.template_style == "furniture_classic"
        assert p.banned_terms == ()
        assert p.pricing_strategy == "cost_plus"
        assert p.price_ends_99 is False
        assert p.force_house_brand is True
        assert p.default_brand == p.brand_name  # generic goods stamp house brand

    def test_force_house_brand_false_defaults_to_unbranded(self, tmp_path):
        yaml_file = tmp_path / "arttoy.yaml"
        yaml_file.write_text(
            "store:\n  brand_name: GrovePop\n"
            "listing:\n  template_style: arttoy_hype\n  force_house_brand: false\n",
            encoding="utf-8",
        )
        p = load_store_profile(yaml_file)
        assert p.force_house_brand is False
        assert p.brand_name == "GrovePop"          # store identity kept
        assert p.default_brand == "Unbranded"       # but NOT stamped on products

    def test_banned_terms_from_comma_string(self, tmp_path):
        yaml_file = tmp_path / "csv.yaml"
        yaml_file.write_text(
            'listing:\n  banned_terms: "POP MART, Original, OEM"\n', encoding="utf-8"
        )
        p = load_store_profile(yaml_file)
        assert p.banned_terms == ("POP MART", "Original", "OEM")

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
