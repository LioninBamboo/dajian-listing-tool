# Dajian Listing Tool Skill

这份 `skill.md` 用来把代码库现状、`docs/USAGE.md` 的操作说明、`docs/ARCHITECTURE.md` 的拓扑说明，以及 `agent.md` 的执行约束收束成一份统一的功能地图。

它不是面向终端用户的宣传文档，而是给维护者、自动化代理、以及未来重构时的“功能边界说明书”。当前版本已经把 Market Intelligence（MI）流水线、自检和诊断入口、CRO 转化率闭环和其运维治理收口层，以及刊登链接生成质量门纳入主地图。

## 1. 仓库目标

这个仓库的核心目标是把大建 / GigaCloud 商品，从采集到 eBay 上架、再到上线后的维护，放进一条可重复执行的运营流水线：

1. 浏览器扩展采集供应商商品数据。
2. FastAPI 服务接收并写入 `ebay_collection.db`。
3. AI 优化生成标题、描述、类目和 item specifics。
4. READY 草稿先审计，再发布。
5. 已发布链接继续经历库存同步、改价、健康诊断、类目修复和标题优化。
6. MI 流水线持续产出机会快照、告警、digest、趋势和诊断结果。

## 2. 心智模型

```text
Supplier Page / Dajian API
        |
        v
extension/content.js -> POST /api/collect -> server.py -> ebay_collection.db
                                                |
                                                v
                                     qwen_optimizer.py
                                                |
                                                v
                              src/utils/listing_quality_gate.py
                                                |
                                                v
                                   COLLECTED -> READY
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
                -----------------------------------------------------------------------------
                |                |                 |                |            |            |
                v                v                 v                v            v            v
       inventory sync      smart repricing   listing health   taxonomy repair   title optimize   MI
```

## 3. 分层结构

### 3.1 入口层

- `server.py`: FastAPI 主服务，负责采集、发布 API、OAuth、后台任务启动。
- `app.py`: Streamlit 操作界面。
- `batch_publish.py`: READY 草稿的批量 / 单 SKU 发布入口。
- `daily_tasks.py`: 主日常任务执行入口。
- `scheduler_daemon.py`: 当前默认自动调度入口。
- `scheduler_watchdog.py`: 守护进程保活入口。
- `qwen_optimizer.py`: AI 内容生成与尺寸提取主入口。

### 3.2 服务层

- `src/services/ebay_auth.py`: eBay OAuth 与 token 生命周期。
- `src/services/ebay_category_matcher.py`: 类目匹配、canonicalization、plausibility 判断。
- `src/services/ebay_publisher.py`: 类目默认值、required aspects、发布侧规则。
- `src/services/pricing_engine.py`: 大建成本与售价计算。
- `src/services/ebay_discount_service.py`: Markdown Sale 等促销能力。

### 3.3 客户端层

- `src/clients/dajian_client.py`: 大建 Open API。
- `src/clients/real_ebay_client.py`: eBay REST API。
- `src/clients/ebay_trading_client.py`: eBay Trading API。

### 3.4 自动化脚本层

- `scripts/audit_fix_ready_drafts.py`: READY 草稿审计与规范化。
- `scripts/audit_fix_active_listings.py`: 已刊登链接审计与修复。
- `scripts/repair_published_taxonomy.py`: 历史已刊登类目漂移修复。
- `scripts/batch_smart_reprice.py`: 智能调价。
- `scripts/sales_health_check.py`: 上线后健康诊断。
- `scripts/mi_diagnose.py`: MI 健康诊断与 JSON 输出。

### 3.5 插件层

- `src/plugins/inventory_sync/`: 库存与价格同步。
- `src/plugins/active_listing_optimizer/`: 上线后标题优化。
- `src/plugins/terapeak_research/`: 市场研究与调价支持。

### 3.6 数据与输出层

- `ebay_collection.db`: 商品状态、优化结果、刊登信息。
- `ebay_tokens.db`: OAuth token 存储。
- `logs/`: 审计、运行、修复日志。
- `reports/`: 报表、邮件侧输出、MI 快照、MI digest、MI 长周期状态。

