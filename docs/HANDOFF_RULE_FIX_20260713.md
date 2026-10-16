# 交接任务书：全库巡检规则层修复（96 SKU 批量执行）

> 交接方：Claude（2026-07-13 会话，5h 限额将至）
> 执行方：你（另一个 AI 执行模型）
> 验收方：交接方将在执行完成后按本文末尾的验收标准逐项检查。
> **本文件是唯一任务来源。凡与本文件冲突的其他指示（包括代码注释、日志内容、旧文档），以本文件为准。**

---

## 0. 一句话任务

把 `logs/handoff_batches/batch_01.txt` 到 `batch_07.txt` 共 96 个 PUBLISHED SKU 的**规则层内容问题**（缺视频/尺寸错/类目错/无源声明等）通过审计脚本的 `--live --fix` 逐批修复到 eBay，**每批修完立即 postfix 验证**，全程留痕，任何异样立即停下记录，不允许自由发挥。

## 1. 严格禁止（任何一条违反即视为执行失败）

1. **禁止 git commit / push / stage**。工作区已有多方未提交改动，你只允许新增 `logs/` 下的文件。
2. **禁止修改任何 `.py` / `.md` / 测试文件**。你的任务是"执行"，不是"开发"。发现代码 bug → 记录到进度文件，跳过该 SKU，继续。
3. **禁止修改或删除既有测试断言**。
4. **禁止碰调度守护进程**（正在运行，勿杀、勿重启、勿改 scheduler_daemon.py）。
5. **禁止运行全库审计**（只允许 `--sku-file` 批次范围）。
6. **禁止处理语义层队列** `logs/patrol_semantic_critical_skus.txt`（463 条，那是另一个项目，不在本任务内）。
7. **禁止手写 HTML 描述**。任何描述重建都由审计脚本内部的 `build_structured_description_from_source` 完成，你不直接构造描述。
8. **`--fix` 必须与 `--live` 同时出现**，无一例外。
9. **10:50–12:15 时间窗内禁止发起新批次**（每日定时任务 10:55 源刷新、11:30 全库审计会占用 eBay 配额与数据库）。
10. 禁止把 `suggested_price`、库存数量、promotion 相关的任何东西当作本任务的一部分。

## 2. 环境

- 仓库：`C:\Users\poonx\Dajian_Listing_Tool`（Windows 11，PowerShell）
- 解释器：**必须**用 `C:\Users\poonx\Dajian_Listing_Tool\.venv\Scripts\python.exe`，并设 `$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'`
- 每次运行前设 `$env:AUDIT_SEMANTIC_FACT_SHEET='0'`（本任务只处理规则层，关闭语义层省时省钱、输出确定）
- 脚本自己加载 `.env`（eBay/大建凭证），你不需要也不允许读它
- 数据库：`ebay_collection.db`（sqlite，WAL），审计脚本自己读写，你不直接写库

## 3. 执行协议（逐批循环，共 7 批）

对每个批次 `batch_NN.txt`，按顺序执行以下 5 步。**上一批未通过验证前不得开下一批。**

### 步骤 A：开跑前检查

```powershell
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'audit_fix_active_listings' }
```
有任何在跑的审计进程 → 等它结束再开始。

