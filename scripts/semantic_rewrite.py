#!/usr/bin/env python3
"""CLI for source-faithful semantic rewrite (P0 dry-run by default).

Live apply requires dual gate: --apply AND SEMANTIC_REWRITE_APPLY_ENABLED=1.
P0 must not set the environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from src.services.semantic_rewrite import (  # noqa: E402
    APPLY_ENV_FLAG,
    RewritePlan,
    SkipResult,
    apply_rewrite,
    dual_gate_allows_apply,
    dual_gate_block_reason,
    plan_rewrite,
    write_preview_markdown,
)


def _db_path() -> Path:
    return ROOT / "ebay_collection.db"


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def _make_dajian():
    from src.clients.dajian_client import DaJianClient

    cid = os.getenv("DAJIAN_API_KEY")
    sec = os.getenv("DAJIAN_API_SECRET")
    if not cid or not sec:
        return None
    return DaJianClient(cid, sec)


def _make_ebay():
    from src.clients.real_ebay_client import create_real_ebay_client

    env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper()
    return create_real_ebay_client(env)


def _read_sku_file(path: str) -> list[str]:
    skus = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            skus.append(s)
    return skus


# Issue types this pipeline can act on. Prefix matching alone silently drops
# any detector that does not happen to be named "semantic_*"/"claim_*": when
# source_aspect_mismatch and assembly_* landed on 2026-08-09 they produced 373
# of 376 CRITICAL rows and NONE of them reached the queue, so a run that should
# have had hundreds of candidates processed exactly one SKU. New detector, no
# route to any fixer, no error — the backlog just stopped moving.
_QUEUE_TYPE_PREFIXES: tuple[str, ...] = ("semantic_", "claim_")
_QUEUE_TYPE_SUBSTRINGS: tuple[str, ...] = ("hallucin",)
# Named explicitly so adding a detector is a deliberate decision, not an
# accident of naming. Keep in sync with audit_fix_active_listings issue types.
_QUEUE_TYPE_NAMES: frozenset[str] = frozenset({
    "source_aspect_mismatch",
    "assembly_required_mismatch",
    "assembly_package_conflict",
    "assembly_description_contradiction",
    "description_raw_source_dump",
    "incomplete_title",
})


def queue_type_is_actionable(issue_type: str) -> bool:
    """Can the rewrite pipeline do anything about this issue type?"""
    t = str(issue_type or "")
    if t in _QUEUE_TYPE_NAMES:
        return True
    if any(t.startswith(p) for p in _QUEUE_TYPE_PREFIXES):
        return True
    return any(s in t for s in _QUEUE_TYPE_SUBSTRINGS)


def _score_audit_for_queue(data: dict) -> dict[str, int]:
    scores: dict[str, int] = {}
    for row in data.get("issues") or []:
        sku = row.get("sku")
        if not sku:
            continue
        points = 0
        for iss in row.get("issues") or []:
            if not isinstance(iss, dict):
                continue
            if not queue_type_is_actionable(iss.get("type")):
                continue
            sev = str(iss.get("severity") or "").upper()
            if sev == "CRITICAL":
                points += 10
            elif sev == "HIGH":
                points += 1
        if points:
            scores[sku] = scores.get(sku, 0) + points
    return scores


def pick_latest_full_corpus_audit(
    files: list[Path],
    *,
    min_published: int = 500,
) -> tuple[Path, dict[str, int]] | None:
    """Among non-empty audits, prefer full-corpus reports and take newest mtime.

    Full-corpus = total_published > min_published (default 500). Within that set
    (and for the non-full fallback set), always pick highest st_mtime so a newer
    daily job cannot lose to an older fatter report ranked by issue_rows.
    """
    full: list[tuple[float, Path, dict[str, int]]] = []
    other: list[tuple[float, Path, dict[str, int]]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        scores = _score_audit_for_queue(data)
        if not scores:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        published = int(data.get("total_published") or 0)
        entry = (mtime, path, scores)
        if published > min_published:
            full.append(entry)
        else:
            other.append(entry)
    pool = full if full else other
    if not pool:
        return None
    pool.sort(key=lambda x: x[0], reverse=True)
    _mtime, chosen, scores = pool[0]
    return chosen, scores


def _parse_generated_at(value) -> float | None:
    """Parse report generated_at to a local naive timestamp, or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.timestamp()


def _report_is_full_daily_scope(data: dict) -> bool:
    scope = data.get("scope")
    if scope is None:
        try:
            published = int(data.get("total_published") or 0)
        except (TypeError, ValueError):
            published = 0
        return published >= 20
    return str(scope).strip() == "full"


