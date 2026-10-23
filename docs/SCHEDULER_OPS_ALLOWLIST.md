# Ops scheduler allowlist

`scheduler_profile: ops` is fail-closed. Ads/QC flags can never *add* a daemon job by themselves. An optional ops job is registered only when it is on `scheduler_allowed_jobs` **and** its slice flag is true.

## Semantics

| `scheduler_profile` | Allowlist |
|---|---|
| missing / `full` / `daily` / unknown (e.g. a Main typo) | unrestricted (`None`). Main stays the 39-job furniture cadence. |
| `ops` with empty or missing `scheduler_allowed_jobs` | `{ops_daily, order_recheck}` |
| `ops` with only known names | that explicit set |
| `ops` with any unknown name | fail-closed back to `{ops_daily, order_recheck}` |
| YAML says ops but cannot be parsed | peek the text and fail-closed to the same two jobs |

Catalog names (`KNOWN_SCHEDULER_JOBS`): `ops_daily`, `order_recheck`, `smart_reprice`, `ad_restore`, `blacklist_cleanup`, `guard_anomaly`, `ads_enroll`, `smart_bid`, `bid_rollback`, `promotion_rotate`, `source_refresh`, `listing_audit`, `source_aspect_autofix`, `semantic_rewrite`, `missing_video_autofix`.

`--task` aliases are mapped before the allowlist check: `daily` → `ops_daily` on ops (else `daily_tasks`), `reprice` → `smart_reprice`, `promotion` → `promotion_rotate`, `health` → `health_check`, `cro_ops` → `cro_ops_snapshot`. Recovery and watchdog use the same allowlist (watchdog maps `reprice` → `smart_reprice`).

To turn on detect-only Content QC later, set on the ZBook local YAML (not in this PR):

```yaml
server:
  scheduler_profile: ops
  scheduler_enable_qc_autofix: true
  scheduler_allowed_jobs: [ops_daily, order_recheck, listing_audit]
```

`source_aspect_autofix` is a live `--fix` write and needs its own explicit yes. This PR does not change GP/Aqua YAML and does not add anything to the default list.

## Out-of-daemon Windows tasks (not gated here)

These are Task Scheduler entries, not `scheduler_daemon` jobs. `KNOWN_SCHEDULER_JOBS` does not include them. PR-A must not register, gate, or change them.

Aqua (Trading / fitment / finance). The in-repo entry points are `scripts/trading_content_qc.py` and `scripts/fitment_audit.py`; this allowlist still does not gate them:

- `AquaRides Content QC` (`trading_content_qc.py --email`)
- `AquaRides Content QC Fix` (`--fix --email --limit 30`, live Trading writes)
- `AquaRides Fitment Audit`
- `AquaRides Fitment Apply` (`--apply --limit 20`, live writes)
- `AquaRides 5pct Discount Rotate`
- `AquaRides Finance Sync`

GrovePop Content QC (`audit_fix_active_listings.py --live --email --record-clean-state --exit-zero-on-issues`) is also out-of-daemon. It has **no `--fix`**, so it makes no eBay writes. This PR leaves that Windows task as-is and does **not** enable the daemon QC slice (`listing_audit` / `source_aspect_autofix` / `semantic_rewrite` / `missing_video_autofix` / `source_refresh`) on GP.
