# Dajian Listing Tool 使用指南

这份文档面向当前实际操作者。目标不是复述历史方案，而是说明现在这套系统怎么启动、怎么跑日常运营、怎么处理 READY 发布、怎么使用 MI，以及遇到故障时先看哪里。

## 1. 启动与停止

### 日常启动

```bash
start.bat
```

`start.bat` 当前会启动四个核心组件：

| 步骤 | 组件 | 说明 |
|------|------|------|
| 1 | FastAPI (`server.py`) | 采集、发布、OAuth、状态接口 |
| 2 | Streamlit (`app.py`) | 人工操作界面 |
| 3 | 调度守护进程 (`scheduler_daemon.py`) | 长驻后台，负责定时编排 |
| 4 | 看门狗 (`scheduler_watchdog.py`) | 检查守护进程是否掉线并补拉起 |

说明：

- 默认自动化入口是 `scheduler_daemon.py`，不是旧的一次性 `daily_tasks.py` 窗口。
- `start.bat` 会尽量复用已有进程，避免重复起多个窗口。
- `daily_tasks.py` 是日常工作负载入口，不是长期驻留调度器。

### 停止服务

```bash
stop.bat
```

### 查看调度状态

```bash
python scheduler_daemon.py --status
```

### 重新注册计划任务 / 看门狗

首次配置或系统任务丢失时：

```bash
setup_scheduled_tasks.bat
```

## 2. 当前产品工作流

```text
浏览器扩展 / 供应商页面
        |
        v
POST /api/collect
        |
        v
server.py -> ebay_collection.db
        |
        v
COLLECTED
        |
        v
daily_tasks.py / qwen_optimizer.py
        |
        v
READY
        |
        v
scripts/audit_fix_ready_drafts.py
        |
        v
batch_publish.py / POST /api/publish/{sku}
        |
        v
PUBLISHED
        |
        +--> inventory sync
        +--> smart repricing
        +--> listing audit
        +--> health check
        +--> taxonomy repair
        +--> market intelligence digest / alerts
```

### 状态流转

| 状态 | 含义 |
|------|------|
| `PENDING` | 已接收，等待处理 |
| `COLLECTED` | 已落库，等待分析 |
| `READY` | 已生成刊登素材，但仍需发布前审计 |
| `PUBLISHED` | 已上线 |
| `ENDED` / `DELISTED` | 已结束或主动下架 |
| `ERROR` | 处理失败，需要人工介入 |

关键点：`READY` 不等于“直接可发”。安全路径仍然是先审计、再 dry-run、再 live publish。

## 3. 当前自动调度时间表

当前以 `scheduler_daemon.py` 为准：

| 时间 | 任务标签 | 说明 |
|------|----------|------|
| `09:30` | `daily_tasks` | 全量工作负载，含分析、增量库存同步、全量缺货审核、审计、日报、MI 快照；智能调价仅周一/周四执行 |
| `09:40` | `ad_restore` | 广告恢复审计与修复 |
| `10:05` | `mi_check` | MI 自检，检查快照 / digest / 长周期 trend / 状态文件 |
| `11:30` | `listing_audit` | `scripts/audit_fix_active_listings.py --live --email`，detect-only；对 live eBay listing 与 GIGA 原文做内容核对并发邮件 |
| `12:10` | `source_aspect_autofix` | 白名单 `--fix-key` + `--email --fix`（禁止裸 `--fix`）；见 `docs/QC_PIPELINE_RED_LINES.md` |
| `12:30` | `semantic_rewrite` | 语义改写闭环 `--from-daily-audit --limit 80 --apply --email` |
| `13:00` | `missing_video_autofix` | 缺视频限量同步 `--fix-key __sync_video__ --limit 80 --email --fix` |
| `09:45` | `bl_cleanup` | 广告黑名单自动清理 |
| `09:50` | `guard_alert` | 守门员异常率告警 |
| `09:55` | `cro_monthly_report` | CRO 效果验证 / 阈值反馈门控 |
| `10:00` | `cro_consume` | 执行 CRO 队列中的 `price_drop` |
| `10:15` | `cro_image_refresh` | 执行 CRO 队列中的主图刷新 |
| `10:20` | `cro_fill_specifics` | 执行 CRO 队列中的 specifics 修复 |
| `10:25` | `cro_promote` | 执行 CRO 队列中的 promote；已推广提 bid，未推广走安全开广告 |
| `10:30` | `cro_sentinel` | 输出 CRO 告警与恶化 SKU |
| 每 `2h` | `auto_analyze` | `daily_tasks.py --analyze-only` |
| `20:00` | `health_check` | `scripts/sales_health_check.py --auto-fix --email` |
| 每 `6h` | `promotion_rotate` | `scripts/auto_rotate_promotions.py` |
| 周日 `02:00` | `cro_learn_thresholds` | 生成待审核的 CRO 阈值候选 |
| 周日 `02:30` | `cro_promote_thresholds` | 通过 shadow gate 后推广阈值 |
| 周二 `10:00` | `smart_bid` | 分级 Bid 优化 |
| 周二 `11:00` | `bid_rollback` | 7 天回溯回滚无效 Bid |
| 周一 `11:00` | `cro_delist_email` | 发出人工确认型下架候选 |