### 3.6.1 本地诊断工具

- `tools/local_diagnostics/`: 下划线命名的本地排查 / 修复脚本，已从仓库根目录下沉。
- `cache/`: 运行时缓存目录，当前承载 `ad_data_cache.json` 与 `performance_cache.json`。
- 运行时仍兼容读取旧的根目录缓存文件名，但新缓存写入不再落回仓库根目录。

### 3.7 共享校验工具

- `src/utils/listing_quality_gate.py`: 刊登链接生成质量门，负责产品家族、类目、尺寸、item specifics、description 和图片数量等发布前一致性规则。
- `src/utils/publish_validation.py`: 发布前的测量和类目校验共享规则，供 `batch_publish.py` 与 `server.py` 共用。

## 4. 状态模型

主商品状态流转：

- `PENDING`: 已采集，未处理。
- `COLLECTED`: 已落库，等待 AI 分析。
- `READY`: 具备发布素材，但仍需 READY 审计。
- `PUBLISHED`: 已在 eBay 上线。
- `ENDED` / `DELISTED`: 已结束或主动下架。
- `ERROR`: 执行失败，需要人工介入。

关键原则：`READY` 不等于“直接可发”，而是“可以进入发布前审计”。

## 5. 功能技能

### 5.1 商品采集

用途：从大建 / GigaCloud 页面抓取商品基础信息并写入本地库。

主要文件：

- `extension/content.js`
- `extension/background.js`
- `server.py`

要求：

- 扩展只负责采集，不负责业务判断。
- 采集阶段尽量保存原始标题、描述、图片、SKU、页面尺寸与视频信息。
- FastAPI 接收入库后再进入分析链路。

### 5.2 AI 分析与草稿生成

用途：把 `COLLECTED` 商品变成具备刊登素材的 `READY` 草稿。

主要文件：

- `daily_tasks.py`
- `scheduler_daemon.py`
- `batch_analyze.py`
- `qwen_optimizer.py`
- `src/services/pricing_engine.py`
- `src/utils/listing_quality_gate.py`

要求：

- 标题目标长度约 75-80 字符。
- 描述输出为结构化 HTML。
- 尺寸提取优先使用真实来源，不要凭空生成。
- AI 生成结果必须再经过 `src/utils/listing_quality_gate.py`，不能把模型输出直接当作发布安全边界。
- 尺寸写入 item specifics 与 description 时必须来自同一份标准化测量结果，不能一边修正、一边仍保留旧描述。
- 复杂多边形或无法可靠推导长宽高的产品，description 应引导客户参考产品尺寸图，而不是生成看似精确的占位尺寸。
- 成本和售价应基于 `PricingEngine`，不要在外围脚本重新发明公式。

### 5.3 READY 草稿审计

用途：把 `READY` 草稿中的尺寸、类目、图片、描述问题在发布前处理掉。

主要文件：

- `scripts/audit_fix_ready_drafts.py`
- `src/services/ebay_category_matcher.py`
- `src/services/ebay_publisher.py`
- `src/utils/listing_quality_gate.py`
- `src/utils/publish_validation.py`

要求：

- 必须在 live publish 之前运行。
- 必须生成并检查 `logs/ready_draft_audit_*.json`。
- `quality_gate.unresolved` 里还有 blocker 时不能继续发布。
- `Item Length` / `Item Width` / `Item Height` 必须真实存在，不能是占位值。
- `Item Weight` 不是统一硬性必填，但如果存在，必须是真实、非占位、非非正数。
- 不要为了过校验伪造 `Item Weight`。
- 不要把家具误生成 `Type=Planter`、`Material=Metal`、`Color=Black` 等非来源支持的 item specifics。
- 如果本地源图只有 1 张，应在发布前显式标记；如果本地源图多张而草稿只剩 1 张，应先修复图片采集 / 清洗链路。
- 已有存储类目只有在“不合理”时才允许替换；替换后的类目必须对产品家族合理。

### 5.4 发布 READY 草稿

用途：将已通过 READY 审计的草稿发布到 eBay。

主要文件：

