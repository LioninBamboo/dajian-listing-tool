# 交接任务书 — 存量 CRITICAL 批量清理（2026-07-26）

分支：`codex/live-audit-semantic-guard`
执行者：下一个 AI 执行方
验收人：本会话（人工最终确认）

---

## 0. 一句话目标

把**存量** CRITICAL 幻觉一次性批量清完，而不是每天靠增量队列啃 40 条。

---

## 1. 先读这一节：清单会变，不要用旧数字

用户口头说的是「143 个 CRITICAL SKU」。**这个数字已经过期，不要用它。**

7/26 我方修了三处审计噪声（提交 `4674f02`），其中最关键一条：
eBay 的 aspects（Item Specifics）过去被喂进事实表提取器当成"文案声明"。
但 aspects 是**类目必填枚举**——`Features: With Cushions`、`Style: Modern`、
`Room: Living Room` 都是从 eBay 固定选项里选的，源永远不会那样措辞，于是条条误报。

按 7/26 审计数据回算：

| | 旧报告（7/26 22:00） | 修复后真实基线 |
|---|---|---|
| CRITICAL 行 | 207 | **133** |
| CRITICAL SKU | 143 | **98** |
| 其中来自 aspect 枚举、将自动消失 | — | 64 行 |

> ### 🔴 红线 R1：必须先重跑审计，再动任何 listing
> 那 64 行**不是要修的问题**，是审计自己的误报。如果你照旧报告去"修"，
> 会把**正确的 eBay 属性改坏**——这比不修严重得多。
>
> 因此第一步永远是重跑审计拿新基线。**不允许**直接使用
> `logs/listing_audit_fix_20260726_212604.json` 或本文档里的任何 SKU 清单去执行写入。
> 本文档提供的清单只用于**预估工作量和交叉核对**，不是执行输入。

---

## 2. 预置清单（仅供核对，不可直接用于写入）

我方已生成，基于修复后的真实基线：

| 文件 | 条数 | 含义 |
|---|---|---|
| `logs/crit_batch_all.txt` | 98 | 全部真实 CRITICAL SKU |
| `logs/crit_batch_rewrite.txt` | 76 | 改写管线可处理（材质/容量/计数/认证/尺寸） |
| `logs/crit_batch_category.txt` | 30 | 类目/组装类，改写管线**不负责** |
| `logs/_crit_true_baseline.json` | — | 逐 SKU 逐条明细 |

（76+30 有 8 个重叠，是两类问题都有的 SKU。）

真实基线按类型：

```
41  semantic_material          ← 描述文案里的真幻觉
21  semantic_dimension
19  semantic_count
19  semantic_capacity
16  assembly_required_mismatch ← 不走改写管线
14  category_mismatch          ← 不走改写管线
 2  semantic_certification
 1  hallucinated_leather
```

---

## 3. 真实可自动化率：约 55%，不是 100%

我方在 `logs/crit_batch_rewrite.txt` 前 20 条上实跑了 dry-run：

**11 条 pushable / 9 条转人工（55%）**

比每日增量队列的 70% 低——存量本来就是难啃的剩饭。**不要试图把这个数字提上去。**
被拒的都是有正当理由的：

| 拒绝原因 | 含义 | 正确处理 |
|---|---|---|
| `title_category_conflict` | 按源重建标题会触发类目预检冲突 | 转人工队列，**不要**绕过预检 |
| `semantic_dimension: skipped (suspect/untrusted source dims)` | 供应商把包装尺寸填进了整装字段 | 转人工，**不要**用可疑尺寸覆盖 live |
| `semantic_material_compound` | 源是 `Acacia Wood,Rope,Waterproof Fabric` 复合材质 | 转人工仲裁 |

> ### 🔴 红线 R2：转人工就是转人工
> 上述任一原因触发时，该 SKU **退出自动流程**，写进人工队列。
> 不允许放宽校验、不允许绕过类目预检、不允许"这条看起来没问题就推了"。

---

## 4. 执行步骤

### Step 1 — 重跑审计拿新基线（必做）

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/audit_fix_active_listings.py --live --ignore-clean-freeze
```

注意是 `--live`（读 live eBay 内容），不是 `--fix`。这一步**只审计不写入**。
加 `--ignore-clean-freeze` 是为了让上次标记为 clean 的 58 条也重新参与——
本次修复改变了判定逻辑，冻结状态已失效。

跑完记录：CRITICAL 行数、SKU 数。**与本文档第 1 节的 133/98 对比**：
- 落在 110–150 行 → 正常，继续
- 明显偏离（比如仍是 200+，或掉到 30 以下）→ **停下来报告**，不要继续执行

### Step 2 — 从新审计派生队列

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/semantic_rewrite.py --derive-queue
```

产出 `logs/semantic_rewrite_queue.txt`。这才是**唯一合法的执行输入**。

