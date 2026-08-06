# 多店铺系统交接报告（2026-07-23）

> 面向接手的 AI / 工程师。本文自成一体：读完即可接手，无需翻聊天记录。
> 分支 `codex/live-audit-semantic-guard`，全部已 push 到 `origin`（GitHub `LioninBamboo/dajian-listing-tool`）。

---

## 0. 一句话现状

**一套代码、三个 eBay 店铺**（家具主店 + 盲盒 + 汽配），靠 `store_profile` 按实例分流。盲盒、汽配两个子店已完成授权/政策/库位/金丝雀刊登。

> **🚩 战略调整（2026-07-23，业主决定）：美国海关趋严，暂时只做【美国本地仓】，放下中国直邮，等海关宽松再议。**
> 影响：**家具主店（美国仓）+ 汽配 AquaRides（美国仓）= 当前重心**；**盲盒 GrovePop（中国 SpeedPAK 直邮）暂停**——其代码/授权/政策全部保留可随时恢复，但不再是 P0。若扩品类，优先考虑同样走美国仓的（如工具）。

**当前主攻：汽配要进主流 Motors 类目——必须走 Trading API**（已实证可行，待工程化，见 §5 P0-A/P0-A2）。

---

## 1. 系统架构

- **单仓库多实例**：主仓库 `C:\Users\poonx\Dajian_Listing_Tool`。每个子店是一份 `git clone`，`origin` 指向主仓库（**只 pull 不开发**），靠未跟踪的 `config/store_profile.local.yaml` 覆盖出各自的品牌/政策/库位/模板/定价/端口。
- **配置加载**：`src/utils/store_profile.py`，优先级 `STORE_PROFILE_PATH` env > `store_profile.local.yaml` > `store_profile.yaml`（跟踪的默认=主账号现值）> 内置默认。**所有默认值 = 主账号现状**，缺文件/缺键零影响主店。
- **纪律**：只在主仓库开发 → commit → 子实例 `git pull`。子实例的 `.local.yaml`、`.env`、`*.db` 均未跟踪、各自独立。

### 三个实例对照

| | 主账号 AquaVerve | GrovePop（实例C） | AquaRides（实例B） |
|---|---|---|---|
| 目录 | `Dajian_Listing_Tool` | `GrovePop_Listing_Tool` | `AutoParts_Listing_Tool` |
| 品类 | 家具 | 盲盒潮玩 | 汽配 |
| 端口 | 8000 | 8002 | 8001 |
| marketplace | EBAY_US | EBAY_US | EBAY_US |
| category_tree_id | 0 | 0 | 0（暂，见 §5 Motors） |
| template_style | furniture_classic | **arttoy_hype** | furniture_classic |
| 定价 | cost_plus | **undercut**（总到手价） | cost_plus |
| force_house_brand | true | **false**（保留 IP/Unbranded 兜底） | true |
| 账号注册地 | 美国 | 香港 CBT | 香港 CBT |
| 发货 | 美国仓 | 中国 SpeedPAK | 美国仓（多仓代发，地址仅参考） |
| OAuth | 已授权 | 已授权（3 scope） | 已授权（3 scope） |

---

## 2. 账号 / 凭据 / 政策（实体清单）

### OAuth 机制（重要，别再走弯路）
- `.env` 的 `EBAY_REDIRECT_URI` 是 **RuName**（`xiaoting_pan-xiaoting-AquaVe-okccij`），不是 URL。
- client_id：`xiaoting-AquaVerv-PRD-947e7b8cc-97e0e078`（**三个店共用同一套开发者密钥**，一套密钥可授权任意多个卖家账号，无需各自申请 key）。
- RuName 背后的"授权接受 URL"是**云端 Streamlit app**（`https://dajian-listing-tool-...streamlit.app/`，用云服务器固定 IP）。
- **授权流程**：云端 Streamlit 点授权 → **用目标子账号登录 eBay** → 同意 → 页面去掉 `?code=` 重载 → 点"下载 Token 文件" → 传到该实例目录 → `EbayOAuthService('PRODUCTION')._save_token(json.load(...))` 导入。
- **可用 scope**：`sell.inventory` / `sell.account` / `sell.fulfillment`（3 个）。`commerce.taxonomy` 这套密钥**没为用户授权流开通**（用用户 token 调 taxonomy 必 403，改用 **App Token** `get_application_token()`）。`sell.marketing`（广告）新账号无资格，报 `invalid_scope`，待账号有资格后补授。