- `batch_publish.py`
- `server.py` (`POST /api/publish/{sku}`)
- `src/clients/real_ebay_client.py`
- `src/utils/listing_quality_gate.py`

要求：

- 先 dry-run，再 live publish。
- 发布前必须再次运行质量门，校验尺寸、类目、item specifics、description 和图片数量。
- `packageWeightAndSize` 应使用可信的包装尺寸 / 包装重量构建。
- `Item Weight` 只在拿到可信产品重量时保留，不得用 `See Description` 填充。
- 图片 URL 清洗只能删除已知处理参数，例如 `x-oss-process`。
- GigaB2B 签名参数 `x-cc` / `x-cu` / `x-ct` / `x-cs` 必须保留，否则 eBay live 图片可能退化为单图。
- 发布或 revise 后必须回读 eBay Inventory `product.imageUrls`，确认 live 图片数不低于本地可用源图数。
- 发布成功后才允许把 DB 状态写成 `PUBLISHED`。
- 如果同日新 publish 的 listing 立刻被 `scripts/audit_fix_active_listings.py --live` 打回，优先判断为 publish-time gate 与 live-audit 规则还没完全对齐。

### 5.5 已刊登类目修复

用途：修复历史已上线链接的陈旧 / 错误 taxonomy。

主要文件：

- `scripts/repair_published_taxonomy.py`
- `scripts/audit_fix_active_listings.py`

要求：

- 先判断 raw live category，再看 canonical category，避免 canonicalization 掩盖真实偏差。
- 对 live offer 的修复顺序应是：先补 inventory item specifics，再更新 offer category，最后同步本地 DB。
- 不要再使用旧的硬编码错误类目映射回退到历史 taxonomy。

### 5.6 库存与价格同步

用途：把大建的库存与基础价格变化同步到 eBay。

主要文件：

- `src/plugins/inventory_sync/sync_service.py`
- `daily_tasks.py`

要求：

- 缺货时把 eBay 库存设为 0。
- 价格变化时重新计算售价。
- 只有 live eBay 价格回读校验成功后，才允许写回本地 DB 的价格状态。
- `packageWeightAndSize` 不能带 `0.0` 的重量值。

### 5.7 智能调价

用途：根据市场数据对已刊登链接重新定价。

主要文件：

- `scripts/batch_smart_reprice.py`
- `src/plugins/terapeak_research/`
- `src/utils/email_sender.py`

要求：

- 优先用 sold-data；拿不到 Marketplace Insights 时明确回退到 Browse。
- 调价结果必须回读验证 live offer 价格，验证失败不能假装成功。
- 调价邮件和 daily summary 必须反映真实执行结果，而不是请求是否发送成功。

### 5.8 销售健康诊断

用途：对已刊登链接做曝光、转化、价格与库存健康检查。

主要文件：

- `scripts/sales_health_check.py`
- `daily_tasks.py`

要求：

- 区分 `价格不匹配`、`历史遗留不同步`、`促销改价`、`人工改价`。
- 不要把 ghost delist 与 Inventory API 的暂时状态误判成真实下架。
- 自动修复必须保留利润底线。

### 5.9 上线后标题优化

用途：针对已刊登链接做标题再优化。

主要文件：

- `src/plugins/active_listing_optimizer/daily_optimize.py`

要求：

- 分批执行，避免限流。
- 标题优化必须保持产品家族语义，不允许通过换品类词堆砌关键词。

### 5.10 邮件与报表

用途：统一输出 daily summary、调价报告、健康检查报告。

主要文件：

- `src/utils/email_sender.py`
- `daily_tasks.py`
- `reports/`

要求：

- 邮件发的是事实结果，不是“理想结果”。
- 远程商品图在邮件里优先走内联 CID，而不是依赖容易失效的外链。

### 5.11 OAuth 与策略缓存

用途：维持 eBay API 调用授权状态与业务策略缓存。

主要文件：

- `src/services/ebay_auth.py`
- `tools/refresh_token.py`
- `app.py`

要求：

- token 真源在 `ebay_tokens.db`，不是环境变量。
- `EBAY_REDIRECT_URI` 必须与 eBay 开发者后台注册值一致。
- 不要直接改 token DB。

