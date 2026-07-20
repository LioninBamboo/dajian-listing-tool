# 子账号多实例扩展方案书 —— 汽配账号先行

> 起草日期:2026-07-20 | 状态:M1 已完成(§2.4);M2 进行中——实例B已脚手架,eBay 侧五步见 [M2_SUBACCOUNT_ONBOARDING_RUNBOOK.md](M2_SUBACCOUNT_ONBOARDING_RUNBOOK.md);盲盒实例细化方案见 [BLINDBOX_INSTANCE_PLAN.md](BLINDBOX_INSTANCE_PLAN.md)
> 范围:经理号主账号(家具,现行实例)之外,新增汽配子账号实例;盲盒子账号复用同一模式,本文只在架构层预留。

---

## 0. 背景与目标

- 现有 ERP 服务于**一个 eBay 账号**(家具类目,大建云仓/GigaCloud 货源,店铺品牌 AquaVerve)。
- 已获批两个子账号:①汽配类目 ②盲盒类目。子账号在 eBay 侧是**独立卖家账号**:独立 token、独立 business policies、独立店铺信息。
- 目标:汽配子账号从大建云仓或 viomall 采集,**只刊登到汽配账号**;店名/描述模板/政策独立;刊登与运营逻辑 100% 复用现有代码。

**核心结论(可行性调研已完成):采用"一账号一实例"多实例架构,不做代码内多租户。**
依据:
- 全部状态文件(`ebay_tokens.db`、`ebay_collection.db`、`logs/_scheduler.pid`、健康文件)均锚定项目根目录,第二份 checkout 天然隔离;
- `EbayOAuthService`/`RealEbayClient` 直接实例化点有 **109 处、72 个文件**,加 account_id 维度的多租户改造成本远超收益(账号只有 2~3 个);
- 同一套 eBay 开发者密钥(`EBAY_APP_ID/CERT_ID`)可为多个卖家账号分别 OAuth 授权,各实例持有各自 user token,无需新开发者账号。

---

## 1. 总体架构:多实例模式

```
C:\Users\poonx\
├── Dajian_Listing_Tool\          # 实例A:主账号(家具)——现状不动,仅吃 P0 配置化改造
│   ├── .env                      # 主账号 token/店铺配置
│   ├── config\store_profile.yaml # 主账号店铺画像(P0 新增)
│   ├── ebay_tokens.db / ebay_collection.db / logs\
│
├── AutoParts_Listing_Tool\       # 实例B:汽配子账号(同一 git 仓库的第二份 checkout)
│   ├── .env                      # 子账号 token、独立端口、独立通知邮箱
│   ├── config\store_profile.yaml # 汽配店铺画像(店名/模板/政策/位置)
│   ├── ebay_tokens.db / ebay_collection.db / logs\   # 天然隔离,零改造
│
└── (未来) BlindBox_Listing_Tool\ # 实例C:盲盒子账号,同模式
```

- **代码同步**:单一 git 仓库多份 checkout。纪律:只在主目录开发/提交,子账号目录只 `git pull`,禁止在子实例目录改代码。
- **数据隔离**:靠目录隔离,不共库、不加 account 字段。
- **进程隔离**:每实例自己的 `server.py`(不同端口)、`scheduler_daemon` + watchdog(PID 文件目录级,互不干扰;需实测 `process_identity` 的进程识别按路径区分)。

### 账号矩阵

| 实例 | 账号 | 类目 | 货源 | 品牌/模板 | 端口 |
|---|---|---|---|---|---|
| A(现行) | 主账号 | 家具家居 | 大建云仓/GigaCloud | AquaVerve | 8000 |
| B(本方案) | 汽配子账号 | Auto Parts / eBay Motors | 大建云仓 + viomall | 待定(新店名) | 8001 |
| C(预留) | 盲盒子账号 | 潮玩盲盒 | 待定 | 待定 | 8002 |

---

## 2. P0 —— 店铺配置外部化改造(唯一必须动代码的部分)

### 2.1 设计:新增 `store_profile` 配置层

新建 `src/config/store_profile.py`(加载器,带默认值=现主账号值,保证实例A零感知)+ 每实例一份 `config/store_profile.yaml`:

