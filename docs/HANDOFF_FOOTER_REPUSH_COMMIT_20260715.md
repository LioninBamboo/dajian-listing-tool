# 交接任务书 #10：旧 footer 批量重推 + 精确 commit

> 交接方：Claude　执行方：你（repair 工具作者）
> 目标：把已修好但 footer 是旧文案的 live listing 重推成新模板；然后**只 commit 本次会话的 listing-quality 改动**（工作树里混着并行会话的改动，绝不能一起提交）。

## 🔒 红线（沿用）：四条红线只能触发停止，不可重新解释/续跑。

## 铁律
禁 push（本任务只 commit 不 push）；禁 `git add -A` / `git add .`（工作树有并行会话改动）；禁改 .env；live 只碰有旧 footer 的 SKU；republish 原 offer 原 listing_id；每 SKU backup+三层验证+失败回滚；避开 10:50–12:15 与 12:30。

## 阶段 1：旧 footer 批量重推

新模板 footer 已定：`✦ Ships from US Warehouse ✦` + `Quality Guaranteed • Fast US Shipping • Trusted Seller`。
旧文案有三代残留需替换：`Ships from California, USA`、`Dedicated Support`、以及中间态 `Easy Returns` / `Secure Checkout`。

1. 扫全库 PUBLISHED：live offer listingDescription 含上述任一旧串的 → 入队 `logs/footer_repush_queue.txt`（预计 ~490+）。
2. 逐条走 `scripts/repair_broken_listings.py` 的 `build_repair`（它现在生成新 footer）→ 三层验证 → backup（`logs/repair_backups/`）→ `_put_inventory_product_only` + `update_offer_category` + `publish_offer` → live 复查含新 footer 且不含旧串。
3. 改不干净的（verify 不过）跳过留清单，不硬推。类目不碰。
4. 每 25 一批、批末抽 2 条独立复查；phase 标 `FOOTER_REPUSH`；产出 `logs/footer_repush_summary.md`。

## 阶段 2：精确 commit（只提交本次会话的 listing-quality 工作）

**只 stage 下列文件**（逐个 `git add <path>`）。对每个"修改"文件先 `git diff <file>` 确认改动是 listing-quality 相关再 add；不相关就不 add。

**新增文件（?? 直接 add）：**
```
src/utils/listing_fact_sheet.py
src/services/semantic_rewrite.py  src/services/source_refresh.py
scripts/semantic_rewrite.py  scripts/semantic_rewrite_rollback.py
scripts/repair_broken_listings.py  scripts/order_source_recheck.py  scripts/source_content_refresh.py
config/semantic_rewrite_maps.yaml
tests/test_semantic_rewrite.py  tests/test_source_refresh.py
tests/test_order_source_recheck.py  tests/test_listing_fact_sheet.py
docs/PROJECT_SEMANTIC_REWRITE.md  docs/PROJECT_DESCRIPTION_MATERIAL_DEBT_DRAFT.md
docs/LEGACY_FOR_WAYFAIR.md  docs/HANDOFF_*  （本次所有 HANDOFF_ 文档）
```

**修改文件（先 diff 确认是本会话改动再 add）：**
```
scripts/audit_fix_active_listings.py  src/utils/claim_diff_engine.py
src/utils/listing_quality_gate.py  src/utils/dimension_helpers.py
src/services/ebay_category_matcher.py  src/services/ebay_video_uploader.py
scheduler_daemon.py  qwen_optimizer.py  docs/LISTING_QUALITY_GATE.md
tests/test_active_listing_audit_cli.py  tests/test_claim_diff_engine.py
tests/test_ebay_category_matcher.py  tests/test_listing_quality_gate.py
```

**绝对不要 stage（并行会话的，与本工作无关）：**
```
batch_publish.py  scripts/cro_title_rewrite.py  scripts/fix_measurement_quality_issues.py
tests/test_collection_analysis_regressions.py  tests/test_cro_title_rewrite.py
tests/test_scheduler_cro_title_rewrite.py
```
（`tests/test_scheduler_recovery.py` 若 diff 只是 source_refresh/order_recheck stub 则可 add；若含 cro 相关则跳过。存疑就跳过。）

提交前跑 `pytest tests/test_semantic_rewrite.py tests/test_listing_fact_sheet.py tests/test_claim_diff_engine.py tests/test_listing_quality_gate.py tests/test_active_listing_audit_cli.py tests/test_source_refresh.py tests/test_order_source_recheck.py -q` 全绿（既有 2 桩若已修则 0 失败）。
commit message：
```
feat(listing-qc): source-fact semantic guard, broken-listing repair, footer refresh

- source refresh / order recheck / semantic fact-sheet guard / rewrite pipeline
- raw-source & truncated-title audit guards + repair_broken_listings
- foldable/feature source-title evidence fix; store template footer (US Warehouse/Trusted Seller)
```
**只 commit 不 push。** 产出 `logs/commit_summary.md`（列实际 staged 文件 + commit hash + 测试结果）。

## 验收（交接方）
- FOOTER_REPUSH DONE 抽 10-15 条 live 含新 footer、无旧串、listing_id 未变、backup 在。
- `git show --stat HEAD`：staged 文件 = 上面白名单，无 batch_publish/cro_* 等并行文件；未 push（`git status` 显示 ahead）。
- 测试全绿。
