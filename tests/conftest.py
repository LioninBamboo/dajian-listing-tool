"""Shared pytest fixtures. Category-tree lookups stay offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_TREE = Path(__file__).resolve().parent / "fixtures" / "ebay_us_category_tree_index.json"


def load_fixture_category_index() -> dict[str, tuple[str, str]]:
    raw = json.loads(FIXTURE_TREE.read_text(encoding="utf-8"))
    return {str(cid): (str(name), str(parent)) for cid, (name, parent) in raw.items()}


def _repo_root_db_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return root / "ebay_collection.db", root / "ebay_tokens.db"


def _remove_empty_repo_root_dbs() -> None:
    for path in _repo_root_db_paths():
        try:
            if path.is_file() and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass


def pytest_sessionstart(session):
    _remove_empty_repo_root_dbs()


def pytest_sessionfinish(session, exitstatus):
    _remove_empty_repo_root_dbs()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_category_tree: use the on-disk category-tree loader instead of the fixture index",
    )
    config.addinivalue_line(
        "markers",
        "allow_runtime_tree_refresh: permit mocked runtime getCategoryTree refresh",
    )


@pytest.fixture(autouse=True)
def _offline_category_tree(monkeypatch, request):
    if request.node.get_closest_marker("real_category_tree"):
        yield
        return
    index = load_fixture_category_index()
    from src.utils import category_tree_cache

    real_load = category_tree_cache.load_category_index
    default_dir = category_tree_cache.DEFAULT_CACHE_DIR

    def _load(cache_dir=None):
        if cache_dir is not None or category_tree_cache.DEFAULT_CACHE_DIR != default_dir:
            return real_load(cache_dir)
        return dict(index)

    monkeypatch.setattr(category_tree_cache, "load_category_index", _load)
    yield


@pytest.fixture(autouse=True)
def _isolate_test_databases(tmp_path, monkeypatch):
    """Keep tests from creating repo-root ebay_collection.db / ebay_tokens.db."""
    collection = tmp_path / "ebay_collection.db"
    tokens = tmp_path / "ebay_tokens.db"
    monkeypatch.setenv("DAJIAN_COLLECTION_DB", str(collection))
    import src.services.ebay_auth as ebay_auth

    monkeypatch.setattr(ebay_auth, "_TOKEN_DB_PATH", str(tokens))
    import src.services.ebay_category_validator as validator_mod

    monkeypatch.setattr(validator_mod, "default_cache_db_path", lambda: str(collection))
    try:
        import src.db.collection_db as cdb
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        url = f"sqlite:///{collection}"
        engine = create_engine(url, connect_args={"check_same_thread": False})
        monkeypatch.setattr(cdb, "DATABASE_PATH", str(collection))
        monkeypatch.setattr(cdb, "DATABASE_URL", url)
        monkeypatch.setattr(cdb, "engine", engine)
        monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=engine))
    except Exception:
        pass
    try:
        import src.services.ebay_policy_manager as epm

        monkeypatch.setattr(epm, "_POLICY_DB_PATH", str(collection))
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _no_live_taxonomy_refresh(monkeypatch, request):
    """Never call eBay Taxonomy from tests unless a test opts in and mocks it."""
    if request.node.get_closest_marker("allow_runtime_tree_refresh"):
        yield
        return
    from src.utils import category_tree_cache

    monkeypatch.setattr(category_tree_cache, "store_runtime_tree_ids", lambda: ())
    yield
