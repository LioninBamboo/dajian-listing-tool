#!/usr/bin/env python3
"""Formal CRO daily entry: price_drop + image_refresh only.

Default is dry-run: reports + lock file, zero DB / queue / eBay writes.
LIVE requires --live + --candidates + --approve-sha256 and is Main-only.
Promote cannot be enabled by CLI, env, or monkeypatching ALLOWED_ACTIONS.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.process_identity import (  # noqa: E402
    get_process_creation_marker,
    is_process_identity_alive,
)

# Structural lock: never consult a patched ALLOWED_ACTIONS for promote.
_STRUCTURAL_ALLOWED: frozenset[str] = frozenset({"price_drop", "image_refresh"})
ALLOWED_ACTIONS: frozenset[str] = frozenset({"price_drop", "image_refresh"})

DEFAULT_FORCE_SKIP_SKUS: frozenset[str] = frozenset({"W465P451141"})
CST = timezone(timedelta(hours=8))
SCHEMA = "cro_daily_improve/v1"
CANDIDATES_TTL = timedelta(hours=24)
LOCK_NAME = "cro_daily_improve.lock"
HEALTH_NAME = "_scheduler_health.json"

LIMIT_PRICE_DEFAULT = 15
LIMIT_IMAGE_DEFAULT = 8
LIMIT_PRICE_MAX = 30
LIMIT_IMAGE_MAX = 20

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DISALLOWED = 3
EXIT_OPS_LIVE = 4
EXIT_NOT_PRODUCTION = 5
EXIT_LOCK = 6
EXIT_EXPIRED = 7
EXIT_DEGRADED = 8

# Held OS-lock fds for this process. Staleness = "can acquire the OS lock".
_held_lock_fds: Dict[str, int] = {}
_held_lock_guard = threading.Lock()

STORE_EXPECTATIONS: Dict[str, Dict[str, Any]] = {
    "main": {
        "brand_name": "AquaVerve",
        "is_motors": False,
        "scheduler_profile": "full",
    },
    "gp": {
        "brand_name": "GrovePop",
        "is_motors": False,
        "scheduler_profile": "ops",
    },
    "aqua": {
        "brand_name": "AquaRides",
        "is_motors": True,
        "scheduler_profile": "ops",
    },
}

STORE_LABEL = {"main": "Main", "gp": "GrovePop", "aqua": "AquaRides"}

# Credentials / identity that belong to a store checkout. Overlay only inside
# store_root_runtime() and always restore. EBAY_ENVIRONMENT is never written.
STORE_SCOPED_CREDENTIAL_KEYS: tuple[str, ...] = (
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "EBAY_DEV_ID",
    "EBAY_REDIRECT_URI",
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
)


def effective_allowed_actions() -> frozenset[str]:
    """Intersect advertised ALLOWED_ACTIONS with the structural lock."""
    advertised = ALLOWED_ACTIONS
    if not isinstance(advertised, (set, frozenset)):
        advertised = _STRUCTURAL_ALLOWED
    return frozenset(str(x) for x in advertised if str(x) in _STRUCTURAL_ALLOWED)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cro_daily_improve.py",
        description=(
            "Formal CRO daily entry. Default is dry-run: candidates only. "
            "LIVE requires --live + --candidates + --approve-sha256. "
            "Only price_drop and image_refresh; promote is structurally impossible."
        ),
    )
    parser.add_argument("--store", required=True, choices=("main", "gp", "aqua"))
    parser.add_argument(
        "--store-root",
        dest="store_root",
        help="Read another store's DB/.env/profile (dry-run only; sqlite mode=ro).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=False)
    mode.add_argument("--live", dest="live", action="store_true", default=False)
    parser.add_argument("--candidates", help="Dry-run candidate JSON for LIVE")
    parser.add_argument(
        "--approve-sha256",
        dest="approve_sha256",
        help="Full sha256 or first 12 hex chars of --candidates",
    )
    parser.add_argument("--limit-price", type=int, default=LIMIT_PRICE_DEFAULT)
    parser.add_argument("--limit-image", type=int, default=LIMIT_IMAGE_DEFAULT)
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "reports" / "cro_daily_improve"),
    )
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.live and getattr(args, "store_root", None):
        parser.error("--live cannot be combined with --store-root")
    if args.live and (not args.candidates or not args.approve_sha256):
        parser.error("--live requires --candidates and --approve-sha256")
    return args


@dataclass
class ImproveContext:
    store: str
    profile: Any
    project_root: Path
    store_root: Path
    db_path: Path
    queue_path: Path
    out_dir: Path
    lock_path: Path
    health_path: Path
    ebay_environment: str
    profile_path: str
    now: datetime
    readonly_db: bool = True
    ebay_client: Any = None
    ebay_client_factory: Optional[Callable[[], Any]] = None
    load_products_fn: Optional[Callable[["ImproveContext"], List[Dict[str, Any]]]] = None
    load_perf_fn: Optional[Callable[["ImproveContext"], Optional[Dict[str, Any]]]] = None
    load_pending_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None
    recent_terminal_fn: Optional[Callable[..., set] ] = None
    mark_done_fn: Optional[Callable[..., int]] = None
    precheck_price_fn: Optional[Callable[..., tuple[bool, str]]] = None
    read_live_price_fn: Optional[Callable[..., Optional[float]]] = None
    write_price_fn: Optional[Callable[..., bool]] = None
    verify_price_fn: Optional[Callable[..., tuple[bool, Optional[float]]]] = None
    write_price_db_fn: Optional[Callable[..., bool]] = None
    load_local_product_fn: Optional[Callable[[str], Dict[str, Any]]] = None
    compute_new_price_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None
    scheduler_succeeded_fn: Optional[Callable[[str], bool]] = None
    send_email_fn: Optional[Callable[..., Any]] = None
    authorizer: Optional[Callable] = None
    writes: Dict[str, int] = field(default_factory=lambda: {"db": 0, "queue": 0, "ebay": 0})
    token_db_path: Optional[Path] = None
    store_env: Dict[str, str] = field(default_factory=dict)
    isolate_store_root: bool = False

    def __post_init__(self) -> None:
        if self.token_db_path is None:
            self.token_db_path = Path(self.store_root) / "ebay_tokens.db"


class StoreMismatch(Exception):
    pass


def _now_cst(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=CST)
    return now.astimezone(CST)


def git_sha(cwd: Optional[Path] = None) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd or PROJECT_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def sha256_matches(file_hex: str, approve: str) -> bool:
    got = str(file_hex or "").strip().lower()
    want = str(approve or "").strip().lower()
    if not got or not want or len(want) < 12:
        return False
    return got == want or got.startswith(want)


def pid_alive(pid: int, creation_marker: Optional[str] = None) -> bool:
    """Windows-safe liveness: OpenProcess + creation marker, never os.kill."""
    return is_process_identity_alive(pid, creation_marker)


def match_store_profile(store: str, profile: Any) -> None:
    expected = STORE_EXPECTATIONS[store]
    brand = str(getattr(profile, "brand_name", "") or "")
    sched = str(getattr(profile, "scheduler_profile", "") or "").strip().lower()
    is_motors = bool(getattr(profile, "is_motors", False))
    if (
        brand != expected["brand_name"]
        or is_motors != bool(expected["is_motors"])
        or sched != expected["scheduler_profile"]
    ):
        raise StoreMismatch(
            f"store_profile_mismatch store={store} brand={brand!r} "
            f"is_motors={is_motors} scheduler_profile={sched!r}"
        )


def is_ops_store(profile: Any) -> bool:
    return bool(getattr(profile, "is_ops_scheduler", False))


def force_skip_skus(profile: Any) -> set[str]:
    extra = getattr(profile, "cro_force_skip_skus", ()) or ()
    return {str(s).strip() for s in DEFAULT_FORCE_SKIP_SKUS} | {
        str(s).strip() for s in extra if str(s).strip()
    }


def read_dotenv_map(path: Path) -> Dict[str, str]:
    """Read a .env file into a dict. Does not touch os.environ."""
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    raw = dotenv_values(path)
    return {str(k): str(v) for k, v in raw.items() if k and v is not None}


def resolve_store_root_paths(store_root: Path, store_env: Dict[str, str]) -> tuple[Path, Path]:
    """DB + token store for a --store-root checkout (explicit, not Main's cwd)."""
    root = Path(store_root).resolve()
    db_raw = store_env.get("DAJIAN_COLLECTION_DB") or ""
    token_raw = (
        store_env.get("EBAY_TOKEN_DB")
        or store_env.get("EBAY_TOKENS_DB")
        or ""
    )
    db_path = Path(db_raw) if db_raw else (root / "ebay_collection.db")
    token_path = Path(token_raw) if token_raw else (root / "ebay_tokens.db")
    if not db_path.is_absolute():
        db_path = root / db_path
    if not token_path.is_absolute():
        token_path = root / token_path
    return db_path.resolve(), token_path.resolve()


def load_store_root_profile(store_root: Path) -> tuple[Any, str]:
    """Load profile from the target checkout. Ignores STORE_PROFILE_PATH / Main yaml."""
    from src.utils.store_profile import load_store_profile

    root = Path(store_root)
    local = root / "config" / "store_profile.local.yaml"
    tracked = root / "config" / "store_profile.yaml"
    chosen = local if local.is_file() else tracked
    return load_store_profile(chosen), str(chosen.resolve() if chosen.exists() else chosen)


@contextmanager
def store_root_runtime(ctx: ImproveContext) -> Iterator[None]:
    """Temporarily use store-root credentials. Never assigns EBAY_ENVIRONMENT."""
    if not ctx.isolate_store_root:
        yield
        return
    import src.services.ebay_auth as ebay_auth

    saved_env = {key: os.environ.get(key) for key in STORE_SCOPED_CREDENTIAL_KEYS}
    saved_token = ebay_auth._TOKEN_DB_PATH
    try:
        for key in STORE_SCOPED_CREDENTIAL_KEYS:
            value = ctx.store_env.get(key)
            if value:
                os.environ[key] = value
            else:
                # Do not inherit Main credentials when the target .env omits a key.
                os.environ.pop(key, None)
        ebay_auth._TOKEN_DB_PATH = str(ctx.token_db_path)
        yield
    finally:
        for key, old in saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        ebay_auth._TOKEN_DB_PATH = saved_token


def abort_if_promote_loaded() -> Optional[int]:
    if "scripts.cro_promote" in sys.modules:
        return EXIT_DISALLOWED
    return None


def open_db(db_path: Path, readonly: bool, authorizer: Optional[Callable] = None) -> sqlite3.Connection:
    target = Path(db_path)
    if readonly:
        uri = target.resolve().as_posix()
        conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    if authorizer is not None:
        conn.set_authorizer(authorizer)
    return conn


def _parse_json_field(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _age_days(published_at: Any, created_at: Any, now: datetime) -> int:
    raw = published_at or created_at
    if not raw:
        return 30
    try:
        dt = datetime.fromisoformat(str(raw).split("+")[0].replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return max(1, (now - dt).days)
    except Exception:
        return 30


def load_published_products(ctx: ImproveContext) -> List[Dict[str, Any]]:
    if ctx.load_products_fn is not None:
        return list(ctx.load_products_fn(ctx))
    if not ctx.db_path.exists():
        return []
    conn = open_db(ctx.db_path, readonly=True, authorizer=ctx.authorizer)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(collected_products)")}
        select = [
            "sku", "title", "price", "shipping", "suggested_price",
            "cost_breakdown", "optimization", "status", "listing_id",
            "published_at", "created_at", "images",
        ]
        optional = ["stock", "attributes", "specs"]
        for name in optional:
            if name in cols:
                select.append(name)
        rows = conn.execute(
            f"SELECT {', '.join(select)} FROM collected_products WHERE status = 'PUBLISHED'"
        ).fetchall()
    finally:
        conn.close()
    products: List[Dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        cost = _parse_json_field(rec.get("cost_breakdown"), {})
        opt = _parse_json_field(rec.get("optimization"), {})
        images = _parse_json_field(rec.get("images"), [])
        if not isinstance(images, list):
            images = []
        total_cost = 0.0
        try:
            from src.services.pricing_engine import PricingEngine

            total_cost = float(
                PricingEngine.resolve_landed_cost(
                    cost if isinstance(cost, dict) else {},
                    shipping_fallback=float(rec.get("shipping") or 0),
                    specs=_parse_json_field(rec.get("specs"), {}),
                    attributes=_parse_json_field(rec.get("attributes"), {}),
                )
                or cost.get("total_dajian_cost")
                or 0
            )
        except Exception:
            try:
                total_cost = float((cost or {}).get("total_dajian_cost") or 0)
            except Exception:
                total_cost = 0.0
        stock_present = "stock" in rec
        stock_val = rec.get("stock") if stock_present else None
        try:
            stock_int = int(stock_val) if stock_val is not None else None
        except (TypeError, ValueError):
            stock_int = None
        selling = float(rec.get("suggested_price") or 0)
        floor = 0.0
        if total_cost:
            try:
                from src.services.pricing_engine import PricingEngine

                floor = float(
                    PricingEngine.calculate_selling_price(float(total_cost), 0.15)["selling_price"]
                )
            except Exception:
                floor = 0.0
        cat_id = str((opt or {}).get("categoryId") or "")
        aspects = (opt or {}).get("item_specifics") or (opt or {}).get("aspects") or {}
        products.append({
            "sku": rec.get("sku"),
            "title": (opt.get("title") if isinstance(opt, dict) else None) or rec.get("title") or "",
            "categoryId": cat_id,
            "total_cost": total_cost,
            "selling_price": selling,
            "floor_price": floor,
            "safe_15": floor,
            "aspect_count": len(aspects) if isinstance(aspects, dict) else 0,
            "images": images,
            "image_url": images[0] if images else "",
            "listing_id": rec.get("listing_id") or "",
            "published_at": rec.get("published_at"),
            "age_days": _age_days(rec.get("published_at"), rec.get("created_at"), ctx.now),
            "stock": stock_int,
            "stock_known": stock_present and stock_val is not None and stock_int is not None,
            "cost_breakdown": cost if isinstance(cost, dict) else {},
            "impressions": 0,
            "views": 0,
            "ctr": 0,
            "conversion_rate": 0,
            "transactions": 0,
            "sold_qty": 0,
            "revenue": 0,
            "order_count": 0,
            "has_sales": False,
            "net_margin": (
                (selling - total_cost) / selling if selling > 0 else 0
            ),
        })
    return products


def load_stock_map(ctx: ImproveContext, products: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for product in products:
        sku = str(product.get("sku") or "")
        if not sku:
            continue
        if product.get("stock_known"):
            out[sku] = int(product["stock"])
        elif "stock" in product and product.get("stock") is not None:
            try:
                out[sku] = int(product["stock"])
            except (TypeError, ValueError):
                out[sku] = None
        else:
            out[sku] = None
    return out


def load_performance_readonly(ctx: ImproveContext) -> Optional[Dict[str, Any]]:
    if ctx.load_perf_fn is not None:
        return ctx.load_perf_fn(ctx)
    roots = [ctx.store_root]
    if not ctx.isolate_store_root and ctx.store_root != ctx.project_root:
        roots.append(ctx.project_root)
    elif not ctx.isolate_store_root:
        roots = [ctx.project_root]
    candidates = []
    for root in roots:
        candidates.extend((
            root / "logs" / "runtime_cache" / "performance_cache.json",
            root / "logs" / "_performance_cache.json",
            root / "_performance_cache.json",
        ))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def merge_performance(products: List[Dict[str, Any]], perf: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not perf:
        return products
    traffic = perf.get("traffic") or {}
    meta = perf.get("traffic_meta") or {}
    sales = perf.get("sales") or {}
    by_listing = sales.get("by_listing") or {}
    by_sku = sales.get("by_sku") or {}
    truncated = bool(meta.get("is_truncated"))
    api_ok = bool(meta.get("api_ok"))
    for product in products:
        lid = str(product.get("listing_id") or "")
        sku = str(product.get("sku") or "")
        row = traffic.get(lid) or {}
        product["impressions"] = int(row.get("impressions") or 0)
        product["views"] = int(row.get("views") or 0)
        product["ctr"] = float(row.get("ctr") or 0)
        product["conversion_rate"] = float(row.get("conversion_rate") or 0)
        product["transactions"] = int(row.get("transactions") or 0)
        sale = by_listing.get(lid) or by_sku.get(sku) or {}
        product["sold_qty"] = int(sale.get("qty") or sale.get("sold_qty") or 0)
        product["revenue"] = float(sale.get("revenue") or 0)
        product["order_count"] = int(sale.get("orders") or sale.get("order_count") or 0)
        product["has_sales"] = bool(product["sold_qty"] or product["transactions"])
        product["traffic_has_record"] = bool(lid and lid in traffic)
        product["traffic_coverage_limited"] = truncated
        product["traffic_api_ok"] = api_ok
    return products


def load_market_data(ctx: ImproveContext) -> Dict[str, Any]:
    roots = [ctx.store_root / "reports"]
    if not ctx.isolate_store_root and ctx.project_root != ctx.store_root:
        roots.append(ctx.project_root / "reports")
    elif not ctx.isolate_store_root:
        roots = [ctx.project_root / "reports"]
    reports: List[Path] = []
    for root in roots:
        if root.is_dir():
            reports.extend(root.glob("reprice_report_*.json"))
    if not reports:
        return {}
    latest = max(reports, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("market_research") or {}


def scheduler_task_succeeded_today(ctx: ImproveContext, task_name: str) -> bool:
    if ctx.scheduler_succeeded_fn is not None:
        return bool(ctx.scheduler_succeeded_fn(task_name))
    path = ctx.health_path
    if not path.exists():
        return False
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    info = (health.get("tasks") or {}).get(task_name) or {}
    status = str(info.get("status") or "").lower()
    at = info.get("at")
    if status not in {"success", "ok", "partial_success", "completed_with_errors"} or not at:
        return False
    try:
        last = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=ctx.now.tzinfo)
    return last.astimezone(ctx.now.tzinfo).date() == ctx.now.date()


def _parse_ts(raw: Any, default_tz: timezone) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def load_pending_allowed(ctx: ImproveContext, action: str) -> List[Dict[str, Any]]:
    if ctx.load_pending_fn is not None:
        return list(ctx.load_pending_fn(action_type=action, queue_path=ctx.queue_path))
    from src.services.cro_action_queue import load_pending

    return load_pending(action_type=action, queue_path=ctx.queue_path)


def recent_terminal(ctx: ImproveContext, hours: int) -> set[tuple[str, str]]:
    if ctx.recent_terminal_fn is not None:
        return set(ctx.recent_terminal_fn(hours=hours, queue_path=ctx.queue_path))
    from src.services.cro_action_queue import recent_terminal_keys

    return recent_terminal_keys(queue_path=ctx.queue_path, hours=hours)


def queue_is_fresh(ctx: ImproveContext, rows: Iterable[Dict[str, Any]]) -> bool:
    cutoff = ctx.now - timedelta(hours=24)
    for row in rows:
        ts = _parse_ts(row.get("enqueued_at"), ctx.now.tzinfo or CST)
        if ts is not None and ts >= cutoff:
            return True
    return False


def _lock_key(lock_path: Path) -> str:
    return str(Path(lock_path).resolve())


def _try_exclusive_os_lock(fd: int) -> bool:
    """Non-blocking exclusive lock. Windows: msvcrt.locking; POSIX: fcntl.flock."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_os_lock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def acquire_lock(lock_path: Path, now: datetime) -> Optional[int]:
    """Hold an exclusive OS lock on the file for the whole run.

    Stale = we can acquire the OS lock (previous holder died and the kernel
    released it). Never unlink-by-path: a live holder keeps the fd/lock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = _lock_key(lock_path)
    with _held_lock_guard:
        if key in _held_lock_fds:
            return EXIT_LOCK

    payload = {
        "pid": os.getpid(),
        "at": now.isoformat(),
        "name": "cro_daily_improve",
        "creation_marker": get_process_creation_marker(os.getpid()),
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b" ")
            os.fsync(fd)
        if not _try_exclusive_os_lock(fd):
            os.close(fd)
            return EXIT_LOCK
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, encoded)
        os.ftruncate(fd, len(encoded))
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    with _held_lock_guard:
        if key in _held_lock_fds:
            # Another thread in this process already holds it. Do not LOCK_UN:
            # flock is process-wide and would drop the winner's lock.
            os.close(fd)
            return EXIT_LOCK
        _held_lock_fds[key] = fd
    return None


def release_lock(lock_path: Path) -> None:
    try:
        key = _lock_key(lock_path)
    except Exception:
        return
    with _held_lock_guard:
        fd = _held_lock_fds.pop(key, None)
    if fd is None:
        return
    try:
        _unlock_os_lock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def guard_price_fail_closed(
    ctx: ImproveContext,
    sku: str,
    new_price: float,
    listing_id: Optional[str],
) -> tuple[bool, str]:
    fn = ctx.precheck_price_fn
    if fn is None:
        from src.services.repricing_guard import precheck_price

        fn = precheck_price
    try:
        ok, reason = fn(
            sku=sku,
            new_price=new_price,
            db_path=str(ctx.db_path),
            listing_id=listing_id,
            allow_ad_disable=False,
        )
    except Exception as exc:
        return False, f"guard_error:{exc}"
    reason_s = str(reason or "")
    # precheck_price fail-opens on missing cost; this CLI must fail closed (#44).
    if reason_s in {"no_cost_data", "pricing_engine_unavailable"} or reason_s.startswith("no_cost"):
        return False, f"guard_error:{reason_s or 'no_cost_data'}"
    if not ok:
        return False, reason_s or "guard_denied"
    return True, reason_s or "ok"


def price_drop_skip_reason(live_price: float, new_price: float) -> Optional[str]:
    """Shared dry-run / LIVE drop check. Only a strictly lower price is a drop."""
    if new_price >= float(live_price):
        return "not_a_drop"
    return None


def current_stock_reason(ctx: ImproveContext, sku: str) -> Optional[str]:
    products = load_published_products(ctx)
    return stock_exclude_reason(sku, load_stock_map(ctx, products))


def stock_exclude_reason(sku: str, stock_map: Dict[str, Optional[int]]) -> Optional[str]:
    if sku not in stock_map:
        return "stock_unknown"
    value = stock_map.get(sku)
    if value is None:
        return "stock_unknown"
    try:
        qty = int(value)
    except (TypeError, ValueError):
        return "stock_unknown"
    if qty <= 0:
        return "stock_zero"
    return None


def compute_new_price(
    ctx: ImproveContext,
    product: Dict[str, Any],
    live_price: Optional[float],
    market_data: Dict[str, Any],
    queue_row: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if ctx.compute_new_price_fn is not None:
        return ctx.compute_new_price_fn(product, live_price, market_data, queue_row)
    detail = (queue_row or {}).get("detail") or {}
    suggested = detail.get("suggested_price")
    if suggested is None:
        suggested = (queue_row or {}).get("suggested_price")
    if suggested is not None:
        try:
            return {
                "new_price": float(suggested),
                "floor": float(detail.get("floor_price") or product.get("floor_price") or 0),
                "strategy": str(detail.get("strategy") or queue_row.get("reason") or "queue"),
            }
        except (TypeError, ValueError):
            pass
    total_cost = float(product.get("total_cost") or 0)
    if total_cost <= 0:
        return None
    cat = str(product.get("categoryId") or "")
    bucket = market_data.get(cat) or {}
    market_avg = float(bucket.get("avg_price") or bucket.get("median") or 0)
    from src.services.pricing_engine import PricingEngine

    priced = PricingEngine.calculate_smart_price(
        total_cost=total_cost,
        market_price=market_avg,
    )
    return {
        "new_price": float(priced["final_price"]),
        "floor": float(priced["floor_price"]),
        "strategy": str(priced.get("strategy") or ""),
        "market_avg": market_avg,
    }


class ReadOnlyEbayClient:
    """Dry-run wrapper: live price + inventory GET only. Write APIs raise."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def get_inventory_item(self, sku: str) -> Any:
        return self._inner.get_inventory_item(sku)

    def get_offers_by_sku(self, sku: str, marketplace_id: Optional[str] = None) -> Any:
        return self._inner.get_offers_by_sku(sku, marketplace_id=marketplace_id)

    def get_offer(self, offer_id: str) -> Any:
        return self._inner.get_offer(offer_id)

    @property
    def oauth(self) -> Any:
        return getattr(self._inner, "oauth", None)

    def __getattr__(self, name: str) -> Any:
        def _blocked(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError(f"dry-run write blocked: {name}")

        return _blocked


def create_production_client(ctx: ImproveContext) -> Any:
    """Build the store-scoped eBay client. Caller holds store_root_runtime()."""
    from src.clients.real_ebay_client import create_real_ebay_client

    client = create_real_ebay_client(environment=ctx.ebay_environment or "SANDBOX")
    if ctx.readonly_db:
        return ReadOnlyEbayClient(client)
    return client


def fetch_live_price_readonly(ctx: ImproveContext, sku: str, listing_id: str) -> Optional[float]:
    """GET-only live price. Uses store-root credentials when isolated."""
    try:
        from scripts.batch_smart_reprice import read_current_listing_price

        with store_root_runtime(ctx):
            client = get_ebay_client(ctx)
            if client is None:
                return None
            oauth = getattr(client, "oauth", None)
            inner = getattr(client, "_inner", client)
            oauth = oauth or getattr(inner, "oauth", None)
            return read_current_listing_price(
                oauth, sku, listing_id or "", client=client, profile=ctx.profile,
            )
    except Exception:
        return None


def verify_live_price(
    ctx: ImproveContext, sku: str, new_price: float, listing_id: str,
) -> tuple[bool, Optional[float]]:
    live = fetch_live_price_readonly(ctx, sku, listing_id)
    if live is None:
        return False, None
    try:
        value = float(live)
    except (TypeError, ValueError):
        return False, None
    return abs(value - float(new_price)) <= 0.05, value


def write_price_ebay_only(
    ctx: ImproveContext, sku: str, new_price: float, listing_id: str,
) -> bool:
    """Price-only eBay write. Never calls ad APIs or update_ebay_price."""
    from scripts.batch_smart_reprice import _select_best_offer, update_trading_price
    from src.services.repricing_guard import reprice_write_channel

    with store_root_runtime(ctx):
        client = get_ebay_client(ctx)
        if client is None:
            return False
        inner = getattr(client, "_inner", client)
        oauth = getattr(client, "oauth", None) or getattr(inner, "oauth", None)
        if oauth is None:
            return False
        if reprice_write_channel(listing_id, profile=ctx.profile) == "trading":
            return bool(update_trading_price(
                oauth, sku, new_price, listing_id, profile=ctx.profile,
            ))
        getter = getattr(inner, "get_offers_by_sku", None) or getattr(client, "get_offers_by_sku", None)
        if getter is None:
            return False
        offers = getter(sku) or []
        offer = _select_best_offer(offers, expected_listing_id=listing_id)
        if not offer or not offer.get("offerId"):
            return False
        payload = dict(offer)
        pricing = dict(payload.get("pricingSummary") or {})
        price = dict(pricing.get("price") or {})
        price["value"] = str(round(float(new_price), 2))
        pricing["price"] = price
        payload["pricingSummary"] = pricing
        session = getattr(inner, "session", None)
        base = getattr(inner, "base_url", None) or "https://api.ebay.com"
        token = oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
        }
        url = f"{base.rstrip('/')}/sell/inventory/v1/offer/{offer['offerId']}"
        if session is not None:
            resp = session.put(url, headers=headers, json=payload, timeout=60)
        else:
            import requests

            resp = requests.put(url, headers=headers, json=payload, timeout=60, verify=False)
        return int(getattr(resp, "status_code", 0) or 0) in (200, 204)


def read_live_price(ctx: ImproveContext, sku: str, listing_id: str) -> Optional[float]:
    if ctx.read_live_price_fn is not None:
        return ctx.read_live_price_fn(sku, listing_id)
    return fetch_live_price_readonly(ctx, sku, listing_id)


def get_ebay_client(ctx: ImproveContext) -> Any:
    if ctx.ebay_client is not None:
        return ctx.ebay_client
    factory = ctx.ebay_client_factory
    if factory is None:
        def factory() -> Any:
            return create_production_client(ctx)
    with store_root_runtime(ctx):
        ctx.ebay_client = factory()
    return ctx.ebay_client


def load_local_product(ctx: ImproveContext, sku: str) -> Dict[str, Any]:
    if ctx.load_local_product_fn is not None:
        return ctx.load_local_product_fn(sku)
    if ctx.isolate_store_root:
        if not ctx.db_path.exists():
            return {"images": [], "title": "", "description": ""}
        conn = open_db(ctx.db_path, readonly=True, authorizer=ctx.authorizer)
        try:
            row = conn.execute(
                "SELECT images, title, description FROM collected_products WHERE sku = ?",
                (sku,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"images": [], "title": "", "description": ""}
        images = _parse_json_field(row["images"], [])
        if not isinstance(images, list):
            images = []
        return {
            "images": images,
            "title": row["title"] or "",
            "description": row["description"] or "",
        }
    from scripts.cro_image_refresh import _load_local_product

    return _load_local_product(sku)


def inspect_image(
    ctx: ImproveContext,
    sku: str,
    local: Dict[str, Any],
) -> Dict[str, Any]:
    from scripts.cro_image_refresh import (
        _should_refresh,
        build_strict_refresh_payload,
        payload_image_urls,
        urls_keep_giga_sigs,
    )

    with store_root_runtime(ctx):
        client = get_ebay_client(ctx)
        if client is None:
            return {"sku": sku, "status": "excluded", "reason": "image_get_unavailable"}
        item = client.get_inventory_item(sku)
    if not item:
        return {"sku": sku, "status": "excluded", "reason": "no inventory item"}
    product = item.get("product") or {}
    live_urls = list(product.get("imageUrls") or [])
    local_images = list(local.get("images") or [])
    need = _should_refresh(local_images, live_urls)
    payload, payload_reason = build_strict_refresh_payload(item, local)
    result = {
        "sku": sku,
        "live_n": len(live_urls),
        "local_n": len(local_images),
        "lead_changed": bool(local_images and live_urls and local_images[0] != live_urls[0]),
        "live_lead": live_urls[0] if live_urls else "",
        "live_qty": None if payload is None and payload_reason in {"qty_unknown", "qty_zero"} else (
            None if payload is None else payload.get("quantity")
        ),
    }
    if payload_reason:
        result["status"] = "excluded"
        result["reason"] = payload_reason
        return result
    if payload is not None and not urls_keep_giga_sigs(local_images, payload_image_urls(payload)):
        result["status"] = "excluded"
        result["reason"] = "image_sig_stripped"
        return result
    if not need:
        result["status"] = "excluded"
        result["reason"] = "live already healthy"
        return result
    result["status"] = "ok"
    result["reason"] = need
    result["payload_qty"] = payload.get("quantity") if payload else None
    return result


def build_read_only_context(
    store: str,
    products: List[Dict[str, Any]],
    diagnoses: List[Dict[str, Any]],
    perf: Optional[Dict[str, Any]],
    stock_map: Dict[str, Optional[int]],
) -> Dict[str, Any]:
    ads_cut: List[Dict[str, Any]] = []
    for product in products:
        try:
            margin = float(product.get("net_margin") or 0)
        except (TypeError, ValueError):
            margin = 0.0
        if float(product.get("selling_price") or 0) > 0 and margin < 0.07:
            ads_cut.append({
                "sku": product.get("sku"),
                "listing_id": product.get("listing_id"),
                "net_margin": margin,
                "reason": "profit cannot cover 5% ad + 2% buffer (report only)",
            })
    title_rows: List[Dict[str, Any]] = []
    end_rows: List[Dict[str, Any]] = []
    for diag in diagnoses:
        for action in diag.get("actions") or []:
            kind = action.get("type") or action.get("action")
            row = {
                "sku": diag.get("sku"),
                "listing_id": diag.get("listing_id"),
                "action": kind,
                "reason": action.get("reason"),
            }
            if kind in {"title_refresh", "title_rewrite", "cro_title_rewrite"}:
                title_rows.append(row)
            elif kind in {"delist", "end"}:
                end_rows.append(row)
    orders = {
        "order_count": sum(int(p.get("order_count") or 0) for p in products),
        "sold_qty": sum(int(p.get("sold_qty") or 0) for p in products),
        "revenue": round(sum(float(p.get("revenue") or 0) for p in products), 2),
    }
    meta = (perf or {}).get("traffic_meta") or {}
    traffic = {
        "api_ok": bool(meta.get("api_ok")),
        "is_truncated": bool(meta.get("is_truncated")),
        "record_count": meta.get("record_count"),
        "listings_with_traffic": sum(1 for p in products if p.get("traffic_has_record")),
        "impressions": sum(int(p.get("impressions") or 0) for p in products),
        "views": sum(int(p.get("views") or 0) for p in products),
    }
    aqua_qty = []
    if store == "aqua":
        sku = "W465P451141"
        aqua_qty.append({
            "sku": sku,
            "stock": stock_map.get(sku),
            "force_skipped": True,
            "note": "qty check report-only; SKU stays force-skipped",
        })
    return {
        "traffic_summary": traffic,
        "orders_since_last_run": orders,
        "ads_cut_candidates_report_only": ads_cut[:30],
        "title_rewrite_candidates_report_only": title_rows[:30],
        "end_candidates_report_only": end_rows[:30],
        "aqua_qty_check_report_only": aqua_qty,
    }


def empty_excluded() -> Dict[str, List[Any]]:
    return {
        "not_a_drop": [],
        "guard_error": [],
        "guard_denied": [],
        "stock_unknown": [],
        "stock_zero": [],
        "recent_edit_72h": [],
        "force_skip": [],
        "qty_unknown": [],
        "qty_zero": [],
        "image_sig_stripped": [],
        "image_skip": [],
        "limit": [],
        "live_price_unavailable": [],
        "other": [],
    }


def _exclude(excluded: Dict[str, List[Any]], bucket: str, row: Dict[str, Any]) -> None:
    excluded.setdefault(bucket, []).append(row)


def collect_seed_actions(
    ctx: ImproveContext,
    products: List[Dict[str, Any]],
    perf: Optional[Dict[str, Any]],
    market_data: Dict[str, Any],
    limit_price: int,
    limit_image: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, bool]:
    """Return (seeds, diagnoses, source, degraded)."""
    traffic_ok = bool(((perf or {}).get("traffic_meta") or {}).get("api_ok"))
    if is_ops_store(ctx.profile):
        if not traffic_ok:
            return [], [], "readonly_diagnose", True
        from src.services.cro_daily_runner import diagnose_readonly

        report = diagnose_readonly(
            products,
            market_data=market_data,
            action_types=("price_drop", "image_refresh"),
            action_quotas={"price_drop": limit_price, "image_refresh": limit_image},
            db_path=ctx.db_path,
        )
        return list(report.get("actions") or []), list(report.get("diagnoses") or []), "readonly_diagnose", False

    pending: List[Dict[str, Any]] = []
    for action in effective_allowed_actions():
        pending.extend(load_pending_allowed(ctx, action))
    daily_ok = scheduler_task_succeeded_today(ctx, "daily_tasks")
    if daily_ok and queue_is_fresh(ctx, pending):
        return pending, [], "queue", False
    if not traffic_ok:
        return [], [], "readonly_diagnose", True
    from src.services.cro_daily_runner import diagnose_readonly

    report = diagnose_readonly(
        products,
        market_data=market_data,
        action_types=("price_drop", "image_refresh"),
        action_quotas={"price_drop": limit_price, "image_refresh": limit_image},
        db_path=ctx.db_path,
    )
    return list(report.get("actions") or []), list(report.get("diagnoses") or []), "readonly_diagnose", False


def build_candidates(
    ctx: ImproveContext,
    products: List[Dict[str, Any]],
    perf: Optional[Dict[str, Any]],
    market_data: Dict[str, Any],
    limit_price: int,
    limit_image: int,
) -> tuple[List[Dict[str, Any]], Dict[str, List[Any]], List[Dict[str, Any]], str, bool]:
    by_sku = {str(p.get("sku") or ""): p for p in products}
    stock_map = load_stock_map(ctx, products)
    skip = force_skip_skus(ctx.profile)
    recent72 = recent_terminal(ctx, 72)
    seeds, diagnoses, source, degraded = collect_seed_actions(
        ctx, products, perf, market_data, limit_price, limit_image,
    )
    excluded = empty_excluded()
    for sku in sorted(skip):
        if sku in by_sku:
            _exclude(excluded, "force_skip", {
                "sku": sku, "action": "*", "reason": "cro_force_skip_skus",
            })
    if degraded:
        return [], excluded, diagnoses, source, True

    price_out: List[Dict[str, Any]] = []
    image_out: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for seed in seeds:
        action = str(seed.get("action") or "")
        sku = str(seed.get("sku") or "").strip()
        if not sku or action not in effective_allowed_actions():
            if action and action not in effective_allowed_actions():
                _exclude(excluded, "other", {"sku": sku, "action": action, "reason": "seed_not_allowed"})
            continue
        key = (sku, action)
        if key in seen:
            continue
        seen.add(key)
        if sku in skip:
            _exclude(excluded, "force_skip", {"sku": sku, "action": action, "reason": "cro_force_skip_skus"})
            continue
        if key in recent72:
            _exclude(excluded, "recent_edit_72h", {"sku": sku, "action": action})
            continue
        product = by_sku.get(sku) or {
            "sku": sku,
            "listing_id": seed.get("listing_id") or "",
            "total_cost": 0,
            "selling_price": 0,
            "floor_price": 0,
            "images": [],
            "stock_known": False,
        }
        listing_id = str(seed.get("listing_id") or product.get("listing_id") or "")
        if action == "price_drop":
            stock_reason = stock_exclude_reason(sku, stock_map)
            if stock_reason:
                _exclude(excluded, stock_reason, {"sku": sku, "action": action})
                continue
            live_price = read_live_price(ctx, sku, listing_id)
            if live_price is None:
                _exclude(excluded, "live_price_unavailable", {
                    "sku": sku, "action": action, "reason": "live_price_unavailable",
                })
                continue
            priced = compute_new_price(ctx, product, live_price, market_data, seed)
            if not priced:
                _exclude(excluded, "other", {"sku": sku, "action": action, "reason": "price_unavailable"})
                continue
            new_price = float(priced["new_price"])
            drop_reason = price_drop_skip_reason(float(live_price), new_price)
            if drop_reason:
                _exclude(excluded, drop_reason, {
                    "sku": sku,
                    "listing_id": listing_id,
                    "live_price": float(live_price),
                    "new_price": new_price,
                })
                continue
            ok, reason = guard_price_fail_closed(ctx, sku, new_price, listing_id)
            if not ok:
                bucket = "guard_error" if str(reason).startswith("guard_error") else "guard_denied"
                _exclude(excluded, bucket, {
                    "sku": sku, "listing_id": listing_id,
                    "new_price": new_price, "reason": reason,
                })
                continue
            price_out.append({
                "action": "price_drop",
                "sku": sku,
                "listing_id": listing_id,
                "live_price": float(live_price),
                "new_price": new_price,
                "floor": priced.get("floor"),
                "strategy": priced.get("strategy"),
                "guard": "ok",
                "guard_result": reason,
            })
        else:
            local = {
                "images": list(product.get("images") or []),
                "title": product.get("title") or "",
                "description": product.get("description") or "",
            }
            if not local["images"]:
                local = load_local_product(ctx, sku)
            inspected = inspect_image(ctx, sku, local)
            reason = inspected.get("reason") or ""
            if inspected.get("status") != "ok":
                bucket = reason if reason in excluded else "image_skip"
                _exclude(excluded, bucket, {"sku": sku, "action": action, **inspected})
                continue
            image_out.append({
                "action": "image_refresh",
                "sku": sku,
                "listing_id": listing_id,
                "reason": reason,
                "live_n": inspected.get("live_n"),
                "local_n": inspected.get("local_n"),
                "lead_changed": inspected.get("lead_changed"),
                "live_lead": inspected.get("live_lead"),
                "payload_qty": inspected.get("payload_qty"),
            })

    price_out = price_out[:limit_price]
    image_out = image_out[:limit_image]
    return price_out + image_out, excluded, diagnoses, source, False


def preflight_block(ctx: ImproveContext) -> Dict[str, Any]:
    profile = ctx.profile
    return {
        "store": ctx.store,
        "brand_name": str(getattr(profile, "brand_name", "") or ""),
        "is_motors": bool(getattr(profile, "is_motors", False)),
        "site_id": str(getattr(profile, "ebay_site_id", "0") or "0"),
        "marketplace": str(getattr(profile, "ebay_marketplace_id", "EBAY_US") or "EBAY_US"),
        "ebay_environment": ctx.ebay_environment,
        "git_sha": git_sha(ctx.project_root),
        "profile_path": ctx.profile_path,
        "is_ops_scheduler": is_ops_store(profile),
        "store_root": str(Path(ctx.store_root).resolve()),
        "db_path": str(Path(ctx.db_path).resolve()),
        "token_db_path": str(Path(ctx.token_db_path or (Path(ctx.store_root) / "ebay_tokens.db")).resolve()),
    }


def render_md(report: Dict[str, Any], candidates_path: Path, digest: str) -> str:
    lines: List[str] = []
    env = (report.get("preflight") or {}).get("ebay_environment")
    if env != "PRODUCTION":
        lines.append(f"⚠️ EBAY_ENVIRONMENT={env!r} — LIVE 会 exit 5")
    store = report.get("store")
    label = STORE_LABEL.get(store, store)
    n_price = sum(1 for c in report.get("candidates") or [] if c.get("action") == "price_drop")
    n_img = sum(1 for c in report.get("candidates") or [] if c.get("action") == "image_refresh")
    n_ex = sum(len(v) for v in (report.get("excluded") or {}).values() if isinstance(v, list))
    lines.append(f"{label} 候选 改价 {n_price} / 换图 {n_img}，排除 {n_ex}")
    for cand in report.get("candidates") or []:
        if cand.get("action") == "price_drop":
            lines.append(
                f"- {cand.get('sku')} price_drop ${cand.get('live_price')} → ${cand.get('new_price')}"
            )
        else:
            lines.append(
                f"- {cand.get('sku')} image_refresh {cand.get('reason')} "
                f"live_n={cand.get('live_n')} local_n={cand.get('local_n')}"
            )
    remaining = 25 - len(lines) - 2
    if remaining < 0:
        lines = lines[:23]
    lines.append(
        f"LIVE: python scripts/cro_daily_improve.py --store {store} --live "
        f"--candidates {candidates_path.as_posix()} --approve-sha256 {digest[:12]}"
    )
    lines.append(f"sha256={digest} expires_at={report.get('expires_at')}")
    return "\n".join(lines[:25]) + "\n"


def write_reports(report: Dict[str, Any], out_dir: Path, stem: str) -> tuple[Path, Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    digest = file_sha256(json_path)
    md_path.write_text(render_md(report, json_path, digest), encoding="utf-8")
    return json_path, md_path, digest


def validate_candidates_file(path: Path, approve: str, now: datetime, store: str) -> tuple[Optional[Dict[str, Any]], Optional[int]]:
    if not path.is_file():
        return None, EXIT_USAGE
    digest = file_sha256(path)
    if not sha256_matches(digest, approve):
        return None, EXIT_USAGE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, EXIT_USAGE
    if not isinstance(data, dict):
        return None, EXIT_USAGE
    if str(data.get("store") or "") != store:
        return None, EXIT_USAGE
    if str(data.get("mode") or "") != "dry":
        return None, EXIT_USAGE
    created = _parse_ts(data.get("created_at"), CST)
    expires = _parse_ts(data.get("expires_at"), CST)
    if created is None or expires is None:
        return None, EXIT_USAGE
    if now > expires or now - created > CANDIDATES_TTL:
        return None, EXIT_EXPIRED
    try:
        age_sec = max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
    except OSError:
        return None, EXIT_USAGE
    if age_sec > CANDIDATES_TTL.total_seconds():
        return None, EXIT_EXPIRED
    rows = list(data.get("candidates") or [])
    allowed = effective_allowed_actions()
    for row in rows:
        action = str(row.get("action") or "")
        if action not in allowed:
            return None, EXIT_DISALLOWED
    price_n = 0
    image_n = 0
    capped: List[Dict[str, Any]] = []
    for row in rows:
        action = str(row.get("action") or "")
        if action == "price_drop":
            if price_n >= LIMIT_PRICE_MAX:
                continue
            price_n += 1
        elif action == "image_refresh":
            if image_n >= LIMIT_IMAGE_MAX:
                continue
            image_n += 1
        capped.append(row)
    data["candidates"] = capped
    return data, None


def apply_price_live(ctx: ImproveContext, cand: Dict[str, Any]) -> Dict[str, Any]:
    sku = str((cand or {}).get("sku") or "")
    try:
        listing_id = str(cand.get("listing_id") or "")
        new_price = float(cand["new_price"])
        if sku in force_skip_skus(ctx.profile):
            return {
                "sku": sku, "action": "price_drop", "status": "skipped",
                "reason": "force_skip", "before": None, "after": None,
            }
        stock_reason = current_stock_reason(ctx, sku)
        if stock_reason:
            return {
                "sku": sku, "action": "price_drop", "status": "skipped",
                "reason": stock_reason, "before": None, "after": None,
            }
        live = read_live_price(ctx, sku, listing_id)
        if live is None:
            return {
                "sku": sku, "action": "price_drop", "status": "skipped",
                "reason": "live_price_unavailable", "before": None, "after": None,
            }
        cand_live = cand.get("live_price")
        try:
            live_delta = abs(float(live) - float(cand_live))
        except (TypeError, ValueError):
            live_delta = None
        if live_delta is None or live_delta > 0.05:
            return {
                "sku": sku, "action": "price_drop", "status": "skipped",
                "reason": "live_changed", "before": live, "after": None,
            }
        drop_reason = price_drop_skip_reason(float(live), new_price)
        if drop_reason:
            return {
                "sku": sku, "action": "price_drop", "status": "skipped",
                "reason": drop_reason, "before": live, "after": None,
            }
        ok, reason = guard_price_fail_closed(ctx, sku, new_price, listing_id)
        if not ok:
            return {
                "sku": sku, "action": "price_drop", "status": "skipped",
                "reason": reason, "before": live, "after": None,
            }
        writer = ctx.write_price_fn
        if writer is None:
            def writer(sku_: str, price_: float, listing_id_: str) -> bool:
                return write_price_ebay_only(ctx, sku_, price_, listing_id_)

        wrote = bool(writer(sku, new_price, listing_id))
        if wrote:
            ctx.writes["ebay"] += 1
        verifier = ctx.verify_price_fn
        verified = False
        readback = None
        if wrote and verifier is not None:
            verified, readback = verifier(sku, new_price, listing_id)
        elif wrote:
            readback = read_live_price(ctx, sku, listing_id)
            verified = readback is not None and abs(float(readback) - new_price) <= 0.05
        if wrote and verified:
            db_writer = ctx.write_price_db_fn
            if db_writer is not None:
                if db_writer(sku, new_price, cand):
                    ctx.writes["db"] += 1
            else:
                if _write_price_db(ctx, sku, new_price):
                    ctx.writes["db"] += 1
            return {
                "sku": sku, "action": "price_drop", "status": "done",
                "before": live, "after": readback if readback is not None else new_price,
                "readback": readback, "reason": "ok",
            }
        return {
            "sku": sku, "action": "price_drop", "status": "failed",
            "before": live, "after": readback, "reason": "write_or_verify_failed",
        }
    except Exception as exc:
        return {
            "sku": sku, "action": "price_drop", "status": "failed",
            "reason": f"error:{exc}",
        }


def _write_price_db(ctx: ImproveContext, sku: str, new_price: float) -> bool:
    conn = open_db(ctx.db_path, readonly=False, authorizer=ctx.authorizer)
    try:
        row = conn.execute(
            "SELECT cost_breakdown FROM collected_products WHERE sku = ?",
            (sku,),
        ).fetchone()
        cost = _parse_json_field(row["cost_breakdown"] if row else None, {})
        if not isinstance(cost, dict):
            cost = {}
        cost["selling_price"] = new_price
        cost["repriced_at"] = ctx.now.isoformat()
        conn.execute(
            "UPDATE collected_products SET cost_breakdown = ?, suggested_price = ? WHERE sku = ?",
            (json.dumps(cost, ensure_ascii=False), new_price, sku),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def _image_qty_skip_reason(qty_reason: Optional[str]) -> Optional[str]:
    if not qty_reason:
        return None
    if qty_reason in {"live quantity missing", "live quantity unknown"}:
        return "qty_unknown"
    return "qty_zero"


def apply_image_live(ctx: ImproveContext, cand: Dict[str, Any]) -> Dict[str, Any]:
    from scripts.cro_image_refresh import (
        _INVENTORY_IMAGE_LIMIT,
        _inventory_refresh_put_body,
        _put_inventory_refresh,
        _quantity_skip_reason,
        _readback_image_urls,
        _should_refresh,
        strip_oss_keep_giga_sig,
        urls_keep_giga_sigs,
    )

    sku = str((cand or {}).get("sku") or "")
    try:
        if sku in force_skip_skus(ctx.profile):
            return {
                "sku": sku, "action": "image_refresh", "status": "skipped",
                "reason": "force_skip",
            }
        with store_root_runtime(ctx):
            client = get_ebay_client(ctx)
            if client is None:
                return {"sku": sku, "action": "image_refresh", "status": "failed", "reason": "no_client"}
            item = client.get_inventory_item(sku)
            if not item:
                return {"sku": sku, "action": "image_refresh", "status": "skipped", "reason": "no inventory item"}
            product = item.get("product") or {}
            live_urls = list(product.get("imageUrls") or [])
            live_n = len(live_urls)
            live_lead = live_urls[0] if live_urls else ""
            if live_n != cand.get("live_n") or live_lead != (cand.get("live_lead") or ""):
                return {
                    "sku": sku, "action": "image_refresh", "status": "skipped",
                    "reason": "live_changed", "before": live_n, "after": None,
                }
            qty_skip = _image_qty_skip_reason(_quantity_skip_reason(item))
            if qty_skip:
                return {
                    "sku": sku, "action": "image_refresh", "status": "skipped",
                    "reason": qty_skip,
                }
            local = load_local_product(ctx, sku)
            local_images = list(local.get("images") or [])
            prepared = [strip_oss_keep_giga_sig(u) for u in local_images if isinstance(u, str) and u]
            prepared = prepared[:_INVENTORY_IMAGE_LIMIT]
            if not urls_keep_giga_sigs(local_images, prepared):
                return {
                    "sku": sku, "action": "image_refresh", "status": "skipped",
                    "reason": "image_sig_stripped",
                }
            need = _should_refresh(local_images, live_urls)
            if not need:
                return {
                    "sku": sku, "action": "image_refresh", "status": "skipped",
                    "reason": "live already healthy",
                }
            body = _inventory_refresh_put_body(item, prepared)
            _put_inventory_refresh(client, sku, body)
            ctx.writes["ebay"] += 1
            item2 = client.get_inventory_item(sku) or {}
            live2 = _readback_image_urls(item2)
            if len(live2) != len(prepared):
                return {
                    "sku": sku, "action": "image_refresh", "status": "failed",
                    "reason": "verification mismatch",
                    "before": live_n, "after": len(live2),
                    "expected_count": len(prepared),
                }
            return {
                "sku": sku, "action": "image_refresh", "status": "done",
                "reason": need, "before": live_n, "after": len(live2),
            }
    except Exception as exc:
        return {
            "sku": sku, "action": "image_refresh", "status": "failed",
            "reason": f"error:{exc}",
        }


def mark_done_skus(ctx: ImproveContext, rows: List[Dict[str, Any]]) -> None:
    by_action: Dict[str, List[str]] = {}
    for row in rows:
        if row.get("status") != "done":
            continue
        by_action.setdefault(str(row.get("action")), []).append(str(row.get("sku")))
    marker = ctx.mark_done_fn
    if marker is None:
        from src.services.cro_action_queue import mark_done as marker
    for action, skus in by_action.items():
        if action not in effective_allowed_actions() or not skus:
            continue
        n = int(marker(skus, action=action, queue_path=ctx.queue_path) or 0)
        ctx.writes["queue"] += n


def build_context(args: argparse.Namespace, profile: Any = None) -> ImproveContext:
    project_root = PROJECT_ROOT
    isolate = bool(getattr(args, "store_root", None))
    store_root = Path(args.store_root).resolve() if isolate else project_root
    store_env: Dict[str, str] = {}
    profile_path = ""
    if isolate:
        store_env = read_dotenv_map(store_root / ".env")
        # Preflight READ of the target store's value. Never copy it into os.environ.
        ebay_env = store_env.get("EBAY_ENVIRONMENT") or ""
        db_path, token_db_path = resolve_store_root_paths(store_root, store_env)
        if profile is None:
            profile, profile_path = load_store_root_profile(store_root)
    else:
        ebay_env = os.getenv("EBAY_ENVIRONMENT") or ""
        db_path = (store_root / "ebay_collection.db").resolve()
        token_db_path = (store_root / "ebay_tokens.db").resolve()
        if profile is None:
            from src.utils.store_profile import get_store_profile

            profile = get_store_profile()
            profile_path = str(
                os.getenv("STORE_PROFILE_PATH")
                or (project_root / "config" / "store_profile.local.yaml")
            )
    match_store_profile(args.store, profile)
    now = _now_cst()
    ctx = ImproveContext(
        store=args.store,
        profile=profile,
        project_root=project_root,
        store_root=store_root,
        db_path=db_path,
        queue_path=store_root / "logs" / "cro_action_queue.jsonl",
        out_dir=Path(args.out_dir),
        lock_path=store_root / "logs" / "locks" / LOCK_NAME,
        health_path=store_root / "logs" / HEALTH_NAME,
        ebay_environment=ebay_env,
        profile_path=profile_path,
        now=now,
        readonly_db=not bool(getattr(args, "live", False)),
        token_db_path=token_db_path,
        store_env=store_env,
        isolate_store_root=isolate,
    )
    return attach_production_io(ctx)


def attach_production_io(ctx: ImproveContext) -> ImproveContext:
    """Wire real eBay I/O on every build_context path (dry-run, LIVE, store-root)."""
    ctx.ebay_client_factory = lambda: create_production_client(ctx)
    ctx.read_live_price_fn = lambda sku, lid: fetch_live_price_readonly(ctx, sku, lid)
    ctx.write_price_fn = lambda sku, price, lid: write_price_ebay_only(ctx, sku, price, lid)
    ctx.verify_price_fn = lambda sku, price, lid: verify_live_price(ctx, sku, price, lid)
    return ctx


def run_improve(args: argparse.Namespace, ctx: ImproveContext) -> int:
    loaded = abort_if_promote_loaded()
    if loaded is not None:
        return loaded
    live = bool(getattr(args, "live", False))
    if live and getattr(args, "store_root", None):
        return EXIT_USAGE
    if live and is_ops_store(ctx.profile):
        return EXIT_OPS_LIVE
    if live and not (getattr(args, "candidates", None) and getattr(args, "approve_sha256", None)):
        return EXIT_USAGE
    if live and ctx.ebay_environment != "PRODUCTION":
        return EXIT_NOT_PRODUCTION

    lock_err = acquire_lock(ctx.lock_path, ctx.now)
    if lock_err is not None:
        return lock_err
    try:
        loaded = abort_if_promote_loaded()
        if loaded is not None:
            return loaded
        limit_price = max(0, min(int(args.limit_price), LIMIT_PRICE_MAX))
        limit_image = max(0, min(int(args.limit_image), LIMIT_IMAGE_MAX))
        stamp = ctx.now.strftime("%Y%m%d_%H%M")
        mode = "live" if live else "dry"
        run_id = f"{ctx.store}-{stamp}-{uuid.uuid4().hex[:8]}"
        preflight = preflight_block(ctx)
        if live:
            data, err = validate_candidates_file(
                Path(args.candidates), args.approve_sha256, ctx.now, ctx.store,
            )
            if err is not None:
                return err
            assert data is not None
            results: List[Dict[str, Any]] = []
            consume_ok = False
            image_ok = False
            try:
                recent24 = recent_terminal(ctx, 24)
                consume_ok = scheduler_task_succeeded_today(ctx, "cro_consume")
                image_ok = scheduler_task_succeeded_today(ctx, "cro_image_refresh")
                for cand in data.get("candidates") or []:
                    sku = str(cand.get("sku") or "")
                    action = str(cand.get("action") or "")
                    try:
                        if (sku, action) in recent24:
                            results.append({
                                "sku": sku, "action": action, "status": "skipped",
                                "reason": "recent_terminal_24h",
                            })
                            continue
                        if action == "price_drop":
                            results.append(apply_price_live(ctx, cand))
                        elif action == "image_refresh":
                            results.append(apply_image_live(ctx, cand))
                    except Exception as exc:
                        results.append({
                            "sku": sku, "action": action, "status": "failed",
                            "reason": f"error:{exc}",
                        })
            finally:
                try:
                    mark_done_skus(ctx, results)
                except Exception:
                    pass
                report = {
                    "schema": SCHEMA,
                    "run_id": run_id,
                    "store": ctx.store,
                    "mode": "live",
                    "git_sha": preflight["git_sha"],
                    "created_at": ctx.now.isoformat(),
                    "expires_at": (ctx.now + CANDIDATES_TTL).isoformat(),
                    "preflight": preflight,
                    "promote_disabled": True,
                    "allowed_actions": sorted(effective_allowed_actions()),
                    "candidates": data.get("candidates") or [],
                    "excluded": data.get("excluded") or empty_excluded(),
                    "read_only_context": data.get("read_only_context") or {},
                    "results": results,
                    "scheduler": {
                        "cro_consume_succeeded_today": consume_ok,
                        "cro_image_refresh_succeeded_today": image_ok,
                    },
                    "writes": dict(ctx.writes),
                }
                write_reports(report, ctx.out_dir, f"{ctx.store}_{stamp}_live")
        else:
            error = None
            candidates: List[Dict[str, Any]] = []
            excluded = empty_excluded()
            diagnoses: Dict[str, Any] = {}
            source = "error"
            degraded = False
            read_only: Dict[str, Any] = {}
            try:
                products = load_published_products(ctx)
                perf = load_performance_readonly(ctx)
                merge_performance(products, perf)
                market_data = load_market_data(ctx)
                candidates, excluded, diagnoses, source, degraded = build_candidates(
                    ctx, products, perf, market_data, limit_price, limit_image,
                )
                stock_map = load_stock_map(ctx, products)
                read_only = build_read_only_context(
                    ctx.store, products, diagnoses, perf, stock_map,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                degraded = True
            report = {
                "schema": SCHEMA,
                "run_id": run_id,
                "store": ctx.store,
                "mode": "dry",
                "git_sha": preflight["git_sha"],
                "created_at": ctx.now.isoformat(),
                "expires_at": (ctx.now + CANDIDATES_TTL).isoformat(),
                "preflight": preflight,
                "promote_disabled": True,
                "allowed_actions": sorted(effective_allowed_actions()),
                "candidate_source": source,
                "degraded": degraded,
                "candidates": candidates,
                "excluded": excluded,
                "read_only_context": read_only,
                "writes": {"db": 0, "queue": 0, "ebay": 0},
            }
            if error:
                report["error"] = error
            write_reports(report, ctx.out_dir, f"{ctx.store}_{stamp}_dry")
            if error:
                return EXIT_DEGRADED
        loaded = abort_if_promote_loaded()
        if loaded is not None:
            return loaded
        return EXIT_OK
    finally:
        release_lock(ctx.lock_path)


def main(argv: Optional[List[str]] = None, ctx: Optional[ImproveContext] = None) -> int:
    args = parse_args(argv)
    try:
        runtime = ctx or build_context(args)
        if ctx is not None:
            match_store_profile(args.store, runtime.profile)
    except StoreMismatch:
        return EXIT_USAGE
    return run_improve(args, runtime)


def _module_has_no_environ_production_assign(source: Optional[str] = None) -> bool:
    tree = ast.parse(source if source is not None else Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_ebay_environment_target(target):
                    return False
        if isinstance(node, ast.AugAssign) and _is_ebay_environment_target(node.target):
            return False
    return True


def _is_ebay_environment_target(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    sl = node.slice
    if isinstance(sl, ast.Constant) and sl.value == "EBAY_ENVIRONMENT":
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
