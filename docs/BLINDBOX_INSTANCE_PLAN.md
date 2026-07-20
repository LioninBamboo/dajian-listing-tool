# 盲盒子账号(实例C)细化方案书

> 2026-07-20 起草,基于两项调研:①仓库现有能力盘点 ②`ebay-listing-pro`(用户桌面应用,React+Gemini)的样式与规则逆向。
> 上位文档:[SUBACCOUNT_MULTI_INSTANCE_PLAN.md](SUBACCOUNT_MULTI_INSTANCE_PLAN.md)(多实例架构不变,本文只写盲盒实例的差异化设计)。

---

## 0. 盲盒实例与家具/汽配的四个本质差异

| 维度 | 家具(主账号)/汽配(实例B) | 盲盒(实例C) |
|---|---|---|
| 发货 | 美仓(merchant location = 美国仓库) | **中国直发 SpeedPAK**(location=中国;5 工作日处理 + 8-12 日达) |
| 采集源 | 大建云仓 / viomall API | **eBay 链接采集**(图片 + item specifics) |
| 刊登形态 | 单 SKU 固定价 | **多变体**(款式/端盒/整套)+ 可选拍卖 |
| 合规重心 | 材质/尺寸事实一致性 | **IP 禁词**(POP MART 是 eBay 受限词 + VeRO 活跃品牌) |

前两条只是配置差异;后两条是**新能力开发**,是盲盒实例的主要工作量。

---

## 1. eBay 链接采集(图片 + Item Specifics)

### 1.1 技术路线:Browse API,零爬虫

仓库已在用 Browse API 详情端点([qwen_optimizer.py:224](../qwen_optimizer.py) 竞品分析即调 `GET /buy/browse/v1/item/{item_id}`,App Token 即可,无需卖家授权)。采集链路:

```
用户粘贴/点击 eBay 链接
  → 解析 itemId:https://www.ebay.com/itm/<slug>/256123456789 或 /itm/256123456789
  → Browse API GET /buy/browse/v1/item/v1|256123456789|0
  → 提取:title, price, image.imageUrl + additionalImages[].imageUrl(高清原图),
          localizedAspects[](即 item specifics 名值对), categoryPath/categoryId,
          condition, itemLocation, brand, epid
  → 写入 CollectedProduct(images=图片URL数组, attributes=aspects, url=源链接)
```

### 1.2 交互形态(二选一或并存)

