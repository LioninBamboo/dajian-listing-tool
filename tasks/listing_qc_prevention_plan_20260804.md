# eBay / GIGA 刊登质量缺陷前置预防计划

> 版本：v1.0
>
> 编制日期：2026-08-04（Asia/Shanghai）
>
> 目标：把本轮 live 修复中验证过的 QC 经验前置到“生成 → 草稿审计 → 发布 → live 回读”，降低同类 CRITICAL 问题在下一次生成、同步或发布后再次出现的概率。

## 1. 任务目标与当前基线

### 1.1 目标

建立一条可追踪、可阻断、可回读的 listing 质量链：

```text
供应商事实 / GIGA 库存
        ↓
生成阶段 FactSheet + 结构化规则
        ↓
READY 草稿审计
        ↓
发布前同一套 QC 门
        ↓
eBay Inventory / Offer 写入
        ↓
写入后回读与 protected-field 对账
        ↓
live audit + 复发根因记录
```

本轮不是为了把审计数字“压到零”而降低规则，而是让每一个被放行的 SKU 都有：

- source/candidate 的事实指纹；
- 质量规则版本和 FactSheet 版本；
- 阻断、警告或不可用的明确状态；
- 发布前快照、发布后回读和差异证据；
- 可定位到生成器、缓存、发布器或外部库存同步的复发根因。

### 1.2 已知基线

- 上一轮原始目标为 30 条 CRITICAL、23 个 SKU；修复后针对这 23 个 SKU 的定向复审为 `CRITICAL=0`、运输失败 `=0`。
- 同一份定向复审仍有 48 条非 CRITICAL 内容问题，说明“CRITICAL 清零”不等于质量链已经稳定。
- 之前一轮曾出现 67 条 CRITICAL，修复后降至 6 条；随后新一轮又出现 49 条。现有代码与运行证据表明，主要风险不是单个 SKU 修复遗漏，而是不同入口的 QC 不一致、生成/重生成重新引入事实冲突、缓存/源快照缺少可见溯源，以及 eBay replacement-style PUT 失败后状态不确定。
- 当前 `daily_tasks.py` 的生成路径执行结构化质量门，但没有直接调用统一 FactSheet 检查；`batch_publish.py` 与 `scripts/audit_fix_ready_drafts.py` 各自复制 FactSheet 调用和错误处理。
- 之前 live 修复已证明：eBay API 返回 400 时可能存在部分字段已经写入的情况。因此“请求失败”不能直接等价于“没有改变”，必须回读、比较并决定回滚或补偿。

### 1.3 本轮边界

本轮允许：

- 修改生成、草稿审计、发布前校验和回读校验的共享代码；
- 添加回归测试、dry-run 报告和审计证据；
- 读取 eBay/GIGA/本地数据库，做 live detect-only 验证。

本轮默认不做：

- 批量结束、删除、重刊、改价或改库存；
- 通过白名单、降低 severity、跳过 FactSheet 或伪造源事实来“消除”问题；
- 修改 `.env`、token、调度器和无关 SKU；
- 在没有用户再次明确授权前批量写 live listing。

## 2. 设计原则与验收口径

### 2.1 QC 状态

共享门面统一返回三种主状态：

| 状态 | 含义 | 处理 |
|---|---|---|
| `pass` | 结构化 QC 和 FactSheet 均通过 | 可以进入下一阶段 |
| `blocked` | 存在质量阻断或事实违规 | 不得标 READY、不得发布 |
| `unavailable` | 必要的事实抽取/外部校验不可用 | 保守阻断，等待重试或人工确认 |

每次结果至少包含：`sku`、`quality_issues`、`fact_sheet_status`、`blockers`、`warnings`、`source_fingerprint`、`candidate_fingerprint`、`ruleset_version`、`fact_sheet_version` 和时间戳。

### 2.2 严重级别策略

- `CRITICAL`：自动阻断，必须修复或人工裁决并留下证据。
- `HIGH`：默认阻断，不能自动放行。
- `MEDIUM`：先作为可见 warning 进入审计队列；若属于事实不确定、库存/类目/尺寸等关键域，则升级为阻断。第一条代码切片保持现有发布入口的保守行为，不擅自放宽既有门。
- 运输/回读失败：独立计数为 transport failure，不能当成“无内容问题”。