### Step 3 — 全量 dry-run，人工放行

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/semantic_rewrite.py --sku-file logs/semantic_rewrite_queue.txt --preview-dir reports/crit_backlog_preview
```

把 pushable / needs_human 的**数量和分类**汇报给验收人。等待放行后再进 Step 4。

### Step 4 — 金丝雀 10 条

从 Step 3 判定为 pushable 的 SKU 里挑 10 条，**要覆盖不同问题类型**
（材质/容量/计数各若干），不要全挑同一类。写进 `logs/_canary10.txt`，一行一个 SKU：

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/semantic_rewrite.py --sku-file logs/_canary10.txt --apply
```

推完**逐条人工抽验**（见第 5 节验收清单），确认无误再继续。

### Step 5 — 分批推余下

每批 ≤ 30 条，**每批之间停下来核对**。批与批之间必须重新确认上一批 live 正常。

### Step 6 — 类目/组装类单独处理

`logs/crit_batch_category.txt` 那 30 条**不要**用改写管线。
`category_mismatch` 历史上出过误伤（把床头柜改成沙发类目、把控制台桌改成沙发）。
这一类**全部转人工**，只输出建议清单，不写入。

---

## 5. 每条推送后的验收清单

对金丝雀和每批抽样，逐项确认：

- [ ] **描述模板完整**：banner（AquaVerve）+ footer（`✦ Ships from US Warehouse ✦` /
      `Quality Guaranteed • Fast US Shipping • Trusted Seller`）都在
- [ ] **在 offer 的 `listingDescription` 上验证**，不是 inventory 那份
      （inventory 描述会被截断到 4000 字符，验证那份会误判）
- [ ] **标题不残**：没有截断、没有半句话结尾
- [ ] **材质大小写正确**：`MDF` 不是 `Mdf`，`PU` 不是 `Pu`
- [ ] **listing_id / offer_id 未变**：必须原位更新，**绝不允许 end + relist**
- [ ] 描述里没有中文、没有原始源 HTML 结构

---

## 6. 红线汇总（只能触发停止，执行方无权重新解释）

> 历史教训：曾有执行方在正确触发「批量回滚 >2 条则停止」后，
> 自行把它重新解释为「批次栅栏——结束本批、继续下一批」。
> **红线的唯一合法反应是停下来报告并等待人工指示。**

| # | 红线 | 触发后 |
|---|---|---|
| R1 | 未重跑审计就用旧清单写入 | 禁止执行 |
| R2 | 转人工的 SKU 被绕过校验推送 | 立即停止 |
| R3 | 单批回滚 > 2 条 | 立即停止，等人工 |
| R4 | 任何 end-listing / relist 行为 | 立即停止 |
| R5 | 修改或删除既有测试断言 | 禁止（只能新增测试） |
| R6 | `git add -A` / `git add .` | 禁止（工作区有并行 session 的改动） |
| R7 | 修改 `.env` | 禁止 |
| R8 | 杀掉/重启调度守护进程 | 禁止 |
| R9 | `git push` | 禁止（只提交，不推送） |
| R10 | 绕过 PricingEngine / RepricingGuard / EbayPublisher | 禁止 |

---

## 7. 已知的、不要"修"的东西

- **LLM 提取存在 run-to-run 抖动**：同一 SKU 两次跑出的 violation 数可能不同
  （实测有 SKU 从 8 条变 3 条、pushable 从 True 变 False）。
  **规则：只对连续两次都被标记的问题动手。**不要为了消除抖动去改提取器。
- **MEDIUM 数量仍会有上千条**：那是营销措辞噪声，本次任务**不处理 MEDIUM**。
  只看 CRITICAL。不要因为 MEDIUM 多就扩大作业范围。
- **`_generic_material_supported` 的家族表**：已在 `4674f02` 校准过，
  12 条正反用例锁定（`tests/test_listing_fact_sheet.py`）。
  放行必须要求源里有该家族的**具体成员**——
  纯软体沙发（源只有 chenille/foam）claim "engineered wood frame" 是**真幻觉**，必须继续拦。
  不要为了少几条告警去松这个条件。

---

## 8. 环境与命令约定

```bash
# 测试（必须用 venv 解释器）
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'; .\.venv\Scripts\python.exe -m pytest <slice> -q

# 双闸已开（不需要你改）
# .env: SEMANTIC_REWRITE_APPLY_ENABLED=1  +  命令行 --apply

# 回滚单条
.venv/Scripts/python.exe scripts/semantic_rewrite_rollback.py --sku <SKU> --apply
```

提交时**只 stage 你自己改的文件**：`git add <具体路径>`。
工作区里 `batch_publish.py`、`cro_title_rewrite.py`、`fix_measurement_quality_issues.py`
等是并行 session 的改动，**不要碰、不要提交**。

---

## 9. 交付物

1. Step 1 新审计的 CRITICAL 基线数字（与 133/98 的对比）
2. Step 3 全量 dry-run 的 pushable / needs_human 分类统计
3. 金丝雀 10 条的逐条验收结果
4. 分批推送的实际落地数（SKU 级清单）
5. 人工队列清单 + 每条的转人工原因
6. 期末重跑一次审计，给出 CRITICAL **前后对比**
7. 全量测试结果（`pytest tests/ -q`），基线是 **2068 passed**

**不要自评"任务完成"。** 交付上述数据后等待验收人确认。
