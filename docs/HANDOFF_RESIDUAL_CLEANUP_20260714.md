# 交接任务书 #8：残余清理（四阶段）+ 测试修复

> 交接方：Claude（2026-07-14）
> 执行方：你（semantic_rewrite 工具作者）
> 目标：把语义改写残余（26 硬停未处理 + 156 human_queue = **182 唯一 SKU**）尽量清完，并修掉 2 个既有测试桩失败。
> **诚实前提**：这 182 里有一部分是**真幻觉/真冲突**，工具正确地不敢自动改——那部分产出人工复核清单，不硬修。不许写脚本绕过验证盲改。

---

## 铁律（沿用，违反任一即失败）
禁 git commit；禁改 .env（双闸已开）；禁碰守护进程；live 只碰本任务队列 SKU；republish 原 offer 原 listing_id；四条红线（回滚失败留坏态/单批回滚>2/缩水拦截失效/listing_id变化）触发即全停；避开 10:50–12:15 与 12:30 窗口；apply 用 SEMANTIC_REWRITE_APPLY_ENABLED=1，审计 AUDIT_SEMANTIC_FACT_SHEET=1。
**代码修改仅限阶段 D 点名的测试相关文件**；阶段 A/B/C 只执行不改产品代码（发现 bug 记录+跳过）。

## 阶段 A：残余重跑（最大收益，安全）

`logs/residual_rerun_queue.txt`（182 SKU）里**大部分是在今天 4 处工具修复之前进队列的**（grounding / 二级材质键 / foldable 词形 / cert grounding），重跑会自动清一大块。
- 按任务书 #7 §3 单 SKU 协议串行跑（plan→pushable才apply留backup→连续两次postfix dry 0语义CRITICAL→不收敛即回滚→仍不行留human）
- phase="RESIDUAL_A"；每 25 一批、批末抽 2 条独立双引擎复核
- **注意**：这批是残余、较硬，预期过审率低于追赶的 71%（估 30-45%）。不收敛/needs_human 的**留在 human_queue**，进入阶段 B/C 分流。
- 产出 `logs/residual_A_summary.md`

## 阶段 B：复合材质映射表扩充（人工闸）

阶段 A 后仍 human 的里，`semantic_material_compound` 一类（约 29）用策展表收：
1. 提取阶段 A 后剩余的复合材质模式（源 Main Material 含逗号/`+`/` and `），归并候选写 `logs/residual_compound_candidates.yaml`，每条带 evidence + 建议 primary（源可溯，禁臆造）
2. 追加进 `config/semantic_rewrite_maps.yaml` 的 compound_material_map（保持 enabled 状态；新增条目**默认 policy=human 待批**）
3. **硬停**产出 `logs/residual_compound_review.md`，等用户逐行批准/改/打回
4. 用户批准后对这批走单 SKU 协议（phase="RESIDUAL_B"）

## 阶段 C：真残余人工复核清单（不硬修）

阶段 A/B 后仍 human 的，**分类产出人工复核清单** `logs/residual_human_review.md`，不自动改，按类分组每条给：SKU / live 现值 / 源证据 / 建议动作（改文案/删声明/接受）：
- **真幻觉**（live 有、源无、无共享 token，如 canvas/wire mesh）→ 建议：人工改文案或删该声明
- **标题-类目冲突**（约 18）→ 建议：保留标题 or 人工核类目
- **claim 层残留**（LED 等约 8）→ 逐条看源是否支持
- **尺寸/数量边界**（约 18）→ 标注是真差异还是 LLM 提取波动（重跑 2 次仍在=真）
这份清单是给运营/用户看的，**执行方只整理不改 live**。

## 阶段 D：修 2 个既有测试失败

诊断已由交接方给出：
1. **test_rollback_dual_gate**（`AttributeError: FakeEbay has no oauth`，audit L1947 需 `ebay.oauth.get_valid_token()`）
   - 修法：给 `tests/test_semantic_rewrite.py` 的 `FakeEbay` 加最小 mock：`self.oauth = types.SimpleNamespace(get_valid_token=lambda: "tok")`；若 `_put_inventory_product_only` 还发真实 HTTP，需 monkeypatch requests 或让回滚测试改走 `create_or_replace_inventory_item`（FakeEbay 已 mock）路径。**只补桩，不改被测逻辑。**
2. **test_gap4_compound_material_flags_human**（断言 `"semantic_material_compound" in human` 失败）
   - 根因：用户已批准的 `config/semantic_rewrite_maps.yaml` 现在会**解析** "Polyester,Rubber Wood" → 不再判 human（映射表按设计工作，测试过时了）。
   - 修法：更新测试——改用一个**不在映射表里**的复合值断言仍 human，或断言在映射表命中时被解析（`Material ← <primary>`）。**这是"测试跟上新行为"，不是削弱断言。**
- 修完跑 `pytest tests/test_semantic_rewrite.py -q` 全绿（0 失败）。

## 阶段 E：把坏 listing 守卫接入 --fix 自动修（2026-07-14 W6018 事故收口）

