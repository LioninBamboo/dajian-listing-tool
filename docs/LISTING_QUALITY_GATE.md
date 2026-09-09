# Listing Quality Gate

这份文档说明链接生成和发布前的统一质量门。它是当前 `COLLECTED -> READY -> PUBLISHED` 链路的安全边界，用来防止 AI 草稿、eBay taxonomy suggestion、历史类目或图片处理逻辑把错误数据带到 live listing。

## 目标

质量门要解决这些已经真实出现过的问题：

- 产品类目错配：patio furniture set 被分到室内 sofa/table，bench 被分到 table 或 pet furniture，coffee table 被描述里的 sofa/couch 词误导成 sofa 类目。
- item specifics 错配：家具草稿出现 `US Shoe Size`、`Sport`、`Pet Type` 等非家具字段。
- set includes 错配：chair-only set 被写成 `Dining Table & Chairs`，bench 或 ottoman 被写成 table。
- 测量值不可信：`Item Length` / `Item Width` / `Item Height` 缺失或是 `See Description` / `Refer to Product Images`。
- 描述和 item specifics 不一致：description 中没有同步 item specifics 的长宽高重量数字。
- 图片不足或发布后塌图：本地多图但 live eBay 只剩一张或明显少图。
- 文本乱码：例如 `Boucl谷`、`Bouclй`、`每` 进入标题、描述或 item specifics。
- 标题截断幻觉：本地标题超过 eBay 80 字符后被硬截成半个词，live listing 结尾出现 `Inc`、`Ou`、`wi` 之类残片。
- 上线后文案漂移：`Foldable`、`Assembly Status`、`Assembly Required` 等 live 字段与 GIGA 原文不一致。
- 上线后语义幻觉：source 只写了 generic material / adjustable，却在 live 文案里扩写成 `Acacia Wood`、`With Cushion`、`UV Resistant`、`5-Position Backrest` 这类未被 source 支持的细节。

## 核心模块

| 文件 | 责任 |
|------|------|
| `src/utils/listing_quality_gate.py` | 统一质量门：产品画像、草稿规范化、质量校验、阻断原因 |
| `src/services/ebay_category_matcher.py` | 类目 canonicalization 与 plausibility 判断 |
| `src/utils/publish_validation.py` | 测量值和类目基础校验 |
| `src/utils/publish_aspect_completion.py` | 发布侧 item specifics 补全 |
| `src/utils/publish_autofix.py` | 占位值与单值字段清洗 |
| `src/utils/title_sanitizer.py` | 标题清洗 + eBay 80 字符安全裁剪 |
| `scripts/audit_fix_ready_drafts.py` | READY 草稿批量修复并输出 unresolved |
| `batch_publish.py` | dry-run 和 live publish 前再次执行质量门 |

## 进入点

质量门已经接入以下生成和发布入口：

- `POST /api/collect` 的 source preflight
- `batch_analyze.py`
- `daily_tasks.py`
- `server.py`
- `scripts/audit_fix_ready_drafts.py`
- `batch_publish.py`

这意味着现在有两层防线：

- `COLLECTED` 阶段先做 source preflight，尽早标出源视频不可发布等问题；
- 新草稿在进入 `READY` 前会先规范化并过 quality gate；
- 即使历史 READY 草稿绕过了生成入口，发布前也会再次被 `scripts/audit_fix_ready_drafts.py` 和 `batch_publish.py` 拦截。

## Source Preflight

采集时不再只看图片/描述长度，还会额外提取 `source_facts`：

- `assembly_required` / `assembly_status`
- `foldable` 是否被源数据明确支持
- `USB/charging` 是否被源数据明确支持
- source video 是否存在，以及是否属于可直接发布的文件 URL
- `source_category_hint`：本地关键词规则给出的类目提示

当前明确拦截的源视频问题：

- `.txt` 视频清单
- `.m3u8` 流媒体清单
- `youku.com` / `youtube.com` / `youtu.be` / `vimeo.com` 这类页面 URL

这些问题会在采集日志 `logs/collection_quality_issues.jsonl` 中留下 `source_facts` 和 `source_video_not_publishable` 标记，避免后面只看到 “missing_video” 却追不回源头。