_LAST_DAILY_AUDIT_SKIP = "SKIP: no fresh daily live_audit"


def last_daily_audit_skip_message() -> str:
    return _LAST_DAILY_AUDIT_SKIP


def _report_timestamp(path: Path, data: dict | None, clock: datetime) -> float | None:
    age_ts = _parse_generated_at((data or {}).get("generated_at")) if data else None
    if age_ts is None:
        try:
            age_ts = path.stat().st_mtime
        except OSError:
            return None
    return age_ts


_AUDIT_FILENAME_DATE_RE = re.compile(r"listing_audit_fix_(\d{8})")


def _file_in_picker_window(path: Path, clock: datetime, allow_yesterday: bool) -> bool:
    """Cheap pre-filter so the picker does not read every historical JSON."""
    name = getattr(path, "name", "") or ""
    match = _AUDIT_FILENAME_DATE_RE.search(name)
    if match:
        try:
            file_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            file_date = None
        if file_date is not None:
            if allow_yesterday:
                return file_date >= (clock - timedelta(hours=36)).date()
            return file_date == clock.date()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if allow_yesterday:
        return mtime >= clock.timestamp() - 36 * 3600
    start = datetime(clock.year, clock.month, clock.day).timestamp()
    return mtime >= start


def _same_local_day_after_six(ts: float, clock: datetime) -> bool:
    try:
        generated = datetime.fromtimestamp(ts)
    except (TypeError, ValueError, OSError):
        return False
    if generated.date() != clock.date():
        return False
    return generated.hour >= 6


def pick_latest_daily_live_audit(
    files: list[Path],
    *,
    max_age_hours: float = 36,
    now: datetime | None = None,
    allow_yesterday: bool = False,
) -> tuple[Path, dict] | None:
    """Newest same-day full-store live_audit after 06:00.

    36h is only an upper bound for the manual ``--from-daily-audit`` CLI when
    ``allow_yesterday`` is on. Scheduled tasks never accept yesterday.

    If any ``listing_audit_fix_*.json`` newer than the chosen report is
    unparseable, or is a full live_audit missing the ``issues`` key, fail
    closed.
    """
    global _LAST_DAILY_AUDIT_SKIP
    _LAST_DAILY_AUDIT_SKIP = "SKIP: no fresh daily live_audit"
    clock = now or datetime.now()
    try:
        window_hours = float(max_age_hours)
    except (TypeError, ValueError):
        window_hours = 36.0
    cutoff = clock.timestamp() - window_hours * 3600

    indexed: list[tuple[float, Path, dict | None, str | None]] = []
    for path in files or []:
        if path is None:
            continue
        name = getattr(path, "name", "") or ""
        if "listing_audit_fix_" not in name:
            continue
        if not _file_in_picker_window(path, clock, allow_yesterday):
            continue
        parse_error = None
        data = None
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
            else:
                parse_error = f"{path.name}: not an object"
        except Exception as exc:
            parse_error = f"{path.name}: {exc}"
        ts = _report_timestamp(path, data, clock)
        if ts is None:
            continue
        indexed.append((ts, path, data, parse_error))

    if not indexed:
        return None
    indexed.sort(key=lambda item: item[0], reverse=True)

    candidates: list[tuple[float, Path, dict]] = []
    for ts, path, data, parse_error in indexed:
        if parse_error or data is None:
            continue
        if str(data.get("mode") or "") != "live_audit":
            continue
        if not _report_is_full_daily_scope(data):
            continue
        if ts < cutoff:
            continue
        if allow_yesterday:
            pass
        elif not _same_local_day_after_six(ts, clock):
            continue
        candidates.append((ts, path, data))

    if not candidates:
        return None
    _ts, chosen, data = candidates[0]

    for ts, path, newer_data, parse_error in indexed:
        if ts <= _ts:
            continue
        if parse_error:
            logger_name = path.name
            _LAST_DAILY_AUDIT_SKIP = f"SKIP: no fresh daily live_audit (unparseable newer {logger_name})"
            print(f"[pick] fail-closed: newer report unparseable: {logger_name}")
            return None
        if (
            newer_data is not None
            and str(newer_data.get("mode") or "") == "live_audit"
            and _report_is_full_daily_scope(newer_data)
            and "issues" not in newer_data
        ):
            _LAST_DAILY_AUDIT_SKIP = (
                f"SKIP: no fresh daily live_audit (partial newer {path.name} missing issues)"
            )
            print(f"[pick] fail-closed: newer full live_audit missing issues key: {path.name}")
            return None

    issues = data.get("issues")
    if "issues" not in data:
        _LAST_DAILY_AUDIT_SKIP = f"SKIP: no fresh daily live_audit (partial {chosen.name} missing issues)"
        return None
    if not isinstance(issues, list) or not issues:
        _LAST_DAILY_AUDIT_SKIP = f"SKIP: fresh daily live_audit has 0 issues ({chosen.name})"
        return None
    return chosen, data