### 业务政策 / 库位（已建）

**GrovePop**（`store_profile.local.yaml` 已回填）：
- payment `264935681017` · return `264935682017`（30天/买家付退货费）· fulfillment `264935711017`（SpeedPAK Standard/免邮/处理5天/发 美+全欧(排除RU)+加+澳）
- location `GROVEPOP_CN_WAREHOUSE`（深圳）

**AquaRides**（`store_profile.local.yaml` 已回填）：
- payment `262397299013` · return `262397300013`（30天/买家付退货费）· fulfillment `262397301013`（免邮 ShippingMethodStandard/处理2天/只发美国本土，排除13区域）
- location `AQUARIDES_US_WAREHOUSE`（Los Angeles/CA/US，极简地址，多仓代发仅参考）
- **Motors 专用退货政策 `262619354013`**（30天/**卖家付退货费**/MONEY_BACK）——Trading 发 Motros P&A 必须用它，否则报"non-compliant domestic return policy"

### 金丝雀 listing（已验证）
- GrovePop `318615532421`：16 变体 Naruto/EAKI 盲盒——**已被 eBay 判仿冒(counterfeit)下架**（自动化系统判定，非 VeRO 投诉；被移除物品不能重新刊登）。
- AquaRides `188683585596`：车顶架，**ACTIVE**，树0 类目 `177849 Car & Truck Racks`（在 Sporting Goods>Cycling 下，非 Motors）。
- Trading Motors 测试 `188732319492`：拖车钩→Motors `33653`+77 车型适配，**发布成功后已 EndFixedPriceItem**（证明 Trading 通道可行）。

---

## 3. 已完成的功能模块（全部已 push）

盲盒里程碑用 B 编号，代码在主仓库：

| 模块 | 内容 | 关键文件 |
|---|---|---|
| **M1** | store_profile 配置外部化（品牌/政策/库位/端口去硬编码） | `src/utils/store_profile.py` |
| **B1** | 禁词硬校验守卫 + undercut 定价（**按总到手价=卖价+运费**）+ 竞品分析 | `src/utils/banned_terms_guard.py`、`src/services/undercut_pricing.py` |
| **B1.1** | undercut 改按 price+shipping；采集运费；shipping_model(free/fixed) | 同上 + `ebay_browse_collector.py` |
| **B2** | eBay 链接采集（parse/fetch/map，App Token，`/api/collect-ebay`） | `src/clients/ebay_browse_collector.py`、`server.py` |
| **B2.1** | 多变体整组采集（盲盒系列，`collect_group_from_url`） | 同上 |
| **B3** | 潮玩 Hypebeast 描述模板 + qwen 按 template_style 分流 + 中文对照 + 描述智能截断 | `src/services/arttoy_prompt.py`、`qwen_optimizer.py` |
| **B3.1** | force_house_brand（潮玩保留自有 IP，缺失兜底 Unbranded） | `store_profile.py`、`arttoy_prompt.py` |
| **禁词剥离** | finalize 确定性剥离禁词（不靠 LLM 自觉，降级不返回带禁词源） | `banned_terms_guard.py`（strip_banned_terms/clean_banned_aspects） |
| **B5** | 多变体发布（inventory_item_group，dry_run 预览） | `src/clients/real_ebay_client.py`、`src/services/variation_publisher.py` |
| **Motors 配置** | category_tree_id 与 marketplace 解耦；aspects 改 App Token | `store_profile.py`、`ebay_category_matcher.py`、`batch_publish.py` |
| **采集目标店选择器** | 扩展弹窗选目标店（家具8000/汽配8001/盲盒8002），chrome.storage 粘性保持；采集按钮常显并着色当前目标、toast 报实际入库店 | `extension/stores.js`、`popup.*`、`background.js`、`content.js` |
| **品类路由（安全网）** | 判定 auto/furniture/arttoy/unknown（含"假朋友"防误判）；**操作者的显式选择是权威**，路由只在与 `store_kind` 不符时告警 + 搬运工具 | `src/services/product_router.py`、`tools/route_products.py` |
| **杂项修复** | `replace_description_measurements` 漏 import（主店描述尺寸归一化一直静默失效）；token 刷新窗口 5→30min（时钟偏差） | `qwen_optimizer.py`、`src/services/ebay_auth.py` |

