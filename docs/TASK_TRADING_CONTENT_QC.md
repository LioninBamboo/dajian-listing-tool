# 任务书：AquaRides 汽配（Trading 通道）通用内容质检（持续质检脚本）

**交付对象**：执行 AI（可独立完成）
**实例**：`C:\Users\poonx\AutoParts_Listing_Tool`（AquaRides，store_kind=auto，Trading/Motors，SiteID 100）
**创建日期**：2026-08-12 ｜ **更新 2026-08-12**

> ⚠️ **状态更新（务必先读）**：任务书原含的「一次性重推 45 条清乱码 + 补 KEY FEATURES」**已由本人完成，你不要再做**：
> - 乱码根因已修：`motors_trading.ascii_safe()` 已接进 `build_add/build_revise`（commit 1fe9f75，19 测试过）。
> - 存量 45 条已全部重推干净：`scripts/repush_clean_descriptions.py`（commit 70cfb12）跑过，**实测 45/45 零乱码、45/45 有 KEY FEATURES**。
>
> **你的实际任务只剩一件**：建**持续质检脚本** `scripts/trading_content_qc.py`（读 live→检测→--fix 修 CRITICAL/HIGH→--email）+ 单测 + 注册每日任务。见 §4/§5/§6。§2/§3 是背景与复用件参考。

---

## 0. 背景：为什么要新建

通用质检脚本 `scripts/audit_fix_active_listings.py` 只会用 **Inventory API** 读 live listing。
但 AquaRides 是 **Trading 通道**发布的，Inventory API 读不到 → 45 条里 43 条误报
"imageUrls collapsed to 0"、86 条 "Could not read live eBay"（全是假警报，也修不了）。
所以汽配需要一个**走 Trading `GetItem` 读取**的通用质检，与 GrovePop 的 `Content QC`（Inventory）对等。

## 1. 目标（剩余）

1. ~~一次性修复现存 45 条~~ **已完成（见顶部状态更新，勿重复）**。
2. **建持续质检** `scripts/trading_content_qc.py`：每天读 live 描述、检测问题、自动修复 CRITICAL/HIGH、发邮件。
3. 注册为每日 Windows 计划任务（参考已存在的 `AquaRides Fitment Audit`）。

## 2. 已知缺陷与已修好的底层（勿重复）

- **乱码（已根治底层，需回推现存）**：eBay 在 Trading `<Description>` CDATA 里会把多字节字符（× é 等）
  存成 `�`（如尺寸 "15.7 × 5.7" 变 "15.7 � 5.7"）。**已修**：`src/services/motors_trading.py::ascii_safe()`
  把描述转 ASCII 安全（× → x、décor → decor…），且 `build_add_fixed_price_item_xml` /
  `build_revise_fixed_price_item_xml` 已对 description 调用它。**未来发布已干净**；现存 45 条需重推一遍才干净。
  验证过：重推后 `GetItem` 读回 0 个 `�`、尺寸显示 "L x W x H"。
- **模板对齐（部分完成）**：多数已有 KEY FEATURES，但个别偏薄（如 ItemID 188683585596 无 KEY FEATURES）。
  KEY FEATURES 由 `src/utils/conversion_copy.build_conversion_feature_bullets()` 生成，源特征太少时会空。
- **Assembly 行已去**：`_store_hides_assembly()` 门控，auto 店描述不含 "Assembly Required"，保持现状。
- **SKU / UPC / MPN**：已回填，勿动。

## 3. 描述生成 & 重推（核心链路，复用现成件）

- 生成：`src/services/semantic_rewrite.build_description_from_source(title, source_description, attrs, specs, aspects)`
  → 委托 `scripts/audit_fix_active_listings.build_structured_description_from_source`（确定性版式，零 LLM）。
- 重推：`src/services/motors_trading.build_revise_fixed_price_item_xml(item_id=..., description=...)`
  （已内置 ascii_safe + UPC/Brand/MPN 占位符），POST 到 `https://api.ebay.com/ws/api.dll`
  头 `X-EBAY-API-CALL-NAME: ReviseFixedPriceItem`、`SITEID: 100`、`COMPATIBILITY-LEVEL: 1155`，
  body 用 `.encode('utf-8')`。Ack=Success/Warning 即成功。
- 认证：`src/services/ebay_auth.EbayOAuthService(environment='PRODUCTION').get_valid_token()`。
- 数据源：`SELECT sku, listing_id, title, description, attributes, specs, optimization FROM collected_products
  WHERE status='PUBLISHED' AND listing_id!=''`（`optimization` 里取 `aspects`）。

## 4. trading_content_qc.py 检测项（severity）