```yaml
store:
  brand_name: "AquaVerve"            # 店铺自有品牌(aspects Brand、banner、邮件标题)
  brand_tagline: "PREMIUM HOME FURNISHINGS"
  description_footer_line1: "✦ Ships from US Warehouse ✦"
  description_footer_line2: "Quality Guaranteed • Fast US Shipping • Trusted Seller"
  promotion_prefix: "AquaVerve Auto Sale"
ebay:
  merchant_location_key: "DAJIAN_LA_WAREHOUSE"
  fallback_fulfillment_policy_id: "321897899021"
  fallback_return_policy_id: "321896608021"
  fallback_payment_policy_id: "321896606021"
quality_gate:
  banner_marker: "aquaverve"         # 语义改写质量门用的描述标记
  footer_marker: "california"
server:
  port: 8000
```

### 2.2 逐文件改造清单

| # | 文件:行 | 现状硬编码 | 改造 |
|---|---|---|---|
| 1 | [real_ebay_client.py:39-43](../src/clients/real_ebay_client.py) | `FALLBACK_LISTING_POLICIES` 三个政策 ID 常量 | 改读 store_profile;政策 ID 是账号私有的,子账号用错会刊登失败或用错运费模板 |
| 2 | [real_ebay_client.py:782](../src/clients/real_ebay_client.py) / :1066 | `merchantLocationKey: "DAJIAN_LA_WAREHOUSE"` | 改读 store_profile;子账号需先建自己的 inventory location(见 §3) |
| 3 | [server.py:377](../server.py) | `BRAND_NAME = "AquaVerve"` | 改读 store_profile |
| 4 | [batch_publish.py:97](../batch_publish.py) | `BRAND_NAME = "AquaVerve"` | 同上 |
| 5 | [qwen_optimizer.py](../qwen_optimizer.py):478, 535, 648, 668, 680, 749, 811, 859-860, 1224 | LLM 提示词内嵌品牌名、AQUAVERVE banner HTML、footer、`Brand: ["AquaVerve"]` 强制值 | 提示词模板参数化(品牌名/tagline/footer 由 store_profile 注入) |
| 6 | [audit_fix_active_listings.py:2178-2205](../scripts/audit_fix_active_listings.py) | `build_structured_description_from_source` 内嵌 AQUAVERVE banner(:2181)、tagline(:2182)、footer(:2201-2202) | 模板片段由 store_profile 渲染;此函数同时被 [semantic_rewrite.py:934](../src/services/semantic_rewrite.py) 复用,改一处两链路受益 |
| 7 | [semantic_rewrite.py:1038-1039](../src/services/semantic_rewrite.py) | 质量门 `has_banner: "aquaverve" in desc`、`has_footer: "california" in desc` | 标记词改读 store_profile.quality_gate;否则子账号的改写全部过不了质量门 |
| 8 | [app.py](../app.py):613, 631, 677, 685, 690, 916 | 默认 aspects `{"Brand": ["AquaVerve"]}` | 改读 store_profile |
| 9 | [daily_tasks.py](../daily_tasks.py):1252, 1301 | 邮件标题"AquaVerve 每日任务汇总" | 改读 store_profile(每实例发各自邮件,`NOTIFICATION_EMAIL` 已在 .env) |
| 10 | [auto_rotate_promotions.py:38](../scripts/auto_rotate_promotions.py) | `PROMOTION_PREFIX = "AquaVerve Auto Sale"` | 改读 store_profile |
| 11 | [title_sanitizer.py:11](../src/utils/title_sanitizer.py) | `KEEP_BRAND_PREFIXES = ("aquaverve",)` | 改读 store_profile(保护自有品牌不被当供应商前缀剥掉) |
| 12 | [server.py:2299](../server.py) | `uvicorn.run(..., port=8000)` | 改读 `SERVER_PORT` 环境变量/ store_profile |
| 13 | [ebay_policy_manager.py](../src/services/ebay_policy_manager.py):30 等 5 处 | `sqlite3.connect("ebay_collection.db")` **相对 CWD 路径** | 顺手修:锚定项目根(同 ebay_auth 的做法);否则从其他工作目录跑脚本会在错误位置建库 |
| 14 | [extension/](../extension/) manifest.json:3,5、popup.html:56、content.js:3 | 扩展显示名含 AquaVerve | 可选,不影响链路;汽配实例可另打包一份扩展指向 8001 端口 |
| 15 | 测试:tests/test_real_ebay_client_offer_updates.py:52、tests/test_semantic_rewrite.py 等 | 断言中的 AquaVerve/位置常量 | 测试改为从 store_profile 默认值断言,保证两实例都可跑测试 |