补充说明：

- 看门狗每 10 分钟检查守护进程是否失活。
- 标题优化定时任务已停用；只有显式设置 `ENABLE_SCHEDULED_TITLE_OPTIMIZATION=1` 时才会重新启用 `daily_optimize.py`。
- MI 自检失败时会把结果写入 `logs/_scheduler_health.json`，必要时发高优先级提醒邮件。
- CRO 的执行链路不是全部在 `09:30` 一次跑完，而是先诊断入队，再由 `10:00-10:30` 的消费窗口逐步落地。
- Streamlit CRO 页的优先动作清单会展示完整动作分布；“自动执行安全 P1”会去重入队并立即消费 `price_drop` / `image_refresh` / `fill_specifics` / `promote`，`delist` 仍只走人工确认。

## 4. 当前推荐操作路径

### 发布 READY 草稿

```bash
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
python batch_publish.py --sku YOURSKU
```

要求：

- 先看最新 `logs/ready_draft_audit_*.json`
- 只发布没有 unresolved measurement / category / quality gate blocker 的草稿
- 不要绕过 dry-run 直接 live publish
- `batch_publish.py --dry-run` 通过才说明当前本地草稿可以进入 live publish

### 链接质量门

生成和发布链路现在共用 `src/utils/listing_quality_gate.py`：

| 入口 | 质量门作用 |
|------|------------|
| `batch_analyze.py` | AI 生成后，进入 `READY` 前规范化 |
| `daily_tasks.py` | 自动分析和 MI 自动起草后，进入 `READY` 前规范化 |
| `server.py` | 单 SKU 后台分析后，进入 `READY` 前规范化 |
| `scripts/audit_fix_ready_drafts.py` | 批量修复历史 READY 草稿并输出 unresolved |
| `batch_publish.py` | dry-run / live publish 前再次阻断 |

质量门会自动修复或阻断：

- patio furniture set / bench / ottoman / dining chair / coffee table 等产品族类目错误
- shoe / sport / pet 等非家具 item specifics
- chair-only set 被写成 `Dining Table & Chairs`
- `Item Length` / `Item Width` / `Item Height` 缺失或占位
- description 未同步长宽高重量数字
- `Boucle` 相关乱码
- 只有一张图或本地图片明显不足
- source 不支持却被 AI 写出来的 `Foldable` / `Collapsible` / `USB` / `Charging` 功能词
- source 有视频但链接本身是 `.txt` / `.m3u8` / 页面 URL，后续无法直接发到 eBay Media
- source 标题在本地关键词规则下明显应归到别的类目，但草稿类目不合理

`POST /api/collect` 还会把 source preflight 问题写入 `logs/collection_quality_issues.jsonl`，包括 `source_facts`、`source_category_hint` 和 `source_video_not_publishable` 标记。详细规则见 `docs/LISTING_QUALITY_GATE.md`。如果质量门误挡，优先修产品画像、源事实提取或 category matcher，并加回归测试，不要只手工改单个 SKU。

### 修复已刊登链接

```bash
python scripts/audit_fix_active_listings.py
python scripts/audit_fix_active_listings.py --live --fix --sku-file logs/residual_listing_audit_skus.txt
python scripts/repair_published_taxonomy.py
python scripts/repair_published_taxonomy.py --apply
```

适用场景：

- 已刊登类目漂移
- 描述 / 尺寸 / item specifics 异常
- live title 被 eBay 80 字符硬截断成半个词
- `Foldable` / `Assembly` 等 live 文案与 GIGA 原文不一致
- live 图片塌成单图

