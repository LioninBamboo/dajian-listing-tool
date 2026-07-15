# GIGA 源事实质检体系 —— 可移植 Legacy（供 Aquawood Smart ERP-Wayfair 采用）

> 来源项目：Dajian_Listing_Tool（采集 GIGA / 平台 eBay）
> 目标项目：Aquawood Smart ERP-Wayfair（采集 GIGA / 平台 Wayfair）
> 本文把 2026-07 在 eBay 项目建成的整套"源事实驱动内容质检"体系提炼成可复用蓝图，**明确标注哪些是平台无关（可直接搬）、哪些是 eBay 专属（需换成 Wayfair 等价物）**。

---

## 0. 核心理念（平台无关，最重要的一条）

**"供应商源数据（GIGA）是唯一事实基准，刊登内容的任何声明都必须能回溯到源；回溯不到的就是幻觉，要么阻断要么改写。"**

这条理念不依赖任何平台。Wayfair 项目照搬即可。整个体系都是围绕它展开的四道防线 + 一个安全底座。

---

## 1. 四道防线（★=平台无关可直接搬，◆=eBay 专属需替换）

### 防线一：生成期三层门（防"AI 写错"）★★◆
在草稿进入"可刊登"状态前拦截幻觉：
- **L1 生成侧约束**★：给文案 LLM 的 prompt 注入源约束 + 反幻觉 system prompt（禁材质升级/编造功能/编造认证）
- **L2 规则引擎**★：`claim_diff_engine` —— 材质升级链、木种、功能词、数量、认证的确定性正则表
- **L3 LLM 事实核查**★：`llm_fact_checker` —— 抓严重主观夸大/医疗/假认证
- 平台差异：三层门本身平台无关；只有"进入哪个状态算可刊登"（eBay 是 READY→PUBLISHED）要换成 Wayfair 的状态机。

### 防线二：语义事实表护栏（防"规则表追不上长尾"）★★★
这是本体系最通用、最值得搬的部分：
- **LLM 只做提取**：把源内容和 live 内容各转成结构化事实表（材质/功能/数量/容量/认证/尺寸）
- **判定是确定性 diff**：live 声明必须被源事实表支持，token 重叠 + 同义词组桥接（"4 season"≈"year-round"）
- **材质词典兜底**：`lexical_materials` 防 LLM 把标题材质词读成品类词（"Canvas Bell Tent"→漏掉 canvas）
- **按内容 hash 缓存**：`fact_sheet_cache` 表，源/live 没变就不重复调 LLM，成本几乎为零
- 完全平台无关。Wayfair 项目直接搬 `listing_fact_sheet.py` 的三个函数（extract / compare / cache）即可。

### 防线三：刊登后每日审计（防"卖家改源"+"存量幻觉"）★◆
- **源内容刷新**★：`source_refresh` —— 每天重抓全库 GIGA 源，字段级漂移检测（enrichment/normalize/repair/change 四类，只对 change 告警），修本地快照。**平台无关，直接搬。**关键坑：GigaB2B 媒体 URL 带轮换签名（x-ct/x-cs），比较要按 path 不按全串；尺寸数值 0.1 容差。
- **live 审计**◆：规则层可自动修 + republish —— republish 机制是 eBay 专属（offer/inventory API），Wayfair 要换成 Wayfair 的内容更新 API。审计的**检测逻辑**（规则层+守卫+语义层）平台无关。

### 防线四：出单触发复核（防"发货后退款"，最后窗口）★◆
- 订单进来立即重抓源 + 比对 live 声明，CRITICAL 即告警——**理念平台无关**★
- eBay 专属◆：用 Trading `GetOrders` 拉订单；Wayfair 要换成 Wayfair 订单 API。比对逻辑本身可复用。

---

## 2. 源事实驱动自动改写管线（存量债务清偿）★★◆

把"语义护栏报告的债务"升级成"自动改写闭环"：
- **生成零 LLM**★（最重要决策）：描述用店铺模板构建器 + 源 characteristics 逐字做卖点；aspects 走确定性修正表；标题只在含违规时用源标题重建。LLM 只做验证不做生成——**因为债务本就是 LLM 写手造成的，用 LLM 修等于重新掷骰子**。
- 三层验证全过才推、postfix 连续两次 0 CRITICAL 才算收敛、推送前快照可回滚——★平台无关。
- eBay 专属◆：`_put_inventory_product_only`→`update_offer_category`→`publish_offer` 的推送三段式；Wayfair 换成对应更新调用。
- **踩过的坑（Wayfair 也会遇到）**：① 改 Material aspect 不等于内容干净，描述层还会残留源不支持的材质词（foam 等）→ 需描述层也做源锚定；② 复合源材质（"Polyester,Rubber Wood"）不能自动拆，需人工审策展映射表；③ 平台必填字段被删会报错，改写不能盲删必填项。

---

## 3. 安全底座（★全部平台无关，务必照搬）

1. **双闸**：live 写入需 CLI flag + 环境变量二者齐备（`SEMANTIC_REWRITE_APPLY_ENABLED=1`），防误触发。
2. **金丝雀 + 人工放行**：大规模改写前先跑 15 条，人工看 before/after 报告放行，再铺量。
3. **推送前快照 + 分阶段自动回滚**：任一推送阶段失败即用 backup 恢复。
4. **原记录原地更新**：eBay 是"原 offer 原 listing_id 禁 end+relist"；Wayfair 对应"原 listing 原地改，不删旧建新"——理念一致。
5. **策展表人工审定**：批量映射（如复合材质）必须人工审表后才生效，`enabled` 顶层闸默认关。

---

## 4. 数据面（★平台无关表结构可照搬）

| 表 | 作用 |
|----|------|
| `source_drift_log` | 源漂移审计轨迹 |
| `order_recheck_log` | 出单复核记录（去重） |
| `fact_sheet_cache` | 事实表缓存（内容 hash 键） |
| `semantic_rewrite_backups` | 改写前快照（回滚依据） |
| `人工队列 human_queue` | 不敢自动处理的留人工 |

---

## 5. Wayfair 采用建议顺序

1. **先搬防线二（语义事实表护栏）**——最通用、独立、立竿见影，`listing_fact_sheet.py` 几乎零改动
2. **再搬防线三的源刷新**——把"源事实基准"救活，其余防线才有比对对象
3. **搬安全底座**——双闸/金丝雀/回滚是所有 live 写入的前提
4. **最后做平台适配层**——把 eBay 的 offer/inventory/Trading 调用换成 Wayfair API，其余逻辑复用
5. Wayfair 与 eBay 的关键差异要单独调研：类目/属性体系（Wayfair 用自己的 taxonomy）、内容更新 API、订单 API、图片/视频托管方式。检测与安全逻辑不变，只换这层"平台驱动"。

---

## 6. 一句话总结

**搬"检测逻辑 + 安全模型 + 源刷新"（平台无关的 80%），换"平台 API 适配层"（eBay 专属的 20%）。** 核心资产是那套"源为唯一事实、LLM 只验不写、双闸金丝雀回滚"的方法论，不是任何一段 eBay API 代码。
