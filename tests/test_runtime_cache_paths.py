from pathlib import Path

from src.utils.runtime_cache import get_runtime_cache_path, resolve_runtime_cache_path


def test_runtime_cache_path_uses_dedicated_cache_directory(tmp_path):
    cache_path = get_runtime_cache_path("ad_data_cache.json", project_root=tmp_path)

    assert cache_path == tmp_path / "cache" / "ad_data_cache.json"
    assert cache_path.parent.exists()


def test_runtime_cache_path_prefers_legacy_file_when_new_cache_missing(tmp_path):
    legacy_path = tmp_path / "_performance_cache.json"
    legacy_path.write_text("{}", encoding="utf-8")

    resolved = resolve_runtime_cache_path(
        "performance_cache.json",
        legacy_filename="_performance_cache.json",
        project_root=tmp_path,
    )

    assert resolved == legacy_path


def test_runtime_cache_path_prefers_new_cache_file_over_legacy(tmp_path):
    new_path = get_runtime_cache_path("performance_cache.json", project_root=tmp_path)
    new_path.write_text("{}", encoding="utf-8")
    legacy_path = tmp_path / "_performance_cache.json"
    legacy_path.write_text("stale", encoding="utf-8")

    resolved = resolve_runtime_cache_path(
        "performance_cache.json",
        legacy_filename="_performance_cache.json",
        project_root=tmp_path,
    )

    assert resolved == new_path