当前 active listing 修复规则：

- 修复时必须以 live eBay inventory/offer 快照作为基底，不能只用本地 `optimization`。
- 对 `PUBLISHED` 全库执行 `--fix` 时必须带 `--live`；不允许再对全库跑非 `--live` 的修复。
- 非 `--live` `--fix` 只允许用于定向范围，例如 `--sku` 或 `--sku-file`。
- 标题进入 eBay 前统一走 `src/utils/title_sanitizer.normalize_listing_title_for_ebay()`，避免 `[:80]` 重新制造半个词。
- 当只改 inventory product 字段（例如标题、features）时，也要重新 publish offer，让 live listing 吃到这次 revise。
- 审计 / 修复 JSON 报告会持久化 `source` 字段；当前值为 `live_ebay` 或 `local_db_optimization`，用于事后追溯命令来源。

### 如何读“今天还有近 500 条问题”

不要先把这个数字当成“500 个重复 bug”，先看报告语义：

- `logs/listing_audit_fix_*.json -> total_with_issues` 统计的是“有至少一个问题的唯一 listing 行数”。
- 一条 listing 可以同时命中多个 issue type，所以 issue instance 数会高于 listing 行数。
- `11:30` 的定时 `listing_audit` 当前是 detect-only：`--live --email` 会产出报告和邮件，但不会自动修 full live corpus。

`2026-06-17` 这次的实际例子是 `logs/listing_audit_fix_20260617_113018.json`：

- `512` 条唯一 listing 有问题，不是重复 SKU。
- 这 `512` 条里共有 `532` 个 issue instance，因为有 `18` 条 listing 同时带了多个问题标签。
- 其中 `497` 条是历史 live 债务：`469 hallucinated_foldable`、`21 missing_video`、`13 category_mismatch`、`4 incomplete_title`。
- 另外 `15` 条是 `2026-06-17` 当天新发布后立刻被 live audit 打回：`15 assembly_description_missing`、`8 missing_foldable`、`2 non_applicable_aspect`。

所以“近 500 条”的真正含义是：

- 大头仍然是历史 live 残留，主要是 `foldable` 幻觉。
- 少部分是今天新发链接暴露出 publish-time 规则还没有完全追平 live-audit 规则。
- 这不是调度器自己修过又重复报的假重；而是 detect-only 报告把还没修掉的 live 债务继续完整暴露出来。

### 跑日常运营链路

```bash
python daily_tasks.py
python daily_tasks.py --analyze-only
python daily_tasks.py --sync-only
python daily_tasks.py --ops-only
python daily_tasks.py --audit-only
```

### 子店 ops 调度（GrovePop / AquaRides）

子店实例在 `config/store_profile.local.yaml` 设置 `scheduler_profile: ops` 后，只跑库存运营与出单源复核，不触发主店的 MI/CRO/语义改写任务。

| 时间 | 任务 | 入口 |
|------|------|------|
| 每天 09:30 | 库存运营包 | `daily_tasks.py --ops-only`（增量同步 + 幽灵缺货恢复 + 精简邮件） |
| 每 6 小时 | 出单源复核 | `scripts/order_source_recheck.py --hours-back 48 --email` |

金丝雀验收（子店目录内）：

```bash
python daily_tasks.py --ops-only
python scripts/order_source_recheck.py --sku <PUBLISHED_SKU> --dry-run
python scheduler_daemon.py --status
```

注册 Windows 计划任务（需管理员 PowerShell）：

```powershell
.\scripts\register_substore_ops_scheduler.ps1 -StorePrefix GrovePop
.\scripts\register_substore_ops_scheduler.ps1 -StorePrefix AquaRides
```

`store_profile.local.yaml` 示例：

```yaml
scheduler_profile: ops
scheduler_mutex_name: Global\GrovePopSchedulerDaemonMutex
qc_profile: arttoy   # AquaRides 用 motors
```

### 库存日报口径

每日库存结果拆成两个互不混淆的审计范围：

- `增量库存同步`：只统计本次实际进入 `InventorySyncService.sync_all()` 的 SKU；`检查数`是本次实际处理数，另显示本次范围总数和跳过数。
- `全量缺货审核`：独立复核全部 `PUBLISHED` 链接的 eBay 实时数量，再交叉检查供应商库存；`检查数`是全量审核实际完成数。

