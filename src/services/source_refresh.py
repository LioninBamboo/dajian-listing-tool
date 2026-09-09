"""Source snapshot refresh service.

Re-fetches GIGA/Dajian product detail for published SKUs, detects content
drift (seller edited title / params / copy / video), and repairs the local
``collected_products`` source snapshot so downstream audits compare against
the supplier's *current* truth instead of the collection-time freeze.

Shared by:
  - scripts/source_content_refresh.py  (scheduled batch refresh)
  - scripts/order_source_recheck.py    (order-triggered per-SKU recheck)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from src.utils.dimension_helpers import extract_dajian_measurements
from src.utils.title_sanitizer import strip_supplier_brand_prefix


# Characters that only appear when a GBK/UTF-8 mixup corrupted the stored
# snapshot (seen in pre-2026-07 web-scraped descriptions).
_MOJIBAKE_MARKERS = ("�", "ӧ", "谷«", "Êô", "ʿ")

_ATTR_DIM_KEYS = (
    ("assembledLength", "Assembled Length (in.)"),
    ("assembledWidth", "Assembled Width (in.)"),
    ("assembledHeight", "Assembled Height (in.)"),
    ("productWeight", "Product Weight (lbs.)"),
)

_SPEC_DIM_KEYS = (
    ("length", "Package Length (in.)"),
    ("width", "Package Width (in.)"),
    ("height", "Package Height (in.)"),
    ("packageWeight", "Package Weight (lbs.)"),
)


@dataclass
class SourceSnapshot:
    """Normalized view of a fresh Dajian product detail."""

    sku: str
    title: str
    description_html: str | None
    attributes: dict[str, str]
    specs: dict[str, str]
    videos: list[str]
    sku_available: bool
    mpn: str
    characteristics: list[str] = field(default_factory=list)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def has_mojibake(text: str) -> bool:
    if not text:
        return False
    return any(marker in text for marker in _MOJIBAKE_MARKERS)


def _parses_positive_float(text: str) -> bool:
    try:
        return float(text) > 0
    except (TypeError, ValueError):
        return False


def _values_equal(old: str, new: str) -> bool:
    """Numeric-aware equality: 177.16 vs 177.2 is storage rounding, not drift."""
    if old == new:
        return True
    try:
        return abs(float(old) - float(new)) <= 0.1
    except (TypeError, ValueError):
        return False


def _media_url_key(url: str) -> str:
    """GigaB2B media URLs carry rotating signature params (x-ct/x-cs); the
    stable identity is scheme+host+path."""
    from urllib.parse import urlparse

    parsed = urlparse(str(url or "").strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _normalized_text(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def build_source_snapshot(detail: Mapping[str, Any] | None) -> SourceSnapshot | None:
    """Build a normalized snapshot from a Dajian ``detailInfo`` payload.

    Returns None when the payload is unusable (missing SKU or product name),
    so callers never clobber a stored snapshot with an empty fetch.
    """
    detail = dict(detail or {})
    sku = _clean_text(detail.get("sku"))
    raw_title = _clean_text(detail.get("productName"))
    if not sku or not raw_title:
        return None

    title, _, _ = strip_supplier_brand_prefix(raw_title)

    measurements = extract_dajian_measurements(detail)
    attributes: dict[str, str] = {}
    for source_key, attr_key in _ATTR_DIM_KEYS:
        # Prefer the supplier's raw value so precision is preserved
        # (extract_dajian_measurements rounds 177.16 -> 177.2, which would
        # read as drift against unrounded collection-time snapshots).
        raw = _clean_text(detail.get(source_key))
        value = raw if _parses_positive_float(raw) else measurements.get(source_key)
        if value:
            attributes[attr_key] = str(value)
    for key, value in (detail.get("attributes") or {}).items():
        text = _clean_text(value)
        if text:
            attributes[_clean_text(key)] = text
    main_color = _clean_text(detail.get("mainColor"))
    main_material = _clean_text(detail.get("mainMaterial"))
    if main_color:
        attributes.setdefault("Main Color", main_color)
    if main_material:
        attributes.setdefault("Main Material", main_material)

    specs: dict[str, str] = {}
    raw_spec_fields = {"length": "length", "width": "width", "height": "height", "packageWeight": "weight"}
    for source_key, spec_key in _SPEC_DIM_KEYS:
        raw = _clean_text(detail.get(raw_spec_fields[source_key]))
        value = raw if _parses_positive_float(raw) else measurements.get(source_key)
        if value:
            specs[spec_key] = str(value)

    videos: list[str] = []
    for candidate in [detail.get("productVideoUrl"), *(detail.get("videoUrls") or [])]:
        url = _clean_text(candidate)
        if url and url not in videos:
            videos.append(url)

    characteristics = [
        _clean_text(item) for item in (detail.get("characteristics") or []) if _clean_text(item)
    ]
    combo_info = detail.get("comboInfo") or []
    if isinstance(combo_info, list) and combo_info:
        specs["Combo Box Count"] = str(len(combo_info))
        if detail.get("comboFlag"):
            attributes.setdefault("Product Type", "Combo Item")
        combo_note = (
            f"Ships as {len(combo_info)} separate carton(s); "
            "components must be connected before use."
        )
        if combo_note not in characteristics:
            characteristics.append(combo_note)

    description_html = _build_description_html(detail, characteristics, attributes, specs)

    return SourceSnapshot(
        sku=sku,
        title=title,
        description_html=description_html,
        attributes=attributes,
        specs=specs,
        videos=videos,
        sku_available=bool(detail.get("skuAvailable", True)),
        mpn=_clean_text(detail.get("mpn")),
        characteristics=characteristics,
    )


def _build_description_html(
    detail: Mapping[str, Any],
    characteristics: list[str],
    attributes: Mapping[str, str],
    specs: Mapping[str, str],
) -> str | None:
    """Canonical source-description snapshot: features first, then specs."""
    api_description = str(detail.get("description") or "").strip()
    if not characteristics and not api_description:
        return None

    parts = ["<div>"]
    if characteristics:
        parts.append("<h3>Product Features</h3><ul>")
        parts.extend(f"<li>{item}</li>" for item in characteristics)
        parts.append("</ul>")
    if api_description:
        parts.append(api_description)
    spec_lines = {**attributes, **specs}
    mpn = _clean_text(detail.get("mpn"))
    if mpn:
        spec_lines["MPN"] = mpn
    if spec_lines:
        parts.append("<h3>Specifications</h3><ul>")
        parts.extend(f"<li>{key}: {value}</li>" for key, value in spec_lines.items())
        parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def _parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def diff_source_row(
    row: Mapping[str, Any],
    snapshot: SourceSnapshot,
    *,
    first_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Field-level drift between the stored source snapshot and a fresh one.

    ``row`` needs the collected_products columns: title, description,
    attributes, specs, videos.

    Each entry carries a ``kind``:
      - ``enrichment``: the field was never captured at collection time
      - ``repair``: the stored snapshot is corrupted (mojibake)
      - ``normalize``: first refresh replaces a processed/truncated stored
        value with the supplier's faithful current value (not seller action)
      - ``change``: the supplier genuinely changed the value — alert-worthy
    """
    drifts: list[dict[str, Any]] = []

    old_title = _clean_text(row.get("title"))
    if old_title and snapshot.title and old_title != snapshot.title:
        drifts.append(
            {
                "field": "title",
                "old": old_title,
                "new": snapshot.title,
                "kind": "normalize" if first_refresh else "change",
            }
        )

    old_attrs = _parse_json(row.get("attributes"), {})
    for key, new_value in snapshot.attributes.items():
        old_value = _clean_text(old_attrs.get(key))
        if not old_value:
            drifts.append({"field": f"attributes.{key}", "old": None, "new": new_value, "kind": "enrichment"})
        elif not _values_equal(old_value, new_value):
            drifts.append({"field": f"attributes.{key}", "old": old_value, "new": new_value, "kind": "change"})

    old_specs = _parse_json(row.get("specs"), {})
    for key, new_value in snapshot.specs.items():
        old_value = _clean_text(old_specs.get(key))
        if not old_value:
            drifts.append({"field": f"specs.{key}", "old": None, "new": new_value, "kind": "enrichment"})
        elif not _values_equal(old_value, new_value):
            drifts.append({"field": f"specs.{key}", "old": old_value, "new": new_value, "kind": "change"})

    old_videos = [_clean_text(v) for v in _parse_json(row.get("videos"), []) if _clean_text(v)]
    if {_media_url_key(v) for v in old_videos} != {_media_url_key(v) for v in snapshot.videos}:
        drifts.append({"field": "videos", "old": old_videos, "new": snapshot.videos, "kind": "change"})

    old_description = str(row.get("description") or "")
    if snapshot.description_html is not None:
        old_norm = _normalized_text(old_description)
        if has_mojibake(old_description):
            drifts.append(
                {"field": "description", "old": "<mojibake snapshot>", "new": "<rebuilt from API>", "kind": "repair"}
            )
        elif snapshot.characteristics and not all(
            _normalized_text(item) in old_norm for item in snapshot.characteristics
        ):
            drifts.append(
                {
                    "field": "description",
                    "old": "<stored snapshot>",
                    "new": "<supplier copy changed>",
                    "kind": "normalize" if first_refresh else "change",
                }
            )

    if not snapshot.sku_available:
        drifts.append({"field": "sku_available", "old": True, "new": False, "kind": "change"})

    return drifts