对应测试：`tests/test_banned_terms_guard.py`、`test_undercut_pricing.py`、`test_ebay_browse_collector.py`、`test_arttoy_listing.py`、`test_store_profile.py`、`test_variation_publisher.py`、`test_product_router.py`。全量约 **2057 用例通过**。

---

## 3.5 汽配的"站点"层（重要概念，先厘清）

eBay 把汽配归到独立站点 **eBay Motors US（SiteID = 100）**，它是"车辆与配件专站"，不是另一个国家——就是美国站，但和综合站 eBay.com US（SiteID = 0）是**两个站/两棵类目树**：

| | 综合站 eBay.com US | **eBay Motors US** |
|---|---|---|
| SiteID | 0 | **100** |
| 类目树 | 0 | **100** |
| Marketplace（Sell API） | EBAY_US | EBAY_US（**没有可用的 EBAY_MOTORS_US marketplace**，见 gotcha 2） |
| 汽配类目在这 | ❌（只有边缘的 Sporting Goods>Cycling 里几个） | ✅ 拖车钩33653/车顶架33651/… |

**结论：真·汽配 listing 的站点必须是 eBay Motors（SiteID 100），只能用 Trading API 指定（`X-EBAY-API-SITEID: 100`）。** Inventory API 没有"站点"概念、只有 marketplaceId，进不了 Motors。

⚠️ **当前 AquaRides 金丝雀 `188683585596` 不在 Motors** —— 它用的 `177849 Car & Truck Racks` 其实挂在综合站的 `Sporting Goods > Cycling` 下（GetItem 实证）。这只是"Inventory 通道下能凑合发的位置",**买拖车钩的人不会去自行车类目搜**。要进买家真正逛的汽配类目，走 §5 P0-A 的 Trading + SiteID 100。

---

## 4. 关键技术发现 / 血泪 gotchas（务必先读）

