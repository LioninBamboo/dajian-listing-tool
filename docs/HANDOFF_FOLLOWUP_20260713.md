# 交接任务书 #2：巡检遗留跟进（开发型任务，5 项）

> 交接方：Claude（2026-07-13 会话）
> 执行方：你（AI 执行模型）
> 验收方：交接方将按第 8 节标准逐项验收。
> **本文件是唯一任务来源，与其他任何文字冲突时以本文件为准。**
> 前置阅读（只读）：`docs/HANDOFF_RULE_FIX_20260713.md`（上一棒的任务书）、`logs/handoff_rule_fix_summary.md`（上一棒的执行总结，本任务的 5 个条目全部源于其第 3/4 节）。

---

## 0. 与上一棒的本质区别

上一棒是"纯执行、禁改代码"。**本棒允许并要求改代码**，但每个条目只允许触碰点名的文件；条目之间互不依赖，按 1→4 顺序做（条目 5 只调查不执行）。每完成一个条目立即写进度，再开下一个。

## 1. 通用纪律（违反任一条即执行失败）

1. **禁止 git commit / stage / push**。改动留在工作区，验收后由用户决定提交。
2. 每个条目只允许改动其"允许触碰的文件"清单内的文件 + **新增**测试；**禁止修改或删除任何既有测试断言**（只许新增测试函数/类）。
3. 改完任一 `.py` 必须：`py_compile` 通过 + 运行该条目指定的测试切片全绿。
4. 测试命令模板（必须用 venv）：
   `$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'; & C:\Users\poonx\Dajian_Listing_Tool\.venv\Scripts\python.exe -m pytest <切片> -q`
5. live 写操作只允许触及本任务书点名的 SKU；`--fix` 必与 `--live` 同现；避开 10:50–12:15 定时窗口；开跑前确认无其他 `audit_fix_active_listings` 进程。
6. 上一棒的三条红线继续有效（桌/柜进 38208/175758 全停；尺寸修复偏离标题 >30% 全停；单批未知 ERROR >3 全停）。
7. 禁碰：调度进程、语义队列（463 条）、`suggested_price`/库存/promotion、`scheduler_daemon.py`。
8. 环境：`AUDIT_SEMANTIC_FACT_SHEET='0'`；脚本自带 .env 加载。

## 2. 条目 1（最高优先）：foldable 判定震荡根治

**现象**：对 `logs/handoff_followup/foldable_skus.txt` 的 4 个 SKU 连续跑 `--live --fix`，`missing_foldable`（"源支持 foldable 但 Features 缺失"→ 补上）与 claim 引擎的 foldable 违规（"源不支持 foldable"→ 移除）交替出现，永不收敛。

**根因方向（需你确认）**：两套"源是否支持 foldable"的检测器结论相反——
- A 检测器：`src/utils/listing_quality_gate.py` 的 `build_source_facts` / `CLAIM_PATTERNS["foldable"]`（audit 的 `missing_foldable` 用它）
- B 检测器：`src/utils/claim_diff_engine.py` 的 `FEATURE_SOURCE_PATTERNS["foldable"]`（audit 的 `__claim_diff_violations__` 用它）
- 注意 audit 里 `build_source_constraints` 的调用（`scripts/audit_fix_active_listings.py` ~L1137）传的是 `attrs={}`——B 检测器可能看不到 A 看到的字段。4 个 SKU 的源标题都含 fold 词（"Folding Mattress"、"Extra-Long Folding Top"），先查两边各自喂了什么文本。

**设计决策（按此实现，不要另起炉灶）**：
1. 仲裁唯一化：新建或指定一个函数作为**唯一** foldable 源证据判定入口，A、B 两处都改为调用它（或让 B 的输入与 A 完全一致）。
2. 语义边界：源文本含 fold/folding/collapsible/折叠 → 支持；**extendable / extending / drop leaf 本身不构成 foldable 证据**（但同一标题同时含 "Folding Top" 这类 fold 词时按支持算）。
3. 收敛判据（硬性）：对 4 个 SKU 依次 `--live --fix`，随后**连续两次** dry 审计，两次都必须 0 条 foldable 相关 issue（missing_foldable 与 claim foldable 都为 0）。

**允许触碰**：`src/utils/listing_quality_gate.py`、`src/utils/claim_diff_engine.py`、`scripts/audit_fix_active_listings.py`（仅 foldable 相关行）+ 新增测试。
**测试切片**：`tests/test_listing_quality_gate.py tests/test_claim_diff_engine.py tests/test_active_listing_audit_cli.py`
**必须新增**：用 4 个 SKU 之一的真实源标题构造的震荡回归测试（两检测器对同一输入必须同判）。

## 3. 条目 2：12 条视频 UNSUPPORTED_SOURCE

**现象**：`logs/handoff_followup/video_skus.txt` 的 12 个 SKU，上传器下载源视频得到 text/plain 小文件。

**首要假设（先验证再动手）**：数据库快照里的 GigaB2B 视频 URL 带签名参数（`x-ct` 是过期时间戳），**采集时的签名早已过期**，下载得到的是错误页。验证方法：取一条 DB 里的 videos URL 看 `x-ct` 时间戳是否已过期；再用 `DaJianClient.get_product_detail_by_sku` 现取 `videoUrls` 对比下载。