def ensure_source_drift_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_drift_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            context TEXT NOT NULL,
            drift_json TEXT NOT NULL,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def drift_signature(drift: Mapping[str, Any]) -> str:
    """Stable identity of a change: same field settling on the same new value."""
    return f"{_clean_text(drift.get('field'))}={_clean_text(drift.get('new'))}"


def load_alerted_signatures(conn, skus: Iterable[str]) -> set[tuple[str, str]]:
    """Signatures of ``change`` drifts already reported in an earlier run.

    Needed because several drift kinds are recomputed from scratch every run
    rather than against stored state — a delisted source re-derives
    ``sku_available True->False`` forever. Without this the daily mail repeated
    the same SKUs indefinitely and genuinely new seller edits were invisible
    inside the noise (2026-07-20..22).
    """
    ensure_source_drift_table(conn)
    wanted = {s for s in skus if s}
    if not wanted:
        return set()
    seen: set[tuple[str, str]] = set()
    for sku, drift_json in conn.execute(
        "SELECT sku, drift_json FROM source_drift_log"
    ).fetchall():
        if sku not in wanted:
            continue
        try:
            entries = json.loads(drift_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("kind") == "change":
                seen.add((sku, drift_signature(entry)))
    return seen


def record_source_drift(conn, sku: str, drifts: list[dict[str, Any]], context: str) -> None:
    if not drifts:
        return
    ensure_source_drift_table(conn)
    conn.execute(
        "INSERT INTO source_drift_log (sku, context, drift_json) VALUES (?, ?, ?)",
        (sku, context, json.dumps(drifts, ensure_ascii=False)),
    )
    conn.commit()


def apply_snapshot(conn, sku: str, snapshot: SourceSnapshot, drifts: list[dict[str, Any]]) -> bool:
    """Write drifted source fields back to collected_products.

    Only source columns are touched (title/description/attributes/specs/videos)
    — never optimization, status, or pricing. Returns True when a write happened.
    """
    drift_fields = {d["field"].split(".", 1)[0] for d in drifts}
    updates: dict[str, Any] = {}

    if "title" in drift_fields and snapshot.title:
        updates["title"] = snapshot.title
    if "attributes" in drift_fields:
        row = conn.execute(
            "SELECT attributes FROM collected_products WHERE sku = ?", (sku,)
        ).fetchone()
        merged = _parse_json(row[0] if row else None, {})
        merged.update(snapshot.attributes)
        updates["attributes"] = json.dumps(merged, ensure_ascii=False)
    if "specs" in drift_fields:
        row = conn.execute(
            "SELECT specs FROM collected_products WHERE sku = ?", (sku,)
        ).fetchone()
        merged = _parse_json(row[0] if row else None, {})
        merged.update(snapshot.specs)
        updates["specs"] = json.dumps(merged, ensure_ascii=False)
    if "videos" in drift_fields:
        updates["videos"] = json.dumps(snapshot.videos, ensure_ascii=False)
    if "description" in drift_fields and snapshot.description_html:
        updates["description"] = snapshot.description_html

    if not updates:
        return False

    row = conn.execute("SELECT logs FROM collected_products WHERE sku = ?", (sku,)).fetchone()
    logs = _parse_json(row[0] if row else None, [])
    changed = ", ".join(sorted({d["field"] for d in drifts}))
    logs.append(f"Source refresh at {datetime.now().isoformat()}: drift in [{changed}]")
    updates["logs"] = json.dumps(logs, ensure_ascii=False)

    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE collected_products SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE sku = ?",
        (*updates.values(), sku),
    )
    conn.commit()
    return True


