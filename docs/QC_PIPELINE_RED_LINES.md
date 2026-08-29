# 质量巡检调度红线（三店通用）

## 禁止

- **禁止裸 `--fix`**：对 `audit_fix_active_listings.py` / 等价写回脚本，不传 `--fix-key`（或 Motors 侧等价白名单）等于允许全部 key，含 `categoryId`。历史事故：室内软包被改判 Outdoor Daybeds 并 republish。
- **禁止**把「内容审计」计划任务配成全量写 live。
- **禁止**三店共用同一个 Windows 任务名 / 同一个 Global Mutex（主店 `Dajian*`，GrovePop 必须用 `GrovePop*`）。

## 正确闭环

| 阶段 | 行为 | 邮件 |
|------|------|------|
| T0 只读审计 | `--live --email`，无 `--fix` | 标题含「内容审计」，`mode=live_audit` |
| T1 白名单写回 | `--source-report` + `--issue-type` + `--fix-key` + `--fix`（可加 `--limit`） | 标题含「修复」，分栏 applied / failed / residual |
| T2 专用腿 | 语义改写 / 缺视频限量 / Motors Fitment apply | 各自独立摘要 |
| 类目 | 只导出 `category_mismatch_manifest_*.csv`，人工 `--allow-category` | 不进自动腿 |

## 各店入口

### 主店 Dajian

- 调度：`Dajian Scheduler Daemon`（`scheduler_daemon.py`）
- Mutex：`Global\DajianSchedulerDaemonMutex`
- 日程：11:30 audit → 12:10 source_aspect（白名单+email）→ 12:30 semantic → 13:00 missing_video（limit 30）
- 次日验收：`python scripts/verify_listing_audit_pipeline_acceptance.py`

### GrovePop

- 调度：`GrovePop Scheduler Daemon` / `GrovePop Scheduler Watchdog`（`scheduler_profile: ops`）
- Mutex：`Global\GrovePopSchedulerDaemonMutex`（`store_profile.local.yaml` → `scheduler_mutex_name`）
- **ops 档允许任务**：`09:30 daily_tasks.py --ops-only`（增量库存同步 + 幽灵缺货恢复）、`每 6h order_source_recheck`
- **ops 档禁止**：MI/CRO/语义改写/`listing_audit` 写回/GIGA 履约推单（本期）
- 注册：`scripts/register_substore_ops_scheduler.ps1 -StorePrefix GrovePop`
- **`GrovePop Content QC` 必须保持 Disabled**（仅紧急只读；参数不得含裸 `--fix`）
- 验收：金丝雀 `python daily_tasks.py --ops-only`；`python scripts/order_source_recheck.py --sku <SKU> --dry-run`

### AquaRides (AutoParts)

- 调度：`AquaRides Scheduler Daemon` / `AquaRides Scheduler Watchdog`（`scheduler_profile: ops`）
- Mutex：`Global\AquaRidesSchedulerDaemonMutex`
- **ops 档允许任务**：同 GrovePop；出单复核走 `qc_profile: motors`（fitment 漂移检查）
- 注册：`scripts/register_substore_ops_scheduler.ps1 -StorePrefix AquaRides`
- **不走**家具全量 `listing_audit` 写回主路径；用 Trading / Fitment 专用任务：
  - `AquaRides Content QC`：`--email`（只读）
  - `AquaRides Content QC Fix`：`--fix --email --limit 30`（12:10）
  - `AquaRides Fitment Audit`：`--email`（只读+JSON）
  - `AquaRides Fitment Apply`：`--apply --email --limit 20`（仅 missing_fitment，带回读）
- 注册：`scripts/register_aquarides_content_qc.ps1`、`register_aquarides_content_qc_fix.ps1`、`register_aquarides_fitment_tasks.ps1`