1. **汽配类目只在 eBay Motors 站/树（SiteID 100 / 树 100），综合站 EBAY_US（树 0）没有**。`get_default_category_tree_id`：EBAY_US=0、EBAY_MOTORS_US=100。33653/33651 在树0 查 400。见 §3.5。
2. **`marketplace_id` ≠ `category_tree_id`**：Inventory offer 的 marketplaceId **必须 EBAY_US**（`EBAY_MOTORS_US` 报 errorId 2004 "Could not serialize field [marketplaceId]"）；Motors 是"目录"不是"市场"。
3. **🔴Inventory API 发不了 Motors 类目**：offer 建得成，publish 报 **errorId 25005 "invalid category"**，即便带完整 fitment 也一样。主账号、AquaRides 都拒。3 月还能发（老 listing 262216/174020 为证），eBay 之后收紧了。
4. **✅Trading API 能发 Motors**：`AddFixedPriceItem`（`X-EBAY-API-SITEID: 100`）接受 Motros 类目 + `ItemCompatibilityList`。**卡点是退货政策**：eBay Motors 配件(P&A)硬性要求**卖家承担退货运费**。
5. **关键词类目表"名字对、号码错"**：`qwen_optimizer` 和 `ebay_category_matcher` 里的汽配映射，注释是 Motors 类目名但 ID 在树100 里其实是别的（262216=Anchors、174020=Brake Pad Wear Sensors…）。**历史上汽配"数据不好被下架"的真正根因**。⚠️曾做过一轮重映射又**全部 revert**（因当时误判成"API 不支持 Motors"）；重做时**必须逐个实发验证**（"taxonomy 有效"≠"能发进去"）。
6. **盲盒仿冒下架**：GrovePop 首条被自动判 counterfeit。触发画像=新账号+强 IP+低价+**直接用源卖家图**。**换自有实拍图从"版权礼貌"升级为"防误判假货"的刚需**。禁词守卫只拦通用宣称词（POP MART/Authentic），拦不住任意 IP 名（Naruto/Universal Monsters）。
7. **token 时钟偏差**：本地时钟偏慢会让 `get_valid_token` 误判 token 未过期不刷新→eBay 报 401 "Invalid access token"。已把刷新窗口从 5min 放宽到 30min；仍可能残留，遇 401 手动 `o.refresh_access_token()`。
8. **eBay 自家拼写 typo**：SpeedPAK 服务代码是 `US_StandardSppedPAK` / `US_IntlStandardSppedPAK`（"Sppedpak"，别拼对）。
9. **政策 PUT 需带 `globalShipping`/`pickupDropOff`/`freightShipping` 顶层字段**（POST 自动补，PUT 不补会 400）。
10. **EBAY_US 国际目的地**多数国家只能用**大区名**（如 `Europe`），仅 CA/AU/GB/DE/FR 可单列国家码。
11. **handlingTime.unit 用 `DAY`**（非 BUSINESS_DAY）。
12. **政策/库位在 Inventory 与 Trading、EBAY_US 与 EBAY_MOTORS_US 之间共用同一套 ID**（不用为 Motors 重建，退货政策除外——见 gotcha 4）。

---

## 5. 未完成 / 下一步（按优先级，2026-07-23 按"只做美国仓"战略重排）

> **排序原则**：①美国仓的活优先（中国直邮线整体后置）；②能让**已授权好的店真正出单**的活优先于新建店；③新建店优先选美国仓品类。
>
> | 顺位 | 事项 | 为什么在这个位置 |
> |---|---|---|
> | **P0-A / A2** | 汽配 Trading 通道 + 汽配描述模板 | AquaRides 账号/政策/库位/金丝雀全就绪，**只差这一步就能进主流汽配类目出单**；美国仓，符合战略 |
> | **P1-a** | 工具店评估与决策 | 树0 + Inventory API，**是最省事的扩张**（不碰 Motors/Trading/fitment）；美国仓 |
> | **P1-b** | viomall 采集渠道 | 汽配第二货源，喂饱 P0-A 打通的通道 |
> | **P1-c** | 质检/营销上子店 | 子店有量之后才需要；且需先做质检按品类分流 |
> | **⏸ 暂停** | 盲盒全部（换图/变体维度/广告） | 中国直邮线，战略暂停 |

### 🔴 P0-A：Trading API 汽配刊登通道（汽配真正跑起来的前提，已实证可行）
把已验证成功的 `AddFixedPriceItem`+`ItemCompatibilityList` 封装进管线：
1. `real_ebay_client` 加 `add_fixed_price_item_motors(...)`（封装 §6 那套 XML，**`X-EBAY-API-SITEID: 100` = eBay Motors 站**；见 §3.5）。
2. `store_profile` 加 `listing_channel: inventory | trading`（汽配=trading）+ `ebay_site_id`（汽配=100/Motors，其余=0）。汽配实例配置改：`listing_channel: trading`、`ebay_site_id: "100"`、`category_tree_id: "100"`（现为 0 的临时凑合值）。
3. `batch_publish` 按 channel 分流（家具/盲盒走 Inventory，汽配走 Trading）。
4. 汽配用 Motors 退货政策 `262619354013`（回填 AquaRides 的 return policy）。
5. Motros 类目重映射——**这次逐个实发验证**（拖车钩 33653、车顶架 33651、货筐 121984 或 262220、踏板 33650、尾门 33647/262150…；先查 taxonomy 建议再实发）。
6. fitment 数据清洗（去无效车型组合，如 1998 Mazda B2300 这类只是 Warning，但应过滤）。
7. Motros 类目的必填 aspect 覆盖（如车顶架要 `Capacity`，值须来自源数据不得编造）。

