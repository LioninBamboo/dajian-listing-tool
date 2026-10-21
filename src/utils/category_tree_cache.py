"""On-disk eBay category-tree cache for publish path resolution.

Publish and dry-run never call get_category_subtree / get_category_tree.
A separate refresh helper writes ``data/cache/category_tree_<id>.json``.
This module loads those snapshots once per process into an
``id -> (name, parent_id)`` map and walks parents in memory.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TREE_IDS = ("0", "100")
PATH_SEPARATOR = " > "
PRODUCTION_API_BASE = "https://api.ebay.com"
SOURCE_HOST_FIELD = "source_host"

logger = logging.getLogger(__name__)

# Process-level flatten. None means "not loaded yet".
_INDEX: Optional[dict[str, tuple[str, str]]] = None
_INDEX_DIR: Optional[Path] = None
_CACHE_WARNED: set[str] = set()


def normalize_api_base(api_base: str) -> str:
    return str(api_base or "").strip().rstrip("/")


def is_production_api_base(api_base: str) -> bool:
    return normalize_api_base(api_base).lower() == PRODUCTION_API_BASE


def require_production_api_base(api_base: str, *, allow_sandbox: bool = False) -> str:
    host = normalize_api_base(api_base)
    if is_production_api_base(host) or allow_sandbox:
        return host
    raise ValueError(
        "Refusing to write category tree cache from non-production host "
        f"{host or '(empty)'}; pass --allow-sandbox to override"
    )


def snapshot_source_host(payload: Mapping[str, Any]) -> str:
    return normalize_api_base(
        payload.get(SOURCE_HOST_FIELD) or payload.get("sourceHost") or ""
    )


def is_production_snapshot(payload: Mapping[str, Any]) -> bool:
    return is_production_api_base(snapshot_source_host(payload))


def category_tree_cache_path(tree_id: str, cache_dir: Optional[Path] = None) -> Path:
    directory = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    return directory / f"category_tree_{tree_id}.json"


def reset_category_tree_cache() -> None:
    global _INDEX, _INDEX_DIR
    _INDEX = None
    _INDEX_DIR = None
    _CACHE_WARNED.clear()


def flatten_category_tree(payload: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    dest: dict[str, tuple[str, str]] = {}
    root = payload.get("rootCategoryNode")
    if isinstance(root, Mapping):
        _flatten_node(root, "", dest)
    return dest


def _flatten_node(
    node: Mapping[str, Any],
    parent_id: str,
    dest: dict[str, tuple[str, str]],
) -> None:
    category = node.get("category") if isinstance(node.get("category"), Mapping) else {}
    cid = str(category.get("categoryId") or "").strip()
    name = str(category.get("categoryName") or "").strip()
    if cid:
        dest[cid] = (name, str(parent_id or ""))
    children = node.get("childCategoryTreeNodes") or []
    if not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, Mapping):
            _flatten_node(child, cid, dest)


def _cache_is_fresh(path: Path, now: Optional[float] = None) -> bool:
    if not path.is_file():
        return False
    age = (now if now is not None else time.time()) - path.stat().st_mtime
    return 0 <= age <= CACHE_TTL_SECONDS


def _warn_cache_unavailable(tree_id: str, path: Path, reason: str) -> None:
    key = f"{path}:{reason}"
    if key in _CACHE_WARNED:
        return
    _CACHE_WARNED.add(key)
    logger.warning(
        "Category tree cache %s for tree %s (%s); using blacklist-by-id",
        reason,
        tree_id,
        path,
    )


def _load_one_tree(tree_id: str, cache_dir: Path) -> dict[str, tuple[str, str]]:
    path = category_tree_cache_path(tree_id, cache_dir)
    if not path.is_file():
        _warn_cache_unavailable(tree_id, path, "missing")
        return {}
    if not _cache_is_fresh(path):
        _warn_cache_unavailable(tree_id, path, "stale")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _warn_cache_unavailable(tree_id, path, "unreadable")
        return {}
    if not isinstance(payload, Mapping):
        _warn_cache_unavailable(tree_id, path, "unreadable")
        return {}
    if not is_production_snapshot(payload):
        _warn_cache_unavailable(tree_id, path, "non-production")
        return {}
    return flatten_category_tree(payload)


def load_category_index(cache_dir: Optional[Path] = None) -> dict[str, tuple[str, str]]:
    """Load tree 0 and 100 once per process into id -> (name, parent_id)."""
    global _INDEX, _INDEX_DIR
    directory = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    if _INDEX is not None and _INDEX_DIR == directory:
        return _INDEX
    index: dict[str, tuple[str, str]] = {}
    for tree_id in DEFAULT_TREE_IDS:
        index.update(_load_one_tree(tree_id, directory))
    _INDEX = index
    _INDEX_DIR = directory
    return index


def build_category_path(
    category_id: str,
    cache_dir: Optional[Path] = None,
    index: Optional[Mapping[str, tuple[str, str]]] = None,
) -> str:
    cid = str(category_id or "").strip()
    if not cid:
        return ""
    flat = dict(index) if index is not None else load_category_index(cache_dir)
    return _path_from_flat(cid, flat)


def _path_from_flat(category_id: str, flat: Mapping[str, tuple[str, str]]) -> str:
    if category_id not in flat:
        return ""
    names: list[str] = []
    seen: set[str] = set()
    current = category_id
    while current and current not in seen:
        seen.add(current)
        name, parent_id = flat.get(current, ("", ""))
        if name and not (current == "0" and name.lower() == "root"):
            names.append(name)
        if not parent_id or parent_id == current:
            break
        current = parent_id
    if not names:
        return ""
    names.reverse()
    return PATH_SEPARATOR.join(names)


def write_category_tree_cache(
    tree_id: str,
    payload: Mapping[str, Any],
    cache_dir: Optional[Path] = None,
    *,
    source_host: Optional[str] = None,
    allow_sandbox: bool = False,
) -> Path:
    host = require_production_api_base(
        source_host or snapshot_source_host(payload) or PRODUCTION_API_BASE,
        allow_sandbox=allow_sandbox,
    )
    stamped = dict(payload)
    stamped[SOURCE_HOST_FIELD] = host
    path = category_tree_cache_path(tree_id, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamped), encoding="utf-8")
    reset_category_tree_cache()
    return path


def fetch_category_tree(
    tree_id: str,
    *,
    session: Any,
    token: str,
    api_base: str,
    timeout: int = 120,
) -> Mapping[str, Any]:
    """GET get_category_tree only. Used by the refresh helper, never publish."""
    url = f"{str(api_base).rstrip('/')}/commerce/taxonomy/v1/category_tree/{tree_id}"
    response = session.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    status = getattr(response, "status_code", None)
    if status != 200:
        raise RuntimeError(f"get_category_tree HTTP {status} for tree {tree_id}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"get_category_tree returned a non-object for tree {tree_id}")
    return payload


def refresh_category_tree_cache(
    tree_ids: Iterable[str] = DEFAULT_TREE_IDS,
    *,
    fetch: Callable[[str], Mapping[str, Any]],
    cache_dir: Optional[Path] = None,
    source_host: Optional[str] = None,
    allow_sandbox: bool = False,
) -> list[Path]:
    written: list[Path] = []
    for tree_id in tree_ids:
        payload = fetch(str(tree_id))
        written.append(
            write_category_tree_cache(
                str(tree_id),
                payload,
                cache_dir,
                source_host=source_host,
                allow_sandbox=allow_sandbox,
            )
        )
    return written


def is_cached_leaf_category(
    category_id: str,
    cache_dir: Optional[Path] = None,
    index: Optional[Mapping[str, tuple[str, str]]] = None,
) -> bool:
    """True when ``category_id`` exists in the cached tree and has no children.

    Publish/audit use this instead of get_category_subtree. A missing cache or
    unknown id is not a leaf (fail closed).
    """
    cid = str(category_id or "").strip()
    if not cid:
        return False
    flat = dict(index) if index is not None else load_category_index(cache_dir)
    if cid not in flat:
        return False
    return all(parent != cid for _name, parent in flat.values() if parent)
