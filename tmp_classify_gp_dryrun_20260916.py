"""Classify GrovePop dry-run publish_results into live vs held_far vs fail."""
import json
import re
from pathlib import Path

results = json.loads(Path("logs/publish_results_20260916_181003.json").read_text(encoding="utf-8"))
log = Path("logs/batch_publish_20260916_175538.log").read_text(encoding="utf-8", errors="replace").splitlines()

by_sku = {}
cur = None
for line in log:
    m = re.search(r"\[(\d+)/\d+\]\s+(\S+)", line)
    if m:
        cur = m.group(2)
        by_sku.setdefault(cur, {})
        continue
    if not cur:
        continue
    m = re.search(
        r"\[PRICE\] Smart: \$([0-9.]+) \(strategy=([^,]+), margin=([^,]+), market=\$([0-9.]+), cost=\$([0-9.]+)\)",
        line,
    )
    if m:
        by_sku[cur].update(
            {
                "smart": float(m.group(1)),
                "strategy": m.group(2),
                "margin": m.group(3),
                "market": float(m.group(4)),
                "cost": float(m.group(5)),
            }
        )
    m = re.search(r"\[PRICE\] Raise smart \$([0-9.]+) -> safe15 \$([0-9.]+).*market=\$([0-9.]+)", line)
    if m:
        by_sku[cur]["smart"] = float(m.group(2))
        by_sku[cur]["market"] = float(m.group(3))
    m = re.search(r"\[PRICE\] Standard: \$([0-9.]+)", line)
    if m:
        by_sku[cur]["smart"] = float(m.group(1))
        by_sku[cur]["market"] = by_sku[cur].get("market") or 0
    m = re.search(r"DRY: Would publish at \$([0-9.]+)", line)
    if m:
        by_sku[cur]["dry_price"] = float(m.group(1))
    m = re.search(r"FAIL: (.+)$", line)
    if m:
        by_sku[cur]["fail"] = m.group(1).strip()

SOFT_HOLDS = {"W5727P465774"}
FAR = 1.5
live, held_far, held_fail, held_other, held_soft = [], [], [], [], []
for r in results:
    sku = r.get("sku")
    meta = by_sku.get(sku, {})
    item = {"sku": sku, "status": r.get("status"), "message": (r.get("message") or "")[:200], **meta}
    if sku in SOFT_HOLDS:
        held_soft.append({**item, "reason": "soft_hold"})
        continue
    if r.get("status") != "dry_run":
        held_fail.append(item)
        continue
    market = float(meta.get("market") or 0)
    price = float(meta.get("dry_price") or meta.get("smart") or 0)
    ratio = (price / market) if market > 0 else 999.0
    item["ratio"] = ratio
    item["price"] = price
    if market > 0 and ratio >= FAR:
        held_far.append(item)
    elif market <= 0:
        held_other.append({**item, "reason": "no_market"})
    else:
        live.append(item)

out = {
    "live": live,
    "held_far": held_far,
    "held_fail": held_fail,
    "held_other": held_other,
    "held_soft": held_soft,
    "counts": {
        "live": len(live),
        "held_far": len(held_far),
        "held_fail": len(held_fail),
        "held_other": len(held_other),
        "held_soft": len(held_soft),
        "total": len(results),
    },
}
Path("cache/giga_grovepop_ready_dryrun_classified_20260916.json").write_text(
    json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
)
live_skus = [x["sku"] for x in live]
Path("cache/giga_grovepop_ready_live_candidates_20260916.txt").write_text(
    "\n".join(live_skus) + ("\n" if live_skus else ""), encoding="utf-8"
)
print(json.dumps(out["counts"], ensure_ascii=False))
print("LIVE", live_skus)
print("FAR")
for x in held_far:
    print(f"  {x['sku']} price={x.get('price')} market={x.get('market')} ratio={x.get('ratio'):.2f}")
print("FAIL")
for x in held_fail:
    print(f"  {x['sku']}: {x.get('fail') or x.get('message')}")
print("OTHER", [x["sku"] for x in held_other])
print("SOFT", [x["sku"] for x in held_soft])
