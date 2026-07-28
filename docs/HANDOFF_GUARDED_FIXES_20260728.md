# 交接任务书 — 类目还原与剩余收尾（2026-07-28）

分支：`codex/live-audit-semantic-guard`（不要切换、不要新建）

---

## 0. 为什么这份任务书和以前不一样

前两轮都出现了同一个方向的越界：

| 轮次 | 约定 | 实际 |
|---|---|---|
| #1 | "批量回滚 >2 条则**停止**" | 被重新解释成"结束本批、继续下一批" |
| #2 | "B 档未经放行**不许写入**"、"不许动 `category_mismatch`" | 直接跑了 3 次 `--fix`，改了 3 条类目 |

第 #2 轮的后果是真的：两条**室内软包储物凳**（`W3098P470268`/`W3098P470275`）
被改判进 **Outdoor Daybeds** 并 republish 上线，现在还挂着。

根因不全是执行方的问题——`audit_fix_active_listings.py --fix` **不传
`--fix-key` 就会应用该 SKU 的全部待修项**。执行方本意是修组装标志，
顺带把类目也改了。

**所以本轮起，写入不再靠文字约定，改用清单强制。**

---

## 1. 新机制：`scripts/apply_approved_fixes.py`

你**不再自己拼 `--fix` 命令**。只提供一份 CSV 清单，脚本按行构造
被 `--fix-key` 严格限定的命令：

```csv
sku,fix_keys,note
W1234,Assembly Required,source says Yes
W5678,categoryId|categoryName,approved by <人名> 2026-07-28
```

机制保证（15 条测试锁定）：
- 每个 SKU 单独一条命令，`--sku` 只出现一次
- 每个 fix key 都变成独立的 `--fix-key`，**apply 时必定带 key 范围**
- **`categoryId` / `categoryName` 属守卫键**，清单里出现就直接退出 2，
  除非额外传 `--allow-category`
- 任一行失败即整体停止，不继续后面的行

> 🔴 **禁止绕过这个脚本直接调 `audit_fix_active_listings.py --fix`。**
> 这是本轮唯一的写入通道。需要写入清单以外的东西 → 停下来报告。

---

## 2. 任务

### Step 1 — 还原两条被误判的类目（已授权）

清单：`logs/manifest_revert_categories.csv`（3 行，我方已生成，**不要改内容**）

匹配器缺陷已修（`d69f684`）：描述里一句 "or a leisure bench on the balcony"
压过了同一段明写的四个室内房间（Living Room / Entryway / Dormitory / Bedroom），
叠加标题里的 "Bench Daybed" 就判成户外。

> ⚠️ 该判定在代码里有**两份独立实现**：`ebay_category_matcher.py` 和
> `listing_quality_gate.classify_listing_profile`。上一轮只修了前者，
> 而审计走的是后者，所以 live 上的错误类目当时依旧不报。两处现已都修
> （`d69f684` + 本轮）。再遇到"改了匹配器却没生效"，先确认走的是哪条路径。

第三行 `WF319383AAK` 是另一个情况：**live 是 139849，本地库是 79694，两者不一致**，
下次审计还会继续报。本行的目的是让两边重新对齐。

```bash
# 先 dry-run（不加 --apply），确认每条的目标类目符合预期
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/apply_approved_fixes.py logs/manifest_revert_categories.csv --allow-category
```

dry-run 输出里逐条确认目标类目与下表一致：

| SKU | 当前（错） | 目标 |
|---|---|---|
| W3098P470268 | 138996 Outdoor Daybeds | **262980 Benches** |
| W3098P470275 | 138996 Outdoor Daybeds | **20490 Ottomans, Footstools & Poufs** |
| WF319383AAK | live 139849 / db 79694 不一致 | 以审计判定为准 |

> 两条的目标**不一样**——470275 的标题是 "Bed End Foot Stool"，审计判它是脚凳
> 而非长凳。这是审计的真实判定，不是笔误。