单供应商批量刊登前，先跑收藏/API 可读性闸门（未收藏的 SKU 无法查库存/价格）：

```bash
python scripts/supplier_favorite_api_gate.py --sku-list tools/w714_listing/B4_skus.txt --require-all
```

流程说明见 `tools/w714_listing/SUPPLIER_BATCH_PLAYBOOK.md`。

两栏统一返回 `audit_scope`、`checked_count`、`qty_zero_count`、`supplier_oos_count`、`restocked_count`、`error_count`。每日主流程只执行一次全量缺货审核，销售健康诊断复用结果但不再重复调用数量审核。

说明：

- `python daily_tasks.py` 会走默认全量工作负载，其中已经包含 `run_mi_snapshot()`。
- 同一条默认链路也会运行 `run_cro_diagnose()`，写出 CRO 漏斗诊断和动作队列。
- `20:00` 的独立 `health_check` 是另一条诊断任务，可独立执行全量数量审核；它不属于 `09:30` 主流程内的重复调用。
- 当前没有 `--mi-only` 参数；如果需要独立诊断 MI，请用 `scripts/mi_diagnose.py`。

### 跑 CRO 主线

默认不建议人工乱序触发所有 CRO 子步骤。推荐路径是：

1. 跑 `python daily_tasks.py` 或等待 `09:30` 调度，先生成最新诊断和动作队列。
2. 检查 `logs/cro_action_queue.jsonl`、`logs/cro_sentinel_*.json`、以及相关日报输出。
3. 让 `scheduler_daemon.py` 在 `10:00-10:30` 消费动作队列，或在 Streamlit CRO 页点击“自动执行安全 P1”。明确排障时再单独运行对应 `scripts/cro_*.py`。
4. 如需了解完整运行链路、S131-S140 运维闭环模块和接口，读 `docs/CRO_SYSTEM.md`。
5. 如果你看不懂 CRO 邮件或不清楚为什么这次只有 `promote` 没有 `price_drop` / `image_refresh` / `fill_specifics`，直接看 `docs/CRO_SYSTEM.md` 里的 `Operator Cheat Sheet` 章节。

### 调价与健康诊断

```bash
python scripts/batch_smart_reprice.py
python scripts/batch_smart_reprice.py --apply --email
python scripts/sales_health_check.py
python scripts/sales_health_check.py --auto-fix --email
```

## 5. MI 运行与排障

### MI 是怎么触发的

- 默认路径：`09:30` 的 `daily_tasks.py` 全量任务。
- 手动路径：直接运行 `python daily_tasks.py`。
- 诊断路径：`python scripts/mi_diagnose.py` 或 `python scripts/mi_diagnose.py --json`。
- 当 MI 发现的机会 SKU 仍处于 `PENDING` / `COLLECTED` 时，后台会自动生成本地 `READY` 草稿，但不会自动刊登。

当前推荐范围说明：

- 本地 MI 的 `auto_discover_opportunities()` 现在只扫描 `PENDING` / `COLLECTED` / `READY`，不再把 `PUBLISHED` 混进“推荐刊登”列表。
- 每日 MI digest 里的 Top 区块现在表示“当前 Top 未刊登候选”，不是“已刊登里谁分数高”。
- 如果要找新的未刊登机会，优先使用 MI 页面里的“🌐 GigaCloud 未刊登扫描”；那条链路会跳过当前 `PUBLISHED` / `READY`，但允许 `ENDED` / `DELISTED` 作为重刊候选。

### MI 关键产物

| 路径 | 说明 |
|------|------|
| `reports/mi_opportunities_*.json` | 每日机会快照，含 `by_category` |
| `reports/mi_digest_*.html` | 每日 digest HTML 归档 |
| `reports/mi_long_window_history.json` | 长周期 trend 历史 |
| `reports/mi_alerts_state.json` | 告警抑制状态 |
| `reports/mi_blacklist.json` | 操作者屏蔽的 SKU |
| `mi_categories.json` | MI 品类规则配置 |
| `logs/_scheduler_health.json` | 调度器最新健康检查结果 |

当前保留策略：

- 快照保留 30 天
- digest HTML 保留 3 天
- 长周期 trend 保留 14 条

### 运行期报告与日志清理

每日主任务启动时会调用统一清理器；MI 快照清理和邮件报告清理也复用同一入口。
已登记的报告/日志命名按专用规则处理，未登记但带日期的常规文本产物走 30 天安全兜底：