### 🔴 P0-A2：汽配描述模板（配合 P0-A；现用家具模板，是凑合值）
⚠️ AquaRides 现在 `template_style: furniture_classic` —— 生成的描述是**家具味**（KEY FEATURES 罗列、生活方式调性，无车型适配段/规格表/安装说明）。汽配/工具买家决策逻辑完全不同：**适配件看"装不装得上我的车"，工具看参数**。要新设计，**沿用 B3 的 `template_style` 分流模式（和 arttoy 一模一样）**：
1. `template_style` 加值 `auto_technical`，AquaRides 改用它。
2. 新建 `src/services/auto_prompt.py`（与 `arttoy_prompt.py` 平级）：`build_auto_system_prompt` + `finalize_auto_listing`。**HTML 描述含两种内容模式**（由"是否有 fitment/compatibility 数据"决定，采集数据/product_router 已知）：
   - **模式 A 适配件**（拖车钩/踏板/大灯…）：①顶部醒目【✓ FITS THESE VEHICLES 车型适配】②规格表(Type/Material/尺寸/承重/MPN，含 eBay 必填 aspect) ③安装说明/难度/所需工具 ④含什么 ⑤保修/退货。调性：功能、参数、可信，**不要生活方式抒情/hype**。
   - **模式 B 工具/通用件**（千斤顶/胎压泵/脚垫/车衣…）：①主打参数/能力 ②用途场景+卖点 ③含什么+安全 ④保修。
3. `qwen_optimizer.optimize_product_full` 顶部分流加一支 `template_style == "auto_technical" → optimize_auto_listing`（现已支持 arttoy 分流，照抄）。
4. 车型适配数据 `motorsCompatibility.compatibleProducts` 本就在——描述里渲染成醒目适配段，同时进 Trading 的 `ItemCompatibilityList`（P0-A 第5点）。**把适配放描述最前也是降退货手段**（汽配第一退货因=装错/不适配）。
5. 家具模板一字不动（受 §7.5 主店契约保护）。

### 🟡 P1-a：工具店评估与决策（美国仓扩张的首选，**决策题不是技术题**）
工具落哪棵树决定复杂度——实测（taxonomy 建议）：

| 品类 | 树0（eBay.com，Inventory API） | 树100（Motors，Trading API） | 归属建议 |
|---|---|---|---|
| 电钻/扳手套装（通用工具） | 184655 Cordless Drills / 84237 Socket Wrenches ✅精准 | 35625 Other Auto Hand Tools（凑合） | **树0**，与汽车无关 |
| 汽车千斤顶 | 43593 Jacks & Stands ✅ | 179511 Jacks & Jack Stands ✅ | 两边都行，树0 更省事 |
| 胎压泵 | 30506 Air Compressors ✅ | 262093 **A/C Compressors（错）** | **树0** |
| OBD 诊断仪 | 175837 Other Consumer Electronics（泛） | 179476 Code Readers（精准） | Motors 更准但要 Trading |

**关键判断：是否单开店 = 三条正交的轴，只有一条真需要独立"店"**
1. **账号/品牌轴（唯一需要开店的理由，商业决策）**：想要独立品牌定位（"高性能汽配"店卖通用电钻会违和）／隔离账号风险与表现。
2. **通道/站点轴（配置即可，不必开店）**：Motors 精准类目走 Trading/SiteID 100，树0 类目走 Inventory——**同一账号可两通道并行**（P0-A 的 `listing_channel` 按商品分流）。
3. **内容模板轴（配置即可）**：工具 = P0-A2 的**模式 B**（主打参数，无 fitment），或单起 `tools_technical`。