### 2.3 保护字段

任何发布后修复或补偿写入都必须保护并回读对账：

- availability / GIGA 库存映射；
- condition；
- package weight/size；
- brand / MPN 等源识别字段；
- imageUrls / videoIds；
- listing ID / offer ID；
- 未授权的分类字段。

## 3. 分阶段实施计划

### Phase 0：冻结基线、建立证据目录

**任务：**

1. 保存本计划和 todo；记录工作区已有改动，不覆盖用户变更。
2. 读取当前 live audit、定向复审、修复结果、备份和测试结果。
3. 建立本轮报告命名规则，并明确“代码测试”和“live 写入”分开统计。

**验收：**

- 计划、todo 可独立阅读；
- 明确当前 23 SKU 的 `CRITICAL=0`、transport `=0` 和 48 条非 critical 的证据路径；
- 没有因写计划覆盖已有 `tasks/plan.md`。

### Phase 1：共享 QC 门面（当前执行）

**任务：**

1. 新建一个无外部写入的 QC service，集中调用：
   - `normalize_generated_listing`；
   - `validate_listing_quality`；
   - `blocking_issue_messages`；
   - `check_fact_sheet_violations`。
2. 将结构化质量问题、FactSheet 违规、FactSheet unavailable、异常统一为可序列化结果。
3. 去重阻断消息，保留 `claim_type`、severity、source evidence 和指纹。
4. 先用单元测试覆盖 `pass / blocked / unavailable / exception` 四类路径，再接入生产入口。

**验收：**

- 没有 QWEN/Factsheet 结果时不会误报 pass；
- FactSheet CRITICAL/HIGH 不能被普通质量门覆盖；
- 测试不触发网络或 eBay 写入。

### Phase 2：把 QC 前置到生成和 READY

**任务：**

1. 在 `daily_tasks.analyze_collected_products` 的每次 AI 输出归一化后调用共享门面。
2. 失败时把结构化 blockers 传回重试提示；最终失败保持原状态并记录原因，不写成 READY。
3. 在 `scripts/audit_fix_ready_drafts.py` 中复用同一门面，避免生成审计和发布审计规则漂移。
4. 保留 source/candidate 指纹和规则版本，便于回答“今天为什么又冒出来”。

**验收：**

- 一个含事实冲突的候选 listing 在生成阶段就不能进入 READY；
- FactSheet unavailable 时为 `unavailable/blocked`，而不是空 blockers；
- 旧的 READY/ERROR 定向审计测试和新增测试全部通过。

### Phase 3：统一发布前门并生成 dry-run 证据

**任务：**

1. `batch_publish.py` 复用共享 QC 结果，不再复制 FactSheet 字符串拼装逻辑。
2. dry-run 结果保存 QC 版本、源/候选指纹、目标类目、图片数、运输/库存检查结果。
3. 只允许通过 QC、价格、类目、尺寸、图片、视频和 Motors compatibility 的 SKU 进入 publish allowlist。
4. 任何阻断都关联 SKU 和具体 source evidence。

**验收：**

- generation、ready audit、publish dry-run 对同一候选返回一致的 blocker；
- allowlist 与 dry-run 结果集合完全相等；
- dry-run 不产生 eBay live 写入。

### Phase 4：发布写入的状态机和回读

**任务：**

1. 发布前保存 protected-field snapshot。
2. Inventory PUT、Offer PUT/PATCH、publish 后都做最小必要回读。
3. API 异常一律标记 `write_state=unknown`，先回读再决定补写或回滚；不根据异常文本猜测“未写入”。
4. 回读发现 availability、图片、视频、类目或 listing/offer ID 不符合预期时，停止后续批次并生成人工确认队列。
5. 不改变现有 listing ID，不在未授权时修改分类。

**验收：**

- 模拟“请求报错但部分字段已写入”时，测试能识别 partial write；
- live 写入失败不会被记录成 success；
- 失败状态有可重试/可回滚边界。

### Phase 5：FactSheet 缓存溯源与规则版本化

**任务：**

