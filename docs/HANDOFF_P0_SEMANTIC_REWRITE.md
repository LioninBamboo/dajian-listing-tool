# 交接任务书 #3：语义改写管线 P0（纯工具开发 + dry-run，零 live 写入）

> 交接方：Claude（2026-07-13）
> 执行方：你（AI 执行模型）
> 验收方：交接方按第 7 节验收；P1 金丝雀放行权在用户，不在你也不在交接方。
> **规格来源：`docs/PROJECT_SEMANTIC_REWRITE.md` 第 3 节（架构决策已锁死）。本任务书告诉你做什么和边界；立项书第 3 节告诉你怎么做。两文件冲突时以立项书第 3 节为准。**
> 前置阅读（只读）：立项书全文、`docs/LISTING_QUALITY_GATE.md` 的"语义事实表护栏"章节。

---

## 0. 一句话任务

实现 `semantic_rewrite` 工具（改写引擎 + 回滚脚本 + 测试），并对 5 个指定 SKU 产出 **dry-run** before/after 预览报告。**本阶段对 eBay 和 collected_products 零写入**——你交付的是工具和预览，不是修复。

## 1. 严格禁止（违反任一条即执行失败）

1. **禁止任何 live 写操作**：不许调用 `publish_offer` / `create_or_replace_inventory_item` / `update_offer_category` 的真实路径（代码里可以有，dry-run 下不可达，且 `--apply` 必须双闸：CLI flag + 环境变量 `SEMANTIC_REWRITE_APPLY_ENABLED=1`，两者缺一即拒绝并退出）。本阶段你**不允许**设置该环境变量。
2. **禁止写 collected_products**（fact_sheet_cache 缓存表可写，这是设计内的）。
3. **禁止修改任何既有文件**。你只允许**新增**：
   - `src/services/semantic_rewrite.py`（核心逻辑，可测试）
   - `scripts/semantic_rewrite.py`（CLI 薄壳）
   - `scripts/semantic_rewrite_rollback.py`
   - `tests/test_semantic_rewrite.py`
   - `logs/semantic_rewrite_p0_preview/` 下的报告文件
   需要复用既有函数一律 **import**（`build_structured_description_from_source`、`_put_inventory_product_only`、`refresh_skus`、`fact_sheet_for_content`、`compare_fact_sheets`、`detect_claim_violations`、`normalize_listing_title_for_ebay` 等都已存在，W3636 修复已验证过 import scripts.audit_fix_active_listings 的可行性）。不够用就在自己的新文件里写适配层，**不许改宿主文件**。
4. 禁 git commit / stage / push；禁改既有测试断言；禁碰调度进程 / 语义队列批量执行 / 价格库存。
5. LLM 只允许出现在验证路径（事实表提取），生成路径零 LLM——这是立项书 3.1 的铁律。

## 2. 交付物 A：核心逻辑 `src/services/semantic_rewrite.py`

按立项书 3.2 单 SKU 流实现，函数化、可注入 fake client（供测试）：

```
plan_rewrite(conn, dajian, ebay, sku) -> RewritePlan | SkipResult
    # 源刷新 → 事实表 → 违规清单 → 生成新 title/aspects/description（零 LLM）
    #   → 三层验证 → 返回计划（含 before/after 与验证结果），不推送
apply_rewrite(ebay, conn, plan) -> ApplyResult
    # 快照留档到 logs/semantic_rewrite_backups/{sku}.json → 推送 → 由调用方跑 postfix
    # 双闸检查在此函数入口
```

要点（全部来自立项书 3.1-3.3，此处只列易错点）：
- 描述重建喂 `build_structured_description_from_source` 的 source_description 参数时，先裁出源快照的 Product Features 片段（防规格行混入卖点，参考 W3636 模式）
- aspects 修正严格按立项书 3.3 表；未在表内的违规类型 → 计划标记 `needs_human`，不生成修正
- 标题只在自身含违规声明时重建；重建 = 源标题过 `normalize_listing_title_for_ebay`
- SkipResult 情形：源 characteristics 为空/过薄（重建器无料）、源已下架（skuAvailable=False）、`suspect_source_dimensions` 时不写尺寸、描述缩水 >60% 红线
- 验证三层缺一不可：claim 引擎 0 CRITICAL、新内容事实表 diff 0 CRITICAL、质量门要件（模板标记/L×W×H 数字/assembly copy/标题 ≤80 word-safe）