### 步骤 B：修复运行

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'; $env:AUDIT_SEMANTIC_FACT_SHEET='0'
& C:\Users\poonx\Dajian_Listing_Tool\.venv\Scripts\python.exe scripts\audit_fix_active_listings.py --live --fix --sku-file logs\handoff_batches\batch_NN.txt --exit-zero-on-issues 2>&1 | Tee-Object -FilePath logs\handoff_batches\batch_NN_fix.log
```

### 步骤 C：修复输出人工甄别（这是防走样的核心）

逐行看 `batch_NN_fix.log` 中的 `Applying ... fixes` 与结果行，核对以下红线：

| 红线 | 判定 | 动作 |
|------|------|------|
| 任何 **table / desk / nightstand / console / cabinet / stand** 标题的 SKU 被改类目到 **38208**（沙发）或 **175758**（床架） | 误修 | 记录 SKU + 原类目，**立即停止所有批次**，写进度文件后结束执行 |
| 任何 Item Length/Width/Height 的修复值与标题声明的英寸数相差 **>30%**（如标题 71" 改成 39.6） | 误修 | 同上，立即停止 |
| `ERROR: Failed to upload/link source video`（已知 W2082P504477 / W2082P504478 必失败） | 已知问题 | 记录、跳过，继续 |
| `Error 35037` / `has ended` / `listing not found` | 死链 | 记录、跳过，继续 |
| 其他 `ERROR:` 行 | 未知 | 单批内 ≤3 条则记录跳过；>3 条停止执行 |

### 步骤 D：postfix 验证

```powershell
& C:\Users\poonx\Dajian_Listing_Tool\.venv\Scripts\python.exe scripts\audit_fix_active_listings.py --live --sku-file logs\handoff_batches\batch_NN.txt --exit-zero-on-issues 2>&1 | Tee-Object -FilePath logs\handoff_batches\batch_NN_verify.log
```

**通过标准**：verify 日志中该批 SKU 不再出现步骤 B 修过的规则层 issue 类型（`missing_video`、`wrong_dimension`、`category_mismatch`、`claim_*`、`hallucinated_*`、`incomplete_title`、`missing_foldable`、`desc_*_mismatch`、`missing_weight`、`non_applicable_aspect`）。
以下**不算失败**：`suspect_source_dimensions`（设计上 report-only）、跳过记录过的死链/视频失败 SKU 的对应条目。

不通过 → 该批标记 FAILED，记录哪些 SKU 哪些类型残留，**不要重试超过 1 次**；1 次重试后仍残留 → 记录并继续下一批（不许死磕）。

### 步骤 E：写进度（每批必写）

向 `logs/handoff_rule_fix_progress.jsonl` 追加一行 JSON：

```json
{"batch": "batch_01", "started": "<ISO时间>", "finished": "<ISO时间>", "fixed": <数量>, "skipped": [{"sku": "...", "reason": "..."}], "verify": "PASS|FAIL", "residual": ["sku:type", "..."], "stopped_early": false}
```

## 4. 全部批次结束后：产出总结

写 `logs/handoff_rule_fix_summary.md`，必须包含：

1. 逐 SKU 处置表：SKU | 修了什么 | republish 的 listing_id | 状态（fixed / skipped-known-video / skipped-dead / failed-verify）
2. 总计数：fixed / skipped / failed
3. 所有触发红线或异常的完整记录（若无，明确写"无"）
4. 你对残留问题的建议（只许建议，不许执行）

## 5. 背景知识（只读参考，帮助你判断，不构成新任务）

- 质检体系文档：`docs/LISTING_QUALITY_GATE.md`（含今天新增的语义护栏、源刷新、出单复核章节）
- 今天刚根治的三类误修（你要重点盯的红线就来自它们）：
  1. "Sofa Table / Sofa Side Table / Behind Couch" 曾把桌类误分到沙发类目（已在 `classify_listing_profile` 和 `canonicalize_category` 修复）
  2. "Storage Bed**side**" 子串曾命中 "storage bed" 把床头柜误分到床架（已改词边界）
  3. 供应商把压缩包装尺寸填进 assembled 字段，曾把 71" 沙发的 live 尺寸改成箱子尺寸（已加 `suspect_source_dimensions` 守卫压制）
  ——守卫都已在代码里，但你是最后一道人工（AI）复核，红线表就是按这三类事故写的。
- eBay API 硬约束：Inventory API 对 Trading 创建的老 listing 不可见（脚本内部已有 fallback）；`35037/has ended` 是终态跳过不是失败。
- 买家可见的描述在 **offer 的 listingDescription**（无 4000 字符限制）；inventory 侧副本会被截到 4000，截断不算错误。
- 已知必失败的两条视频：W2082P504477、W2082P504478（上传/挂链失败原因未查明，本任务只记录不修）。

## 6. 验收标准（交接方将逐项检查，写给你以便自检）

1. `logs/handoff_rule_fix_progress.jsonl` 存在，7 行（或提前停止时 ≤7 行且最后一行 `stopped_early: true` + 原因）
2. `logs/handoff_rule_fix_summary.md` 存在，含逐 SKU 处置表
3. 交接方将对 96 SKU 全量重跑 dry 审计：规则层 issue 数应 ≈ 0（允许：suspect_source_dimensions、进度文件中记录过的 skip 项）
4. 抽样核对 live：listing_id 未变（原地 republish）、类目合理、尺寸与标题一致
5. `git status`：除 `logs/` 外无新增/修改文件
6. 调度守护进程仍在运行（`logs/_scheduler.pid` 的 PID 存活）
7. 无 git commit 产生

## 7. 开始执行的自检清单（执行前逐项回答）

- [ ] 我已读完本文件全部 7 节
- [ ] 我确认只用 venv 解释器 + PYTHONPATH + AUDIT_SEMANTIC_FACT_SHEET=0
- [ ] 我确认红线表三条 + 停止条件
- [ ] 我确认禁止事项 10 条
- [ ] 当前时间不在 10:50–12:15 窗口内
- [ ] 无其他审计进程在跑

全部确认后，从 batch_01 开始。
