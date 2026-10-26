"""Plan, gate, apply, and read back single-SKU field edits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.db.collection_db import DATABASE_PATH, MAINTENANCE_PATH
from src.db.collection_models import _utcnow_naive
from src.db.database_safety import (
    DatabaseSafetyError,
    assert_runtime_not_in_maintenance,
    assert_single_link_database,
)
from src.services.live_listing_writes import (
    _put_inventory_product_only,
    _put_inventory_snapshot,
    _update_offer_description_only,
    compress_html,
    get_category_matcher,
)
from src.utils.html_truncator import smart_truncate_html
from src.utils.approved_category import (
    is_valid_category_id,
    locked_category_write_error,
    mark_category_approved,
)
from src.utils.category_tree_cache import (
    is_cached_leaf_category,
    is_cached_leaf_in_tree,
    load_category_indexes,
    lookup_category_in_tree,
)
from src.utils.claim_strip import (
    OFFER_GET_ONLY_KEYS,
    STATUS_ALREADY_ABSENT,
    STATUS_PROTECTED_ZONE,
    STATUS_WILL_STRIP,
    aspect_values,
    claim_matches_text,
    claim_pattern,
    claim_still_in_unprotected,
    collapse_ws,
    fetches_price_or_qty_changed,
    find_aspect_key,
    is_features_aspect,
    is_required_aspect,
    live_category_id,
    live_price,
    live_quantity,
    normalize_entities,
    offer_category_only_put_payload,
    strip_claim_from_aspects,
    strip_claim_from_description,
    strip_html_to_text,
    ul_would_be_empty,
)
from src.utils.db_backup import create_online_backup
from src.utils.publish_fee_preview import guard_category_write_high_fee
from src.utils.store_profile import get_store_profile
from src.utils.task_worker import _acquire_worker_lock, _lock_is_stale
from src.utils.text_deletion_guard import (
    deletion_only_aspects,
    deletion_only_text,
    ordered_subsequence,
    tokenize_text,
)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_RESTORED = 3
EXIT_RESTORE_FAILED = 4
EXIT_DRIFT = 5

MAX_SKUS = 50
PRODUCTION_API = "https://api.ebay.com"

STORE_EXPECTATIONS: dict[str, dict[str, str]] = {
    "main": {"brand_name": "AquaVerve", "listing_channel": "inventory"},
    "gp": {"brand_name": "GrovePop", "listing_channel": "inventory"},
    "aqua": {
        "brand_name": "AquaRides",
        "store_kind": "auto",
        "listing_channel": "trading",
        "ebay_site_id": "100",
    },
}

LISTING_WRITER_TASKS = frozenset(
    {
        "daily_tasks",
        "auto_publish",
        "ops_daily",
        "listing_audit",
        "source_aspect_autofix",
        "semantic_rewrite",
        "missing_video_autofix",
        "cro_fill_specifics",
        "cro_title_rewrite",
        "cro_image_refresh",
        "smart_reprice",
        "source_refresh",
        "listing_status_sync",
    }
)
WRITER_TASK_TIMEOUT = {
    "daily_tasks": 14400,
    "auto_publish": 3600,
    "ops_daily": 1800,
    "listing_audit": 10800,
    "source_aspect_autofix": 7200,
    "semantic_rewrite": 7200,
    "missing_video_autofix": 7200,
    "cro_fill_specifics": 1800,
    "cro_title_rewrite": 900,
    "cro_image_refresh": 1800,
    "smart_reprice": 7200,
    "source_refresh": 1800,
    "listing_status_sync": 1800,
}

EDITABLE_OPT_KEYS = frozenset(
    {
        "title",
        "description",
        "aspects",
        "categoryId",
        "categoryName",
        "categoryApproved",
        "categoryApprovedId",
    }
)
QTY_PRICE_FIELDS = frozenset(
    {
        "stock",
        "quantity",
        "availableQuantity",
        "availability",
        "price",
        "StartPrice",
        "suggested_price",
    }
)
NEVER_EDITABLE_FIELDS = frozenset(
    {
        "price",
        "stock",
        "shipping",
        "cost_breakdown",
        "suggested_price",
        "status",
        "listing_id",
        "published_at",
        "images",
        "videos",
        "attributes",
        "specs",
        "url",
        "sku",
        "motorsCompatibility",
        "quantity",
        "availableQuantity",
        "availability",
        "StartPrice",
    }
)
PLANNED_OPT_KEYS = (
    "description",
    "title",
    "aspects",
    "categoryId",
    "categoryName",
    "categoryApproved",
    "categoryApprovedId",
)
_MEASUREMENT_TAIL_RE = re.compile(
    r"(?:\s+\d+(?:\.\d+)?\s*(?:lbs?|kg|in|cm|ft|oz|mm)\b)+\s*$",
    re.I,
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class EditOp:
    kind: str
    text: str = ""
    location: str = ""
    aspect_name: str = ""
    aspect_value: str = ""
    field_name: str = ""
    path: str = ""
    category_id: str = ""
    raw: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class GateVerdict:
    name: str
    ok: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "reason": self.reason}


@dataclass
class SkuFieldEditContext:
    db_path: Path
    store: str
    profile: Any
    clock: Callable[[], datetime] = _utcnow_naive
    sleep: Callable[[float], None] = time.sleep
    backup_root: Path | None = None
    ebay_client: Any = None
    trading_client: Any = None
    category_index: Mapping[str, tuple[str, str]] | None = None
    category_indexes: Mapping[str, Mapping[str, tuple[str, str]]] | None = None
    required_aspect_loader: Callable[[str | None], list[str] | None] | None = None
    high_fee_blacklist_path: str | Path | None = None
    lock_dir: Path | None = None
    task_lock_dir: Path | None = None
    maintenance_path: Path | None = None
    create_backup: Callable[..., Any] | None = None
    acquire_lock: Callable[..., Any] | None = None
    writer_timeouts: Mapping[str, int] | None = None
    lock_is_stale: Callable[..., bool] | None = None


@dataclass
class SkuPlan:
    sku: str
    listing_id: str = ""
    row: dict[str, Any] = dc_field(default_factory=dict)
    row_sha256: str = ""
    ops: list[dict[str, Any]] = dc_field(default_factory=list)
    gates: list[GateVerdict] = dc_field(default_factory=list)
    diff: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    optimization_before: dict[str, Any] = dc_field(default_factory=dict)
    optimization_after: dict[str, Any] = dc_field(default_factory=dict)
    status: str = "planned"
    refuse_reason: str = ""
    live: dict[str, Any] = dc_field(default_factory=dict)
    live_after: dict[str, Any] = dc_field(default_factory=dict)
    qty_zero_preserved: bool = False
    db_changed: bool = False
    live_changed: bool = False
    db_readback: dict[str, Any] = dc_field(default_factory=dict)
    live_readback: dict[str, Any] = dc_field(default_factory=dict)


class ToolRefused(Exception):
    def __init__(self, reason: str, exit_code: int = EXIT_REFUSED):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_sha256(row: Mapping[str, Any]) -> str:
    payload = {
        "optimization": _as_opt(row.get("optimization")),
        "status": row.get("status"),
        "listing_id": row.get("listing_id"),
        "price": row.get("price"),
        "stock": row.get("stock"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_opt(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw) if raw else {}
    return deepcopy(dict(raw or {}))


def _as_logs(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
    if isinstance(raw, list):
        return list(raw)
    return [raw]


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    raw_opt = data.get("optimization")
    raw_logs = data.get("logs")
    data["_optimization_raw"] = (
        raw_opt if isinstance(raw_opt, str) else json.dumps(_as_opt(raw_opt), ensure_ascii=False)
    )
    if isinstance(raw_logs, str):
        data["_logs_raw"] = raw_logs
    else:
        data["_logs_raw"] = json.dumps(_as_logs(raw_logs), ensure_ascii=False)
    data["optimization"] = _as_opt(raw_opt)
    data["logs"] = _as_logs(raw_logs)
    return data


def title_head_noun(title: str) -> str:
    try:
        from src.utils.product_context_signals import first_product_noun

        noun = first_product_noun(title)
        if noun:
            return str(noun).casefold()
    except Exception:
        pass
    cleaned = _MEASUREMENT_TAIL_RE.sub("", title or "").strip()
    tokens = tokenize_text(cleaned)
    if len(tokens) >= 2:
        return " ".join(tokens[-2:])
    return tokens[-1] if tokens else ""


def head_noun_kept(before: str, after: str) -> bool:
    head = title_head_noun(before)
    if not head:
        return True
    ok, _ = ordered_subsequence(tokenize_text(after), tokenize_text(head))
    return ok


def title_would_degrade(before: str, after: str) -> bool:
    return len(after or "") < 20 or not head_noun_kept(before, after)


def store_tree_id(store: str) -> str:
    return "100" if store == "aqua" else "0"


def listing_writer_timeouts() -> dict[str, int]:
    return {name: int(WRITER_TASK_TIMEOUT[name]) for name in LISTING_WRITER_TASKS}


def _task_lock_stale(lock_info: Mapping[str, Any], timeout_sec: int) -> bool:
    return bool(_lock_is_stale(dict(lock_info), int(timeout_sec), datetime.now()))


def parse_location(location: str) -> tuple[str, str]:
    loc = str(location or "").strip()
    if loc.lower().startswith("aspect:"):
        return "aspect", loc.split(":", 1)[1].strip()
    return loc.lower(), ""


def parse_name_value(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise ToolRefused("field_not_editable")
    name, value = spec.split("=", 1)
    return name.strip(), value.strip()


def parse_cli_ops(raw_ops: Iterable[Mapping[str, Any]]) -> list[EditOp]:
    ops: list[EditOp] = []
    for item in raw_ops:
        kind = str(item.get("kind") or "")
        if kind == "strip_claim":
            location = str(item.get("location") or "").strip()
            if not location:
                raise ToolRefused("field_not_editable")
            loc_kind, aspect_name = parse_location(location)
            if loc_kind not in {"description", "features", "title", "all", "aspect"}:
                raise ToolRefused("field_not_editable")
            ops.append(
                EditOp(
                    kind="strip_claim",
                    text=str(item.get("text") or ""),
                    location=loc_kind,
                    aspect_name=aspect_name,
                    raw=dict(item),
                )
            )
        elif kind == "remove_aspect_value":
            name, value = parse_name_value(str(item.get("spec") or ""))
            ops.append(EditOp(kind="remove_aspect_value", aspect_name=name, aspect_value=value, raw=dict(item)))
        elif kind == "remove_aspect":
            ops.append(EditOp(kind="remove_aspect", aspect_name=str(item.get("name") or ""), raw=dict(item)))
        elif kind == "set_from_file":
            field_name, path = parse_name_value(str(item.get("spec") or ""))
            ops.append(EditOp(kind="set_from_file", field_name=field_name, path=path, raw=dict(item)))
        elif kind == "set_category":
            ops.append(EditOp(kind="set_category", category_id=str(item.get("category_id") or ""), raw=dict(item)))
        else:
            raise ToolRefused("field_not_editable")
    return ops


def parse_manifest_ops(raw_ops: Iterable[Mapping[str, Any]]) -> list[EditOp]:
    ops: list[EditOp] = []
    for item in raw_ops:
        kind = str(item.get("op") or item.get("kind") or "").replace("-", "_")
        if kind == "strip_claim":
            location = str(item.get("location") or "")
            loc_kind, aspect_name = parse_location(location)
            ops.append(
                EditOp(
                    kind="strip_claim",
                    text=str(item.get("text") or item.get("claim") or ""),
                    location=loc_kind,
                    aspect_name=aspect_name or str(item.get("aspect") or ""),
                    raw=dict(item),
                )
            )
        elif kind == "remove_aspect_value":
            if item.get("spec"):
                name, value = parse_name_value(str(item.get("spec")))
            else:
                name = str(item.get("name") or item.get("aspect") or "")
                value = str(item.get("value") or "")
            ops.append(EditOp(kind="remove_aspect_value", aspect_name=name, aspect_value=value, raw=dict(item)))
        elif kind == "remove_aspect":
            ops.append(
                EditOp(
                    kind="remove_aspect",
                    aspect_name=str(item.get("name") or item.get("aspect") or ""),
                    raw=dict(item),
                )
            )
        elif kind == "set_from_file":
            field_name = str(item.get("field") or "")
            path = str(item.get("path") or "")
            if not field_name and item.get("spec"):
                field_name, path = parse_name_value(str(item.get("spec")))
            ops.append(EditOp(kind="set_from_file", field_name=field_name, path=path, raw=dict(item)))
        elif kind == "set_category":
            ops.append(
                EditOp(
                    kind="set_category",
                    category_id=str(item.get("category_id") or item.get("categoryId") or ""),
                    raw=dict(item),
                )
            )
        else:
            field_name = str(item.get("field") or kind)
            if field_name in NEVER_EDITABLE_FIELDS or field_name in QTY_PRICE_FIELDS:
                raise ToolRefused("qty_price_not_editable")
            raise ToolRefused("field_not_editable")
    return ops


def collect_skus(args: Any) -> list[str]:
    if getattr(args, "sku", None):
        skus = [str(args.sku).strip()]
    elif getattr(args, "skus", None):
        skus = [part.strip() for part in str(args.skus).split(",") if part.strip()]
    elif getattr(args, "skus_file", None):
        text = Path(args.skus_file).read_text(encoding="utf-8")
        skus = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    else:
        raise ToolRefused("field_not_editable")
    if not skus:
        raise ToolRefused("field_not_editable")
    if len(skus) > MAX_SKUS:
        raise ToolRefused("too_many_skus")
    return skus


def match_store_profile(store: str, profile: Any) -> None:
    expected = STORE_EXPECTATIONS[store]
    actual = {key: str(getattr(profile, key, "") or "") for key in expected}
    wanted = {key: str(value) for key, value in expected.items()}
    if actual != wanted:
        raise ToolRefused(f"store_profile_mismatch: {actual}")


def resolve_backup_root(args: Any, profile: Any, override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    if getattr(args, "backup_dir", None):
        return Path(args.backup_dir)
    profile_dir = str(getattr(profile, "sku_edit_backup_dir", "") or "").strip()
    if profile_dir:
        return Path(profile_dir)
    return (_REPO_ROOT / ".." / "deploy_backup").resolve()


def connect_db(ctx: SkuFieldEditContext) -> sqlite3.Connection:
    try:
        assert_runtime_not_in_maintenance(ctx.maintenance_path or MAINTENANCE_PATH)
        assert_single_link_database(ctx.db_path)
    except DatabaseSafetyError as exc:
        raise ToolRefused(str(exc)) from exc
    conn = sqlite3.connect(str(ctx.db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def load_row(conn: sqlite3.Connection, sku: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM collected_products WHERE sku = ?", (sku,))
    row = cur.fetchone()
    return _row_dict(row) if row is not None else None


def add_gate(plan: SkuPlan, name: str, ok: bool, reason: str = "") -> None:
    plan.gates.append(GateVerdict(name, ok, reason))


def refuse(plan: SkuPlan, name: str, reason: str) -> SkuPlan:
    add_gate(plan, name, False, reason)
    plan.status = "refused"
    plan.refuse_reason = reason
    return plan


def sku_edit_required_aspect_names(category_id: str | None) -> list[str] | None:
    """Return required aspect names. ``[]`` means Taxonomy 200 with none.

    ``None`` is fail-closed. ``_get_category_aspects`` returns ``([], [])`` on
    token failure and ``_fallback_aspects`` on non-200; those must not look
    like a successful empty required list. The matcher's ``_aspects_cache`` is
    written only after a 200 parse.
    """
    if not category_id:
        return None
    try:
        matcher = get_category_matcher()
        cid = str(category_id)
        required_aspects, _ = matcher._get_category_aspects(cid)
        if cid not in getattr(matcher, "_aspects_cache", {}):
            return None
        return [aspect.get("name") for aspect in required_aspects if aspect.get("name")]
    except Exception:
        return None


def required_aspect_names(ctx: SkuFieldEditContext, category_id: str | None) -> tuple[list[str], bool]:
    loader = ctx.required_aspect_loader or sku_edit_required_aspect_names
    try:
        loaded = loader(category_id)
    except Exception:
        return [], False
    if loaded is None:
        return [], False
    return list(loaded), True


def _normalize_aspects(raw: Any) -> dict[str, list[str]]:
    return {str(key): [str(item) for item in aspect_values(value)] for key, value in dict(raw or {}).items()}


def _drop_aspect_value(
    aspects: dict[str, list[str]],
    name: str,
    value: str,
    required_names: Iterable[str],
    required_available: bool,
) -> tuple[dict[str, list[str]] | None, str]:
    key = find_aspect_key(aspects, name)
    if key is None:
        return aspects, STATUS_ALREADY_ABSENT
    kept = [item for item in aspects[key] if item != value and item.casefold() != value.casefold()]
    if kept == aspects[key]:
        return aspects, STATUS_ALREADY_ABSENT
    if not kept:
        if not is_features_aspect(key) and (not required_available or is_required_aspect(key, required_names)):
            return None, "required_aspect_would_empty"
        out = dict(aspects)
        out.pop(key, None)
        return out, STATUS_WILL_STRIP
    out = dict(aspects)
    out[key] = kept
    return out, STATUS_WILL_STRIP


def _strip_claim_from_aspect_values(
    aspects: dict[str, list[str]],
    name: str,
    claim: str,
    required_names: Iterable[str],
    required_available: bool,
) -> tuple[dict[str, list[str]] | None, str]:
    key = find_aspect_key(aspects, name) if name else None
    if name and key is None:
        return aspects, STATUS_ALREADY_ABSENT
    targets = [key] if key else list(aspects)
    changed = False
    out = dict(aspects)
    for aspect_key in targets:
        if name == "" and not is_features_aspect(aspect_key) and not claim_matches_text(claim, " ".join(out.get(aspect_key, []))):
            # location=all on non-features: still strip matching values
            pass
        values = list(out.get(aspect_key) or [])
        kept = [item for item in values if not claim_matches_text(claim, item)]
        if kept == values:
            continue
        if not kept:
            if not is_features_aspect(aspect_key) and (
                not required_available or is_required_aspect(aspect_key, required_names)
            ):
                return None, "required_aspect_would_empty"
            out.pop(aspect_key, None)
        else:
            out[aspect_key] = kept
        changed = True
    return (out, STATUS_WILL_STRIP if changed else STATUS_ALREADY_ABSENT)


def _strip_title(title: str, claim: str) -> tuple[str, str]:
    if not claim_matches_text(claim, title or ""):
        return title, STATUS_ALREADY_ABSENT
    pattern = claim_pattern(claim)
    if pattern is None:
        return title, STATUS_ALREADY_ABSENT
    after = pattern.sub("", title or "")
    after = re.sub(r"\s+", " ", after).strip(" \t-|,;:")
    if title_would_degrade(title, after):
        return title, "title_would_degrade"
    return after, STATUS_WILL_STRIP


def apply_ops(
    plan: SkuPlan,
    ops: list[EditOp],
    ctx: SkuFieldEditContext,
    args: Any,
) -> SkuPlan:
    opt = deepcopy(plan.optimization_before)
    aspects = _normalize_aspects(opt.get("aspects"))
    description = str(opt.get("description") or "")
    title = str(opt.get("title") or "")
    category_id = str(opt.get("categoryId") or "")
    changed = False
    removed_desc_units: list[str] = []
    removed_title_units: list[str] = []
    required_names, required_available = required_aspect_names(ctx, category_id)

    for op in ops:
        plan.ops.append({"kind": op.kind, **{k: v for k, v in op.raw.items() if k != "kind"}})
        if op.kind == "set_from_file" and op.field_name in QTY_PRICE_FIELDS:
            return refuse(plan, "qty_price_not_editable", "qty_price_not_editable")
        if op.kind == "set_from_file" and (
            op.field_name in NEVER_EDITABLE_FIELDS or op.field_name not in {"description", "title"}
        ):
            return refuse(plan, "field_not_editable", "field_not_editable")
        if op.kind == "strip_claim":
            claim = op.text
            locations: list[str]
            if op.location == "all":
                locations = ["description", "features", "aspects_all", "title"]
            else:
                locations = [op.location]
            op_changed = False
            for loc in locations:
                if loc == "description":
                    hit = strip_claim_from_description(
                        description, claim, _description_snippet(description, claim)
                    )
                    if hit.status == STATUS_PROTECTED_ZONE:
                        return refuse(plan, "protected_zone", "protected_zone")
                    if hit.status == STATUS_WILL_STRIP:
                        if ul_would_be_empty(description, [hit.removed_unit_html] if hit.removed_unit_html else []):
                            add_gate(plan, "empty_ul", True, "empty_ul_cleaned")
                        if hit.removed_unit_html:
                            removed_desc_units.append(hit.removed_unit_html)
                        description = hit.after_html
                        op_changed = True
                    elif hit.status == STATUS_ALREADY_ABSENT:
                        if claim_still_in_unprotected(claim, description):
                            return refuse(plan, "strip_already_absent", "strip_already_absent")
                    elif claim_still_in_unprotected(claim, description):
                        return refuse(plan, f"strip_{hit.status}", f"strip_{hit.status}")
                elif loc == "features":
                    result, status = _strip_claim_from_aspect_values(
                        aspects, "Features", claim, required_names, required_available
                    )
                    if status == "required_aspect_would_empty":
                        return refuse(plan, "required_aspect_would_empty", "required_aspect_would_empty")
                    if status == STATUS_WILL_STRIP and result is not None:
                        aspects = result
                        op_changed = True
                elif loc == "aspect":
                    target = op.aspect_name
                    hit = strip_claim_from_aspects(
                        aspects,
                        target,
                        claim,
                        required_names,
                        required_aspects_available=required_available,
                    )
                    if hit.status in {"required_aspect_would_empty", "required_aspects_unavailable"}:
                        return refuse(plan, "required_aspect_would_empty", "required_aspect_would_empty")
                    # Prefer value-contains match when exact claim != full value.
                    result, status = _strip_claim_from_aspect_values(
                        aspects, target, claim, required_names, required_available
                    )
                    if status == "required_aspect_would_empty":
                        return refuse(plan, "required_aspect_would_empty", "required_aspect_would_empty")
                    if status == STATUS_WILL_STRIP and result is not None:
                        aspects = result
                        op_changed = True
                    elif hit.status == STATUS_WILL_STRIP:
                        aspects = _normalize_aspects(hit.after_aspects)
                        op_changed = True
                elif loc == "aspects_all":
                    result, status = _strip_claim_from_aspect_values(
                        aspects, "", claim, required_names, required_available
                    )
                    if status == "required_aspect_would_empty":
                        return refuse(plan, "required_aspect_would_empty", "required_aspect_would_empty")
                    if status == STATUS_WILL_STRIP and result is not None:
                        aspects = result
                        op_changed = True
                elif loc == "title":
                    new_title, status = _strip_title(title, claim)
                    if status == "title_would_degrade":
                        return refuse(plan, "title_would_degrade", "title_would_degrade")
                    if status == STATUS_WILL_STRIP:
                        removed_title_units.append(title)
                        title = new_title
                        op_changed = True
            if op_changed:
                changed = True
        elif op.kind == "remove_aspect_value":
            result, status = _drop_aspect_value(
                aspects, op.aspect_name, op.aspect_value, required_names, required_available
            )
            if status == "required_aspect_would_empty":
                return refuse(plan, "required_aspect_would_empty", "required_aspect_would_empty")
            if status == STATUS_WILL_STRIP and result is not None:
                aspects = result
                changed = True
        elif op.kind == "remove_aspect":
            key = find_aspect_key(aspects, op.aspect_name)
            if key is None:
                continue
            if not is_features_aspect(key) and (not required_available or is_required_aspect(key, required_names)):
                return refuse(plan, "required_aspect_would_empty", "required_aspect_would_empty")
            aspects = dict(aspects)
            aspects.pop(key, None)
            changed = True
        elif op.kind == "set_from_file":
            try:
                content = Path(op.path).read_text(encoding="utf-8")
            except Exception:
                return refuse(plan, "file_unreadable", "file_unreadable")
            current = description if op.field_name == "description" else title
            ok, offenders = deletion_only_text(current, content)
            if not ok:
                return refuse(plan, "text_add_refused", "text_add_refused:" + ",".join(offenders))
            if op.field_name == "title" and title_would_degrade(title, content.strip()):
                return refuse(plan, "title_would_degrade", "title_would_degrade")
            if op.field_name == "description":
                description = content
            else:
                title = content.strip()
            changed = True
        elif op.kind == "set_category":
            if str(op.category_id) == str(category_id):
                continue
            verdict = run_category_gates(op.category_id, opt, aspects, ctx, args, plan=plan)
            if not verdict[0]:
                return refuse(plan, verdict[1], verdict[1])
            add_gate(plan, "category", True, "")
            opt["categoryId"] = str(op.category_id)
            opt["categoryName"] = verdict[2]
            if getattr(args, "mark_category_approved", False):
                mark_category_approved(opt, op.category_id)
            category_id = str(op.category_id)
            required_names, required_available = required_aspect_names(ctx, category_id)
            changed = True

    opt["description"] = description
    opt["title"] = title
    opt["aspects"] = aspects
    plan.optimization_after = opt

    text_ok, offenders = deletion_only_text(
        str(plan.optimization_before.get("description") or ""),
        description,
    )
    if not text_ok:
        return refuse(plan, "text_add_refused", "text_add_refused:" + ",".join(offenders))
    title_ok, title_offenders = deletion_only_text(
        str(plan.optimization_before.get("title") or ""),
        title,
    )
    if not title_ok:
        return refuse(plan, "text_add_refused", "text_add_refused:" + ",".join(title_offenders))
    asp_ok, asp_offenders = deletion_only_aspects(plan.optimization_before.get("aspects"), aspects)
    if not asp_ok:
        return refuse(plan, "text_add_refused", "text_add_refused:" + ",".join(asp_offenders))
    add_gate(plan, "deletion_only", True, "")

    if not changed:
        plan.status = "already_absent"
        plan.optimization_after = deepcopy(plan.optimization_before)
        plan.db_changed = False
        return plan

    plan.optimization_after = opt
    plan.diff = build_diff(
        plan.optimization_before,
        opt,
        removed_desc_units=removed_desc_units,
        removed_title_units=removed_title_units,
    )
    plan.db_changed = True
    plan.status = "planned"
    return plan


def store_category_indexes(ctx: SkuFieldEditContext) -> Mapping[str, Mapping[str, tuple[str, str]]] | None:
    if ctx.category_indexes is not None:
        return ctx.category_indexes
    if ctx.category_index is not None:
        return None
    return load_category_indexes(allow_runtime_refresh=False)


def run_category_gates(
    category_id: str,
    optimization: Mapping[str, Any],
    aspects: Mapping[str, list[str]],
    ctx: SkuFieldEditContext,
    args: Any,
    plan: SkuPlan | None = None,
) -> tuple[bool, str, str]:
    if not getattr(args, "allow_category", False):
        return False, "category_flag_required", ""
    if not is_valid_category_id(category_id):
        return False, "not_leaf", ""
    tree_id = store_tree_id(ctx.store)
    indexes = store_category_indexes(ctx)
    tree = (indexes or {}).get(tree_id) if indexes is not None else None
    if indexes is not None and not tree:
        return False, "unverified", ""
    leaf = is_cached_leaf_in_tree(
        category_id,
        tree_id,
        indexes=indexes,
    )
    if indexes is None and ctx.category_index is not None:
        leaf = is_cached_leaf_category(category_id, index=ctx.category_index)
    if not leaf:
        return False, "not_leaf", ""
    name = resolve_category_name(category_id, ctx)
    blocked = guard_category_write_high_fee(
        category_id,
        name,
        index=ctx.category_index if ctx.category_index is not None else (
            dict(tree) if tree is not None else None
        ),
        blacklist_path=str(ctx.high_fee_blacklist_path) if ctx.high_fee_blacklist_path else None,
    )
    if blocked:
        reason = "BLOCKED_HIGH_FEE_UNVERIFIED" if "UNVERIFIED" in blocked else "BLOCKED_HIGH_FEE"
        return False, reason, name
    lock_err = locked_category_write_error(
        optimization,
        override=bool(getattr(args, "override_category_lock", False)),
    )
    if lock_err:
        return False, "category_locked", name
    required_names, available = required_aspect_names(ctx, category_id)
    if not available:
        return False, "target_required_aspect_missing", name
    for req in required_names:
        key = find_aspect_key(aspects, req)
        if key is None or not aspects.get(key):
            return False, "target_required_aspect_missing", name
    if getattr(args, "push_live", False) and plan is not None and plan.live:
        live_aspects = _live_aspects_from_plan(plan)
        for req in required_names:
            key = find_aspect_key(live_aspects, req)
            if key is None or not live_aspects.get(key):
                return False, "target_required_aspect_missing", name
    if ctx.store == "aqua" and getattr(args, "push_live", False):
        return False, "aqua_live_category_unsupported", name
    return True, "", name


def resolve_category_name(category_id: str, ctx: SkuFieldEditContext) -> str:
    tree_id = store_tree_id(ctx.store)
    indexes = store_category_indexes(ctx)
    hit = lookup_category_in_tree(category_id, tree_id, indexes=indexes)
    if hit:
        return hit[0]
    if ctx.category_index is not None and category_id in ctx.category_index:
        return str(ctx.category_index[category_id][0])
    return ""


def _live_aspects_from_plan(plan: SkuPlan) -> dict[str, list[str]]:
    if "inventory" in plan.live:
        return _normalize_aspects(((plan.live.get("inventory") or {}).get("product") or {}).get("aspects"))
    return _normalize_aspects(plan.live.get("aspects"))


def _live_aspects_changed(plan: SkuPlan) -> bool:
    if not plan.live:
        return False
    return _planned_live_aspects(plan) != _live_aspects_from_plan(plan)


_HTML_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)([^>]*)>", re.S)
_HTML_ATTR_RE = re.compile(
    r"""([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""",
    re.S,
)


def normalize_html_for_readback(html_src: str) -> str:
    """Whitespace / entity / attribute-order tolerant HTML compare form."""

    def _tag(match: re.Match[str]) -> str:
        closing, name, raw_attrs = match.group(1), match.group(2).lower(), match.group(3) or ""
        if closing:
            return f"</{name}>"
        attrs: list[tuple[str, str]] = []
        for item in _HTML_ATTR_RE.finditer(raw_attrs):
            key = item.group(1).lower()
            if key in {"/", ""}:
                continue
            value = item.group(2)
            if value is None:
                value = item.group(3)
            if value is None:
                value = item.group(4) or ""
            attrs.append((key, value))
        attrs.sort()
        rendered = "".join(f' {key}="{value}"' for key, value in attrs)
        self_close = raw_attrs.rstrip().endswith("/")
        return f"<{name}{rendered}{' /' if self_close else ''}>"

    text = _HTML_TAG_RE.sub(_tag, normalize_entities(html_src or ""))
    text = collapse_ws(text)
    return re.sub(r">\s+<", "><", text)


def _description_strip_claims(plan: SkuPlan) -> list[str]:
    claims: list[str] = []
    for op in plan.ops:
        kind = str(op.get("kind") or "")
        location = str(op.get("location") or "")
        if kind == "strip_claim" and location in {"description", "all", ""}:
            claim = str(op.get("text") or op.get("claim") or "")
            if claim:
                claims.append(claim)
    return claims


def _removed_units_absent(html_src: str, plan: SkuPlan) -> bool:
    got_text = strip_html_to_text(html_src).casefold()
    got_norm = normalize_html_for_readback(html_src)
    for claim in _description_strip_claims(plan):
        if claim_still_in_unprotected(claim, html_src) or claim_matches_text(claim, html_src):
            return False
    for unit in (plan.diff.get("description") or {}).get("removed_units") or []:
        unit_html = str(unit or "")
        if not unit_html:
            continue
        unit_text = strip_html_to_text(unit_html)
        if unit_text and unit_text.casefold() in got_text:
            return False
        unit_norm = normalize_html_for_readback(unit_html)
        if unit_norm and unit_norm in got_norm:
            return False
    return True


def inventory_sent_description(html: str) -> str:
    """Same compress / 4000-char truncate as ``_put_inventory_product_only``."""
    description = html or ""
    compressed = compress_html(description)
    if len(compressed) <= 4000:
        return compressed
    return smart_truncate_html(description, max_length=4000, min_length=3600)


def _aqua_description_matches(got: str, want: str, plan: SkuPlan) -> bool:
    if normalize_html_for_readback(got) != normalize_html_for_readback(want):
        return False
    return _removed_units_absent(got, plan)


def build_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    removed_desc_units: list[str] | None = None,
    removed_title_units: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    import difflib

    diff: dict[str, dict[str, Any]] = {}
    for key in ("title", "description"):
        left = str(before.get(key) or "")
        right = str(after.get(key) or "")
        if left != right:
            entry = {
                "before": left,
                "after": right,
                "unified": "\n".join(
                    difflib.unified_diff(
                        left.splitlines(),
                        right.splitlines(),
                        fromfile=f"{key}.before",
                        tofile=f"{key}.after",
                        lineterm="",
                    )
                ),
            }
            units = removed_desc_units if key == "description" else removed_title_units
            if units:
                entry["removed_units"] = list(units)
            diff[key] = entry
    before_asp = _normalize_aspects(before.get("aspects"))
    after_asp = _normalize_aspects(after.get("aspects"))
    if before_asp != after_asp:
        removed: dict[str, list[str]] = {}
        added: dict[str, list[str]] = {}
        names = set(before_asp) | set(after_asp)
        for name in names:
            b = before_asp.get(name, [])
            a = after_asp.get(name, [])
            gone = [item for item in b if item not in a]
            extra = [item for item in a if item not in b]
            if gone:
                removed[name] = gone
            if extra:
                added[name] = extra
        diff["aspects"] = {"before": before_asp, "after": after_asp, "removed": removed, "added": added}
    if str(before.get("categoryId") or "") != str(after.get("categoryId") or ""):
        diff["categoryId"] = {
            "before": before.get("categoryId"),
            "after": after.get("categoryId"),
            "name_before": before.get("categoryName"),
            "name_after": after.get("categoryName"),
        }
    return diff


def fetch_live(plan: SkuPlan, ctx: SkuFieldEditContext, args: Any) -> SkuPlan:
    listing_id = str(plan.listing_id or "")
    try:
        if ctx.store == "aqua":
            trading = ctx.trading_client
            ebay = ctx.ebay_client
            if trading is None or ebay is None:
                return refuse(plan, "fetch_failed", "fetch_failed")
            xml = trading.get_item(listing_id, site_id="100")
            description = ebay.get_item_description_motors(listing_id)
            compatibility = ebay.get_item_compatibility_motors(listing_id)
            parsed = parse_getitem_xml(xml or "")
            plan.live = {
                "xml": xml,
                "description": description or parsed.get("description") or "",
                "title": parsed.get("title") or "",
                "aspects": parsed.get("aspects") or {},
                "start_price": parsed.get("start_price"),
                "quantity": parsed.get("quantity"),
                "compatibility": compatibility or [],
                "category_id": parsed.get("category_id") or "",
            }
        else:
            ebay = ctx.ebay_client
            if ebay is None:
                return refuse(plan, "fetch_failed", "fetch_failed")
            inventory = ebay.get_inventory_item(plan.sku) or {}
            offers = ebay.get_offers_by_sku(plan.sku) or []
            offer = None
            for item in offers:
                if str((item or {}).get("listingId") or "") == listing_id:
                    offer = item
                    break
            if offer is None:
                return refuse(plan, "listing_id_mismatch", "listing_id_mismatch")
            plan.live = {"inventory": inventory, "offer": offer}
            qty = live_quantity(inventory, offer)
            if qty == 0 or str(qty) == "0":
                plan.qty_zero_preserved = True
                add_gate(plan, "qty_zero_preserved", True, "qty_zero_preserved")
    except Exception as exc:
        return refuse(plan, "fetch_failed", f"fetch_failed:{exc}")
    return plan


def _description_snippet(html_src: str, claim: str) -> str:
    """Pick the smallest claim_strip unit so --strip-claim can anchor without a live report."""
    for li in re.findall(r"<li\b[^>]*>.*?</li>", html_src or "", flags=re.I | re.S):
        if claim_matches_text(claim, li):
            from src.utils.claim_strip import strip_html_to_text as _to_text

            return _to_text(li) or claim
    from src.utils.claim_strip import strip_html_to_text as _to_text

    text = _to_text(html_src)
    pattern = claim_pattern(claim)
    if pattern:
        match = pattern.search(text)
        if match:
            start = max(text.rfind(".", 0, match.start()) + 1, text.rfind("!", 0, match.start()) + 1)
            end = min(
                (pos for pos in (text.find(".", match.end()), text.find("!", match.end())) if pos >= 0),
                default=len(text),
            )
            window = text[start:end].strip()
            if window:
                return window
    return claim


def parse_getitem_xml(xml: str) -> dict[str, Any]:
    text = xml or ""
    def _tag(name: str) -> str:
        match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", text, re.S | re.I)
        return re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", match.group(1), flags=re.S) if match else ""

    aspects: dict[str, list[str]] = {}
    specifics = re.search(r"<ItemSpecifics>(.*?)</ItemSpecifics>", text, re.S | re.I)
    aspect_src = specifics.group(1) if specifics else ""
    for block in re.findall(r"<NameValueList>(.*?)</NameValueList>", aspect_src, re.S | re.I):
        name_m = re.search(r"<Name>(.*?)</Name>", block, re.S | re.I)
        if not name_m:
            continue
        name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip()
        values = [re.sub(r"<[^>]+>", "", item).strip() for item in re.findall(r"<Value>(.*?)</Value>", block, re.S | re.I)]
        if name:
            aspects.setdefault(name, []).extend([item for item in values if item])
    start = _tag("StartPrice")
    qty = _tag("Quantity")
    return {
        "title": _tag("Title"),
        "description": _tag("Description"),
        "aspects": aspects,
        "start_price": start,
        "quantity": qty,
        "category_id": _tag("CategoryID"),
    }


def print_plan(plan: SkuPlan) -> None:
    sku = plan.sku
    if plan.status == "refused":
        print(f"已拒绝: {sku} {plan.refuse_reason}")
    elif plan.status == "already_absent":
        print(f"已存在: {sku} already_absent")
    else:
        print(f"计划: {sku} {', '.join(plan.diff.keys()) or 'no-op'} — {plan.status}")
    for field_name, payload in plan.diff.items():
        print(f"  {field_name}:")
        if field_name in {"description", "title"} and payload.get("removed_units"):
            for unit in payload["removed_units"]:
                print(f"    removed: {unit}")
        elif field_name == "aspects":
            print(f"    removed={payload.get('removed')} added={payload.get('added')}")
        elif field_name == "categoryId":
            print(f"    {payload.get('before')} → {payload.get('after')}")
        elif "unified" in payload:
            print(payload["unified"])
        else:
            print(f"    {payload.get('before')} → {payload.get('after')}")
    for gate in plan.gates:
        mark = "ok" if gate.ok else "FAIL"
        print(f"  gate {gate.name}: {mark} {gate.reason}".rstrip())
    if plan.qty_zero_preserved:
        print("  flag qty_zero_preserved")
    print("  fee_preview: n/a (no insert)")


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def plan_to_report(plan: SkuPlan) -> dict[str, Any]:
    return {
        "sku": plan.sku,
        "listing_id": plan.listing_id,
        "ops": plan.ops,
        "gates": [gate.as_dict() for gate in plan.gates],
        "diff": plan.diff,
        "db_readback": plan.db_readback,
        "live_readback": plan.live_readback,
        "status": plan.status,
        "qty_zero_preserved": plan.qty_zero_preserved,
        "refuse_reason": plan.refuse_reason,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_tool_lock(ctx: SkuFieldEditContext) -> tuple[int | None, Path]:
    lock_dir = Path(ctx.lock_dir or ctx.task_lock_dir or (_REPO_ROOT / "logs" / "_task_locks"))
    lock_path = lock_dir / "sku_field_edit.lock"
    acquire = ctx.acquire_lock or _acquire_worker_lock
    fd = acquire(
        lock_path,
        task_name="sku_field_edit",
        timeout_sec=3600,
        owner_token=f"sku_field_edit-{os.getpid()}",
    )
    if fd is None:
        pid = "?"
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = existing.get("pid") or existing.get("worker_pid") or "?"
        except Exception:
            pass
        raise ToolRefused(f"another sku_field_edit is running (pid {pid})")
    return fd, lock_path


def release_tool_lock(fd: int | None, lock_path: Path) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        lock_path.unlink()
    except OSError:
        pass


def refuse_if_writer_locks(ctx: SkuFieldEditContext) -> None:
    lock_dir = Path(ctx.task_lock_dir or ctx.lock_dir or (_REPO_ROOT / "logs" / "_task_locks"))
    timeouts = dict(ctx.writer_timeouts) if ctx.writer_timeouts is not None else listing_writer_timeouts()
    stale_fn = ctx.lock_is_stale or _task_lock_stale
    for name in LISTING_WRITER_TASKS:
        path = lock_dir / f"{name}.lock"
        if not path.exists():
            continue
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            info = {}
        timeout = int(timeouts.get(name, 3600))
        if not stale_fn(info, timeout):
            raise ToolRefused(f"writer_lock_held:{name}")


def preflight(args: Any, ctx: SkuFieldEditContext) -> None:
    match_store_profile(ctx.store, ctx.profile)
    try:
        assert_runtime_not_in_maintenance(ctx.maintenance_path or MAINTENANCE_PATH)
        if Path(ctx.db_path).exists():
            assert_single_link_database(ctx.db_path)
    except DatabaseSafetyError as exc:
        raise ToolRefused(str(exc)) from exc
    if getattr(args, "push_live", False):
        env = str(os.environ.get("EBAY_ENVIRONMENT") or "")
        base = str(getattr(ctx.ebay_client, "base_url", "") or "")
        print(f"EBAY_ENVIRONMENT={env}")
        print(f"RealEbayClient.base_url={base}")
        if env.casefold() != "production" or base.rstrip("/") != PRODUCTION_API:
            raise ToolRefused("push_live_not_production")
        if ctx.store == "aqua":
            from scripts.trading_content_qc import _profile_guard

            try:
                _profile_guard()
            except Exception as exc:
                raise ToolRefused(str(exc)) from exc


def select_ops_for_sku(args: Any, sku: str, manifest: list[dict[str, Any]] | None) -> list[EditOp]:
    if manifest is not None:
        for item in manifest:
            if str(item.get("sku") or "") == sku:
                return parse_manifest_ops(item.get("ops") or [])
        raise ToolRefused("field_not_editable")
    return parse_cli_ops(getattr(args, "cli_ops", None) or [])


def snapshot_and_plan(
    conn: sqlite3.Connection,
    sku: str,
    ops: list[EditOp],
    ctx: SkuFieldEditContext,
    args: Any,
) -> SkuPlan:
    row = load_row(conn, sku)
    if row is None:
        plan = SkuPlan(sku=sku)
        return refuse(plan, "missing_sku", "missing_sku")
    plan = SkuPlan(
        sku=sku,
        listing_id=str(row.get("listing_id") or ""),
        row=row,
        row_sha256=row_sha256(row),
        optimization_before=deepcopy(row["optimization"]),
        optimization_after=deepcopy(row["optimization"]),
    )
    if getattr(args, "push_live", False):
        plan = fetch_live(plan, ctx, args)
        if plan.status == "refused":
            return plan
    plan = apply_ops(plan, ops, ctx, args)
    if plan.status == "refused":
        return plan
    if getattr(args, "push_live", False) and plan.live:
        plan = apply_live_ops(plan, ops, ctx, args)
    return plan


def apply_live_ops(
    plan: SkuPlan,
    ops: list[EditOp],
    ctx: SkuFieldEditContext,
    args: Any,
) -> SkuPlan:
    """Apply the same text/aspect ops to the LIVE snapshot, never DB text."""
    if ctx.store == "aqua":
        offer_desc = str(plan.live.get("description") or "")
        inv_desc = offer_desc
        title = str(plan.live.get("title") or "")
        aspects = _normalize_aspects(plan.live.get("aspects"))
    else:
        inventory = plan.live.get("inventory") or {}
        offer = plan.live.get("offer") or {}
        product = inventory.get("product") or {}
        offer_desc = str(offer.get("listingDescription") or "")
        inv_desc = str(product.get("description") or "")
        title = str(product.get("title") or "")
        aspects = _normalize_aspects(product.get("aspects"))
    live_offer_before, live_inv_before, live_title_before = offer_desc, inv_desc, title
    live_aspects_before = deepcopy(aspects)
    category_id = str(plan.optimization_after.get("categoryId") or plan.optimization_before.get("categoryId") or "")
    required_names, required_available = required_aspect_names(ctx, category_id)
    changed = False

    def _strip_live_html(html: str, claim: str) -> tuple[str, str | None]:
        hit = strip_claim_from_description(html, claim, _description_snippet(html, claim))
        if hit.status == STATUS_PROTECTED_ZONE:
            raise ToolRefused("protected_zone")
        if hit.status == STATUS_WILL_STRIP:
            return hit.after_html, hit.removed_unit_html or None
        if hit.status == STATUS_ALREADY_ABSENT:
            if claim_still_in_unprotected(claim, html):
                raise ToolRefused("strip_already_absent")
            return html, None
        if claim_still_in_unprotected(claim, html):
            raise ToolRefused(f"strip_{hit.status}")
        return html, None

    try:
        for op in ops:
            if op.kind == "set_from_file" and op.field_name in QTY_PRICE_FIELDS:
                return refuse(plan, "qty_price_not_editable", "qty_price_not_editable")
            if op.kind == "strip_claim":
                claim = op.text
                locations = ["description", "features", "aspects_all", "title"] if op.location == "all" else [op.location]
                for loc in locations:
                    if loc == "description":
                        offer_desc, _ = _strip_live_html(offer_desc, claim)
                        inv_desc, _ = _strip_live_html(inv_desc, claim)
                    elif loc == "title":
                        new_title, status = _strip_title(title, claim)
                        if status == "title_would_degrade":
                            return refuse(plan, "title_would_degrade", "title_would_degrade")
                        if status == STATUS_WILL_STRIP:
                            title = new_title
                    elif loc in {"features", "aspect", "aspects_all"}:
                        target = "Features" if loc == "features" else (op.aspect_name if loc == "aspect" else "")
                        result, status = _strip_claim_from_aspect_values(
                            aspects, target, claim, required_names, required_available
                        )
                        if status == "required_aspect_would_empty":
                            return refuse(plan, "required_aspect_would_empty_live", "required_aspect_would_empty_live")
                        if status == STATUS_WILL_STRIP and result is not None:
                            aspects = result
            elif op.kind == "remove_aspect_value":
                result, status = _drop_aspect_value(
                    aspects, op.aspect_name, op.aspect_value, required_names, required_available
                )
                if status == "required_aspect_would_empty":
                    return refuse(plan, "required_aspect_would_empty_live", "required_aspect_would_empty_live")
                if status == STATUS_WILL_STRIP and result is not None:
                    aspects = result
            elif op.kind == "remove_aspect":
                key = find_aspect_key(aspects, op.aspect_name)
                if key is None:
                    continue
                if not is_features_aspect(key) and (
                    not required_available or is_required_aspect(key, required_names)
                ):
                    return refuse(plan, "required_aspect_would_empty_live", "required_aspect_would_empty_live")
                aspects = dict(aspects)
                aspects.pop(key, None)
            elif op.kind == "set_from_file":
                try:
                    content = Path(op.path).read_text(encoding="utf-8")
                except Exception:
                    return refuse(plan, "file_unreadable", "file_unreadable")
                if op.field_name == "description":
                    for before in (offer_desc, inv_desc):
                        ok, offenders = deletion_only_text(before, content)
                        if not ok:
                            return refuse(
                                plan, "text_add_refused_live", "text_add_refused_live:" + ",".join(offenders)
                            )
                    offer_desc = content
                    inv_desc = content
                else:
                    ok, offenders = deletion_only_text(title, content)
                    if not ok:
                        return refuse(plan, "text_add_refused_live", "text_add_refused_live:" + ",".join(offenders))
                    if title_would_degrade(title, content.strip()):
                        return refuse(plan, "title_would_degrade", "title_would_degrade")
                    title = content.strip()
            elif op.kind == "set_category":
                required_names, required_available = required_aspect_names(ctx, op.category_id)
    except ToolRefused as exc:
        return refuse(plan, exc.reason, exc.reason)

    for label, before, after in (
        ("offer_description", live_offer_before, offer_desc),
        ("inventory_description", live_inv_before, inv_desc),
        ("title", live_title_before, title),
    ):
        ok, offenders = deletion_only_text(before, after)
        if not ok:
            return refuse(plan, "text_add_refused_live", "text_add_refused_live:" + ",".join(offenders))
    asp_ok, asp_offenders = deletion_only_aspects(live_aspects_before, aspects)
    if not asp_ok:
        return refuse(plan, "text_add_refused_live", "text_add_refused_live:" + ",".join(asp_offenders))
    add_gate(plan, "deletion_only_live", True, "")

    if ctx.store == "aqua":
        plan.live_after = {"description": offer_desc, "title": title, "aspects": aspects}
    else:
        plan.live_after = {
            "offer_description": offer_desc,
            "inventory_description": inv_desc,
            "title": title,
            "aspects": aspects,
        }
    changed = (
        offer_desc != live_offer_before
        or inv_desc != live_inv_before
        or title != live_title_before
        or aspects != live_aspects_before
    )
    plan.live_changed = changed
    if changed and plan.status == "already_absent":
        plan.status = "planned"
    return plan


def backup_run(
    ctx: SkuFieldEditContext,
    args: Any,
    plans: list[SkuPlan],
    run_id: str,
    stamp: str,
) -> dict[str, Any]:
    root = resolve_backup_root(args, ctx.profile, ctx.backup_root)
    run_dir = Path(root) / f"sku_edit_{ctx.store}_{stamp}_{run_id}"
    if run_dir.exists():
        raise ToolRefused("backup_failed")
    run_dir.mkdir(parents=True, exist_ok=True)
    rows_before = [
        {"sku": plan.sku, "row": plan.row, "row_sha256": plan.row_sha256}
        for plan in plans
        if plan.status in {"planned", "already_absent"} or plan.row
    ]
    write_json(run_dir / "rows_before.json", rows_before)
    db_dest = run_dir / "ebay_collection.db"
    backup_fn = ctx.create_backup or create_online_backup
    source = sqlite3.connect(str(ctx.db_path), timeout=30)
    try:
        report = backup_fn(source, db_dest)
    finally:
        source.close()
    db_meta = {
        "path": str(db_dest),
        "size": db_dest.stat().st_size,
        "sha256": file_sha256(db_dest),
        "integrity": getattr(report, "integrity_check", "ok"),
    }
    if getattr(args, "push_live", False):
        live_before = {plan.sku: _live_before_payload(plan, ctx) for plan in plans if plan.live}
        write_json(run_dir / "live_before.json", live_before)
    write_json(
        run_dir / "plan.json",
        {"ops": [plan.ops for plan in plans], "plans": [plan_to_report(plan) for plan in plans]},
    )
    return {"dir": run_dir, "db": db_meta}


def _live_before_payload(plan: SkuPlan, ctx: SkuFieldEditContext) -> dict[str, Any]:
    if ctx.store == "aqua":
        return {
            "xml": plan.live.get("xml"),
            "description": plan.live.get("description"),
            "compatibility": plan.live.get("compatibility"),
            "title": plan.live.get("title"),
            "aspects": plan.live.get("aspects"),
        }
    return {
        "inventory": plan.live.get("inventory"),
        "offer": plan.live.get("offer"),
    }


def apply_db_write(
    conn: sqlite3.Connection,
    plan: SkuPlan,
    ctx: SkuFieldEditContext,
    args: Any,
    run_id: str,
    rows_before: list[dict[str, Any]],
) -> int:
    if plan.status != "planned" or not plan.db_changed:
        return EXIT_OK
    conn.execute("BEGIN IMMEDIATE")
    fresh = load_row(conn, plan.sku)
    if fresh is None or row_sha256(fresh) != plan.row_sha256:
        conn.execute("ROLLBACK")
        plan.status = "drift"
        plan.refuse_reason = "drift"
        return EXIT_DRIFT
    now = ctx.clock()
    ts = now.isoformat(sep=" ") if isinstance(now, datetime) else str(now)
    logs = list(fresh.get("logs") or [])
    logs.append(
        {
            "ts": ts,
            "tool": "sku_field_edit",
            "run_id": run_id,
            "fields": sorted(plan.diff.keys()),
            "note": getattr(args, "note", ""),
        }
    )
    try:
        conn.execute(
            "UPDATE collected_products SET optimization=?, updated_at=?, logs=? WHERE sku=?",
            (
                json.dumps(plan.optimization_after, ensure_ascii=False),
                ts,
                json.dumps(logs, ensure_ascii=False),
                plan.sku,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    ok, mismatches = db_readback_ok(conn, plan)
    plan.db_readback = {"ok": ok, "mismatches": mismatches}
    if ok:
        plan.status = "applied"
        return EXIT_OK
    restored = restore_db_row(conn, plan.sku, rows_before)
    if restored:
        plan.status = "restored"
        return EXIT_RESTORED
    plan.status = "restore_failed"
    return EXIT_RESTORE_FAILED


def db_readback_ok(conn: sqlite3.Connection, plan: SkuPlan) -> tuple[bool, list[str]]:
    row = load_row(conn, plan.sku)
    mismatches: list[str] = []
    if row is None:
        return False, ["missing_row"]
    after = _as_opt(row.get("optimization"))
    planned = plan.optimization_after
    before = plan.optimization_before
    changed = set(plan.diff.keys())
    if "categoryId" in changed:
        changed.update({"categoryName"})
        if planned.get("categoryApproved") != before.get("categoryApproved"):
            changed.update({"categoryApproved", "categoryApprovedId"})
    for key in changed:
        if canonical_json(after.get(key)) != canonical_json(planned.get(key)):
            mismatches.append(key)
    for key in set(before) | set(after):
        if key in changed:
            continue
        if canonical_json(after.get(key)) != canonical_json(before.get(key)):
            mismatches.append(f"other:{key}")
    for col in ("status", "price", "stock", "listing_id"):
        if row.get(col) != plan.row.get(col):
            mismatches.append(col)
    return (not mismatches), mismatches


def restore_db_row(conn: sqlite3.Connection, sku: str, rows_before: list[dict[str, Any]]) -> bool:
    snapshot = next((item for item in rows_before if item.get("sku") == sku), None)
    if not snapshot:
        return False
    row = snapshot.get("row") or {}
    opt_raw = row.get("_optimization_raw")
    if not isinstance(opt_raw, str):
        raw = row.get("optimization")
        opt_raw = raw if isinstance(raw, str) else json.dumps(_as_opt(raw), ensure_ascii=False)
    logs_raw = row.get("_logs_raw")
    if not isinstance(logs_raw, str):
        raw_logs = row.get("logs")
        logs_raw = raw_logs if isinstance(raw_logs, str) else json.dumps(_as_logs(raw_logs), ensure_ascii=False)
    updated_at = row.get("updated_at")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE collected_products SET optimization=?, updated_at=?, logs=? WHERE sku=?",
            (opt_raw, updated_at, logs_raw, sku),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False
    return verify_restored_row(conn, sku, opt_raw, logs_raw, updated_at)


def verify_restored_row(
    conn: sqlite3.Connection,
    sku: str,
    opt_raw: str,
    logs_raw: str,
    updated_at: Any,
) -> bool:
    cur = conn.execute(
        "SELECT optimization, logs, updated_at FROM collected_products WHERE sku=?",
        (sku,),
    )
    row = cur.fetchone()
    if row is None:
        return False
    data = dict(row)
    return (
        str(data.get("optimization") or "") == opt_raw
        and str(data.get("logs") or "") == logs_raw
        and data.get("updated_at") == updated_at
    )


def _offer_put(ebay_client: Any, offer_id: str, payload: Mapping[str, Any]) -> Any:
    token = ebay_client.oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }
    url = f"{ebay_client.base_url}/sell/inventory/v1/offer/{offer_id}"
    response = ebay_client.session.put(url, headers=headers, json=dict(payload), timeout=120)
    if getattr(response, "status_code", 200) not in (200, 204):
        raise RuntimeError(
            f"offer PUT failed {getattr(response, 'status_code', '?')}: {getattr(response, 'text', '')[:200]}"
        )
    return response


def apply_live_push(plan: SkuPlan, ctx: SkuFieldEditContext, args: Any) -> int:
    if plan.status not in {"applied", "planned"}:
        return EXIT_OK
    if plan.status == "planned" and not plan.live_changed:
        return EXIT_OK
    try:
        if ctx.store == "aqua":
            _push_aqua(plan, ctx)
        else:
            _push_inventory(plan, ctx)
    except ToolRefused as exc:
        db_restored = False
        return _fail_live(plan, ctx, db_restored=db_restored, reason=str(exc.reason))
    except Exception as exc:
        return _fail_live(plan, ctx, db_restored=False, reason=str(exc))
    ctx.sleep(2)
    try:
        ok, mismatches = live_readback_ok(plan, ctx)
    except Exception as exc:
        return _fail_live(plan, ctx, db_restored=False, reason=f"readback:{exc}")
    plan.live_readback = {
        "ok": ok,
        "mismatches": mismatches,
        "price_qty_unchanged": "price_qty" not in mismatches,
    }
    if ok:
        return EXIT_OK
    return _fail_live(plan, ctx, db_restored=False, reason="live_readback_mismatch")


def _live_planned_text(plan: SkuPlan, key: str, fallback: str = "") -> str:
    """Return the live-planned string for ``key``. Empty is a real value.

    Never falls back to DB ``optimization`` text. Using DB text on a live
    push or readback can add or revert live-only content (H2).
    """
    planned = plan.live_after or {}
    if key in planned and planned[key] is not None:
        return str(planned[key])
    return fallback


def _planned_live_aspects(plan: SkuPlan) -> dict[str, list[str]]:
    if plan.live_after.get("aspects") is not None:
        return _normalize_aspects(plan.live_after.get("aspects"))
    live_aspects = _normalize_aspects(
        ((plan.live.get("inventory") or {}).get("product") or {}).get("aspects")
        if ctx_store_is_inventory(plan)
        else plan.live.get("aspects")
    )
    after = _normalize_aspects(plan.optimization_after.get("aspects"))
    before = _normalize_aspects(plan.optimization_before.get("aspects"))
    planned = deepcopy(live_aspects)
    for name, values in before.items():
        after_key = find_aspect_key(after, name) or name
        after_values = after.get(after_key, after.get(name, []))
        removed = [item for item in values if item not in after_values]
        key = find_aspect_key(planned, name)
        if key is None:
            continue
        kept = [item for item in planned[key] if item not in removed]
        if kept:
            planned[key] = kept
        else:
            planned.pop(key, None)
    for op in plan.ops:
        kind = str(op.get("kind") or op.get("op") or "").replace("-", "_")
        if kind == "strip_claim":
            claim = str(op.get("text") or op.get("claim") or "")
            location = str(op.get("location") or "")
            loc_kind, aspect_name = parse_location(location) if location else (location, "")
            if loc_kind in {"features", "all", "aspect", "aspects_all", ""}:
                target = "Features" if loc_kind == "features" else aspect_name
                names = [find_aspect_key(planned, target) or target] if target else list(planned)
                for name in list(names):
                    key = find_aspect_key(planned, name)
                    if key is None:
                        continue
                    kept = [item for item in planned[key] if not claim_matches_text(claim, item)]
                    if kept:
                        planned[key] = kept
                    else:
                        planned.pop(key, None)
        elif kind == "remove_aspect_value":
            name = str(op.get("name") or op.get("aspect_name") or "")
            value = str(op.get("value") or op.get("aspect_value") or "")
            if "=" in str(op.get("spec") or ""):
                name, value = parse_name_value(str(op.get("spec")))
            key = find_aspect_key(planned, name)
            if key:
                kept = [item for item in planned[key] if item.casefold() != value.casefold()]
                if kept:
                    planned[key] = kept
                else:
                    planned.pop(key, None)
        elif kind == "remove_aspect":
            name = str(op.get("name") or op.get("aspect") or op.get("aspect_name") or "")
            key = find_aspect_key(planned, name)
            if key:
                planned.pop(key, None)
    return planned


def ctx_store_is_inventory(plan: SkuPlan) -> bool:
    return "inventory" in plan.live


def _push_inventory(plan: SkuPlan, ctx: SkuFieldEditContext) -> None:
    ebay = ctx.ebay_client
    inventory = plan.live.get("inventory") or {}
    offer = plan.live.get("offer") or {}
    product = inventory.get("product") or {}
    offer_id = str(offer.get("offerId") or "")
    live_title = str(product.get("title") or "")
    live_desc = str(product.get("description") or "")
    live_offer_desc = str(offer.get("listingDescription") or "")
    new_title = _live_planned_text(plan, "title", live_title)
    new_offer_desc = _live_planned_text(plan, "offer_description", live_offer_desc)
    new_inv_desc = _live_planned_text(plan, "inventory_description", live_desc)
    title_changed = new_title != live_title
    offer_desc_changed = new_offer_desc != live_offer_desc
    inv_desc_changed = new_inv_desc != live_desc
    planned_aspects = _planned_live_aspects(plan)
    aspects_changed = planned_aspects != _normalize_aspects(product.get("aspects"))
    if offer_desc_changed:
        _update_offer_description_only(ebay, offer_id, new_offer_desc)
    if inv_desc_changed or title_changed or aspects_changed:
        _put_inventory_product_only(
            ebay,
            plan.sku,
            new_title if title_changed else live_title,
            new_inv_desc if inv_desc_changed else live_desc,
            planned_aspects if aspects_changed else _normalize_aspects(product.get("aspects")),
        )
    if "categoryId" in plan.diff:
        target = str(plan.optimization_after.get("categoryId") or "")
        live_offer = ebay.get_offer(offer_id)
        payload = offer_category_only_put_payload(live_offer, target)
        _offer_put(ebay, offer_id, payload)


def _first_strip_claim(plan: SkuPlan) -> str:
    for op in plan.ops:
        if op.get("kind") == "strip_claim" or op.get("text") or op.get("claim"):
            return str(op.get("text") or op.get("claim") or "")
    return ""


def _push_aqua(plan: SkuPlan, ctx: SkuFieldEditContext) -> None:
    if "categoryId" in plan.diff:
        raise ToolRefused("aqua_live_category_unsupported")
    ebay = ctx.ebay_client
    kwargs: dict[str, Any] = {}
    planned = plan.live_after or {}
    live_desc = str(plan.live.get("description") or "")
    live_title = str(plan.live.get("title") or "")
    new_desc = _live_planned_text(plan, "description", live_desc)
    new_title = _live_planned_text(plan, "title", live_title)
    if new_desc != live_desc:
        kwargs["description"] = new_desc
    if new_title != live_title:
        kwargs["title"] = new_title
    if planned.get("aspects") is not None and _normalize_aspects(planned.get("aspects")) != _normalize_aspects(
        plan.live.get("aspects")
    ):
        kwargs["item_specifics"] = _planned_live_aspects(plan)
    if not kwargs:
        return
    ebay.revise_fixed_price_item_motors(plan.listing_id, **kwargs)


def live_readback_ok(plan: SkuPlan, ctx: SkuFieldEditContext) -> tuple[bool, list[str]]:
    mismatches: list[str] = []
    if ctx.store == "aqua":
        trading = ctx.trading_client
        ebay = ctx.ebay_client
        xml = trading.get_item(plan.listing_id, site_id="100")
        parsed = parse_getitem_xml(xml or "")
        description = ebay.get_item_description_motors(plan.listing_id)
        compatibility = ebay.get_item_compatibility_motors(plan.listing_id)
        want_desc = _live_planned_text(plan, "description", str(plan.live.get("description") or ""))
        want_title = _live_planned_text(plan, "title", str(plan.live.get("title") or ""))
        got_desc = str(description or parsed.get("description") or "")
        if (plan.live_changed or "description" in plan.diff) and not _aqua_description_matches(
            got_desc, want_desc, plan
        ):
            mismatches.append("description")
        if (plan.live_changed or "title" in plan.diff) and str(parsed.get("title") or "") != want_title:
            mismatches.append("title")
        if (
            "aspects" in plan.diff or _live_aspects_changed(plan)
        ) and _normalize_aspects(parsed.get("aspects")) != _planned_live_aspects(plan):
            mismatches.append("aspects")
        if str(parsed.get("start_price") or "") != str(plan.live.get("start_price") or ""):
            mismatches.append("price_qty")
        if str(parsed.get("quantity") or "") != str(plan.live.get("quantity") or ""):
            mismatches.append("price_qty")
        if len(compatibility or []) != len(plan.live.get("compatibility") or []):
            mismatches.append("compatibility")
        return (not mismatches), mismatches

    ebay = ctx.ebay_client
    inventory = ebay.get_inventory_item(plan.sku) or {}
    offers = ebay.get_offers_by_sku(plan.sku) or []
    offer = None
    for item in offers:
        if str((item or {}).get("listingId") or "") == str(plan.listing_id):
            offer = item
            break
    if offer is None and offers:
        offer = offers[0]
    if fetches_price_or_qty_changed(plan.live.get("inventory"), plan.live.get("offer"), inventory, offer):
        mismatches.append("price_qty")
    product = inventory.get("product") or {}
    if plan.live_changed or "description" in plan.diff:
        want_offer = _live_planned_text(
            plan, "offer_description", str((plan.live.get("offer") or {}).get("listingDescription") or "")
        )
        want_inv = inventory_sent_description(
            _live_planned_text(
                plan,
                "inventory_description",
                str(((plan.live.get("inventory") or {}).get("product") or {}).get("description") or ""),
            )
        )
        got_offer = str((offer or {}).get("listingDescription") or "")
        got_inv = str(product.get("description") or "")
        if normalize_html_for_readback(got_offer) != normalize_html_for_readback(want_offer):
            mismatches.append("description")
        if normalize_html_for_readback(got_inv) != normalize_html_for_readback(want_inv):
            if "description" not in mismatches:
                mismatches.append("description")
        if not _removed_units_absent(got_offer, plan) or not _removed_units_absent(got_inv, plan):
            if "description" not in mismatches:
                mismatches.append("description")
    want_title = _live_planned_text(
        plan, "title", str(((plan.live.get("inventory") or {}).get("product") or {}).get("title") or "")
    )
    if (plan.live_changed or "title" in plan.diff) and str(product.get("title") or "") != want_title:
        mismatches.append("title")
    if (
        "aspects" in plan.diff or _live_aspects_changed(plan)
    ) and _normalize_aspects(product.get("aspects")) != _planned_live_aspects(plan):
        if "aspects" not in mismatches:
            mismatches.append("aspects")
    if "categoryId" in plan.diff and live_category_id(offer) != str(plan.optimization_after.get("categoryId") or ""):
        mismatches.append("categoryId")
    return (not mismatches), mismatches


def restore_live(plan: SkuPlan, ctx: SkuFieldEditContext) -> bool:
    try:
        if ctx.store == "aqua":
            kwargs: dict[str, Any] = {
                "description": plan.live.get("description"),
                "title": plan.live.get("title"),
                "item_specifics": plan.live.get("aspects"),
            }
            ctx.ebay_client.revise_fixed_price_item_motors(plan.listing_id, **kwargs)
            return True
        ebay = ctx.ebay_client
        inventory = plan.live.get("inventory") or {}
        offer = plan.live.get("offer") or {}
        _put_inventory_snapshot(ebay, plan.sku, inventory)
        offer_id = str(offer.get("offerId") or "")
        if offer_id:
            payload = deepcopy(offer)
            for key in OFFER_GET_ONLY_KEYS:
                payload.pop(key, None)
            _offer_put(ebay, offer_id, payload)
        return True
    except Exception:
        return False


def _fail_live(plan: SkuPlan, ctx: SkuFieldEditContext, *, db_restored: bool, reason: str) -> int:
    live_ok = restore_live(plan, ctx)
    prior = [str(item) for item in ((plan.live_readback or {}).get("mismatches") or []) if item]
    mismatches = list(prior)
    if reason not in mismatches:
        mismatches.append(reason)
    plan.live_readback = {
        "ok": False,
        "mismatches": mismatches,
        "price_qty_unchanged": False,
        "live_restored": live_ok,
    }
    if not db_restored:
        # caller restores DB separately
        pass
    if live_ok:
        plan.status = "restored"
        return EXIT_RESTORED
    plan.status = "restore_failed"
    return EXIT_RESTORE_FAILED


def default_report_path(store: str, stamp: str, run_id: str) -> Path:
    return _REPO_ROOT / "logs" / "sku_field_edit" / f"{stamp}_{store}_{run_id}.json"


def build_live_clients(store: str) -> tuple[Any, Any]:
    """Construct production Inventory (and Aqua Trading) clients. Env is read-only.

    Main/GP use ``create_real_ebay_client`` (Inventory). Aqua keeps that client
    for Motors helpers (description/compatibility/revise) and builds
    ``EbayTradingClient`` from legacy ``EbayClient(APP/CERT/DEV, env=production)``
    the same way ``listing_status_sync`` / ``ebay_ad_service`` do. RealEbayClient
    has no ``.env`` and cannot back Trading.
    """
    environment = os.environ.get("EBAY_ENVIRONMENT") or "PRODUCTION"
    from src.clients.real_ebay_client import create_real_ebay_client

    ebay = create_real_ebay_client(environment=environment)
    trading = None
    if store == "aqua":
        from src.clients.ebay_client import EbayClient
        from src.clients.ebay_trading_client import EbayTradingClient

        trading_ebay = EbayClient(
            os.getenv("EBAY_APP_ID"),
            os.getenv("EBAY_CERT_ID"),
            os.getenv("EBAY_DEV_ID"),
            env="production",
        )
        trading = EbayTradingClient(trading_ebay)
    return ebay, trading


def run_from_args(args: Any, ctx: SkuFieldEditContext | None = None) -> int:
    run_id = secrets.token_hex(4)
    now_utc = datetime.now(UTC)
    stamp = now_utc.strftime("%Y%m%d_%H%M%SZ")
    stamp_local = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(args.report) if getattr(args, "report", None) else default_report_path(args.store, stamp, run_id)
    lock_fd = None
    lock_path = None
    backup_info: dict[str, Any] = {}
    wrote_anything = False
    mode = "dry_run"
    if getattr(args, "apply", False) and getattr(args, "push_live", False):
        mode = "apply_push_live"
    elif getattr(args, "apply", False):
        mode = "apply"
    try:
        if getattr(args, "apply", False) and not str(getattr(args, "note", "") or "").strip():
            raise ToolRefused("note_required")
        cli_ops = list(getattr(args, "cli_ops", None) or [])
        manifest = None
        if getattr(args, "manifest", None):
            if cli_ops:
                raise ToolRefused("field_not_editable")
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            if not isinstance(manifest, list):
                raise ToolRefused("field_not_editable")
        skus = collect_skus(args)
        profile = ctx.profile if ctx and ctx.profile is not None else get_store_profile()
        ctx = ctx or SkuFieldEditContext(
            db_path=Path(DATABASE_PATH),
            store=args.store,
            profile=profile,
        )
        ctx.store = args.store
        ctx.profile = profile
        if getattr(args, "push_live", False) and ctx.ebay_client is None:
            ctx.ebay_client, built_trading = build_live_clients(args.store)
            if ctx.trading_client is None:
                ctx.trading_client = built_trading
        preflight(args, ctx)
        lock_fd, lock_path = acquire_tool_lock(ctx)
        if getattr(args, "apply", False):
            refuse_if_writer_locks(ctx)
        conn = connect_db(ctx)
        try:
            plans: list[SkuPlan] = []
            worst = EXIT_OK
            for sku in skus:
                try:
                    ops = select_ops_for_sku(args, sku, manifest)
                    plan = snapshot_and_plan(conn, sku, ops, ctx, args)
                except ToolRefused as exc:
                    plan = SkuPlan(sku=sku, status="refused", refuse_reason=exc.reason)
                    add_gate(plan, exc.reason, False, exc.reason)
                    worst = max(worst, exc.exit_code)
                plans.append(plan)
                print_plan(plan)
                if plan.status == "refused":
                    worst = max(worst, EXIT_REFUSED)
            writable = [
                plan
                for plan in plans
                if plan.status == "planned" and (plan.db_changed or plan.live_changed)
            ]
            if getattr(args, "apply", False) and writable:
                try:
                    backup_info = backup_run(ctx, args, plans, run_id, stamp)
                except Exception as exc:
                    raise ToolRefused(f"backup_failed:{exc}") from exc
                rows_before = json.loads((backup_info["dir"] / "rows_before.json").read_text(encoding="utf-8"))
                for plan in plans:
                    if plan.status != "planned":
                        continue
                    if plan.db_changed:
                        code = apply_db_write(conn, plan, ctx, args, run_id, rows_before)
                        if plan.status in {"applied", "restored", "restore_failed"}:
                            wrote_anything = True
                        print(f"写库: {plan.sku} {plan.status} db_readback={plan.db_readback}")
                    else:
                        code = EXIT_OK
                    if code >= EXIT_RESTORED:
                        if code == EXIT_RESTORE_FAILED:
                            print(
                                f"RESTORE_FAILED backup={backup_info.get('dir')} "
                                f"db={backup_info.get('db', {}).get('path')}"
                            )
                        worst = max(worst, code)
                        if getattr(args, "stop_on_error", True):
                            break
                    if code == EXIT_OK and getattr(args, "push_live", False) and (
                        plan.status == "applied" or plan.live_changed
                    ):
                        live_code = apply_live_push(plan, ctx, args)
                        wrote_anything = True
                        if live_code != EXIT_OK:
                            db_restored = False
                            if plan.db_changed:
                                db_restored = restore_db_row(conn, plan.sku, rows_before)
                            plan.db_readback = {
                                **plan.db_readback,
                                "db_restored": db_restored,
                            }
                            if not db_restored and plan.db_changed:
                                live_code = EXIT_RESTORE_FAILED
                            if live_code == EXIT_RESTORE_FAILED:
                                print(
                                    f"RESTORE_FAILED backup={backup_info.get('dir')} "
                                    f"db={backup_info.get('db', {}).get('path')}"
                                )
                            worst = max(worst, live_code)
                            if getattr(args, "stop_on_error", True):
                                break
                        else:
                            print(f"直播: {plan.sku} live_readback={plan.live_readback}")
                after_payload = {
                    plan.sku: {
                        "db": load_row(conn, plan.sku),
                        "live_readback": plan.live_readback,
                    }
                    for plan in plans
                }
                write_json(backup_info["dir"] / "after.json", after_payload)
            report = {
                "run_id": run_id,
                "store": args.store,
                "mode": mode,
                "ebay_environment": os.environ.get("EBAY_ENVIRONMENT"),
                "note": getattr(args, "note", ""),
                "backup_dir": str(backup_info.get("dir") or ""),
                "db_backup": backup_info.get("db") or {},
                "stamp_utc": stamp,
                "stamp_local": stamp_local,
                "skus": [plan_to_report(plan) for plan in plans],
            }
            write_json(report_path, report)
            if backup_info.get("dir"):
                write_json(backup_info["dir"] / Path(report_path.name), report)
            if worst == EXIT_OK and any(plan.status == "refused" for plan in plans) and not any(
                plan.status == "applied" for plan in plans
            ):
                worst = EXIT_REFUSED
            return worst
        finally:
            conn.close()
    except ToolRefused as exc:
        if wrote_anything:
            print(
                f"RESTORE_FAILED backup={backup_info.get('dir')} "
                f"db={backup_info.get('db', {}).get('path')} after write: {exc.reason}"
            )
            return EXIT_RESTORE_FAILED
        print(f"已拒绝: {exc.reason}")
        report = {
            "run_id": run_id,
            "store": getattr(args, "store", ""),
            "mode": mode,
            "ebay_environment": os.environ.get("EBAY_ENVIRONMENT"),
            "note": getattr(args, "note", ""),
            "backup_dir": "",
            "db_backup": {},
            "stamp_utc": stamp,
            "stamp_local": stamp_local,
            "skus": [{"sku": "*", "status": "refused", "refuse_reason": exc.reason, "gates": [], "diff": {}, "ops": []}],
        }
        try:
            write_json(report_path, report)
        except Exception:
            pass
        return exc.exit_code
    except Exception as exc:
        if wrote_anything:
            print(
                f"RESTORE_FAILED backup={backup_info.get('dir')} "
                f"db={backup_info.get('db', {}).get('path')} after write: {exc}"
            )
            return EXIT_RESTORE_FAILED
        print(f"已拒绝: {exc}")
        return EXIT_REFUSED
    finally:
        if lock_fd is not None and lock_path is not None:
            release_tool_lock(lock_fd, lock_path)


# Silence unused live_price import if tests patch claim_strip helpers.
_ = live_price
