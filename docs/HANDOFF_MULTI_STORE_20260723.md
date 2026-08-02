# 多店铺系统交接报告（2026-07-23）

> 面向接手的 AI / 工程师。本文自成一体：读完即可接手，无需翻聊天记录。
> 分支 `codex/live-audit-semantic-guard`，全部已 push 到 `origin`（GitHub `LioninBamboo/dajian-listing-tool`）。

---

## 0. 一句话现状

**一套代码、三个 eBay 店铺**（家具主店 + 盲盒 + 汽配），靠 `store_profile` 按实例分流。盲盒、汽配两个子店已完成授权/政策/库位/金丝雀刊登。**当前两个待攻坚点：**①汽配要进主流 Motors 类目**必须走 Trading API**（已实证可行，待工程化）；②盲盒首条 listing 被 eBay 判仿冒下架，**换自有图是复工前提**。

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
| **品类路由** | 采集时判定 auto/furniture/arttoy/unknown（含"假朋友"防误判）+ 搬运工具 | `src/services/product_router.py`、`tools/route_products.py` |
| **杂项修复** | `replace_description_measurements` 漏 import（主店描述尺寸归一化一直静默失效）；token 刷新窗口 5→30min（时钟偏差） | `qwen_optimizer.py`、`src/services/ebay_auth.py` |

对应测试：`tests/test_banned_terms_guard.py`、`test_undercut_pricing.py`、`test_ebay_browse_collector.py`、`test_arttoy_listing.py`、`test_store_profile.py`、`test_variation_publisher.py`、`test_product_router.py`。全量约 **2057 用例通过**。

---

## 4. 关键技术发现 / 血泪 gotchas（务必先读）

1. **汽配类目只在 eBay Motors 树（100），EBAY_US 树（0）没有**。`get_default_category_tree_id`：EBAY_US=0、EBAY_MOTORS_US=100。33653/33651 在树0 查 400。
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

## 5. 未完成 / 下一步（按优先级）

### 🔴 P0-A：Trading API 汽配刊登通道（汽配真正跑起来的前提，已实证可行）
把已验证成功的 `AddFixedPriceItem`+`ItemCompatibilityList` 封装进管线：
1. `real_ebay_client` 加 `add_fixed_price_item_motors(product, compatibility, policies, location)`（封装 §4.4 那套 XML；见 §6 字段清单）。
2. `store_profile` 加 `listing_channel: inventory | trading`（汽配=trading）。
3. `batch_publish` 按 channel 分流（家具/盲盒走 Inventory，汽配走 Trading）。
4. 汽配用 Motors 退货政策 `262619354013`（回填 AquaRides 的 return policy）。
5. Motros 类目重映射——**这次逐个实发验证**（拖车钩 33653、车顶架 33651、货筐 121984 或 262220、踏板 33650、尾门 33647/262150…；先查 taxonomy 建议再实发）。
6. fitment 数据清洗（去无效车型组合，如 1998 Mazda B2300 这类只是 Warning，但应过滤）。
7. Motros 类目的必填 aspect 覆盖（如车顶架要 `Capacity`，值须来自源数据不得编造）。

### 🔴 P0-B：盲盒自有图管线（GrovePop 复工前提）
- 采集图仅作草稿，**发布前必须换自有/授权图**（EPS 转存或本地实拍）。现 `real_ebay_client._prepare_inventory_image_urls` 对 eBay 自托管 URL 直接复用——正是被判仿冒的原因。
- 需要：图片转存到自有图床/EPS 的流程 + 发布前门禁（无自有图不许发）。

### 🟡 P1
- **viomall 采集渠道**（汽配第二货源，代码库零覆盖，从头做）。
- **品类路由接线深化**：`product_router` 已在 `/api/collect` 记录判定（advisory），`tools/route_products.py` 可搬运；但"采集入口自动写入对应实例库"尚未全自动。
- **盲盒变体维度**：现用 "Style 1..N" 通用值，非角色名（源数据未干净映射）。
- **GrovePop 广告**：`sell.marketing` 待账号有资格后补授。

### 🟢 P2 / 观察项
- AquaRides 是否需要 Motors 车型兼容性做成采集必抓字段（取决于货源品类）。
- 主店那批历史汽配（484 DELISTED/391 ENDED）是否借 Trading 通道 + 正确类目重新上架（还是并入 AquaRides）。

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

## 8. 给接手 AI 的工作准则（血的教训）

1. **涉及"能不能刊登"的判断，一律实发验证，不空谈**。本会话在 Motors 上错了三次（说 API 不支持/要资格/账号问题），全因没实发就下结论。"taxonomy 类目有效"≠"能发进去"。
2. **改动主账号共享代码要极其克制**：默认值必须 = 主店现状，用 profile 分流给子店，别全局改。
3. **测试脚本操作真实 offer 前先查该 SKU 是否已有已发布 offer**（本会话一个探针脚本误删过金丝雀 offer）。用专用 scratch SKU 做探针，用完删干净。
4. **数据不得编造**：aspect 值（尺寸/承重/兼容车型）必须来自源数据。
5. 外向操作（真实上架）先获用户放行；子账号 OAuth / 身份/银行信息类只能用户本人做。
6. 运行记录见 memory：`memory/subaccount-multi-instance-plan.md`（本文的更细流水账）。