1. 在缓存记录中增加 extractor/ruleset version、来源类型、生成时间和模型状态；旧缓存缺版本时按 miss 处理。
2. 缓存 key 至少绑定内容 hash + FactSheet 版本 + 规则集版本，避免旧解析结果继续影响新 QC。
3. 记录 source snapshot 的采集时间、卖家/URL 变化和 GIGA 库存状态。
4. 对“结构化字段与源文案冲突”的场景记录 conflict evidence，不能静默覆盖。

**验收：**

- 规则或 extractor 版本变化后不会命中旧结果；
- 缓存 miss、LLM 失败和真实 pass 可区分；
- 发生复发时能回答是源变了、生成变了、缓存旧了还是发布写入部分成功。

### Phase 6：持续 QC 与复发监控

**任务：**

1. 每日生成按 SKU 的 `pass/blocked/unavailable/warning/transport` 汇总。
2. 对同一 `claim_type + rule_id + generator_path` 的重复问题聚类，形成经验规则。
3. 设立复发阈值：同一规则连续两天出现或同一 SKU 复发，进入人工确认队列；不自动继续重写。
4. 每次自动修复后做 targeted live audit，并将结果与修复前快照关联。

**验收：**

- 日报能区分新问题、历史未修问题和修复后复发问题；
- transport failure 不会被折算成 clean；
- 规则变化、生成器变化和源数据变化都有版本记录。

## 4. 当前实施顺序与最小变更策略

本次先完成 Phase 0 和 Phase 1，并立即把 Phase 2 的生成入口接入；原因是当前最直接的缺口是生成路径缺少 FactSheet 阻断，而不是继续批量改 live。完成并验证这一条薄切片后，再进入发布状态机和缓存迁移，避免一次改动跨越过多不可逆边界。

本轮不提交 Git commit：工作区已有大量用户/历史改动，待用户确认变更边界后再按文件选择性提交。

## 5. 风险与回退

| 风险 | 预防 | 回退 |
|---|---|---|
| FactSheet 服务不可用导致误放行 | unavailable 保守阻断 | 重试或人工确认，不改源事实 |
| 新门过严导致 READY 数下降 | 先只接入生成路径并保留详细 blockers | 按日志定位规则，不直接删除门 |
| eBay replacement PUT 部分成功 | API 异常后强制回读 | 用 protected snapshot 做补偿/回滚 |
| 缓存旧规则污染结果 | 版本化 key 和 miss-on-version-mismatch | 保留旧表，迁移失败时只读旧数据但禁止放行 |
| 调度器与手工任务并发 | live 阶段前检查运行状态 | 本轮停止 live 写入，保留 dry-run |

## 6. 总体验收标准

- 生成、READY 审计和发布 dry-run 使用同一个共享 QC 门面。
- 任一入口都不能把 FactSheet unavailable 当成 pass。
- 事实冲突、类目/尺寸/数量/库存等关键问题在发布前被阻断或进入人工队列。
- 发布异常可以区分 no-write、partial-write、success 和 readback-failed。
- 关键 protected fields 在修复/发布前后都有证据对账。
- 相关测试全绿；dry-run 结果可程序化解析；live 变更只在用户明确授权的独立阶段执行。

## 7. 本轮执行记录（2026-08-04）

已完成：

- 新增共享 `run_listing_qc`，并接入生成、READY 审计和发布 dry-run；
- FactSheet unavailable、事实违规和质量门异常均不会放行；
- 发布成功判定增加 Inventory/Offer 回读，区分 `verified`、`partial_write`、`mismatch` 和 `transport_failure`；
- FactSheet cache 增加版本、ruleset、source_kind、model_status，并对旧版本记录按 miss 处理；
- 新增 `qc_experience` / `qc_rule_catalog` 登记模型；QC 结果开始返回 `rule_ids`、`experience_ids`、`rule_provenance` 和 `evidence_refs`；
- 建立 6 类首批经验种子目录，覆盖内容事实、类目尺寸、库存策略和发布部分写入；
- 前一阶段相关回归测试合计 281 passed；本阶段新增合并回归为 228 passed，具体命令和结果见 `logs/listing_qc_prevention_validation_20260804.md`。
- 已对真实 `ebay_collection.db` 执行安全迁移：备份为 `backups/ebay_collection_pre_qc_experience_seed_20260804_183056.db`，源库和备份均 `integrity_check=ok`；源库登记 6 个 experience、6 条活动规则。
- 新增可重复 CLI：`scripts/seed_qc_experiences.py` 默认 dry-run，`--apply` 才执行“在线备份 → 短事务 → 完整性校验”；迁移失败会回滚表结构和行数据。
- 新增只读回放/复发 CLI：`scripts/replay_qc_experiences.py`；本轮回放 `verified_cases=6/6`、`rule_provenance_complete=true`、clean control 复发为 0。
- 新增产品族上下文 rule_id：只有标题明确命中 Hall Tree/Side Table 且 issue 类型匹配时才挂窄规则，避免用 SKU 白名单扩大误报。