背景：`scripts/audit_fix_active_listings.py` 已新增两道**检测**守卫（`description_is_raw_source_dump` → issue `description_raw_source_dump` + fix 键 `__rebuild_description_from_source__`；dangling 标题 → `incomplete_title`）。但 fix 键 `__rebuild_description_from_source__` 目前**只标记未接入 apply 循环**——审计能报不能修。本阶段把它接上。

1. 在 audit 的 fix-apply 循环里处理 `__rebuild_description_from_source__`：复刻 `scripts/repair_broken_listings.py` 的重建逻辑（`build_source_snapshot` 现抓源 → `build_structured_description_from_source` 重建模板 → `normalize_listing_title_for_ebay` 修标题 → `_material_case` 保缩写 → 三层验证 → backup → `_put_inventory_product_only` + `update_offer_category` + `publish_offer`）。**改不干净的（claim/模板/标题验证不过）不推，标 issue 留人工。**
2. 复用而非重写：直接 `from scripts.repair_broken_listings import ...` 或抽公共函数；不要复制粘贴两份逻辑。
3. 新增测试：mock 一个 raw-source 描述 SKU，断言 --fix 后 issue 消失且推送了模板；mock 一个重建后 claim=1 的，断言**不推**。
4. **不改每日调度**（禁碰守护进程）；只让 `audit_fix_active_listings.py --live --fix` 具备自动修这两类的能力。在 summary 写明"用户可在维护窗手动跑 `--live --fix --sku-file <坏清单>`，或后续把 listing_audit 定时任务加 --fix 由用户决定"。

## 阶段 F：5 个 held-back 坏 listing 人工件（整理+能修则修）

数据在 `logs/held_back_5.json`（交接方已抓 live 现值+源）。产出一页清单 `logs/held_back_5_review.md`，每条给 [SKU / live 现标题+材质 / 源 productName+Main Material / 为什么被拦 / 建议动作]，并**对有明确源支持解的直接修**（走阶段 E 的重建路径 + 人工确认的标题/材质）：
- `W5368P497697`（源标题完整"Solid Wood Nightstand...Pull-out Panel..."，只是 live 截断）→ 可重建标题+Material←Rubber Wood，**能修**
- `W3098P470269`（live 材质 Wood 错，源是 Polyester 面料椅凳；标题截断"End of"）→ Material←Chenille/Polyester、标题补"End of Bed Bench"，**能修**
- `N704G201258L`（源 Acacia Wood,Polyester；title 说 Solid Wood）→ Material←Acacia Wood；Solid Wood 是否保留需人工判，**倾向人工**
- `W5123P397514`（源标题本身"...Featuring a"半句）→ 需人工拟一个干净 ≤80 标题，**人工**
- `W3166P455683`（源 productName 只有"chicken coop"两词，源数据本身坏）→ 需人工拟标题或修源，**人工**
能修的走单 SKU 协议（backup+三层验证+live复查）；拿不准的留清单，不硬改。

## 产出物（终验依据）
- `logs/residual_A_summary.md` / `logs/residual_compound_review.md`（阶段B硬停）/ `logs/residual_human_review.md`
- `logs/semantic_rewrite_run_progress.jsonl`（phase=RESIDUAL_A / RESIDUAL_B）
- 更新后的 `logs/semantic_rewrite_human_queue.txt`（移除新 DONE）
- 阶段 D：测试全绿输出粘贴到 `logs/residual_cleanup_summary.md`
- 阶段 E：`__rebuild_description_from_source__` 接入 apply 循环 + 新增测试；能力说明写入 summary
- 阶段 F：`logs/held_back_5_review.md`（一页清单）+ 能修的 live 修复记录

## 执行顺序与硬停
阶段 A → **阶段 B 提取候选后硬停等用户审表** → 用户批准后 B 应用 → 阶段 C 整理清单 → 阶段 D 测试 → 阶段 E 接入自动修+测试 → 阶段 F 5件整理+能修则修。阶段 B 硬停是唯一等指令的点；其余连续执行。

## 验收标准（交接方分两次）
- **阶段 B 硬停**：候选表无臆造、有 evidence；未 apply。
- **终验**：对 RESIDUAL_A/B 的 DONE 抽 10-15 条独立双引擎复核（0 语义 CRITICAL、listing_id 未变、backup 在）；human_queue 净减少=新DONE数；阶段 C 清单分类合理；`pytest tests/test_semantic_rewrite.py` 0 失败；**阶段 E：`--live --fix` 对 raw-source 描述能自动重建成模板且改不干净的不推（我会 mock 验+抽 live 验）；阶段 F：能修的 live 独立复查干净、拿不准的在清单有据**；无 commit/.env/守护改动。

## 自检清单
- [ ] 已读本书；理解真幻觉不硬修、只出人工清单
- [ ] 阶段 A/B/C/F 不写产品代码新逻辑（阶段 E 允许改 audit 接 fix 键、复用不复制）；测试只新增不削弱
- [ ] 阶段 B 提取候选后硬停等用户批准
- [ ] 阶段 E 复用 repair_broken_listings.py，不改每日调度/守护进程
- [ ] 四条红线；每批抽检；双闸/审计 env 已就位
- [ ] 禁 commit/改.env/碰守护进程