**建议默认不单开店**（工具作为汽配店里的"通道+模板变体"）；若为品牌考虑要开，**它是最省事的子店**：树0 + Inventory，不碰 Motors/Trading/fitment。开店清单 = 脚手架实例 D（同盲盒/汽配流程）+ 授权 + 政策 + 库位 + `store_kind: tools` + 扩展 `stores.js` 加一项（端口 8003）。

⚠️ **边界毛刺**：汽车工具（千斤顶/OBD/胎压泵）在"汽车"与"工具"之间摇摆——**建议按 eBay 类目落点定归属**（能进 Motors 精准类目→汽配店走 Trading；只在树0 有好类目→工具模式）。另注意胎压泵在树100 被误判成 A/C 压缩机，**再次印证：类目必须实发验证，别信 taxonomy 建议第一条**。

### 🟡 P1-b：viomall 采集渠道（汽配第二货源，喂饱 P0-A 打通的通道）
代码库零覆盖，从头做。评估时先确认：是否美国仓、是否提供 fitment 车型数据（决定走模式 A 还是 B）。

### 🟡 P1-c：质检/营销上子店
按 §5.5 的复用架构（引擎复用 + `qc_profile` 分流 + `cro_tenant_config` 加租户），**不要重写 CRO**。前置：质检按品类分流（家具规则会误伤子店）。子店有量之后再做。

### 🟢 P2 / 观察项
- AquaRides 是否需要 Motors 车型兼容性做成采集必抓字段（取决于货源品类）。
- 主店那批历史汽配（484 DELISTED/391 ENDED）是否借 Trading 通道 + 正确类目重新上架（还是并入 AquaRides）。
- 采集扩展的利润计算器与服务端 `PricingEngine` 参数不一致（弹窗 13% 费率/30% 默认毛利 vs 引擎 13.25%+0.83% 支付费、管线实际 15%）——弹窗只是采购决策草稿纸、不影响刊登价，但会让判断偏乐观（约低估 1.1% 费率）。可对齐常量或接真引擎。
- `Cost Cost (USD)` 标签笔误，应为 `Product Cost`。

---

### ⏸ 已暂停：盲盒 GrovePop 全线（中国直邮，待海关宽松再启）
> 因 §0 战略调整暂停。**代码、OAuth 授权、三条政策、深圳库位、采集/生成/多变体管线全部保留可直接恢复。** 恢复时按以下顺序：
1. **（原 P0-B，复工第一前提）自有图管线**：首条 listing 被 eBay 自动判仿冒下架，触发因是**直接用了源卖家的图**。现 `real_ebay_client._prepare_inventory_image_urls` 对 eBay 自托管 URL 直接复用——正是根因。需要：图片转存自有图床/EPS + **无自有图不许发**的门禁。
2. **盲盒变体维度**：现用 "Style 1..N" 通用值，非角色名（源数据未干净映射）。
3. **GrovePop 广告**：`sell.marketing` 待账号有资格后补授。

### ✅ 已解决（原 P1，记录以免重做）
- ~~品类路由接线深化~~ **已由采集选择器解决**：扩展弹窗选目标店（粘性，家具8000/汽配8001/盲盒8002），采集直接进对应实例；采集按钮常显并着色当前目标。`product_router` 保留为**安全网**（与实例 `store_kind` 不符时在 logs 告警），不再充当决策者。**设计原则：操作者按批次采集，显式意图优于标题猜测。**

---

## 5.5 质检 / 营销上子店：复用架构（骨架，别重写）

**核心结论：引擎复用，规则分流。** 现有的营销（CRO，`src/services/cro_*.py` 共 100+ 模块）和质检（`src/services/listing_qc*.py`、`semantic_rewrite.py`、`src/utils/listing_quality_gate.py`、`claim_diff_engine.py`、`llm_fact_checker.py`、`banned_terms_guard.py`）本就是一套代码、三店同跑。子店上这些功能**不是重写，是沿用 listing 已建好的 `template_style` 分流模式**。复用性分三档：