确认后：

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/apply_approved_fixes.py logs/manifest_revert_categories.csv --allow-category --apply
```

> 🔴 dry-run 目标与上表任一行不符 → 停止报告，不许 apply。

### Step 2 — 补齐 A 档漏掉的 2 条组装标志

清单：`logs/manifest_assembly_remaining.csv`（2 行）

A 档 16 条里这 2 条上轮被类目修复"占用"了，组装标志没改成。

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/apply_approved_fixes.py logs/manifest_assembly_remaining.csv
# 确认后加 --apply
```

无守卫键，不需要 `--allow-category`。

### Step 3 — B 档：把你判定的结果做成清单交回（**不要写入**）

你上轮填的 `logs/review_dimensions21_filled.csv` 判断质量不错：
3 条长宽对调判"保留live"、9 条判"存疑"，都合理。

现在把判 **"采用源"** 的那几条做成清单文件 `logs/manifest_dimensions.csv`，
格式同上，fix_keys 用具体的尺寸字段键（从 audit 的 `fix_keys` 里取真实键名，
**不要臆造**）。

> 🔴 **做完交回等放行，不许 apply。** 这一步只产出文件。

`WF319383AAK` 已在 Step 1 处理，不要重复列入。

### Step 4 — C 档：18 行建议清单（**只出建议**）

`logs/review_human18.csv`，上轮未交付。判断标准只有一条：

> 该材质在**源描述全文**（不只是材质属性字段）里能不能找到依据？
> - 能找到 → 提取器把部件材质当成主材质，标"误报"
> - 找不到 → 真幻觉，标"需删除"并写明改成什么

已核实的真幻觉样例：`W3636P456625` 帐篷，源全文只有 cotton/oxford，
live 却 claim 钢架。

> 🔴 C 档**不许写入 live**，无论结论多明确。

### Step 5 — 期末重跑审计

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/audit_fix_active_listings.py --live --ignore-clean-freeze
```

给出 CRITICAL 前后对比。当前基线 **83 行 / 73 SKU**。

---

## 3. 红线（只能触发停止，执行方无权重新解释）

- **绕过 `apply_approved_fixes.py` 直接 `--fix`** → 停止
- Step 1 dry-run 目标类目与第 2 节表格任一行不符 → 停止
- Step 3 / Step 4 出现任何 live 写入 → 停止
- 单批回滚 > 2 条 → 停止
- 任何 end-listing / relist（必须原位更新 listing_id/offer_id）→ 停止
- 修改或删除既有测试断言 → 禁止（只能新增）
- `git add -A` / `git add .` → 禁止。工作区有并行 session 的改动，只能 `git add <具体路径>`
- 修改 `.env` / 杀重启调度守护进程 / `git push` → 禁止
- 绕过 PricingEngine / RepricingGuard / EbayPublisher → 禁止

---

## 4. 已知情况，不要顺手改

- **定时任务每天 12:30 自己推 40 条**，会**用推送后的状态覆盖回滚快照**。
  某条 backup 时间戳晚于你的推送时间 → **不要 rollback 那一条**，停下来报告。
- 类目匹配器靠单关键词命中、缺上下文否决，本轮已修三例
  （`cooler weather`→冰桶、`balcony`→户外日间床、历史上 `storage bed`→床架）。
  **再遇到类似的不要自己改匹配器**，报告即可。
- 提取器把颜色和表面处理当材质（`glossy`、`green`）—— 已知技术债，本次不处理。
- 验证描述看 **offer 的 `listingDescription`**，不是 inventory 那份（后者截断到 4000 字符）。
- `EbayOAuthService()` 默认是 **SANDBOX**，生产一律传
  `os.getenv('EBAY_ENVIRONMENT','PRODUCTION')`，否则 401。
- 测试基线 **2158 passed**：
  `$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'; .\.venv\Scripts\python.exe -m pytest tests/ -q`

---

## 5. 交付物

1. Step 1：dry-run 输出 + apply 结果 + 三条的 live 类目复核（含 db/live 是否一致）
2. Step 2：2 条组装标志的 dry-run + apply + live 复核
3. Step 3：`logs/manifest_dimensions.csv`（**只交文件，不 apply**）
4. Step 4：18 行建议清单，每行写明"误报"或"需删除+改成什么"，附源依据
5. Step 5：审计前后对比
6. 全量测试结果

**不要自评"任务完成"。** 每步报完等放行；遇到红线或判断不了的，停下来问。
