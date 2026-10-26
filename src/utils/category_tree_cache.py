"""On-disk eBay category-tree cache for publish path resolution.

Publish and dry-run read ``data/cache/category_tree_<id>.json``. When that
snapshot is missing or stale they refresh it in-process via read-only
``getCategoryTree`` (tree 0 for Main/GP; trees 100 and 0 for AquaRides
Motors) using the store app-token path, then write the snapshot back
atomically. Refresh failure uses a last-good snapshot (<=30 days) or
fail-closes. No scheduled task is required.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
LAST_GOOD_SNAPSHOT_SECONDS = 30 * 24 * 60 * 60
RUNTIME_REFRESH_RETRY_SECONDS = 30 * 60
DEFAULT_TREE_IDS = ("0", "100")
PATH_SEPARATOR = " > "
PRODUCTION_API_BASE = "https://api.ebay.com"
SOURCE_HOST_FIELD = "source_host"

logger = logging.getLogger(__name__)

# Process-level flatten. None means "not loaded yet".
_INDEX: Optional[dict[str, tuple[str, str]]] = None
_INDEX_BY_TREE: Optional[dict[str, dict[str, tuple[str, str]]]] = None
_INDEX_DIR: Optional[Path] = None
_INDEX_EXPIRES_AT: Optional[float] = None
_CACHE_WARNED: set[str] = set()
_RUNTIME_REFRESH_ATTEMPTED: dict[str, float] = {}
_UNAVAILABLE_TREES: set[str] = set()
_REFRESH_FAIL_WARNED = False
_clock: Callable[[], float] = time.time


def _now() -> float:
    return float(_clock())


def set_category_tree_clock(clock: Optional[Callable[[], float]] = None) -> None:
    """Tests inject a fake clock so the 30-minute retry can be advanced."""
    global _clock
    _clock = clock or time.time


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


def _invalidate_index_cache() -> None:
    global _INDEX, _INDEX_BY_TREE, _INDEX_DIR, _INDEX_EXPIRES_AT
    _INDEX = None
    _INDEX_BY_TREE = None
    _INDEX_DIR = None
    _INDEX_EXPIRES_AT = None


def reset_category_tree_cache() -> None:
    global _REFRESH_FAIL_WARNED
    _invalidate_index_cache()
    _CACHE_WARNED.clear()
    _RUNTIME_REFRESH_ATTEMPTED.clear()
    _UNAVAILABLE_TREES.clear()
    _REFRESH_FAIL_WARNED = False


def unavailable_runtime_trees() -> frozenset[str]:
    return frozenset(_UNAVAILABLE_TREES)


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


def store_runtime_tree_ids() -> tuple[str, ...]:
    """Every store refreshes both EBAY_US (0) and Motors (100)."""
    return ("0", "100")


def _warn_refresh_failed_once() -> None:
    """One run-level warning after a failed runtime refresh."""
    global _REFRESH_FAIL_WARNED
    if _REFRESH_FAIL_WARNED:
        return
    _REFRESH_FAIL_WARNED = True
    logger.warning(
        "Category tree cache missing or stale and runtime refresh failed; "
        "skipping per-row plausibility flags; category writes fail closed"
    )
    try:
        from src.utils.category_decision import warn_once_tree_unavailable

        warn_once_tree_unavailable()
    except Exception:
        pass


def _snapshot_age_seconds(path: Path, now: Optional[float] = None) -> Optional[float]:
    if not path.is_file():
        return None
    age = (now if now is not None else time.time()) - path.stat().st_mtime
    if age < 0:
        return None
    return age


def _cache_is_last_good(path: Path, now: Optional[float] = None) -> bool:
    age = _snapshot_age_seconds(path, now)
    return age is not None and age <= LAST_GOOD_SNAPSHOT_SECONDS


def _refresh_failure_reason(exc: BaseException) -> str:
    text = str(exc or "")
    if "missing EBAY_APP_ID" in text or "CERT_ID" in text:
        return "missing EBAY_APP_ID/CERT_ID"
    http = re.search(r"HTTP\s+(\d+)", text)
    if http:
        return f"{type(exc).__name__} HTTP {http.group(1)}"
    return type(exc).__name__


def _log_refresh_failure(tree_id: str, exc: BaseException) -> None:
    logger.warning(
        "Runtime category-tree refresh failed for tree %s: %s",
        tree_id,
        _refresh_failure_reason(exc),
    )


def _warn_last_good(tree_id: str, path: Path, now: Optional[float] = None) -> None:
    age = _snapshot_age_seconds(path, now) or 0.0
    logger.warning(
        "Category tree %s refresh failed; using last-good snapshot (%.1f days old) at %s",
        tree_id,
        age / 86400,
        path,
    )


def _default_runtime_refresh(tree_id: str, cache_dir: Optional[Path]) -> bool:
    """GET getCategoryTree with the store app token; write data/cache."""
    import requests

    from src.services.ebay_auth import EbayOAuthService

    if not os.getenv("EBAY_APP_ID") or not os.getenv("EBAY_CERT_ID"):
        raise RuntimeError("missing EBAY_APP_ID/CERT_ID")
    env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper()
    oauth = EbayOAuthService(env)
    api_base = getattr(oauth, "api_base", "") or PRODUCTION_API_BASE
    require_production_api_base(api_base)
    token = oauth.get_application_token()
    payload = fetch_category_tree(
        tree_id,
        session=requests.Session(),
        token=token,
        api_base=api_base,
    )
    write_category_tree_cache(tree_id, payload, cache_dir, source_host=api_base)
    return True


_runtime_refresh_impl: Callable[[str, Optional[Path]], bool] = _default_runtime_refresh


def try_runtime_refresh_tree(tree_id: str, cache_dir: Optional[Path] = None) -> bool:
    """Refresh one missing/stale tree. Mock this in tests; never hit live eBay."""
    directory = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    key = f"{directory}:{tree_id}"
    now = _now()
    last = _RUNTIME_REFRESH_ATTEMPTED.get(key)
    if last is not None and (now - last) < RUNTIME_REFRESH_RETRY_SECONDS:
        return False
    _RUNTIME_REFRESH_ATTEMPTED[key] = now
    try:
        return bool(_runtime_refresh_impl(str(tree_id), directory))
    except Exception as exc:
        _log_refresh_failure(tree_id, exc)
        return False


def _load_one_tree(
    tree_id: str,
    cache_dir: Path,
    *,
    allow_runtime_refresh: bool = True,
) -> dict[str, tuple[str, str]]:
    path = category_tree_cache_path(tree_id, cache_dir)
    owned = str(tree_id) in {str(tid) for tid in store_runtime_tree_ids()}
    needs_refresh = (not path.is_file()) or (not _cache_is_fresh(path))
    used_last_good = False
    if needs_refresh and owned:
        if allow_runtime_refresh and try_runtime_refresh_tree(tree_id, cache_dir):
            path = category_tree_cache_path(tree_id, cache_dir)
        elif _cache_is_last_good(path):
            _warn_last_good(tree_id, path)
            used_last_good = True
        else:
            if allow_runtime_refresh:
                _warn_refresh_failed_once()
            return {}
    if not path.is_file():
        if not owned:
            return {}
        _warn_cache_unavailable(tree_id, path, "missing")
        return {}
    if not _cache_is_fresh(path) and not used_last_good:
        if not owned:
            return {}
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


def _merge_flat_index(
    by_tree: Mapping[str, Mapping[str, tuple[str, str]]],
) -> dict[str, tuple[str, str]]:
    """Merged id -> (name, parent) for stored-id 4250f71 lookups.

    Tree 100 still wins collisions (same as 4250f71). New-target validation
    must use ``lookup_category_in_tree`` / ``is_cached_leaf_in_tree``.
    """
    index: dict[str, tuple[str, str]] = {}
    for tree_id in DEFAULT_TREE_IDS:
        piece = by_tree.get(str(tree_id)) or {}
        index.update(piece)
    return index


def load_category_indexes(
    cache_dir: Optional[Path] = None,
    *,
    allow_runtime_refresh: bool = True,
) -> dict[str, dict[str, tuple[str, str]]]:
    """Load trees 0 and 100 keyed by tree id so colliding ids stay distinct."""
    global _INDEX, _INDEX_BY_TREE, _INDEX_DIR, _INDEX_EXPIRES_AT
    directory = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    now = _now()
    if allow_runtime_refresh and _INDEX_BY_TREE is not None and _INDEX_DIR == directory:
        if _INDEX_EXPIRES_AT is None or now < _INDEX_EXPIRES_AT:
            return {
                tid: dict(piece) for tid, piece in _INDEX_BY_TREE.items()
            }
        _invalidate_index_cache()
    by_tree: dict[str, dict[str, tuple[str, str]]] = {}
    owned = {str(tid) for tid in store_runtime_tree_ids()}
    unavailable: set[str] = set()
    for tree_id in DEFAULT_TREE_IDS:
        piece = _load_one_tree(
            tree_id, directory, allow_runtime_refresh=allow_runtime_refresh
        )
        by_tree[str(tree_id)] = piece
        if str(tree_id) in owned and not piece:
            unavailable.add(str(tree_id))
    if not allow_runtime_refresh:
        return {tid: dict(piece) for tid, piece in by_tree.items()}
    _UNAVAILABLE_TREES.clear()
    _UNAVAILABLE_TREES.update(unavailable)
    _INDEX_BY_TREE = {tid: dict(piece) for tid, piece in by_tree.items()}
    _INDEX = _merge_flat_index(by_tree)
    _INDEX_DIR = directory
    _INDEX_EXPIRES_AT = (
        (now + RUNTIME_REFRESH_RETRY_SECONDS) if unavailable else None
    )
    return {tid: dict(piece) for tid, piece in by_tree.items()}


def load_category_index(cache_dir: Optional[Path] = None) -> dict[str, tuple[str, str]]:
    """Load tree 0 and 100 once per process into id -> (name, parent_id).

    A failed or partial refresh is cached only until the 30-minute retry
    window expires, so long-lived server/Streamlit processes re-attempt
    getCategoryTree instead of holding every publish until restart.

    Colliding ids (present in both trees) resolve to tree 100 in this flat
    view — same as 4250f71. Use ``load_category_indexes`` for tree-aware
    new-target validation.
    """
    load_category_indexes(cache_dir)
    return dict(_INDEX or {})


def lookup_category_in_tree(
    category_id: str,
    tree_id: str,
    cache_dir: Optional[Path] = None,
    indexes: Optional[Mapping[str, Mapping[str, tuple[str, str]]]] = None,
) -> Optional[tuple[str, str]]:
    cid = str(category_id or "").strip()
    tid = str(tree_id or "").strip()
    if not cid or not tid:
        return None
    trees = dict(indexes) if indexes is not None else load_category_indexes(cache_dir)
    flat = trees.get(tid) or {}
    if cid not in flat:
        return None
    name, parent = flat[cid]
    return str(name or ""), str(parent or "")


def is_cached_leaf_in_tree(
    category_id: str,
    tree_id: str,
    cache_dir: Optional[Path] = None,
    indexes: Optional[Mapping[str, Mapping[str, tuple[str, str]]]] = None,
) -> bool:
    cid = str(category_id or "").strip()
    tid = str(tree_id or "").strip()
    if not cid or not tid:
        return False
    trees = dict(indexes) if indexes is not None else load_category_indexes(cache_dir)
    flat = trees.get(tid) or {}
    if cid not in flat:
        return False
    return all(parent != cid for _name, parent in flat.values() if parent)


def colliding_category_ids(
    cache_dir: Optional[Path] = None,
    indexes: Optional[Mapping[str, Mapping[str, tuple[str, str]]]] = None,
) -> list[str]:
    trees = dict(indexes) if indexes is not None else load_category_indexes(cache_dir)
    ids0 = set(trees.get("0") or {})
    ids100 = set(trees.get("100") or {})
    return sorted(ids0 & ids100)


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
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(stamped), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # Invalidate the flattened index only. A mid-load write must not clear
    # per-tree unavailability recorded for a sibling that already failed.
    _invalidate_index_cache()
    return path


def fetch_category_tree(
    tree_id: str,
    *,
    session: Any,
    token: str,
    api_base: str,
    timeout: int = 120,
) -> Mapping[str, Any]:
    """GET get_category_tree only. Used by the refresh helper and runtime refresh."""
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
