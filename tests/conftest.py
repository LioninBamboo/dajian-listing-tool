"""Shared pytest fixtures. Category-tree lookups stay offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_TREE = Path(__file__).resolve().parent / "fixtures" / "ebay_us_category_tree_index.json"


def load_fixture_category_index() -> dict[str, tuple[str, str]]:
    raw = json.loads(FIXTURE_TREE.read_text(encoding="utf-8"))
    return {str(cid): (str(name), str(parent)) for cid, (name, parent) in raw.items()}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_category_tree: use the on-disk category-tree loader instead of the fixture index",
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