def derive_queue_from_latest_audit(
    limit: int | None = None,
    picked: tuple[Path, dict] | None = None,
    allow_yesterday: bool = False,
) -> list[str]:
    """Project §8.2: latest fresh daily live_audit → rewrite queue.

    Accepts an already-picked ``(path, data)`` from ``main()`` so
    ``--allow-yesterday`` is not lost on the second picker call.
    """
    if picked is None:
        logs = ROOT / "logs"
        files = list(logs.glob("listing_audit_fix_*.json"))
        if not files:
            return []
        picked = pick_latest_daily_live_audit(files, allow_yesterday=allow_yesterday)
    if not picked:
        return []
    chosen, data = picked
    scores = _score_audit_for_queue(data)
    ordered = sorted(scores.keys(), key=lambda s: scores[s], reverse=True)
    if limit:
        ordered = ordered[:limit]
    out = ROOT / "logs" / "semantic_rewrite_queue.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = ROOT / "logs" / "semantic_rewrite_queue_source.txt"
    meta.write_text(f"{chosen.name}\n{len(ordered)} skus\n", encoding="utf-8")
    out.write_text("\n".join(ordered) + ("\n" if ordered else ""), encoding="utf-8")
    print(
        f"[queue] source={chosen.name} generated_at={data.get('generated_at')} "
        f"total_published={data.get('total_published')} skus={len(ordered)}"
    )
    return ordered


def _exclude_done_and_human(skus: list[str]) -> list[str]:
    """Drop SKUs already DONE in progress or listed in human_queue."""
    done: set[str] = set()
    progress = ROOT / "logs" / "semantic_rewrite_run_progress.jsonl"
    if progress.is_file():
        latest: dict[str, str] = {}
        for line in progress.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            sku = row.get("sku")
            if sku:
                latest[sku] = str(row.get("result") or "")
        done = {s for s, r in latest.items() if r == "DONE"}
    human: set[str] = set()
    hq = ROOT / "logs" / "semantic_rewrite_human_queue.txt"
    if hq.is_file():
        for line in hq.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.replace("\t", " ").split()
            if parts:
                human.add(parts[0].strip())
    return [s for s in skus if s not in done and s not in human]


def classify_execution_status(stats: dict[str, int]) -> str:
    """Describe an execution outcome without conflating review work and errors."""
    if int(stats.get('applied_fail') or 0) > 0:
        return 'failed'
    if int(stats.get('human') or 0) > 0:
        if int(stats.get('applied_ok') or 0) > 0:
            return 'completed_with_review'
        return 'review_required'
    if int(stats.get('applied_ok') or 0) > 0:
        return 'completed'
    return 'no_change'