本阶段收尾执行结果：

- 已基于最新 READY 审计的 12 个通过 SKU 冻结 allowlist，并运行真实 `batch_publish.py --dry-run --sku-list`；12/12 结果为 `dry_run`，结果集合与 allowlist 恒等，三维尺寸、图片数和 Motors compatibility 结构检查均通过。该 dry-run 仅持久化本地 prepared listing，没有 live 写入。
- 已对历史 23 个 live CRITICAL SKU 运行标准 `audit_fix_active_listings.py --live` detect-only；transport/availability=0，但标准口径仍发现 `W1801109588` 1 条 CRITICAL、16 条 HIGH、34 条 MEDIUM。未带 `--fix`，没有修改 live。
- 已新增只读跨系统对账器 `src/services/listing_qc_cross_system_report.py` 及 CLI `scripts/generate_listing_qc_cross_system_report.py`。实跑 44 个 SKU：source URL 44/44、seller host evidence 44/44、GIGA live inventory 44/44、eBay quantity evidence 23/23；GIGA/eBay 库存冲突 0，9 个 SKU有到货证据。报告采用生产库存口径：buyer 可用量为 0 时回退 seller 可售量；缺少 live 数量不会当成 0。
- 已新增 plan evidence verifier `src/services/listing_qc_plan_verification.py` / `scripts/verify_listing_qc_plan.py`，统一保存 READY、CRITICAL 历史复审、live detect-only、跨系统对账和 publish dry-run 的机器可读证据。
- 本阶段仍没有新增批量 live 写入；上述 1 条 CRITICAL 及 HIGH/MEDIUM 问题进入人工/下一授权批次，不因报告生成而自动修复。

详细执行摘要见 `logs/listing_qc_prevention_execution_summary_20260804.md`，统一证据见 `logs/listing_qc_plan_evidence_20260804.json`。

## 8. QC 经验沉淀闭环（纳入总计划）

### 8.1 目标

以后每次人工修复都必须留下可重放的案例，并最终转化为规则、测试和 QC 结果中的可追踪 provenance。经验不以“某个 SKU 的特殊白名单”形式沉淀，而以有证据、有适用条件、有版本、可回滚的规则形式沉淀。

### 8.2 三层资产

#### A. `qc_experience` 案例库

每个修复案例至少记录：

- `experience_id`、发生日期、SKU、问题域；
- source/GIGA 原始事实、候选内容、live 内容的指纹和关键证据；
- 根因分类：生成器、规则缺失、源数据冲突、缓存过期、库存同步、eBay 部分写入或运输失败；
- 修复动作、修复前后差异、验证报告路径；
- `candidate / confirmed / enforced / rejected / deprecated` 状态；
- 误报/复发说明和可回滚信息。

#### B. `qc_rule_catalog` 规则库

每条规则至少记录：

- `rule_id`、关联 `experience_id`、规则域和适用条件；
- severity、动作（normalize/block/manual_review/reconcile）；
- 是否必须有 source evidence；
- generator path、ruleset version、测试路径；
- 晋级、降级、废弃时间和原因。

#### C. 回归案例库

每个 confirmed/enforced 经验必须有固定输入和期望结果：

- 原始 source facts；
- 生成或 live 候选；
- 期望 `rule_id`、severity、QC status；
- 需要保留的字段和禁止修改的字段。

### 8.3 经验到代码的归属

