"""Wall-clock budget and resume checkpoint for the catalog smart reprice.

Decision: a time budget of 0 or negative means *no budget* (unlimited).
That matches other reprice knobs in this repo (0 disables a guard) and
avoids a surprising "stop before the first SKU" footgun. Unset budget
resolves to ``DEFAULT_TIME_BUDGET_MIN`` (150). Invalid env values log a
warning and fall back to that default.

Checkpoint SKU identity is whole-value equality after ``str.strip()``.
``W465P194603`` and `` W465P194603 `` are the same; ``W465P19460`` is not
a match for ``W465P194603``. None / blank entries are skipped. Integers
are coerced to their decimal string (no crash). Booleans and other types
are skipped.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TIME_BUDGET_MIN = 150
TIME_BUDGET_ENV = "SMART_REPRICE_TIME_BUDGET_MIN"
CHECKPOINT_FILENAME = "smart_reprice_checkpoint.json"
CHECKPOINT_MAX_AGE_DAYS = 7

log = logging.getLogger("smart_reprice_budget")


def is_time_budget_exhausted(
    started_at: float,
    budget_min: float | None,
    *,
    now: float | None = None,
    clock=time.monotonic,
) -> bool:
    """True when elapsed seconds since ``started_at`` meet the minute budget.

    ``budget_min is None`` means unlimited. The check is intended to run
    *before* a SKU starts so PUT+verify are never split.
    """
    if budget_min is None:
        return False
    current = clock() if now is None else now
    return (current - started_at) >= (float(budget_min) * 60.0)


def resolve_time_budget_min(
    explicit: float | int | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> float | None:
    """Return budget minutes, or ``None`` for unlimited (no budget).

    Precedence: explicit function/CLI value, then ``SMART_REPRICE_TIME_BUDGET_MIN``,
    then ``DEFAULT_TIME_BUDGET_MIN``. ``0`` / negative => unlimited.
    """

    if explicit is not None:
        minutes, ok = _parse_minutes(explicit)
        if ok:
            return minutes
        log.warning("Invalid explicit time budget %r; falling back to env/default", explicit)

    env = os.environ if environ is None else environ
    raw = env.get(TIME_BUDGET_ENV) if isinstance(env, Mapping) else None
    if raw is not None and str(raw).strip() != "":
        minutes, ok = _parse_minutes(raw)
        if ok:
            return minutes
        log.warning(
            "Invalid %s=%r; using default %s minutes",
            TIME_BUDGET_ENV,
            raw,
            DEFAULT_TIME_BUDGET_MIN,
        )
    return float(DEFAULT_TIME_BUDGET_MIN)


def _parse_minutes(value: Any) -> tuple[float | None, bool]:
    """Return ``(minutes_or_None_unlimited, parsed_ok)``."""
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None, False
        if number <= 0:
            return None, True
        return number, True
    text = str(value).strip()
    if not text:
        return None, False
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None, False
    if math.isnan(number) or math.isinf(number):
        return None, False
    if number <= 0:
        return None, True
    return number, True


def normalize_sku(value: Any) -> str | None:
    """Canonical SKU string, or ``None`` if the value is not a usable SKU."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer() or math.isnan(value) or math.isinf(value):
            return None
        value = int(value)
    if isinstance(value, int):
        text = str(value).strip()
        return text or None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalize_sku_list(values: Any) -> list[str]:
    """Preserve order, skip invalid entries, dedupe by exact normalized SKU."""
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        return []
    if not isinstance(values, Sequence):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        key = normalize_sku(item)
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def checkpoint_path(project_root: Path | str) -> Path:
    return Path(project_root) / "reports" / CHECKPOINT_FILENAME


def write_json_atomic(path: Path | str, payload: dict[str, Any]) -> None:
    """Write JSON via a sibling tmp file then ``os.replace`` (Windows-safe)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, dest)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _parse_run_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_checkpoint(
    path: Path | str,
    *,
    now: datetime | None = None,
    max_age_days: int = CHECKPOINT_MAX_AGE_DAYS,
) -> dict[str, Any] | None:
    """Return a usable checkpoint, or ``None`` if it should be ignored.

    Missing, empty, corrupt, or older-than-``max_age_days`` files are ignored
    and logged. This function does not raise for those cases.
    """

    dest = Path(path)
    if not dest.exists() or not dest.is_file():
        return None
    try:
        raw = dest.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Ignoring unreadable smart-reprice checkpoint %s: %s", dest, exc)
        return None
    if not raw.strip():
        log.warning("Ignoring empty smart-reprice checkpoint %s", dest)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Ignoring corrupt smart-reprice checkpoint %s: %s", dest, exc)
        return None
    if not isinstance(payload, Mapping):
        log.warning("Ignoring smart-reprice checkpoint %s: payload is not an object", dest)
        return None

    run_date = _parse_run_date(payload.get("run_date"))
    if run_date is None:
        log.warning("Ignoring smart-reprice checkpoint %s: missing/invalid run_date", dest)
        return None

    current = now or datetime.now()
    age = current.date() - run_date
    if age > timedelta(days=max_age_days):
        log.warning(
            "Ignoring smart-reprice checkpoint older than %s days (run_date=%s, age=%s days)",
            max_age_days,
            run_date.isoformat(),
            age.days,
        )
        return None

    remaining_skus = normalize_sku_list(payload.get("remaining_skus"))
    return {
        "run_date": run_date.isoformat(),
        "remaining_skus": remaining_skus,
        "raw": dict(payload),
    }


def write_checkpoint(
    path: Path | str,
    remaining_skus: Sequence[Any],
    run_date: str | date | datetime,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        return
    if isinstance(run_date, datetime):
        date_text = run_date.date().isoformat()
    elif isinstance(run_date, date):
        date_text = run_date.isoformat()
    else:
        parsed = _parse_run_date(run_date)
        date_text = parsed.isoformat() if parsed else datetime.now().date().isoformat()
    payload = {
        "run_date": date_text,
        "remaining_skus": normalize_sku_list(remaining_skus),
        "updated_at": datetime.now().isoformat(),
    }
    write_json_atomic(path, payload)


def clear_checkpoint(path: Path | str, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    dest = Path(path)
    try:
        dest.unlink()
    except FileNotFoundError:
        return


def order_products_for_resume(
    products: Sequence[Mapping[str, Any]],
    remaining_skus: Sequence[Any],
) -> list[Mapping[str, Any]]:
    """Put still-published checkpoint SKUs first, then the rest, deduped.

    Checkpoint SKUs that are missing / no longer in ``products`` are skipped
    (not errors). Identity is exact normalized-string equality.
    """

    by_sku: dict[str, Mapping[str, Any]] = {}
    original_keys: list[str] = []
    invalid: list[Mapping[str, Any]] = []
    for product in products:
        key = normalize_sku(product.get("sku"))
        if key is None:
            invalid.append(product)
            continue
        if key in by_sku:
            continue
        by_sku[key] = product
        original_keys.append(key)

    seen: set[str] = set()
    ordered: list[Mapping[str, Any]] = []
    for raw in remaining_skus:
        key = normalize_sku(raw)
        if key is None or key in seen:
            continue
        product = by_sku.get(key)
        if product is None:
            continue
        ordered.append(product)
        seen.add(key)

    for key in original_keys:
        if key in seen:
            continue
        ordered.append(by_sku[key])
        seen.add(key)
    ordered.extend(invalid)
    return ordered