类目方面，`COLLECTED -> READY` 的分析入口现在也统一传入 shared `category_matcher`。这意味着 AI 或旧草稿一旦带入不合理类目，quality gate 会在进入 `READY` 前直接挡住，而不是等到 publish 或 live audit 才发现。

## 产品画像

`classify_listing_profile()` 根据标题优先识别产品族，再决定类目和关键 item specifics。

当前重点画像：

| 产品族 | 目标类目 | 关键规则 |
|--------|----------|----------|
| patio furniture set | `139849` Patio & Garden Furniture Sets | `Type=Patio Furniture Set`，`Indoor/Outdoor=Outdoor` |
| storage ottoman | `20490` Ottomans, Footstools & Poufs | `Type=Storage Ottoman`，去掉 table-only 字段 |
| indoor bench | `262980` Benches | `Type=Bench`，`Set Includes=Bench` |
| dining chair | `54235` Chairs | chair-only set 用 `Set Includes=Chairs` |
| coffee table | `38204` Coffee Tables | 不允许被 description 中 sofa/couch 词改成 sofa |
| dining table | `38204` Dining Tables | 去掉 upholstery-only 字段 |
| sofa / loveseat | `38208` Sofas, Armchairs & Couches | 只在标题明确 sofa/couch/loveseat/sectional 时触发 |

规则原则：

- 标题是产品身份的第一信号，description 只能辅助，不应覆盖明确标题。
- 标题进入 eBay 前必须先做 marker/prefix 清洗，再做 word-safe 80 字符裁剪。
- 类目修复必须通过 product-family plausibility。
- 历史类目只有在明显不合理时才替换。
- 新增画像时必须同步测试。

## 标题规则

发布前和已刊登修复都必须遵守：

- 不允许直接用 `title[:80]`。
- 必须走 `normalize_listing_title_for_ebay()`，确保标题在 80 字符内且不以半个词结尾。
- 如果 live listing 已经出现截断残片，active listing audit 必须优先清理并 republish。

典型坏样本：

- `...for TVs up to 85 Inc`
- `...Stainless Steel BBQ for Ou`
- `...Entertainment Center Console wi`

标准修复方式：

- 删除截断残片和其前面的悬空连接词；
- 保留尽可能多的完整词；
- 再回写 inventory item，并重新 publish 对应 offer。

## 测量规则

发布硬要求：

- `Item Length` 必须真实存在。
- `Item Width` 必须真实存在。
- `Item Height` 必须真实存在。
- `Item Weight` 可缺省；如果存在，必须真实、非占位、非非正数。

可信来源顺序：

1. 大建 / supplier 的 assembled dimensions 和 product weight
2. description 中可解析的 product dimensions / product weight
3. 经人工确认的 SKU override

不允许：

- 用 `See Description`、`Refer to Product Images`、`N/A`、`Unknown` 填 item specifics 的长宽高重量。
- 用 package dimensions 伪装 item dimensions。
- 用 package weight 伪装 `Item Weight`。

允许：

- 对复杂多边形或组合产品，在 description 中提示参考尺寸图。
- 但 item specifics 仍必须有整体长宽高数值。

## 图片规则

采集阶段：

- 保留原始 GigaB2B 图片 URL。
- 不删除签名参数 `x-cc` / `x-cu` / `x-ct` / `x-cs`。

发布阶段：

- 可移除安全的处理参数，例如 `x-oss-process`。
- 发布或 revise 后必须回读 eBay Inventory `product.imageUrls`。
- 如果本地源图多张但 live 只有一张，优先按 publish/revise 图片问题处理。

全库扫描与修复：

```bash
python tools/scan_image_collapse.py
python tools/restore_listing_images.py SKU1 SKU2
python scripts/audit_fix_active_listings.py --live --fix --sku-file logs/residual_listing_audit_skus.txt
```

## 标准操作

READY 发布前：

```bash
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
python batch_publish.py --sku YOURSKU
```

检查点：

- 最新 `logs/ready_draft_audit_*.json` 中 `unresolved` 必须为空。
- `batch_publish.py --dry-run` 必须全部通过。
- live publish 后如涉及图片，必须确认 live image count 不低于预期。

## 失败处理

质量门失败时，不要直接手改 live publish payload。先判断失败类型：

