# 交接任务书 #9：最终收口（复合材质 B' + 人工件 C'/F' 自助分流）

> 交接方：Claude　执行方：你（semantic_rewrite 工具作者）
> 用户已授权：把阶段 B 复合材质审定、阶段 C/F 人工件的**可安全自动化部分**交给你决策执行；genuinely 需人判断的仍留人工清单。
> 前置：任务书 #8（`docs/HANDOFF_RESIDUAL_CLEANUP_20260714.md`）已跑完 A/C/D/E/F，B 在硬停。本书接着收口。

---

## 🔒 红线条款（最高优先，2026-07-15 偏差收口）
**四条红线只能触发"停止"，执行方无权重新解释、弱化、或改成"batch fence 续跑"。** 上一轮有 AI 把"单批回滚>2→全停"私自改成"只结束本批继续"——**禁止再犯**。触发任一红线：立即停、写清现场、等用户指令，不得自行续跑。
四条红线：① postfix 不收敛且回滚也失败（live 坏态）② 单批回滚>2 ③ 缩水拦截失效被推送 ④ 任何 listing_id 变化。

## 通用铁律
禁 git commit / 改 .env / 碰守护进程与每日调度；live 只碰本书点名 SKU；republish 原 offer 原 listing_id；双闸 `SEMANTIC_REWRITE_APPLY_ENABLED=1` 已开；审计验证 `AUDIT_SEMANTIC_FACT_SHEET=1`；避开 10:50–12:15 与 12:30；每 SKU 走 plan→apply(留backup)→连续两次 postfix dry 0 CRITICAL→不收敛即回滚→仍不行留 human；每批末抽 2 条独立双引擎复核。
**零臆造**：任何写入 live 的材质/声明必须源可溯或 eBay 官方中性值，二选一，否则留 human。

## 阶段 B'：复合材质映射自助审定 + 应用

`config/semantic_rewrite_maps.yaml` 现有 **31 条 `policy: human`** 复合材质候选（阶段 B 硬停产物），逐条自助判定：
1. **只把"主材唯一且源可溯"的改成 `policy: apply`**，判定规则（严格照此，不得放宽）：
   - 复合值里有明确**结构主材**（如 `PU,Solid Wood`→Solid Wood 框架为主材；`Iron,Polypropylene`→Iron）→ apply，primary=结构主材
   - 有次级源字段佐证（`Wood Type`/`Frame Material` 等指向某一成分）→ apply，primary=该成分
   - **两成分对等无法判主**（`Metal,Wood`、`Fabric,Foam+Spring`）→ 保持 `human`
   - 纯泛化或不确定 → 保持 `human`
2. 每条在 `logs/residual_compound_decisions.md` 记 [源复合值 → 判定(apply/human) → primary → 依据 → SKU]。
3. 改完 yaml，对判 apply 的复合材质对应 SKU 走单 SKU 协议（phase=`RESIDUAL_B`）。**不需再硬停等用户**（用户已授权自助，前提是严格遵守上面判定规则 + 零臆造 + 三层验证兜底）。

## 阶段 C'：人工件的可自动化子集

`logs/residual_human_review.md` 三桶（dimension_count 38 / claim_residual 21 / title_category 18）里，**只做确定安全的**，其余留清单：
- **claim_residual（21，如 LED）**：源明确不支持该功能 → 走 rewrite 删该声明（源可溯地删，不新增）。源支持或不确定 → 留 human。
- **dimension_count（38）**：仅当 `source_dims_trustworthy=True`（非包装尺寸伪装）且源尺寸明确 → 用源值修；否则（含"重跑仍波动"的 LLM 提取噪声）→ 留 human。
- **title_category（18）**：**全部留 human**（改标题会牵动类目，需人判断）。
- 处理的走单 SKU 协议 phase=`RESIDUAL_C`；产出更新后的 `residual_human_review.md`（标注哪些已自动处理、哪些仍 human）。

## 阶段 F'：5 件 held-back
上一轮已正确 held（2 例 rebuild claim=1、3 例源坏/半句标题）。**本轮不再尝试自动修**——只把 `held_back_5_review.md` 整理成运营可直接照做的一页（每条：现状/源证据/一句话建议动作），供人工。**live 不动。**

## 产出物（终验依据）
- `logs/residual_compound_decisions.md`（B' 逐条判定）+ 更新的 `config/semantic_rewrite_maps.yaml`
- `logs/semantic_rewrite_run_progress.jsonl`（phase=RESIDUAL_B / RESIDUAL_C）
- 更新的 `logs/residual_human_review.md`、`logs/held_back_5_review.md`
- `logs/final_closeout_summary.md`：各阶段计数、B' 判定统计（apply N / human M）、红线记录（无则"无"）、明确声明未 commit/.env/守护

## 验收标准（交接方终验）
- B' 判定无臆造（每条 primary 源可溯）；对 RESIDUAL_B/C 的 DONE 抽 10-15 条独立双引擎复核（0 语义 CRITICAL、listing_id 未变、backup 在）。
- human_queue / 各桶净减少数 = 新 DONE 数；留 human 的有明确原因。
- **红线若触发，必须是"停"不是"绕"**（核对 progress 与 summary 无私自续跑）。
- `pytest tests/test_semantic_rewrite.py tests/test_active_listing_audit_cli.py -q` 无新增失败；无 commit/.env/守护改动。

## 自检清单
- [ ] 已读本书 + 红线条款；明确红线=停、不可重新解释
- [ ] B' 只 apply "主材唯一且源可溯" 的，对等/不确定留 human
- [ ] C' 只做确定安全子集（源可溯删 claim / 可信源尺寸修），title_category 全留 human
- [ ] F' 不自动修，只整理清单
- [ ] 零臆造；四条红线；每批抽检；禁 commit/.env/守护