- **A. 扩展按钮**(与现有大建采集器同型):extension 增加对 ebay.com/itm/* 页面的"采集"按钮,POST 到实例C的 `/api/collect-ebay`(新端点,复用 CollectedProduct 入库逻辑)。
- **B. 粘贴链接**(实现最快):Streamlit 页面/API 加一个输入框,粘贴 URL 即采集。**建议先做 B**,扩展按钮 M 后期再补。

### 1.3 必须正视的合规风险 ⚠️

- 他人 listing 的**图片受版权保护**,POP MART 相关内容是 VeRO(权利人投诉)高发区;直接搬图搬文案有下架/封号风险。
- 方案定位:eBay 链接采集用于**快速起草**(规格、变体结构、类目、关键词);图片优先用**自有实拍/供应商授权图**,采集图作占位在金丝雀阶段人工替换。这条写进流程,不做成默认直发。
- `ebay-listing-pro` 的禁词规则全盘继承(见 §2.3)。

### 1.4 需要的开发

| 项 | 说明 | 量级 |
|---|---|---|
| `src/clients/ebay_browse_collector.py` | URL→itemId 解析 + getItem + 字段映射(把 qwen_optimizer 里的临时调用抽成正式客户端) | 小 |
| `/api/collect-ebay` 端点 + 页面输入框 | 复用现有入库/状态机(PENDING→READY→PUBLISHED) | 小 |
| 图片转存决策 | eBay 图 URL(i.ebayimg.com)可直接用于刊登,但为脱离源 listing 依赖,建议下载后走 EPS 重传(real_ebay_client 已有 EPS 上传链路) | 小 |

---

## 2. 标题/描述生成:移植 ebay-listing-pro 的样式与规则

### 2.1 从该应用逆向出的核心资产

**视觉模板("潮玩 Hypebeast"风,全内联 CSS,eBay 兼容)**:
- 头部横幅:黑底 + 荧光酸绿(`#ccff00`)标题,Arial Black,斜体大写,信箱圆角
- 正文容器:Trebuchet MS,浅灰底(`#f4f4f4`)
- 卖点卡片:Flexbox 卡组,白底黑粗边框 + `box-shadow: 4px 4px 0 #000` 硬阴影,emoji 图标(🔥⚡💥💎)
- 规格表:Courier 等宽,黑实线边框,高对比
- 包装内容块:品红(`#ff0055`)粗边框居中块
- **静态 SHIPPING & LOGISTICS 卡片**(系统追加,不让 LLM 生成):SpeedPAK 中国直发、5 工作日处理、8-12 日达、售后承诺——这就是盲盒实例的"footer",对应 store_profile 的 footer 概念

**生成规则**:
- 禁词(硬规则,输出后校验而非只靠提示词):`POP MART`(标题/描述/aspects 全禁)、`Original/Genuine/Authentic/Licensed/Official/OEM`;替代词:Designer Toy / Blind Box Figure / Collectible Art Toy / 角色名(Labubu、Molly、Nyota…)
- 标题 ≤80 字符,SEO 关键词前置(该应用靠 Google Search 抓热词;我们用现有 Browse API 关键词分析平替)
- 中文对照字段(titleCN/contentCN)供运营审核
- 多变体检测:自动列出款式/套装变体,含中文名、单价、SKU、毛利率
- 定价:成本价 → ~30% 毛利建议价;运费公式 `CNY = 重量kg × 136 + 23 + 10.6`(SpeedPAK 标准)
- 合规自检:SpeedPAK 禁运品(纯电池/液体/粉末等)

### 2.2 与现有 ERP 的整合方式

现有 qwen_optimizer 是"家具文案专家"人设 + AQUAVERVE 模板。盲盒实例需要**按实例切换提示词模板**,这是 M1 配置层的自然延伸:

```
store_profile 新增字段(v2):
  template_style: "furniture_classic" | "arttoy_hype"   # 描述模板风格
  banned_terms: ["POP MART", "Original", "Genuine", ...]  # 硬校验禁词表
  footer_html: |                                          # 整块 footer(替代 line1/line2)
    <div style="...">SHIPPING & LOGISTICS ...</div>
  shipping_formula: {per_kg: 136, base: 23, surcharge: 10.6, currency: CNY}
  target_margin: 0.30
```

| 改造点 | 说明 |
|---|---|
| qwen_optimizer 提示词模板化 | 家具/潮玩两套 system prompt 文件(如 `config/prompts/<style>.md`),按 `template_style` 加载;M1 已把品牌注入参数化,这步是把整个模板抽出去 |
| 禁词硬校验 | 新 `src/utils/banned_terms_guard.py`:生成后扫标题/描述/aspects,命中即 FAIL(不靠 LLM 自觉);挂进 publish_validation 和质量门 |
| 质量门标记 | 盲盒模板的 banner/footer 标记不同(如 "shipping & logistics"),M1 的 `quality_gate` 字段直接支持 |
| 多变体刊登 | **最大新能力**:Inventory API `inventory_item_group`(变体组)+ 组内 SKU 定价——现有 real_ebay_client 只做单 SKU offer,需新增 create_group/publish_group 链路 |
| 拍卖形式 | 该应用支持 Auction 建议;现有链路只做 FIXED_PRICE,拍卖走 Trading AddItem,建议 **v1 先不做**,固定价+Best Offer 覆盖 |
| SpeedPAK 定价 | pricing_engine 增加"中国直发"成本模型(公式参数来自 store_profile),与现有美仓模型并存按实例选 |

### 2.3 明确不移植的部分

- Gemini + Google Search 实时热词:用现有 Browse API / Terapeak 关键词能力平替,不新增 Gemini 依赖(仓库标准 LLM 是 Qwen)。
- 前端 React 应用本体:只取其提示词资产与输出结构,功能并入现有 Streamlit/FastAPI。

---

## 3. 里程碑(盲盒线,独立于汽配 M 系列,编号 B)

| 阶段 | 内容 | 依赖 |
|---|---|---|
| B1 ✅ | store_profile v2 + 禁词硬校验守卫 + undercut 定价 —— **已完成 2026-07-20** | 无 |
| B2 | eBay 链接采集:browse_collector 客户端 + 粘贴链接入库(交互形态 B) | 无 |
| B3 | 潮玩描述模板 + qwen 提示词模板化 + 中文对照字段 | B1 |
| B4 | 盲盒子账号开店五步(复用 M2 手册)+ 实例C部署(端口 8002) | 子账号就绪 |
| B5 | 多变体刊登(inventory_item_group)+ SpeedPAK 定价模型 | B4 |
| B6 | 金丝雀:5-10 条真实盲盒 listing 全链路(采集→生成→人工审图/审禁词→刊登) | B2+B3+B5 |
| B7 | (可选)扩展按钮采集、拍卖形式 | B6 后按需 |

## 3.1 B1 实施记录(2026-07-20)

- **store_profile v2**([store_profile.py](../src/utils/store_profile.py)):新增 `listing`(template_style / footer_html / banned_terms)和 `pricing`(pricing_strategy / undercut_pct / undercut_min_abs / price_ends_99)两段。默认值 = 家具现状(furniture_classic / cost_plus / 空禁词),现有实例零影响。`_coerce` 扩展支持 list/float/bool。
- **禁词硬校验守卫**([banned_terms_guard.py](../src/utils/banned_terms_guard.py)):`scan_listing` / `assert_clean` 扫 title/description(先 strip HTML)/aspects(键+值);整词匹配(`(?<!\w)…(?!\w)`,OEM 不误伤 OEMs/GOEM),多词跨空白(POP MART 匹配 POP⎵⎵MART),大小写不敏感。禁词表按实例配,默认空=不触发。
- **undercut 定价 + 竞品分析**([undercut_pricing.py](../src/services/undercut_pricing.py)):`recommend_undercut_price(source_price)` 采集价按 max(pct, 绝对额) 下调,严格 ≤ source;竞品 `price_stats`(复用 Browse API,`fetch_competitor_stats` 解耦自 qwen_optimizer)作区间 sanity——source 高于市场中位数时给出低于中位的 `market_aware_price` 替代并打标 `source_above_market_median`,低于市场最低价打 `below_market_min`;`.99` 心理定价向下取整。竞品分析纯建议,绝不把推荐价抬到 source 之上。
- **配置样例**:[config/store_profile.blindbox.example.yaml](../config/store_profile.blindbox.example.yaml)(跟踪的样例,部署时复制为未跟踪的 `store_profile.local.yaml`);主库 `store_profile.yaml` 补了 v2 段注释。
- **测试**:test_store_profile(v2 段)、test_banned_terms_guard、test_undercut_pricing,共 46 用例绿。
- **未接线**:守卫与定价的模块已就绪,挂进采集/生成/刊登流程在 B2/B3/B6 做(B1 只交付能力与配置层)。

## 4. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| VeRO/IP:采集图与 POP MART 词汇导致下架封号 | **高** | 禁词硬校验 + 采集图仅作草稿、金丝雀人工换图;店铺起量前不碰热门 IP 整箱端盒 |
| 多变体链路 bug 波及定价(变体价错乱) | 中 | B5 单独金丝雀;RepricingGuard 对变体组扩展前不开自动调价 |
| 中国直发的物流时效差评 | 中 | 描述里显著承诺 8-12 日(模板已含);handling time 按 5 工作日设置 |
| Browse API 对部分 listing 返回不全(变体母体无单价等) | 低 | 采集时标记缺口字段,进 READY 前人工补 |