**设计决策**：
1. 若假设成立：在 audit 的视频修复路径（`scripts/audit_fix_active_listings.py` 的 video fix 分支）**上传前现抓新鲜源详情拿新签名 URL**（参考 `src/services/source_refresh.py` 的 `build_source_snapshot`），用新 URL 下载上传。
2. 上传器加内容校验：下载结果 Content-Type 非 video/* 或体积 < 256KB → 判为不可用源，抛出/返回明确错误码（不要把 text/plain 传给 eBay）。
3. 若某 SKU 用新鲜 URL 仍失败 → 在进度文件记录该 SKU 为 `video_source_dead`，**不做每日重试豁免机制**（那是另一个话题，只记录）。
4. 修完对 12 个 SKU 跑 `--live --fix --sku-file logs/handoff_followup/video_skus.txt` + postfix 验证，能修多少修多少，剩余逐条记原因。

**允许触碰**：`src/services/ebay_video_uploader.py`、`scripts/audit_fix_active_listings.py`（仅视频路径）+ 新增测试。
**测试切片**：`tests/test_active_listing_audit_cli.py` + 你新增的上传器测试（mock 网络，不许真连网测）。

## 4. 条目 3：W2753P489186 诊断（只读，禁修）

读 inventory item availability、`get_offers_by_sku`、Trading `GetItem`（listing 366417734123）三个面，判断 25604 "Availability not found" 的成因（疑似 Trading 老 listing 与 Inventory 不同步）。**只输出诊断结论与建议到总结文件，不允许 relist / 改数量 / 改状态。**

## 5. 条目 4：W2700P487997 usb claim 写入面

**现象**：claim 修复跑了两次，audit 仍报 `hallucinated_charging`（USB/power 词残留）。
**排查方向**：claim 清理是否只写了 inventory `product.description` 而 offer 的 `listingDescription`（买家可见、audit 读取优先）未更新；对照 `build_live_listing_opt_snapshot` 的读取优先级。
**修复**：确认后修 audit 写入路径（清理必须同时落 offer listingDescription），对该 SKU `--live --fix` + 连续两次 dry 验证 0 residual。
**允许触碰**：`scripts/audit_fix_active_listings.py`（写入路径）+ 新增测试。

## 6. 条目 5：类目迭代候选（只调查，禁执行）

对 `logs/handoff_followup/category_review_skus.txt` 的 5 个 SKU，给出每条的：现类目 → 建议类目（eBay tree-0 leaf）→ 依据（产品族/竞品惯例）→ 需要改哪条 profile/matcher 规则。**写进总结即可，不改代码、不动 live。** 注意 W1151S04803/04808 源已下架（qty 0），价值最低。

## 7. 产出物（验收依据，缺失即失败）

1. `logs/handoff_followup_progress.jsonl` — 每完成一个条目追加一行：
   `{"item": 1, "status": "DONE|PARTIAL|BLOCKED", "started": "...", "finished": "...", "changes": ["file:function", ...], "tests_added": N, "tests_result": "X passed", "live_skus_touched": [...], "notes": "..."}`
2. `logs/handoff_followup_summary.md` — 分条目：根因结论（证据）、改了什么、live 修复结果表、W2753 诊断、类目建议表、遗留。
3. 全部结束后运行并粘贴结果到 summary：
   `pytest tests/test_listing_quality_gate.py tests/test_claim_diff_engine.py tests/test_active_listing_audit_cli.py tests/test_ebay_category_matcher.py tests/test_source_refresh.py tests/test_listing_fact_sheet.py -q`

## 8. 验收标准（交接方将执行）

1. 两个产出物齐全；progress 覆盖 5 个条目（BLOCKED 需带证据）。
2. 交接方重跑第 7.3 节全套测试切片：全绿；`git diff` 核对既有测试**零删改**。
3. foldable 4 SKU：交接方连跑两次 dry 审计，两次均 0 foldable issue（收敛性核验）。
4. 视频 12 SKU：dry 审计中 `missing_video` 只允许出现在 progress 标记为 `video_source_dead` 的 SKU 上；修复成功的须 live `videoIds` 非空。
5. W2700P487997：dry 审计 0 residual，offer listingDescription 无 USB/power 词。
6. W2753P489186：live 状态未被改动（qty/offer/listing 原样）。
7. `git status`：改动仅限各条目"允许触碰"文件 + 新增测试 + logs/；无 commit；调度进程存活。
8. 红线记录：summary 若非"无"，逐条核 live。

## 9. 开工自检清单

- [ ] 已读完本文件与上一棒 summary 第 3/4 节
- [ ] venv + PYTHONPATH + AUDIT_SEMANTIC_FACT_SHEET=0
- [ ] 无其他审计进程、不在 10:50–12:15 窗口
- [ ] 明确：条目 3、5 只读；条目 1、2、4 允许点名文件内的代码修改
- [ ] 明确：禁 commit、禁改既有测试断言、红线三条

确认后从条目 1 开始。