### ✅ 第一档：直接复用（与品类无关的机制）
CRO 绝大多数模块是纯机制、不含品类假设：转化漏斗/流量分析、bandit 实验、审批队列/RBAC、事件总线、快照存储、看板、告警 RCA、定价机制（`cro_offer_pricing`/`cro_pricing_psychology`）、折扣/促销 API（`ebay_discount_service` **已读 store_profile**）、Terapeak 调研、图片质量评分、退货 NLP。换个店的库/token 直接用。

### 🟡 第二档：复用 + 填配置（接缝已存在）
- **`src/services/cro_tenant_config.py` 已是多租户配置层**（`configs/cro_tenants.json`，`_default` + 各租户覆盖，缺则用 default）。给汽配/盲盒各加一个租户条目（阈值/预算/投放策略）即可。
- `store_profile`（品牌/市场/政策，已实例化）、`banned_terms`（盲盒已用）。工作量 = 加配置行。

### 🔴 第三档：按品类加规则分支（唯一实打实的开发；误伤全在这）
**质检的语义守卫规则是家具专属**（`listing_quality_gate.py` 全是 Assembly Status / 尺寸 / 床垫）。直接跑到子店会误伤，命门也不同：
| 店 | 家具规则误伤点 | 该店真正的质检命门 |
|---|---|---|
| 汽配 | 拖车钩没有"组装状态"，尺寸规则误判 | **车型适配(fitment)正确性** |
| 盲盒 | 无尺寸/组装 | **IP/仿冒/真伪**（GrovePop 被判仿冒即例证） |

**做法 = 沿用 `template_style` 分流的同一套模式**：`store_profile` 加 `qc_profile: furniture | motors | arttoy`（默认 furniture，受 §7.5 主店契约测试保护）；质检引擎按它选规则模块；家具规则**一字不动**，汽配 fitment 校验 / 盲盒 IP 校验做成**新模块**。

⚠️ **现状风险 & 前置**：家具质检若现在直接跑到子店会误伤，所以子实例**暂不启动 `scheduler_daemon` / 语义改写**（见 §8）。**质检的按品类分流是子店安全开跑质检/营销的前置条件。**

### 复用率估计
管道/机制 ~80% 直接用 · 配置 ~15% 填条目 · 品类规则 ~5%（但关键，误伤全在这）。**不要去重写那 100 个 CRO 模块。**

```
              一套引擎（CRO 100+ 模块 / QC 引擎 / 队列 / 审批 / 看板）
                                 │
   ┌──────────────────────────────┼──────────────────────────────┐
家具主店 qc:furniture        盲盒 qc:arttoy(IP/真伪)        汽配 qc:motors(fitment)
cro租户:_default             cro租户:grovepop               cro租户:aquarides
（现状全焊在这，受契约保护）  （待建规则模块）               （待建规则模块）
```

---

## 6. 参考：Trading `AddFixedPriceItem` 发 Motors 所需字段（已验证）

Header：`X-EBAY-API-CALL-NAME: AddFixedPriceItem`、`X-EBAY-API-SITEID: 100`、`X-EBAY-API-COMPATIBILITY-LEVEL: 1155`、`X-EBAY-API-IAF-TOKEN: <user token>`、`Content-Type: text/xml`。

Item 必填：`Title`、`Description`(CDATA)、`PrimaryCategory/CategoryID`(Motors id)、`StartPrice`(currencyID)、`Quantity`、`ListingDuration=GTC`、`ListingType=FixedPriceItem`、`Currency=USD`、`Country=US`、`Location`、`PostalCode`、`ConditionID=1000`、`PictureDetails/PictureURL`、`SellerProfiles`(引用政策 ID：ShippingProfileID/ReturnProfileID/PaymentProfileID)、`ItemSpecifics`(NameValueList)、`ItemCompatibilityList`(每条 `<Compatibility><NameValueList><Name>Year|Make|Model|Trim|Engine</Name><Value>...</Value></NameValueList>...</Compatibility>`)。

车型适配数据来源：采集产品的 `optimization.motorsCompatibility.compatibleProducts`（结构 `compatibilityProperties:[{name,value}]`）。样例：主库 SKU `W3611P453305` 有 77 条。