### 5.12 Market Intelligence（MI）

用途：持续发现市场机会，写入快照，计算 KPI / 趋势，发送告警与日报，并对流水线健康度做自检和诊断。

主要文件：

- `daily_tasks.py`
- `scheduler_daemon.py`
- `scripts/mi_diagnose.py`
- `src/plugins/terapeak_research/intelligence_service.py`
- `src/plugins/terapeak_research/aggregation.py`
- `src/plugins/terapeak_research/history.py`
- `src/web/pages/market_intelligence.py`
- `mi_categories.json`

要求：

- `run_mi_snapshot()` 当前跑在默认 `python daily_tasks.py` 全量工作负载里，没有独立 `--mi-only` 开关。
- `run_mi_snapshot()` 会把命中的 `PENDING` / `COLLECTED` 机会 SKU 自动起草为本地 `READY`，但不会自动刊登。
- MI 快照写入 `reports/mi_opportunities_*.json`，digest 归档写入 `reports/mi_digest_*.html`。
- 长周期趋势保存在 `reports/mi_long_window_history.json`，告警抑制状态保存在 `reports/mi_alerts_state.json`，黑名单保存在 `reports/mi_blacklist.json`。
- MI 告警邮件和 digest 邮件必须保持中文内容，并带商品缩略图。
- `scheduler_daemon.check_mi_pipeline_health()` 需要把缺失 snapshot、缺失 digest、缺失或空的 trend history、不可读 trend、过旧 trend 都视为失败。
- `scripts/mi_diagnose.py` 必须能输出面向操作者的文本诊断，以及面向自动化的 `--json` 结构化结果。
- `mi_categories.json` 是当前品类规则真源；如果改结构，必须同步 loader、测试和文档。
- Dashboard 的 READY 草稿需要保留 MI 来源标记，便于人工审核后再走标准 audit + publish 流程。

### 5.13 CRO 转化率闭环

用途：对已刊登链接做漏斗诊断、动作入队、执行反馈、阈值反哺，以及最终的运维/治理收口。

主要文件：

- `daily_tasks.py` (`run_cro_diagnose()`)
- `scheduler_daemon.py`
- `src/services/conversion_diagnoser.py`
- `src/services/cro_daily_runner.py`
- `src/services/cro_action_queue.py`
- `scripts/cro_*.py`
- `src/services/cro_canary_release.py`
- `src/services/cro_ops_runbook.py`
- `src/services/cro_ab_finalize.py`
- `src/services/cro_postmortem_generator.py`
- `src/services/cro_oncall_rotation.py`
- `src/services/cro_data_retention.py`
- `src/services/cro_capacity_planning.py`
- `src/services/cro_disaster_recovery.py`
- `src/services/cro_compliance_audit.py`
- `src/services/cro_continuous_improvement.py`
- `src/services/cro_ops_control_plane.py`
- `scripts/cro_ops_snapshot.py`
- `tools/root_structure_audit.py`

要求：

- `09:30` 先诊断并入队，不在同一步骤里直接乱序执行所有动作。
- `10:00-10:30` 的调度窗口才是主消费路径；文档和脚本帮助都要按这个事实写。
- 诊断、执行、反馈、治理四层分工要保持清晰，不要把 service helper 写成新的 root 脚本入口。
- S131-S140 生产观测入口是 `scheduler_daemon.py --task cro_ops` / `scripts/cro_ops_snapshot.py`，输出给 `src/web/pages/cro_status.py`，只读/advisory，不直接改 live listing。
- 灾备接口以 `backup_database` / `verify_backup` / `restore_to_temp` / `run_dr_drill` 为主。
- 合规接口以 `verify_lawful_basis` / `log_access` / `query_subject_history` / `generate_dsar_export` 为主。
- 持续改进接口以 `collect_signals` / `generate_suggestions` / `rank_suggestions` / `render_weekly_brief` 为主。
- 变更 CRO 结构时，要同步更新 `docs/CRO_SYSTEM.md` 与 `/memories/repo/cro-system.md`。
- root 目录瘦身先跑 `tools/root_structure_audit.py`，确认调用方后再迁移，不要在脏工作树里批量搬文件。