def refresh_skus(
    conn,
    dajian_client,
    skus: Iterable[str],
    *,
    apply: bool = True,
    context: str = "source_refresh",
    chunk_size: int = 200,
    sleep_between_chunks: float = 0.6,
) -> dict[str, Any]:
    """Batch-refresh source snapshots. Returns a per-SKU summary dict."""
    import time as _time

    sku_list = [s for s in dict.fromkeys(skus) if s]
    summary: dict[str, Any] = {
        "checked": 0,
        "drifted": {},      # every SKU with any drift entry (full detail, all kinds)
        "alerts": {},       # NEW kind=change entries — seller genuinely changed something
        "ongoing": {},      # kind=change already reported in an earlier run (do not re-alert)
        "unavailable": [],
        "fetch_failed": [],
        "applied": [],
    }
    already_alerted = load_alerted_signatures(conn, sku_list)

    for start in range(0, len(sku_list), chunk_size):
        chunk = sku_list[start : start + chunk_size]
        try:
            details = dajian_client.get_product_details(chunk)
        except Exception as exc:  # network/API failure: report, never clobber
            summary["fetch_failed"].extend(chunk)
            summary.setdefault("errors", []).append(f"{chunk[0]}..{chunk[-1]}: {exc}")
            continue

        by_sku = {_clean_text(d.get("sku")): d for d in details if isinstance(d, Mapping)}
        for sku in chunk:
            summary["checked"] += 1
            snapshot = build_source_snapshot(by_sku.get(sku))
            if snapshot is None:
                summary["fetch_failed"].append(sku)
                continue

            row = conn.execute(
                "SELECT title, description, attributes, specs, videos, logs FROM collected_products WHERE sku = ?",
                (sku,),
            ).fetchone()
            if row is None:
                summary["fetch_failed"].append(sku)
                continue
            row_map = dict(zip(("title", "description", "attributes", "specs", "videos", "logs"), row))
            first_refresh = "Source refresh at" not in str(row_map.get("logs") or "")

            drifts = diff_source_row(row_map, snapshot, first_refresh=first_refresh)
            if not snapshot.sku_available:
                summary["unavailable"].append(sku)
            if drifts:
                summary["drifted"][sku] = drifts
                changes = [d for d in drifts if d.get("kind") == "change"]
                if changes:
                    # Only alert on drifts not already reported. A delisted SKU
                    # re-reports sku_available True→False every single run (the
                    # diff compares against a hardcoded True), so without this
                    # the daily mail repeated the same ~20 SKUs indefinitely and
                    # new seller edits were lost in the noise (2026-07-20..22).
                    fresh, ongoing = [], []
                    for change in changes:
                        key = (sku, drift_signature(change))
                        (ongoing if key in already_alerted else fresh).append(change)
                        already_alerted.add(key)
                    if fresh:
                        summary["alerts"][sku] = fresh
                    if ongoing:
                        summary["ongoing"][sku] = ongoing
                record_source_drift(conn, sku, drifts, context)
                if apply and apply_snapshot(conn, sku, snapshot, drifts):
                    summary["applied"].append(sku)

        if start + chunk_size < len(sku_list) and sleep_between_chunks:
            _time.sleep(sleep_between_chunks)

    return summary