| 检测 | severity | 判据 |
|---|---|---|
| mojibake | CRITICAL | live 描述含 `�`(U+FFFD) |
| 缺 banner | HIGH | 不含 `AQUARIDES` |
| 缺 KEY FEATURES | HIGH | 不含 `KEY FEATURES` |
| 描述过薄 | HIGH | 去标签后正文 < 800 字符 |
| 含 Assembly | MEDIUM | 含 `Assembly Required`（汽配不该有） |
| 原始中文源 | CRITICAL | 描述里出现中日韩字符 |

- `--fix`：对 CRITICAL/HIGH 重新生成描述并重推（ascii_safe 已内置）；MEDIUM 只报告不修（与主店一致）。
- `--email`：发中文汇总，抬头 banner 突出"需处理 N 条：CRITICAL x / HIGH y"，MEDIUM/LOW 不进正文
  （抄 `scripts/audit_fix_active_listings.py::_send_audit_email` 的 `_filter_actionable` 机制）。
- 读 live 用 Trading `GetItem`（`DetailLevel=ItemReturnDescription`），**不要用 Inventory API**。
  注意 `requests` 解码：用 `resp.content.decode('utf-8','replace')`，再 `html.unescape`（描述是双重转义的）。

## 5. 执行步骤

1. 建 `scripts/trading_content_qc.py`（§4 检测 + --fix + --email），写单测 `tests/test_trading_content_qc.py`
   （mojibake 检测、severity 分级、universal 不误报等），`pytest` 全过。
2. **验证**：`python scripts/trading_content_qc.py`（只读）应报告 ~0 待修（存量已清），
   再故意改坏 1 条测 `--fix` 能修回。**不要**再全量重推（已干净）。
3. 注册每日任务（**先检后修**，见 `docs/QC_PIPELINE_RED_LINES.md`）：
   - `AquaRides Content QC`：`--email`（只读）— `scripts/register_aquarides_content_qc.ps1`
   - `AquaRides Content QC Fix`：`--fix --email --limit 30` — `scripts/register_aquarides_content_qc_fix.ps1`
   - Fitment：`register_aquarides_fitment_tasks.ps1`（Audit 只读；Apply 仅 missing_fitment + 回读）

### 5.1 --fix 重推的认证坑（本人踩过，务必照做）

- Trading 条目重推描述：用 `build_revise_fixed_price_item_xml(item_id=..., description=...)`（已内置 ascii_safe）。
  它**不含** XML 内 token，所以 POST 头**必须带 `X-EBAY-API-IAF-TOKEN: <token>`**（否则报
  `Header "X-EBAY-API-APP-NAME" does not exist`）。参考 `scripts/repush_clean_descriptions.py` 的 header 写法。
- **有 1 条是 Inventory API 建的**（ItemID 188683585596，SKU W2913P449891）：Trading Revise 会报
  `Inventory-based listing management is not currently supported`。这条要走 **Inventory API**：
  `GET /sell/inventory/v1/offer?sku=...` → 改 `listingDescription`（记得先 ascii_safe）→
  `PUT /sell/inventory/v1/offer/{offerId}` → `POST /sell/inventory/v1/offer/{offerId}/publish`。
  QC 脚本应按渠道分流（Trading 用 Revise，Inventory 用 offer PUT+publish），或至少捕获这条错误单独处理。

## 6. 验收标准

- 抽查 ≥6 条（含 188683585596、188752675328、188760790738）`GetItem` 读回：
  - `�` 计数 = 0（尺寸显示 "L x W x H"）；
  - 含 `AQUARIDES` + `KEY FEATURES` + `SPECIFICATIONS`；不含 `Assembly Required`；
  - 去标签正文 > 1500 字符；无中日韩字符。
- 单测全过；`--fix` 只改 CRITICAL/HIGH，不动 MEDIUM。
- 每日任务 `State=Ready`。

## 7. 陷阱

- **不要用 Inventory API 读汽配 listing**（读不到，全假警报）——必须 Trading GetItem。
- Windows 全缓冲：`PYTHONUNBUFFERED=1`，进度查 DB / 日志。
- UTF-8：文件读写 `encoding='utf-8'`；描述重推前底层已 ascii_safe，别再重复转。
- 不要动 Inventory-API 建的那 1 条（188683585596 若是 Inventory 建则 Trading Revise 会报
  "Inventory-based listing management not supported"——跳过记录）。实测它可被 Trading 读，但改要看报错。
- MEDIUM 不修不显示（与主店质检一致，用户明确要求）。

## 8. 参考

- 已修底层：`src/services/motors_trading.py`（ascii_safe / build_revise_fixed_price_item_xml）
- 生成：`src/services/semantic_rewrite.py` / `scripts/audit_fix_active_listings.py`
- 邮件+过滤样例：`scripts/audit_fix_active_listings.py::_send_audit_email` / `_filter_actionable`
- 计划任务样例：已注册的 `AquaRides Fitment Audit`（`scripts/fitment_audit.py`）