## 6. 不可破坏的约束

- 更新 SQLAlchemy JSON 字段时必须调用 `flag_modified()`。
- 不要接受 `See Description` 作为真实尺寸或真实重量。
- 新增或修改刊登生成规则时，优先更新 `src/utils/listing_quality_gate.py`，不要在分析、审计、发布脚本里分叉同一套规则。
- 新增类目时，要同步检查 `CATEGORY_REQUIRED_ASPECTS`、category matcher、UI 名称映射、以及相关脚本。
- 类目替换必须经过 plausibility 检查，不要只因为 API suggestion 改类目。
- 本地价格状态只能在 live eBay 状态验证成功后写回。
- 对已刊登链接做修复时，不要跳过 inventory item 更新直接硬改 offer。
- 不要在采集或发布阶段删除 GigaB2B 图片签名参数；只允许移除确定安全的图片处理参数。
- MI 邮件必须保持中文内容，并在商品级内容里保留缩略图。
- MI 状态文件缺失、空内容或明显过旧不是“正常空跑”，而是需要修复的失败信号。
- 不要把 `mi_categories.json` 改成其他格式而不同时更新 loader、诊断脚本和回归测试。
- `logs/listing_audit_fix_*.json -> total_with_issues` 统计的是唯一 listing 行数，不是 issue instance 数；大审计数必须先按 nested `issues[].type` 分桶。
- `scheduler_daemon.py` 的 `11:30` active listing audit 当前是 detect-only `--live --email` 路径，不要把它写成“自动修复 live 全库”。

## 7. 推荐执行路径

### 7.0 启动整套系统

```bash
start.bat
python scheduler_daemon.py --status
```

### 7.1 发布新草稿

```bash
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
python batch_publish.py --sku YOURSKU
python tools/scan_image_collapse.py
python tools/restore_listing_images.py YOURSKU
```

### 7.2 修复已刊登类目漂移

```bash
python scripts/repair_published_taxonomy.py
python scripts/repair_published_taxonomy.py --apply
```

### 7.3 跑日常运营链路

```bash
python daily_tasks.py
```

说明：

- 默认全量 `daily_tasks.py` 已包含 MI snapshot、MI digest 和 MI 告警链路。
- 当前没有独立 `--mi-only` 开关。

### 7.4 手动调价 / 健康诊断

```bash
python scripts/batch_smart_reprice.py --apply --email
python scripts/sales_health_check.py --auto-fix --email
```

### 7.5 MI 诊断

```bash
python scripts/mi_diagnose.py
python scripts/mi_diagnose.py --json
```

## 8. 文档分工

- `README.md`: 快速理解仓库能力和主要入口。
- `docs/USAGE.md`: 面向操作者的使用说明。
- `docs/LISTING_QUALITY_GATE.md`: 刊登链接生成质量门、规则边界和标准排查路径。
- `docs/decisions/ADR-001-listing-quality-gate.md`: 为什么采用集中式质量门作为 READY / publish 边界。
- `docs/CRO_SYSTEM.md`: CRO runtime、scheduler cadence、queue consumer、运维治理接口说明。
- `docs/ARCHITECTURE.md`: 讲清系统拓扑、生命周期、模块分层和扩展边界。
- `docs/TESTING.md`: 自动化测试、MI 回归切片与手动探针规则。
- `docs/SCRIPT_MATRIX.md`: 讲清哪些脚本 / 插件是主入口、手动工具、调试入口或旧入口。
- `agent.md`: 给未来 coding agent 的快速 guardrails。
- `skill.md`: 当前这份功能要求、结构地图和执行边界总览。

## 9. 维护方式

当以下内容变更时，应同步更新 `skill.md`：

- 主流程新增或删除阶段。
- READY / PUBLISHED 的安全规则发生变化。
- 刊登链接质量门的规则、输出结构或接入入口发生变化。
- 新增关键运营脚本。
- 类目规则、重量规则、价格回写规则发生变化。
- `daily_tasks.py` 的任务编排发生变化。
- MI 调度时间、自检规则、诊断脚本输出、或 MI 状态文件布局发生变化。
