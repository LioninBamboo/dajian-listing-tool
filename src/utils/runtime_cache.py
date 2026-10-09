from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _normalize_project_root(project_root: Path | None = None) -> Path:
    return Path(project_root) if project_root is not None else PROJECT_ROOT


def get_runtime_cache_dir(*, project_root: Path | None = None) -> Path:
    cache_dir = _normalize_project_root(project_root) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_runtime_cache_path(cache_filename: str, *, project_root: Path | None = None) -> Path:
    return get_runtime_cache_dir(project_root=project_root) / str(cache_filename).strip()


def resolve_runtime_cache_path(
    cache_filename: str,
    *,
    legacy_filename: str | None = None,
    project_root: Path | None = None,
) -> Path:
    cache_path = get_runtime_cache_path(cache_filename, project_root=project_root)
    if cache_path.exists():
        return cache_path

    if legacy_filename:
        legacy_path = _normalize_project_root(project_root) / legacy_filename
        if legacy_path.exists():
            return legacy_path

    return cache_path