## 3. 交付物 B：CLI `scripts/semantic_rewrite.py`

```
--sku / --sku-file / --limit     目标选择
--derive-queue                   按立项书 8.2 从最新 listing_audit_fix_*.json 推导队列
                                 → logs/semantic_rewrite_queue.txt（排除下架源、按 CRITICAL 数降序）
（默认 dry-run）                  输出 RewritePlan 预览，不推送
--apply                          双闸推送（本阶段禁用）
--preview-dir DIR                dry-run 时把 before/after 写成报告
```

## 4. 交付物 C：回滚 `scripts/semantic_rewrite_rollback.py`

`--sku X` 读 `logs/semantic_rewrite_backups/{sku}.json`，恢复 title/aspects/description 到 eBay（inventory+offer）并 republish，更新 DB optimization。同样受双闸约束。P0 阶段只需测试覆盖（mock），不实跑。

## 5. 交付物 D：测试 `tests/test_semantic_rewrite.py`

全 mock（fake eBay / fake dajian / 内存 sqlite），不许真连网。至少覆盖：
1. 3.3 修正表逐行（material/capacity/certification/count/feature/dimension 六类）
2. 标题：含违规 → 重建；不含 → 原样
3. SkipResult 四种情形各一
4. 双闸：无环境变量时 apply_rewrite 拒绝
5. 描述缩水 >60% 红线拦截
6. 验证层失败（人为注入无源声明）→ 计划标记不可推
7. W3636 端到端仿真：用其真实源快照数据构造，plan 产物含全部模板标记且三层验证通过

**测试命令**：`$env:PYTHONPATH='...'; .venv\Scripts\python.exe -m pytest tests/test_semantic_rewrite.py tests/test_listing_fact_sheet.py tests/test_claim_diff_engine.py -q` 全绿。

## 6. 交付物 E：5 SKU dry-run 预览（真实数据，只读）

对 `logs/handoff_followup/p0_preview_skus.txt` 的 5 个 SKU（材质/容量/认证/数量/功能过度声明各一代表）运行 dry-run，产出 `logs/semantic_rewrite_p0_preview/`：
- `{sku}.md`：违规清单 → before/after 标题、aspects diff 表、描述纯文本 diff（逐段）、三层验证结果
- `_summary.md`：5 条汇总 + 你发现的任何规格盲区（只许记录，不许自行扩展规格）

注意 dry-run 会读 live eBay（拿当前内容）和大建 API（源刷新走只读路径——**不写 DB**，plan_rewrite 里源刷新用 `refresh_skus(..., apply=False)` 或等价只读方式取新鲜快照供比对）。

## 7. 验收标准（交接方执行）

1. 四个新文件齐全，`git status` 无任何既有文件改动（我会逐一 diff）
2. 我重跑第 5 节测试命令 + `py_compile` 全部新文件：全绿
3. 我逐份审 5 个预览报告：**after 内容中不存在任何一条源不支持的声明**（我会用事实表引擎独立复核 after 内容）；模板标记齐全；aspects 修正与 3.3 表一致
4. 双闸实测：我会在无环境变量下尝试 `--apply --sku <preview sku>`，必须拒绝退出且零副作用
5. collected_products 无写入痕迹（5 个 SKU 的 updated_at 不变）
6. 无 commit；调度进程存活
7. 产出物 `logs/handoff_p0_progress.jsonl`（按交付物 A-E 各一行：status/tests/notes）

## 8. 开工自检清单

- [ ] 已读立项书全文，理解"生成零 LLM"与双闸设计
- [ ] 明确本阶段零 live 写入、零 collected_products 写入
- [ ] 明确只许新增 4 个代码文件 + 报告，不改任何既有文件
- [ ] venv + PYTHONPATH；测试全 mock 不连网
- [ ] 禁 commit、禁改既有断言

确认后按交付物 A→E 顺序执行。
