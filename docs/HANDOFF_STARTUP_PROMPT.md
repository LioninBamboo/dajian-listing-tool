# 贴给下一个 AI 的启动 Prompt

> 用法：把下面 `====` 之间的整段复制，作为新会话的第一条消息发给接手的 AI。

====

你正在接手一个**多店铺 eBay 自动刊登系统**（一套代码服务家具主店 + 盲盒店 + 汽配店，靠 `store_profile` 按实例分流）。

**第一步，先完整读这两份文档，别急着动手：**
- `docs/HANDOFF_MULTI_STORE_20260723.md` —— 全面交接报告（架构、账号/政策/库位实体清单、已完成模块、12 条 gotchas、待办优先级、Trading 字段清单）。
- `memory/subaccount-multi-instance-plan.md` —— 更细的运行流水账（若可访问）。

**环境：**
- 主仓库 `C:\Users\poonx\Dajian_Listing_Tool`，分支 `codex/live-audit-semantic-guard`（已全部 push 到 GitHub `LioninBamboo/dajian-listing-tool`）。
- 子实例是主仓库的 clone、只 pull 不开发：盲盒 `C:\Users\poonx\GrovePop_Listing_Tool`、汽配 `C:\Users\poonx\AutoParts_Listing_Tool`。各实例 `.\.venv\Scripts\python.exe`。
- Windows / PowerShell 或 Git Bash 均可；输出中文时设 `PYTHONIOENCODING=utf-8`。

**当前状态一句话：** 盲盒、汽配两个子店已完成 OAuth/政策/库位/金丝雀刊登。两个待攻坚点见下。

**你的任务（二选一开工，优先级见报告 §5）：**

- **P0-A：汽配 Trading API 刊登通道。** 已实证：真·汽配类目在 **eBay Motors 站（SiteID 100）**，Inventory API 发不进去（errorId 25005），**Trading API `AddFixedPriceItem`（`X-EBAY-API-SITEID: 100`）能发**（含 77 条车型适配已验证成功，ItemID 188732319492 测试后已结束）。要把这套封装进管线：`real_ebay_client` 加 Trading 刊登方法、`store_profile` 加 `listing_channel`/`ebay_site_id`、`batch_publish` 按 channel 分流、汽配用 Motors 合规退货政策 `262619354013`（P&A 硬性要求卖家承担退货费）、Motros 类目重映射（**逐个实发验证，不空谈**）。详见报告 §5-P0-A 与 §6 字段清单。
- **P0-B：盲盒自有图管线。** GrovePop 首条 listing 被 eBay 自动判仿冒下架，触发因是**直接用了源卖家的图**。需要发布前把图转存为自有/EPS 托管 + 无自有图不许发的门禁。

**必须遵守的工作准则（本项目血的教训，报告 §8 有全文）：**
1. **涉及"能不能刊登"的判断，一律真实发布验证，绝不空谈**——"taxonomy 类目有效"≠"能发进去"。前一个 AI 在 Motors 上因没实发就下结论错了三次。
2. **主店隔离是硬约束（报告 §7.5）**：汽配 Trading 通道会动到三店共享的 `real_ebay_client`/`batch_publish`/`store_profile`。规矩：新能力挂 `store_profile` 开关且**默认值=主店现状**、Trading 走**新增函数+新分支**（现有 Inventory 路径一字不改）。改完必须 `pytest tests/test_main_store_contract.py` 全绿——**这是主店的机器守卫**，它红了就是你碰了主店。切勿为了让它过而放松断言。
3. **操作真实 offer 前先查该 SKU 是否已有已发布 offer**；用专用 scratch SKU 做探针，用完删干净（前一个 AI 的探针脚本误删过金丝雀）。
4. **数据不得编造**：aspect 值（尺寸/承重/车型兼容）必须来自源数据。
5. 外向操作（真实上架）先获用户放行；子账号 OAuth、身份/银行信息类只能用户本人做。
6. 改完跑 `PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/ -q`（约 2057 通过）。只在主仓库开发→commit→子实例 `git pull`。

读完文档后，先用三五句话向用户复述你对现状和下一步的理解，确认无误再动手。
====
