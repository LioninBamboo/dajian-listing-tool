"""Warm the eBay Browse market-price cache used by the pre-heart price gate.

Read-only w.r.t. listings: only calls Browse item_summary/search (+ item detail
inside QwenOptimizer.fetch_market_intelligence) with an APPLICATION token and
writes cache/browse_market_intel/<sha1>.json. No DB listing writes, no eBay
revise/end/publish, no email.

Refresh rule: an entry is re-queried only if missing, has no price_stats.median,
or is older than --max-age-days (default 7). Fresh entries are skipped.

Keyword sources (union, normalized with QwenOptimizer._extract_search_keywords,
deduped on the resulting search_words == cache key):
  1. niche kw strings that hit no_market_cache in pre_heart_price_*.json dry-runs
  2. three-store Terapeak niche tracks (reports/terapeak_niche_scout_rescanned_*.json)
  3. niche kw strings from niche checkpoints (cache/**/niche*filtered*/to_collect)
  4. curated common GIGA category phrases (from NA title phrase frequency 10/11-10/12),
     ordered by how often they appear in recent no_market_cache NA titles

  python scripts/warm_ebay_market_price_cache.py                 # run (budget 60)
  python scripts/warm_ebay_market_price_cache.py --dry-run       # plan only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)  # cache paths are cwd-relative
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SHANGHAI = timezone(timedelta(hours=8))

GIGA_CATEGORY_PHRASES = [
    "bathroom vanity ceramic sink", "bathroom vanity", "storage cabinet", "kitchen pantry cabinet",
    "sideboard buffet cabinet", "console table", "coffee table", "end table", "side table",
    "accent table", "dining table", "dining chairs", "outdoor dining table", "kitchen island",
    "platform bed frame", "loft bed", "bunk bed", "murphy bed", "queen bed frame",
    "modular sectional sofa", "sleeper sofa", "sofa bed", "chaise lounge", "swivel accent chair",
    "flush mount ceiling light", "pendant light", "bookshelf", "display cabinet glass doors",
    "shoe storage cabinet", "entryway bench", "hall tree", "coffee bar cabinet",
    "wine cabinet", "bar cabinet", "television stand", "nightstand", "dresser", "spice rack",
    "bathtub drain", "office desk", "gaming chair", "outdoor patio furniture",
]


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_keywords(norm) -> list[dict]:
    """Return ordered [{keyword, search_words, sources:[...], priority}]."""
    out: dict[str, dict] = {}

    def add(kw: str, source: str, priority: int):
        kw = (kw or "").strip()
        if not kw:
            return
        sw = norm(kw)
        if not sw:
            return
        row = out.get(sw)
        if row is None:
            out[sw] = {"keyword": kw, "search_words": sw, "sources": [source], "priority": priority}
        else:
            if source not in row["sources"]:
                row["sources"].append(source)
            row["priority"] = min(row["priority"], priority)

    # 1. no_market_cache misses from pre-heart dry-runs (niche kw) + NA miss titles
    miss_kw: dict[str, int] = {}
    miss_titles: list[str] = []
    for f in glob.glob("cache/**/pre_heart_price_*.json", recursive=True):
        d = _load_json(f) or {}
        for bucket in ("keep", "drop"):
            for r in d.get(bucket, []) or []:
                det = ((r.get("pre_heart_price") or {}).get("detail") or {})
                if not det.get("no_market_cache"):
                    continue
                if r.get("kw"):
                    miss_kw[r["kw"]] = miss_kw.get(r["kw"], 0) + 1
                elif r.get("title"):
                    miss_titles.append(str(r["title"]).lower())
    for kw, _n in sorted(miss_kw.items(), key=lambda x: -x[1]):
        add(kw, "no_market_cache_niche_kw", 0)

    # 2. Terapeak three-store niche tracks
    tp_files = sorted(glob.glob("reports/terapeak_niche_scout_rescanned_*.json")) or \
        sorted(glob.glob("reports/terapeak_niche_scout_*.json"))
    if tp_files:
        tp = _load_json(tp_files[-1]) or {}
        rows = list(tp.get("shortlist") or []) + list(tp.get("ranked") or [])
        for st_rows in (tp.get("by_store") or {}).values():
            rows += list(st_rows or [])
        for r in rows:
            add(r.get("keyword"), f"terapeak:{r.get('store')}", 1)

    # 3. niche kw strings from niche checkpoints
    for f in glob.glob("cache/**/*niche*.json", recursive=True):
        d = _load_json(f)
        rows = d if isinstance(d, list) else ((d or {}).get("filtered_all") or (d or {}).get("selected") or [])
        if not isinstance(rows, list):
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("kw"):
                add(r["kw"], "niche_checkpoint_kw", 2)

    # 4. curated GIGA category phrases, misses first
    def miss_hits(p):
        toks = p.split()
        return sum(1 for t in miss_titles if all(x in t for x in toks))
    for p in sorted(GIGA_CATEGORY_PHRASES, key=lambda p: -miss_hits(p)):
        add(p, "giga_category" + ("_na_miss" if miss_hits(p) else ""), 3 if miss_hits(p) else 4)

    return sorted(out.values(), key=lambda r: r["priority"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-queries", type=int, default=60)
    ap.add_argument("--max-age-days", type=float, default=7.0)
    ap.add_argument("--sleep", type=float, default=0.65)
    ap.add_argument("--allow-terapeak", action="store_true",
                    help="keep Terapeak fallback (its results are NOT cached, so off by default)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    started = _now_iso()
    t0 = time.time()
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    if not os.getenv("QWEN_API_KEY"):
        os.environ["QWEN_API_KEY"] = "warm-noop"  # we only call Browse
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass

    from qwen_optimizer import QwenOptimizer
    q = object.__new__(QwenOptimizer)
    if not args.allow_terapeak:
        q._terapeak_price_fallback = lambda search_words: {}

    cache_dir = PROJECT_ROOT / "cache" / "browse_market_intel"
    before = len(list(cache_dir.glob("*.json"))) if cache_dir.exists() else 0
    max_age = args.max_age_days * 86400

    plan = collect_keywords(q._extract_search_keywords)
    report = {
        "routine": "ebay-market-price-cache-warm",
        "erp": str(PROJECT_ROOT),
        "started": started,
        "max_age_days": args.max_age_days,
        "max_queries": args.max_queries,
        "dry_run": args.dry_run,
        "cache_files_before": before,
        "keywords_planned": len(plan),
        "skipped_fresh": 0,
        "skipped_budget": 0,
        "queried": 0,
        "ok_with_median": 0,
        "failed": 0,
        "hard_fail": False,
        "auth": None,
        "results": [],
    }

    # Mint ONE application token up front (read-only client-credentials) and
    # memoize it so fetch_market_intelligence never falls back to the user token.
    token_ok = False
    if not args.dry_run:
        try:
            from src.services.ebay_auth import EbayOAuthService
            env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
            tok = EbayOAuthService(env).get_application_token()
            token_ok = bool(tok)
            if token_ok:
                EbayOAuthService.get_application_token = lambda self, _t=tok: _t
            report["auth"] = {"environment": env, "application_token": token_ok}
        except Exception as exc:
            report["auth"] = {"environment": os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"),
                              "application_token": False, "error": str(exc)[:300]}
        if not token_ok:
            report["hard_fail"] = True
            report["hard_fail_reason"] = "application token unavailable (auth broken)"

    for row in plan:
        sw = row["search_words"]
        path = q._browse_cache_path(sw, None)
        payload = _load_json(path) if path.exists() else None
        age = None
        median = None
        if isinstance(payload, dict):
            age = time.time() - float(payload.get("cached_at") or 0)
            median = ((payload.get("data") or {}).get("price_stats") or {}).get("median")
        rec = dict(row, cache_file=path.name, age_days=round(age / 86400, 2) if age is not None else None)
        if median and age is not None and 0 <= age < max_age:
            rec["action"] = "skip_fresh"
            rec["median"] = median
            report["skipped_fresh"] += 1
            report["results"].append(rec)
            continue
        if args.dry_run or not token_ok:
            rec["action"] = "would_query" if args.dry_run else "not_queried_auth"
            report["results"].append(rec)
            continue
        if report["queried"] >= args.max_queries:
            rec["action"] = "skip_budget"
            report["skipped_budget"] += 1
            report["results"].append(rec)
            continue
        call_t = time.time()
        report["queried"] += 1
        try:
            mi = QwenOptimizer.fetch_market_intelligence(q, row["keyword"], None) or {}
            after = _load_json(path) if path.exists() else None
            wrote = isinstance(after, dict) and float(after.get("cached_at") or 0) >= call_t - 1
            new_med = ((after or {}).get("data") or {}).get("price_stats", {}).get("median") if wrote else None
            if wrote and new_med:
                rec["action"] = "queried_ok"
                rec["median"] = new_med
                rec["total_listings"] = (after.get("data") or {}).get("total_listings")
                report["ok_with_median"] += 1
            else:
                rec["action"] = "queried_no_median"
                rec["pricing_basis"] = mi.get("pricing_basis")
                rec["total_listings"] = mi.get("total_listings")
                report["failed"] += 1
        except Exception as exc:
            rec["action"] = "error"
            rec["error"] = str(exc)[:300]
            report["failed"] += 1
        report["results"].append(rec)
        print(f"[{report['queried']:>3}] {rec['action']:<18} {sw} median={rec.get('median')}", flush=True)
        time.sleep(args.sleep)

    if not args.dry_run and token_ok and report["queried"] > 0 and report["ok_with_median"] == 0:
        report["hard_fail"] = True
        report["hard_fail_reason"] = "zero successful Browse writes while work was needed"

    report["cache_files_after"] = len(list(cache_dir.glob("*.json"))) if cache_dir.exists() else 0
    report["sample_keywords"] = [r["search_words"] for r in report["results"][:12]]
    report["finished"] = _now_iso()
    report["elapsed_s"] = round(time.time() - t0, 1)
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d")
    out = PROJECT_ROOT / "cache" / f"warm_ebay_market_price_cache_{stamp}{'_dryrun' if args.dry_run else ''}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: report[k] for k in ("keywords_planned", "skipped_fresh", "skipped_budget", "queried",
                                      "ok_with_median", "failed", "hard_fail", "cache_files_before",
                                      "cache_files_after")}
    print("SUMMARY", json.dumps(summary), "REPORT", str(out), flush=True)
    return 2 if report["hard_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