def scheduler_exit_code_for_status(
    from_daily_audit: bool,
    status: str,
    *,
    current_exit_code: int,
) -> int:
    """Let the scheduler record review work without raising a failure alarm."""
    if current_exit_code:
        return current_exit_code
    if from_daily_audit and status in {'review_required', 'completed_with_review'}:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Semantic rewrite pipeline (default dry-run)")
    p.add_argument("--sku", action="append", default=[], help="SKU (repeatable)")
    p.add_argument("--sku-file", help="Text file with one SKU per line")
    p.add_argument("--limit", type=int, help="Max SKUs to process")
    p.add_argument(
        "--derive-queue",
        action="store_true",
        help="Derive queue from latest listing_audit_fix_*.json → logs/semantic_rewrite_queue.txt",
    )
    p.add_argument(
        "--from-daily-audit",
        action="store_true",
        help="Derive incremental queue from latest non-empty audit, exclude DONE/human_queue, then process",
    )
    p.add_argument(
        "--allow-yesterday",
        action="store_true",
        default=False,
        help="For --from-daily-audit only: allow a report from the last 36h even if it is yesterday's",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Push to eBay (requires SEMANTIC_REWRITE_APPLY_ENABLED=1).",
    )
    p.add_argument(
        "--email",
        action="store_true",
        help="Email an HTML execution summary via src.utils.email_sender.send_email",
    )
    p.add_argument(
        "--preview-dir",
        default="",
        help="Write before/after markdown reports into this directory (dry-run)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    skus: list[str] = []
    if args.derive_queue and not args.from_daily_audit:
        skus = derive_queue_from_latest_audit(limit=args.limit)
        print(f"[queue] wrote {len(skus)} SKUs to logs/semantic_rewrite_queue.txt")
        # derive-only: do not process unless explicit --sku / --sku-file / --apply targets
        if not args.sku and not args.sku_file and not args.apply:
            return 0

    from_daily = bool(args.from_daily_audit)
    if from_daily:
        logs = ROOT / "logs"
        picked = pick_latest_daily_live_audit(
            list(logs.glob("listing_audit_fix_*.json")),
            allow_yesterday=bool(args.allow_yesterday),
        )
        if not picked:
            print(last_daily_audit_skip_message())
            return 0
        skus = derive_queue_from_latest_audit(
            limit=None,
            picked=picked,
            allow_yesterday=bool(args.allow_yesterday),
        )
        skus = _exclude_done_and_human(skus)
        if args.limit:
            skus = skus[: args.limit]
        print(f"[from-daily-audit] processing {len(skus)} SKUs after exclude DONE/human")

    if args.sku:
        skus.extend(args.sku)
    if args.sku_file:
        skus.extend(_read_sku_file(args.sku_file))
    # de-dupe preserve order
    seen = set()
    ordered = []
    for s in skus:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    skus = ordered
    if args.limit and not from_daily:
        skus = skus[: args.limit]

    if not skus:
        # Scheduler path: empty incremental queue is success (no false alarm).
        if from_daily and not args.sku and not args.sku_file:
            print("nothing to do (queue empty after exclusions)")
            return 0
        print("No SKUs specified. Use --sku / --sku-file / --derive-queue / --from-daily-audit.")
        return 2

    if args.apply and not dual_gate_allows_apply(cli_apply=True):
        print(f"ERROR: {dual_gate_block_reason(cli_apply=True)}")
        print(f"Refusing to apply. (env {APPLY_ENV_FLAG} must be exactly '1')")
        return 3

    conn = _open_db()
    dajian = _make_dajian()
    ebay = _make_ebay()
    preview_dir = Path(args.preview_dir) if args.preview_dir else None

    stats = {"planned": 0, "skip": 0, "human": 0, "applied_ok": 0, "applied_fail": 0}
    exit_code = 0
    for sku in skus:
        print(f"\n=== {sku} ===")
        stats["planned"] += 1
        result = plan_rewrite(conn, dajian, ebay, sku)
        if isinstance(result, SkipResult):
            print(f"SKIP [{result.code}] {result.reason}")
            stats["skip"] += 1
            if preview_dir:
                write_preview_markdown(result, preview_dir / f"{sku}.md")
            continue

        assert isinstance(result, RewritePlan)
        print(f"violations: {len(result.violations)} | pushable={result.pushable} needs_human={result.needs_human}")
        print(f"title after: {result.after.get('title')}")
        for n in result.notes:
            print(f"  - {n}")
        if preview_dir:
            write_preview_markdown(result, preview_dir / f"{sku}.md")
            print(f"preview → {preview_dir / (sku + '.md')}")

        if result.needs_human or not result.pushable:
            stats["human"] += 1
            continue

        if args.apply:
            ar = apply_rewrite(ebay, conn, result, cli_apply=True)
            print(f"APPLY: ok={ar.ok} {ar.reason}")
            if ar.ok:
                stats["applied_ok"] += 1
            else:
                stats["applied_fail"] += 1
                exit_code = 4

    conn.close()
    execution_status = classify_execution_status(stats)
    print(f"[status] {execution_status}")

    if args.email:
        try:
            from src.utils.email_sender import send_email

            body = (
                "<h2>Semantic rewrite run summary</h2>"
                f"<ul>"
                f"<li>planned: {stats['planned']}</li>"
                f"<li>skip: {stats['skip']}</li>"
                f"<li>human/not-pushable: {stats['human']}</li>"
                f"<li>applied_ok: {stats['applied_ok']}</li>"
                f"<li>applied_fail: {stats['applied_fail']}</li>"
                f"<li>apply={bool(args.apply)}</li>"
                f"<li>status: {execution_status}</li>"
                f"</ul>"
            )
            send_email(
                subject=(
                    f"[Semantic Rewrite] {execution_status} "
                    f"planned={stats['planned']} ok={stats['applied_ok']}"
                ),
                html_body=body,
            )
            print("[email] summary sent (or saved to reports/)")
        except Exception as exc:
            print(f"[email] failed: {exc}")

    return scheduler_exit_code_for_status(
        from_daily,
        execution_status,
        current_exit_code=exit_code,
    )


if __name__ == "__main__":
    raise SystemExit(main())