参考脚本（本次验证用，非生产）：`scratchpad/test_trading_motors.py`（已随会话结束清理，逻辑见本文）。

---

## 7. 环境 / 命令速查

- venv：各实例目录 `.\.venv\Scripts\python.exe`。**pyyaml 必须在**（`pip show pyyaml`；早期漏装导致 profile 静默回退主账号）。
- 优化：`python batch_analyze.py`（处理 COLLECTED→READY）。
- 发布：`python batch_publish.py --sku <SKU> [--dry-run]`。
- 路由报告：`python tools/route_products.py --report`。
- 生成授权链接（3 scope）：主实例跑 `EbayOAuthService('PRODUCTION')` + 手动设 `o.scopes=[inventory,account,fulfillment]` + `get_authorization_url()`。
- 全量测试：`PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/ -q`（约 2057 通过，含 1 条 Windows 文件锁偶发）。

---

## 7.5 主店隔离契约（开发汽配 Trading 通道的强制前置约束）

**问题：汽配 Trading 通道会动到三店共享的 `real_ebay_client` / `batch_publish` / `store_profile`，如何保证不影响家具主店？** 靠四层隔离 + 一个机器守卫，不靠自觉：

1. **配置分流，默认 = 主店**：每个新能力挂 `store_profile` 开关，默认值 = 主店现行为。主店 profile 不写该键 → 走默认 → **从构造上进不了新代码**。（已有：force_house_brand、category_tree_id、shipping_model、template_style 全默认主店值。）
2. **代码路径门控**：Trading 通道是**新增函数**（不改现有 `create_offer`/`publish_offer`）。`batch_publish` 加 `if listing_channel == "trading": <新路径> else: <现有 Inventory 路径，一字不改>`。主店走 else，字节级不变。
3. **运行时隔离**：三店各自目录/DB/token/scheduler，进程互不可及。
4. **pull 门控**：主店实例何时 `git pull` 可控——先在汽配实例验证 + 家具回归全绿，主店再 pull。

**机器守卫（已落地）：`tests/test_main_store_contract.py`**
- Layer A：`StoreProfile()` 零配置的每个行为字段必须 = 主店值（含 `listing_channel` 缺省=inventory、`ebay_site_id` 缺省=0、`category_tree_id`=0、非 Motors）。**任何新字段若默认值不是主店值，或改了现有默认，立即报红。**
- Layer B：家具 profile 下 `create_offer` 产出 marketplaceId=EBAY_US、主店库位、无 Motors/compatibility 泄漏；`create_inventory_item` payload 形状稳定。
- **视其失败为"你碰了主店"**，除非主店行为是**有意**变更。反证已确认：把 `category_tree_id` 默认改成 100 会立即触发 Layer A 失败。

**开发 P0-A 的硬规矩**：改共享代码后必须 `pytest tests/test_main_store_contract.py` 全绿 + 全量 `pytest tests/` 全绿；契约红了先停下查是否误伤主店。

## 8. 给接手 AI 的工作准则（血的教训）

1. **涉及"能不能刊登"的判断，一律实发验证，不空谈**。本会话在 Motors 上错了三次（说 API 不支持/要资格/账号问题），全因没实发就下结论。"taxonomy 类目有效"≠"能发进去"。
2. **改动主账号共享代码要极其克制**：默认值必须 = 主店现状，用 profile 分流给子店，别全局改。
3. **测试脚本操作真实 offer 前先查该 SKU 是否已有已发布 offer**（本会话一个探针脚本误删过金丝雀 offer）。用专用 scratch SKU 做探针，用完删干净。
4. **数据不得编造**：aspect 值（尺寸/承重/兼容车型）必须来自源数据。
5. 外向操作（真实上架）先获用户放行；子账号 OAuth / 身份/银行信息类只能用户本人做。
6. 运行记录见 memory：`memory/subaccount-multi-instance-plan.md`（本文的更细流水账）。