| 失败类型 | 处理方式 |
|----------|----------|
| `missing_measurement` | 补大建 assembled dimensions、从 description 提取，或人工确认 override |
| `category_mismatch` | 修 category matcher 或 product profile，不要只改单个 SKU |
| `forbidden_aspect` | 加入画像 remove list 或清洗规则 |
| `description_measurement_mismatch` | 用 `replace_description_measurements()` 同步描述 |
| `insufficient_images` | 重新采集或用 Dajian/GigaB2B 补图 |
| live image count lower than expected | 用 EPS restore 路径恢复，并重新回读验证 |
| `incomplete_title` | 用 `normalize_listing_title_for_ebay()` 生成安全标题并 republish |
| `unsupported_foldable_claim` / `unsupported_charging_claim` | 修 AI 生成规则或源事实提取，不要带着不被 GIGA 支持的功能词进 `READY` |
| `source_video_not_publishable` | 先把源视频转换成可直链下载文件，或补上可发布 mp4，再进入 `READY` / publish |
| `hallucinated_foldable` | 以 live eBay snapshot 为修复基底，不要只改本地 optimization；全库 `--fix` 必须带 `--live` |
| `hallucinated_wood_species` / `hallucinated_cushion` / `hallucinated_weather_resistance` / `hallucinated_position_count` | 这类是 source 没有支持、AI 却补出来的语义细节；active audit 必须直接报错，修复时按 source material / source wording 回写，不允许继续保留自造卖点 |
| `assembly_description_missing` | 当 item specifics 写了 `Assembly Required=Yes`，description 也必须显式说明需要 assembly；修完后 republish live offer |
| `missing_foldable` | 这是 `hallucinated_foldable` 的反向问题：如果 GIGA 明确支持 foldable/collapsible，例如 drop leaf kitchen island，publish path 不应把该能力误清洗掉 |
| `missing_video` | 先区分 source 是否真的有可发布视频；source 有视频而 live 没有 `videoIds` 时，修复视频补链 / 上传链路，不要只改邮件文案 |
| `suspect_source_dimensions` | 供应商把压缩包装尺寸填进 assembled 字段（assembled == package 且标题声明更大尺寸，例：W5571P440912 的 71" 沙发）。此时源尺寸不可信，audit 自动压制尺寸/重量/描述尺寸修复，仅报告；不要手动把 live 尺寸改成源值 |
| 类目被 "sofa table" 误触发 | "Sofa Table / Sofa Side Table / Behind Couch / Console Table" 是桌类不是沙发；`classify_listing_profile` 和 `canonicalize_category` 均已加排除（2026-07-13 事故：床头柜与玄关桌被改到 38208，"Storage Bedside" 子串还曾命中 "storage bed" 被改到 175758 床架） |

## 语义事实表护栏 (Layer 2.5)

规则表永远追不上幻觉长尾（真实案例 W3636P456662：source 写 `600D Oxford`，live 标题写成 `Canvas`，材质升级链里没有 oxford→canvas，规则层沉默了近三个月）。`src/utils/listing_fact_sheet.py` 用"LLM 提取 + 确定性 diff"补这个洞：

- LLM 只做**提取**：把 source 和 live 内容各转成结构化事实表（materials / features / counts / capacity / certifications / dimensions）；
- 幻觉判定是**确定性 diff**：live 声明必须能被 source 事实表支持（token 重叠 + 同义词组桥接，如 `4 season` ≈ `year-round use`）；
- 材质词典兜底：LLM 可能把标题里的 `Canvas Bell Tent` 读成品类词，`lexical_materials()` 确定性扫描强制补全材质声明；
- 事实表按内容 hash 缓存（`fact_sheet_cache` 表），source/live 没变就不再调 LLM；
- 已接入 `audit_fix_active_listings.py`（Layer 2.5，报告-only，不自动改写），环境开关 `AUDIT_SEMANTIC_FACT_SHEET=0` 可关闭；
- 已知限制：source 中的否定语（"Unlike cotton tents"）会把 cotton 计入 source 支持，v1 不做否定识别。

## 源内容刷新与出单复核

采集快照会过期——卖家会改标题/参数/文案/视频，甚至下架。两条新链路让审计对比的是供应商**当前**真相：