- 日报、邮件报告、健康检查、调价报告：3 天
- MI 快照：30 天
- Terapeak、CRO、compare、普通 audit/reprice/dropship 报告：30 天
- 关键整改、恢复、删除/重刊核验和关键审计产物：180 天
- 带日期的普通日志：30 天；关键审计/恢复/故障日志：180 天

无日期文件、状态文件、锁文件、数据库、图片、密钥和不支持扩展名的文件不会自动删除。

```bash
# 只查看候选，不删除
python scripts/report_retention.py --dry-run

# 按白名单实际删除过期产物
python scripts/report_retention.py --apply
```

清理实现位于 `src/utils/report_retention.py`，规则和变更原因见
`tasks/report_retention_plan.md`。

### MI 邮件与 UI

- MI 告警邮件和每日 digest 邮件都使用中文内容，并带商品缩略图。
- Streamlit 的 MI 页面在 `src/web/pages/market_intelligence.py`，可查看 KPI 横幅、14 天趋势、品类钻取、digest 浏览和异步任务状态。
- Streamlit 的 `🚀 开始发掘爆品` 在手动发现机会后，也会把命中的 `PENDING` / `COLLECTED` SKU 立即自动起草成 `READY` 草稿。
- Dashboard 的 READY 草稿区会对 MI 自动起草的商品显示 `🧠 MI 机会草稿` 标记，便于审核后再手动或批量刊登。

### MI 诊断命令

```bash
python scripts/mi_diagnose.py
python scripts/mi_diagnose.py --json
```

命令会汇总：

- 最近快照数量与最新日期
- 最近 digest 数量与最新日期
- trend 历史状态
- alerts / blacklist / category rules 文件状态
- `scheduler_daemon.check_mi_pipeline_health()` 的结构化结果

### MI 自检失败时先看什么

1. `type logs\_scheduler_health.json`
2. `python scripts/mi_diagnose.py`
3. 检查 `reports/` 下是否缺少当日 `mi_opportunities_*.json` 或 `mi_digest_*.html`
4. 检查 `reports/mi_long_window_history.json` 是否为空、损坏、或最新日期过旧
5. 必要时手动跑一次 `python daily_tasks.py`

### MI 自动起草后的发布路径

- 自动起草只负责把命中的 `PENDING` / `COLLECTED` SKU 推进到 `READY`；这个动作既可能来自 `daily_tasks.run_mi_snapshot()`，也可能来自 Streamlit 手动点击 `🚀 开始发掘爆品`。
- 发布仍然走标准流程：先审计，再手动 review/publish 或批量发布。
- 不会因为 MI 发现机会就直接后台调用 eBay publish。

## 6.1 CRO 运维收口模块

除了主线 diagnose / queue / execute 之外，仓库现在还有一组纯服务层的 CRO 运维收口模块：

- 灰度发布与回滚：`src/services/cro_canary_release.py`
- Runbook 执行：`src/services/cro_ops_runbook.py`
- A/B 收口：`src/services/cro_ab_finalize.py`
- 事故复盘：`src/services/cro_postmortem_generator.py`
- 值班轮转：`src/services/cro_oncall_rotation.py`
- 数据保留：`src/services/cro_data_retention.py`
- 容量规划：`src/services/cro_capacity_planning.py`
- 灾备演练：`src/services/cro_disaster_recovery.py`
- 合规审计：`src/services/cro_compliance_audit.py`
- 持续改进：`src/services/cro_continuous_improvement.py`

这些模块的生产观测入口是 CRO ops 快照，不直接改 live listing：

```bash
python scheduler_daemon.py --task cro_ops
python scripts/cro_ops_snapshot.py --dr-drill --brief logs/cro_weekly_improvement.md
```

输出：

- `logs/cro_ops_snapshot.json`：Streamlit `🚦 CRO 状态` 页面读取的 ops 快照
- `logs/cro_capacity_samples.jsonl`：调度任务记录的容量样本
- `logs/cro_weekly_improvement.md`：可选周度改进摘要

接口说明在 `docs/CRO_SYSTEM.md`。

### Root 目录整理

root 目录瘦身先跑审计，不直接移动或删除文件：

```bash
python tools/root_structure_audit.py --output reports/root_structure_audit.json --markdown reports/root_structure_audit.md
```