**不需要改的(靠实例隔离自动解决)**:`ebay_tokens.db`(项目根相对)、`ebay_collection.db` 主链路(项目根相对)、`logs/` PID/健康文件、`.env` 全套凭证(`EBAY_REFRESH_TOKEN`、`DAJIAN_*`、`GIGACLOUD_*`、`NOTIFICATION_EMAIL`、`QWEN_API_KEY` 等)。

### 2.3 验收标准(P0)

- 实例A(主账号)在改造后:全量测试绿、一次真实刊登 + 一次 semantic rewrite 金丝雀,产出与改造前逐字节一致(模板默认值=现值)。
- 新实例仅凭 `.env` + `store_profile.yaml` 即可完成品牌/政策/位置/端口的全部差异化,`grep -ri aquaverve src/ scripts/ *.py` 在业务代码中为零残留(仅 config/测试 fixture 保留)。

---

### 2.4 M1 实施记录(2026-07-20)

- 新增 `src/utils/store_profile.py`(frozen dataclass + YAML 加载器,缺文件/坏文件回退主账号默认值,`STORE_PROFILE_PATH` 可覆盖路径)+ `config/store_profile.yaml`(主账号画像)+ `tests/test_store_profile.py`(11 用例)。
- 清单 §2.2 第 1-13、15 条全部落地;第 14 条(extension 显示名)按计划保留。额外收编三处漏网:`repair_broken_listings.py`(TEMPLATE_MARKERS 等 3 处功能性品牌标记)、`daily_terapeak_report.py`(报表品牌 4 处)、`ebay_discount_service.py`/`competition_monitor.py`/`daily_optimize.py` 的促销名与邮件头。
- 内部调用方硬编码 `http://localhost:8000` 一并外部化(scheduler_daemon、cro_delist、cro_relist_lifecycle、market_intelligence、competition_monitor → `store_profile.server_base_url`,`CRO_DELIST_BASE_URL` 环境变量仍可覆盖)。
- 验收:全量 pytest 1931 通过;唯一失败 `test_task_worker.py::test_windows_live_worker_handle_prevents_lock_deletion` 为交接遗留的环境性问题(taskkill 超时),与本改造无关。`grep -ri aquaverve` 业务代码零残留(仅 store_profile 默认值与测试 fixture)。
- 顺手修复:交接新增测试通过 `ebay_auth` import 时的模块级 `load_dotenv()` 把真实 `.env` 键泄漏进测试进程,导致 `test_scheduler_cro_title_rewrite` 的 dry-run 断言在组合运行时失败——已在泄漏源测试打 no-op 补丁并让调度器测试显式钳制 `ENABLE_SCHEDULED_TITLE_REWRITE_APPLY`(未改断言)。

## 3. P0 —— 汽配子账号开店准备(eBay 侧操作,不涉及代码)

1. 子账号完成 seller 注册、店铺订阅、店名设定。
2. 用现有 `/ebay/auth` 流程为子账号授权(同一 APP_ID,RuName 回调复用),token 落到实例B自己的 `ebay_tokens.db`。
3. 在子账号下创建 business policies(运费/退货/收款),把三个 ID 填入实例B的 store_profile。
4. 创建 inventory location(汽配仓库地址),key 填入 store_profile。
5. 验证:实例B跑 `tools/get_policy_ids.py` 与一次 sandbox/生产最小刊登。

---

## 4. P1 —— 采集渠道