- `scripts/source_content_refresh.py`（每天 10:55，排在 11:30 审计前）：批量重抓 Dajian `detailInfo`，字段级漂移检测并修复本地快照。漂移分四类：`enrichment`（采集时缺失，静默补全）、`normalize`（首刷把加工过的存量值归位成忠实源值）、`repair`（乱码快照重建）、`change`（卖家真实变更，告警 + 写入 `logs/source_drift_skus.txt` 交给审计）。视频 URL 按 path 比较（GigaB2B 签名参数轮换不算漂移），尺寸数值 0.1 容差（存储精度差异不算漂移）。漂移记录在 `source_drift_log` 表。
- `scripts/order_source_recheck.py`（每 6 小时，lookback 8h 留重叠）：Trading `GetOrders` 拉新订单，对出单 SKU 立即重抓源 + 比对 live 声明（claim 引擎 + 尺寸容差 1in/2lbs + source 可购性），CRITICAL 不符即发邮件——发货前是拦退款的最后窗口。复核记录在 `order_recheck_log` 表（按 order_id+sku 去重）。Inventory-blind 的 Trading 老 listing 走 `GetItem` fallback 读 live 内容。

## Active Listing Audit Guardrails

- `scripts/audit_fix_active_listings.py --fix` 如果作用范围是全量 `PUBLISHED`，必须同时带 `--live`。
- 非 `--live` 修复只允许用于 `--sku` / `--sku-file` 这类定向范围。
- 审计报告必须保留 `source` 字段，用来区分 `live_ebay` 与 `local_db_optimization`。
- `daily_tasks.py` 的定时 audit auto-fix 现在必须先回读 live inventory / live offer，再基于 live snapshot 修复；不能再用本地 optimization 快照直接覆盖 live listing。
- source description、source video 和 live snapshot 必须一起进入修复链路，避免再次出现 `hallucinated_foldable` 回写和 `missing_video` 漏补。

## 2026-06-18 Execution Note

- 2026-06-18 17:59 的全量 postfix live verify 还剩 `141` 条 live 债务，其中 `130` 条为 `hallucinated_foldable`，`11` 条为 `missing_video`。
- 2026-06-18 18:47 的第二轮全量 postfix live verify 已降到 `0` 条，报告见 `logs/listing_audit_verify_20260618_postfix_full_live_round2.json`。
- 这次归零依赖两个条件同时成立：先堵住 `daily_tasks.py` 的非 live 修复口子，再对 live 剩余 SKU 做真实 `--live --fix`。
- `hallucinated_foldable` 和 `missing_video` 经常重叠；对残留 SKU 做二次 live 修复时，应优先一次性处理两个问题，再做 postfix verify。

## Live Audit Report Interpretation

`logs/listing_audit_fix_*.json` 里的几个数字要这样读：

- `total_with_issues`: 有至少一个问题的唯一 listing 数
- `issues[].type`: 真正要拿来分桶的 issue type
- `source`: 这次审计基于 `live_ebay` 还是 `local_db_optimization`
- `fixed_count`: 这次运行实际修掉了多少；如果是定时 `--live --email` 检测任务，通常为 `0`

不要把 `total_with_issues` 当成“根因数”或“重复行数”。一条 listing 可以同时带多个 issue type。

`2026-06-17` 的 live audit 是一个很典型的例子：

- `512` 条唯一 listing 有问题
- 但总 issue instance 是 `532`
- 因为其中 `18` 条 listing 同时带多个 issue type

同一份报告再拆成时间来源，会更清楚：

- `497` 条是历史 live 债务，主因是 `hallucinated_foldable`
- `15` 条是 `2026-06-17` 当天新 publish 后立刻被 live audit 重新打回

这个模式说明了两件事：

1. live audit 首先是“历史 live 内容债务探照灯”，不是只看今天新增了多少问题；
2. 如果同日新发链接立刻出现在报告里，说明 publish-time quality checks 还没有完全覆盖 live-audit 的全部规则。

## 测试

改动质量门、类目匹配、READY 审核或发布前校验时，至少运行：

```bash
python -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py
python -m py_compile src/utils/listing_quality_gate.py src/services/ebay_category_matcher.py batch_analyze.py daily_tasks.py server.py batch_publish.py scripts/audit_fix_ready_drafts.py
```

回归测试必须覆盖真实错误样本，而不是只测理想输入。