审计会把 root 项分为必须保留、运行态文件、可迁移 Python 候选、可迁移数据候选和未知项。后续迁移必须逐项确认调用方，再更新兼容路径、文档和测试。

本轮已归位到 `tools/local_diagnostics/` 的探针脚本包括：

- `_deep_check.py`
- `_extract_links.py`
- `_links.py`
- `check_ready.py`
- `check_ready_detailed.py`
- `compare_ebay_gigacloud.py`
- `compare_ebay_gigacloud_batch2.py`

## 6. 当前主要组件

### 浏览器扩展

- 路径：`extension/`
- 作用：采集供应商页面数据并 POST 到 `/api/collect`
- 备注：Chrome 中仍可能看到历史命名 `AquaVerve`，这是遗留显示名，不影响当前链路

图片采集规则：

- 采集阶段保留原始 URL
- 不删除 GigaB2B 签名参数 `x-cc` / `x-cu` / `x-ct` / `x-cs`
- 只允许在发布侧删除已知图片处理参数，例如 `x-oss-process`

### FastAPI 服务

常用接口：

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/` | 健康检查 |
| `GET` | `/api/products` | 获取产品列表 |
| `POST` | `/api/collect` | 接收采集数据 |
| `POST` | `/api/publish/{sku}` | 发布指定 SKU |
| `POST` | `/api/delist/{sku}` | 下架指定 SKU |
| `GET` | `/api/listing-health` | 刊登质量视图 |
| `GET` | `/history` | 历史页面 |
| `GET` | `/ebay/auth` | 发起 OAuth |
| `GET` | `/ebay/callback` | OAuth 回调 |
| `GET` | `/api/ebay/auth/status` | 检查授权状态 |
| `GET` | `/api/ebay/policies` | 获取策略缓存 |
| `POST` | `/api/ebay/policies/refresh` | 刷新策略缓存 |

### Streamlit 界面

入口：

```bash
streamlit run app.py --server.port 8501
```

当前主要用途：

- eBay OAuth 授权
- 产品查看与人工发布
- 定价 / 市场信息查看
- inventory / competition / market intelligence 页面操作

### 主业务入口

| 文件 | 当前角色 |
|------|-----------|
| `server.py` | API 服务与部分发布入口 |
| `app.py` | Streamlit 操作界面 |
| `batch_publish.py` | READY 草稿发布工具 |
| `daily_tasks.py` | 日常任务工作负载入口 |
| `scheduler_daemon.py` | 自动调度主入口 |
| `scheduler_watchdog.py` | 调度保活 |
| `qwen_optimizer.py` | AI 内容与尺寸提取 |
| `scripts/mi_diagnose.py` | MI 诊断入口 |

## 7. 当前不可破坏的业务规则

### 发布与测量

- `Item Length` / `Item Width` / `Item Height` 不能缺失、不能占位
- `Item Weight` 可以缺省，但如果存在必须真实、非占位、非非正数
- `packageWeightAndSize` 使用可信包装尺寸 / 包装重量，不要伪造产品重量

### 图片

- 不要删除 GigaB2B 签名 query 参数
- 发布或 revise inventory 后，必须回读 eBay Inventory `product.imageUrls`
- 如果本地有多张源图而 live 只剩 1 张，优先判断为 publish / revise 阶段图片 URL 问题
- 全库少图扫描用 `python tools/scan_image_collapse.py`
- 确认需要恢复时用 `python tools/restore_listing_images.py SKU`

### 类目

- 不要仅凭 API suggestion 覆盖当前类目
- 类目修复必须经过 plausibility 判断
- 已刊登类目修复应先补 inventory item specifics，再更新 live offer 类目
- coffee table、bench、ottoman、patio set 等产品族规则应落在 `src/utils/listing_quality_gate.py` 或 `src/services/ebay_category_matcher.py`，不要散落在临时脚本

### 价格

- 改价成功不能只看请求返回
- 只有 live eBay offer 价格回读验证成功，才允许写回本地 DB 状态

### OAuth 与 JSON 字段

- token 真源在 `ebay_tokens.db`
- SQLAlchemy JSON 字段原地修改后必须 `flag_modified()`

## 8. 日志、报告与排障入口

### 常用日志 / 报告

| 路径 | 说明 |
|------|------|
| `logs/ready_draft_audit_*.json` | READY 审计结果 |
| `logs/listing_audit_fix_*.json` | 已刊登链接审计 / 修复结果 |
| `logs/_scheduler.pid` | 调度守护进程 PID |
| `logs/_scheduler_health.json` | 调度与 MI 自检状态 |
| `logs/_watchdog.log` | 看门狗日志 |
| `reports/daily_report_*.html` | 每日报告 |
| `reports/reprice_report_*.json` | 智能调价结果 |
| `reports/mi_opportunities_*.json` | MI 快照 |
| `reports/mi_digest_*.html` | MI digest 归档 |

### 计划任务 / 调度排障

```bash
python scheduler_daemon.py --status
type logs\_scheduler_health.json
type logs\_watchdog.log
```

如果看门狗未注册，重新执行：

```bash
setup_scheduled_tasks.bat
```

### OAuth 排障

```bash
python tools/refresh_token.py
```

说明：

- 不要再使用不存在的 `check_token_db.py`
- 当前应通过 Streamlit 授权页、`EbayOAuthService`、或 `tools/refresh_token.py` 处理授权

### Active Listing Audit 排障

如果每天 `listing_audit` 里的问题数一直很大，按这个顺序判断：

1. 先看 `source` 是否为 `live_ebay`。如果是，就说明这是 live 内容真问题，不是本地 optimization 幻觉。
2. 再按 nested `issues[].type` 分桶，不要只看总数。
3. 如果主要是 `hallucinated_foldable`，优先判定为历史 live 债务；如果主要是 `assembly_description_missing` / `missing_foldable`，优先判定为 publish gate 和 live audit 规则还没对齐。
4. 如果报告来自 `11:30` 定时任务，默认它没有修，只是检测并发邮件；修复要显式跑 `--live --fix`。

## 9. 自动化测试与手动探针

正式自动化测试：

```bash
python -m pytest
```

链接质量门快速回归：

```bash
python -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py
```

MI 相关快速回归：

```bash
python -m pytest tests/test_market_intelligence_scoring.py tests/test_mi_e2e_smoke.py tests/test_scheduler_mi_self_check.py tests/test_mi_diagnose.py -q
```

手动 live 探针：

```bash
python tools/probe_ebay_api.py
python tools/probe_dajian_api.py
python tools/probe_qwen_api.py
```

说明：

- `tests/` 下的是正式自动化回归
- `tools/probe_*.py` 是手动 live 探针，不参与 root `pytest`

## 10. 常见问题

### 10.1 live 链接只剩一张图

优先检查：

1. 本地 `collected_products.images` 是否本来就只有一张
2. 发布 / revise 时是否错误删除了 GigaB2B 签名参数
3. eBay Inventory `product.imageUrls` 回读后是否已经塌成单图

### 10.2 智能调价没有执行

先确认：

- 今天是否为周一或周四
- `scheduler_daemon.py` 是否在运行
- `reports/reprice_report_*.json` 是否生成

### 10.3 MI 没有出新快照或 digest

先确认：

- `09:30` 的 `daily_tasks` 是否执行
- `reports/mi_opportunities_*.json` 是否有当天文件
- `reports/mi_digest_*.html` 是否有当天文件
- `reports/mi_long_window_history.json` 是否可读且不是空列表
- `python scripts/mi_diagnose.py --json` 是否返回 `self_check.ok = true`

### 10.4 调度器没跑

先检查：

```bash
python scheduler_daemon.py --status
type logs\_watchdog.log
```

再确认：

- `logs/_scheduler.pid` 是否存在
- Windows 计划任务 `Dajian Scheduler Watchdog` 是否已注册

### 10.5 Marketplace Insights 返回 403

当前系统会自动回退到 Browse 数据。需要申请权限时，参考：

- `docs/marketplace_insights_request_20260330.md`

## 11. 相关文档

- `README.md`: 仓库总览
- `docs/ARCHITECTURE.md`: 运行时拓扑、模块边界、MI 流水线
- `docs/LISTING_QUALITY_GATE.md`: 链接生成质量门、发布前阻断规则
- `docs/decisions/ADR-001-listing-quality-gate.md`: 集中式质量门的设计决策
- `docs/SCRIPT_MATRIX.md`: 各脚本 / 插件责任划分
- `docs/TESTING.md`: 自动化测试与手动探针规则
- `docs/DOCUMENTATION_AUDIT.md`: 文档同步规则
- `skill.md`: 功能地图与长期约束
- `agent.md`: 给未来 coding agent 的快速 guardrails