| 经验类型 | 沉淀位置 | 默认动作 |
|---|---|---|
| 类目、尺寸、数量、禁用 aspects | `listing_quality_gate` | deterministic block/normalize |
| 材质、功能、容量、认证、数量语义冲突 | `listing_fact_sheet` | FactSheet block/manual review |
| GIGA 库存、到货日期、eBay quantity | inventory reconciliation | reconcile/人工确认，不直接套内容规则 |
| Inventory/Offer/API 部分写入、图片塌缩 | `listing_publish_readback` | stop batch + partial-write/transport 状态 |
| 文案生成提示和重试经验 | generation path | 作为结构化 blocker 反馈给下一次生成 |

### 8.4 标准晋级流程

```text
修复发现
  ↓
记录 experience + source/live 证据
  ↓
先写失败回归测试
  ↓
判定规则域、severity 和适用条件
  ↓
candidate / shadow 验证历史 SKU
  ↓
误报率可接受后晋级 enforced
  ↓
QC 结果带 rule_id / experience_id
  ↓
按复发率、误报率定期保留、降级或废弃
```

晋级约束：

- 一次特例不能直接形成全局白名单；
- 同一类问题重复出现，或同一 SKU 修复后复发，才进入规则晋级队列；
- CRITICAL/HIGH 事实问题默认阻断；
- 无 source evidence 的经验只能进入 candidate/manual review，不能自动放行；
- 每条规则必须有测试和版本号，规则失效时可以按版本回退。

### 8.5 首批经验样本

首批从已有修复记录登记，不改 live：

- 结构化 `Seats=4` 与源文案“5 people”的冲突裁决；
- Hall Tree 尺寸方向归一化；
- Side Table/End Table 类目和 aspects 纠偏；
- GIGA 无库存但有到货日期时保留 listing；
- GIGA 有库存而 eBay quantity=0 时禁止误结束并进入库存恢复核查；
- eBay API 400 后部分字段已经写入时的 readback 分类。

### 8.6 本阶段实施任务

1. 新增 `qc_experience`、`qc_rule_catalog` 的登记接口和 SQLite 迁移演练；
2. 让共享 QC 结果返回 `rule_ids`、`experience_ids`、`evidence_refs`；
3. 把已有人工修复结果登记为初始 confirmed/candidate 案例；
4. 增加历史样本回放，验证规则能拦截旧问题且不过度扩大阻断范围；
5. 生成经验复发日报，并按阈值将 candidate 晋级、降级或转人工确认。

### 8.8 本轮 Phase 7 执行结果（2026-08-04）

- 迁移前 dry-run：源库 `integrity_check=ok`，`qc_experiences=0`、`qc_rule_catalog=0`；没有创建备份或写入。
- 真实迁移：写入 6 个 experience 和 6 条 rule；迁移前快照中不存在经验表，迁移后源库和快照均可独立校验。
- 规则反查：6/6 `rule_id → experience_id → test_path` 完整；库存规则保持 `manual_review`，没有混入内容白名单。
- 历史回放：capacity 命中 10 条、Hall Tree 尺寸命中 2 条、Side Table 类目命中 5 条；三类修复后的 clean control 均为 0 次复发。
- fixture 回放：GIGA 无库存+到货日期 → `preserve_listing/manual_review`；GIGA 有库存+eBay quantity=0 → `hold_end_and_reconcile/manual_review`；部分写入 → `partial_write`，正常对照 → `verified`。
- 复发日报将库存两类和 partial-write 标为 `collect_cross_system_evidence`，因为当前历史日志没有机器可读的 GIGA/eBay/readback 原始对账记录；不据此自动晋级/降级。
- 全部相关回归：257 passed，1 个既有 urllib3 HTTPS warning；新增迁移、回放和上下文 provenance 测试均通过。

### 8.7 本阶段验收标准

- 每次 QC blocker 都能定位到 rule_id；已登记的规则能反查 experience_id 和证据；
- 每条 enforced 规则至少有一个回归测试；
- 规则不会通过 SKU 白名单绕过 source evidence；
- 库存经验和内容经验分域处理；
- 规则版本变化后能识别新旧结果，历史样本回放可复现；
- 未经用户明确授权，不因经验沉淀自动执行批量 live 写操作。