| 渠道 | 现状 | 工作量 |
|---|---|---|
| 大建云仓 | [dajian_client.py](../src/clients/dajian_client.py)(openapi.gigab2b.com)+ 浏览器扩展,直接复用;实例B配自己的 `DAJIAN_API_KEY/SECRET` | 低:只需在采集入口按类目过滤"只采汽配" |
| viomall | **代码库零覆盖,全新开发,本计划最大未知数** | 先调研:①有无开放 API(优先做 `viomall_client.py`,对齐 dajian_client 接口);②无 API 则评估浏览器扩展路径(extension/content.js 适配其页面结构);③产出字段对齐 `CollectedProduct` 模型(标题/价格/图/视频/描述/attributes/specs) |

**路由原则**:不做"采集后分账号路由"——实例B采集的产品只进实例B的库、只刊登到汽配账号,天然满足"汽配一采集就只刊登到这个账号"。

---

## 5. P1 —— 汽配类目适配

1. **fitment 扩展**:[vehicle_compatibility.py](../src/services/vehicle_compatibility.py) 已能解析 `compatibleProducts` 并分类 vehicle-specific / universal / text-only,但 `EBAY_MOTORS_CATEGORIES` 只有 5 个皮卡外饰类目。按实际选品扩类目集合与 fitment 规则;强 fitment 类目(发动机件等)刊登校验需强制 compatibility 或 MPN/OE 号。
2. **类目匹配与 aspects**:[ebay_category_matcher.py](../src/services/ebay_category_matcher.py) 与 [ebay_publisher.py](../src/services/ebay_publisher.py) 的类目知识以家具为主;汽配需补类目映射表 + 必填 aspects(Brand/MPN/OE Number/Fitment Type/Placement on Vehicle)。
3. **守卫分流**:现有质量守卫带家具语义——Assembly Status 白名单、尺寸测量守卫、床垫尺码归一等——对汽配会误伤。方案:守卫按类目分流(Motors 类目走汽配规则集),或在实例B的 store_profile 加守卫开关。
4. **LLM 优化器**:qwen_optimizer 的提示词是家具文案专家人设,汽配实例需要一套汽配人设提示词模板(P0 参数化后按实例注入)。

---

## 6. P2 —— 运维部署

1. 实例B目录部署、`.env` 配置、`daily_tasks.bat`/计划任务复制并指向新目录。
2. 双 `scheduler_daemon` + watchdog 并行实测:确认 [process_identity.py](../src/utils/process_identity.py) 按脚本路径识别进程,两实例 watchdog 不互相误杀。
3. 通知邮件分流(各实例各自 `NOTIFICATION_EMAIL` 或统一收件、标题带店名区分)。
4. 备份策略:`backups/` 逻辑随实例走,确认磁盘容量。
5. git 同步纪律写入 agent.md/CLAUDE 工作规则:主目录开发、子目录只 pull。

---

## 7. 里程碑

| 阶段 | 内容 | 依赖 |
|---|---|---|
| M1 | P0 配置外部化 + 实例A回归验收(§2.3) | 无 |
| M2 | 子账号开店五步(§3)+ 实例B部署、最小刊登打通 | M1 |
| M3 | 大建云仓汽配采集→刊登全链路(含 fitment 基础校验) | M2 |
| M4 | viomall 调研结论 + 客户端开发 | 可与 M3 并行调研 |
| M5 | 汽配守卫分流 + CRO/审计脚本在实例B试运行 | M3 |
| M6 | (预留)盲盒实例C复制该模式 | M1 |

## 8. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| viomall 无开放 API,采集只能走页面抓取 | 高 | M4 先行调研定案;备选:只用大建云仓起量 |
| 汽配强 fitment 类目刊登被 eBay 拒(缺 compatibility) | 中 | 选品先从 universal-fit / 外饰件起步,逐类目扩 fitment |
| 家具语义守卫误伤汽配(尺寸/组装守卫、质量门标记) | 中 | §5.3 分流 + 实例B金丝雀小批量放行 |
| 两份 checkout 代码漂移 | 中 | git 纪律 + 子目录禁改;发布用 tag |
| 双 daemon/watchdog 互相误杀 | 低 | M2 并行实测 process_identity;PID 文件已目录级隔离 |
| 同一开发者密钥 API 配额被两实例共享耗尽 | 低 | 监控 API 调用量;必要时为子账号申请第二套 keyset |
