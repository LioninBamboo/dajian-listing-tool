# 任务书：对齐 AquaRides 汽配 listing 描述模板

**交付对象**：执行 AI（可独立完成，无需本对话上下文）
**实例**：`C:\Users\poonx\AutoParts_Listing_Tool`（AquaRides 汽配店，store_kind=auto，Trading/Motors 通道，SiteID 100）
**创建日期**：2026-08-12

---

## 1. 目标

把**现有全部已发布（PUBLISHED）汽配 listing 的描述**用**现有采集内容**重新生成并重推到 eBay，使它们统一对齐到"好样板"
`https://www.ebay.com/itm/188752675328`（ItemID 188752675328）的完整版式：
`banner → KEY FEATURES(转化卖点) → PERFECT FOR → SPECIFICATIONS → 车型提示 → footer`。

**不改标题、不改价格、不改类目、不改图片** —— 只重建 `<Description>`。

## 2. 现状与根因（已诊断）

- 描述由 `src/services/semantic_rewrite.build_description_from_source()` 生成，它委托给
  `scripts/audit_fix_active_listings.build_structured_description_from_source()`。
- 版式是**确定性**的（源特征驱动、零 LLM 臆造）；KEY FEATURES 区来自
  `src/utils/conversion_copy.build_conversion_feature_bullets()`。
- **不一致的原因不是模板不同，而是部分早期 listing 内容偏薄**：源特征少时 KEY FEATURES 区被省略。
  对比证据：188752675328 有 KEY FEATURES；早期的 188683585596 无 KEY FEATURES（两者 banner 都在）。
- **已完成的相关修复（勿重复）**：
  - 汽配描述已去掉 "Assembly Required" 行（`_store_hides_assembly()` 门控），**保持现状**。
  - Trading 发布已写 `<SKU>`（Custom Label）。

## 3. 执行步骤

1. **只读**列出 PUBLISHED 且有 listing_id 的 SKU：
   `SELECT sku, listing_id FROM collected_products WHERE status='PUBLISHED' AND listing_id!=''`（约 45 条）。
2. 对每个 SKU：从 DB 取 `title / description(源) / attributes / specs / optimization.aspects`，
   调 `build_description_from_source(title, source_description, attrs, specs, aspects)` 生成新描述 HTML。
3. **重推**：走 Trading `ReviseFixedPriceItem`（SiteID 100），只改 `<Description><![CDATA[...]]></Description>`。
   - Motors 类目 Revise 会要求商品标识符：**必须同时带**
     `<ProductListingDetails><UPC>Does Not Apply</UPC></ProductListingDetails>` 和
     `<ItemSpecifics>` 里 `Brand=AquaRides`、`Manufacturer Part Number=Does Not Apply`，否则报
     "UPC field is missing / MPN missing"。参考现成实现 `scripts/backfill_trading_sku.py` 的 revise 封装。
   - Ack=`Success` 或 `Warning` 均算成功（Warning 多为打款提示，非失败）。
4. 逐条 `time.sleep(0.4)` 限速；`PYTHONUNBUFFERED=1 PYTHONUTF8=1`，nohup 后台，日志写 `logs/`。
5. 跑完统计 `成功/失败`，失败逐条打印 `LongMessage`。

## 4. 验收标准

- 抽查 ≥5 条（含之前无 KEY FEATURES 的 188683585596）的 live 描述（GetItem, `DetailLevel=ItemReturnDescription`）：
  - 都含 `AQUARIDES` banner、`KEY FEATURES`、`SPECIFICATIONS`；
  - **不含** `Assembly Required` / `Assembly Instructions`（汽配已门控）；
  - 无 mojibake（乱码）、无原始中文源文本、无截断。
- 描述长度 > 1500 字符（防止退化成空壳）。
- 成功率 ≥ 95%；失败项给出原因清单。

## 5. 陷阱（务必遵守）

- **Windows 全缓冲**：`python x.py > file` 不退出不刷新；用 `PYTHONUNBUFFERED=1` + 查 DB 计数看进度。
- **UTF-8**：所有文件读写 `encoding='utf-8'`；Trading XML 用 `.encode('utf-8')` POST。
- **只读 DB 里的源字段**，不要改 `optimization`/价格/状态。
- **不要动 Inventory-API 建的 listing**（Trading Revise 会报 "Inventory-based listing management is not currently supported"）——遇到就跳过记录。
- 先在**单条**上验证 revise 成功，再批量。
- 不要引入 LLM 重写描述正文（版式是确定性的，LLM 只在采集优化阶段出标题+aspects）。

## 6. 参考文件

- 生成：`src/services/semantic_rewrite.py::build_description_from_source`
- 版式：`scripts/audit_fix_active_listings.py::build_structured_description_from_source` / `rebuild_specifications_table`
- Revise 封装样例：`scripts/backfill_trading_sku.py`
- 认证：`src/services/ebay_auth.EbayOAuthService(environment='PRODUCTION').get_valid_token()`
