# 交接任务书 #5：人工队列批量降解（策展映射表 + 中途人工放行）

> 交接方：Claude（2026-07-14）
> 执行方：你（AI 执行模型，semantic_rewrite 工具作者）
> 规格母文件：`docs/PROJECT_SEMANTIC_REWRITE.md`。语义改写工具已上线并跑完 P2 Day-1 + P3 调度。
> **本项目有一个硬停人工放行点（§4）——映射表必须由用户审定后你才能应用。与 P1 金丝雀同一安全哲学。**

---

## 0. 背景与目标（含一处事实修正）

语义改写人工队列 `logs/semantic_rewrite_human_queue.txt` 现有 82 行，实测桶分布：

| 桶 | 数量 | 本项目是否处理 |
|----|-----:|:---:|
| **required_no_source**（类目必填 aspect 无源可填，多数材质本身是干净单值） | **47** | ✅ 表 B |
| **compound_material**（复合源材质如 "Polyester,Rubber Wood" 不敢自动拆） | **12** | ✅ 表 A |
| postfix_unconverged（改写后仍不收敛，如 semantic_capacity 顽固） | 8 | ❌ 留残余 |
| other_needs_human | 11 | ❌ 留残余 |
| title_category_conflict | 4 | ❌ 留残余 |

**事实修正**：交接方此前口头说"最大桶是复合材质"，**错**——真正最大桶是 required_no_source（47）。本项目主攻这两个可策展桶（59 SKU），其余 23 留作独立残余（在总结里列清单，不处理）。

**目标**：用两张**人工审定**的策展映射表，把 59 条里能安全解析的批量转为 DONE；不能安全解析的保留 human 并说明原因。**零新增幻觉**是最高优先——策展值必须源可溯或 eBay 允许的中性值，绝不臆造卖点。

## 1. 严格禁止（违反任一即执行失败）

1. 禁 git commit / stage / push；禁改 `.env`；禁杀/重启守护进程。
2. 只允许**新增**：
   - `config/semantic_rewrite_maps.yaml`（两张策展表的数据文件）
   - 对 `src/services/semantic_rewrite.py` 的**受限修改**：仅允许在 `_is_simple_material_value` / `protect_and_fill_required_aspects` / 相关加载处接入 YAML 表，**不得改动这两个函数已有的安全语义**（复合值默认仍 human、必填缺失仍 human——只是先查表，查不到才落回原行为）
   - `tests/test_semantic_rewrite.py` 新增测试（禁改既有断言）
   - `logs/humanqueue_drain_*` 报告文件
3. **live 写操作**仅限 human_queue 内的 59 个目标 SKU；`--apply` 双闸不变（CLI + `SEMANTIC_REWRITE_APPLY_ENABLED=1`）；避开 10:50–12:15 与 12:30（语义调度任务）窗口。
4. 四条红线沿用任务书 #4 §1（回滚失败留坏状态 / 单批回滚>2 / 缩水拦截失效 / listing_id 变化 → 全停）。
5. 策展禁止臆造：表里每个映射的 target 必须满足下列之一，否则该模式判 human——
   - 源某字段直接支持（Main Material / Upholstery Material / Frame Material 的某一成分）
   - eBay 该必填项允许 "Does Not Apply" / "Unbranded" 等官方中性值（你需确认该值对该 aspect 合法，不确定就 human）

## 2. 阶段 A：提取待决模式（只读，产出候选表）

1. 遍历 human_queue 的 compound_material 与 required_no_source 两桶，对每个 SKU 拉源 attributes/事实表
2. 归并成两张**候选**表写入 `logs/humanqueue_drain_candidates.yaml`：
   - **表 A 候选**：`{源复合材质字符串: {suggested_primary: <你的建议>, evidence: <源依据>, skus: [...]}}`（如 `"Polyester,Rubber Wood": {suggested_primary: "Polyester", evidence: "Upholstery Material=Polyester 为面料主材", skus: [...]}`）
   - **表 B 候选**：`{(category_id, required_aspect): {policy: source|neutral|human, suggested_value: <值或null>, evidence: ..., skus: [...]}}`
3. 每个 suggested 值都要带 evidence；拿不准的 policy 直接标 `human`
4. **不写任何 live、不改代码**——阶段 A 只产出候选 YAML

## 3. 阶段 B：接入机制 + 测试（改代码，仍不碰 live）

1. 定 `config/semantic_rewrite_maps.yaml` 的最终 schema（两张表 + 版本号 + 注释说明来源）；先用阶段 A 候选填入（policy=human 的条目也要在，标注待人工确认）
2. 在 `_is_simple_material_value` 附近加载表 A：复合值先查表 → 命中且有 primary 则视为可解析（返回该 primary），未命中仍走原逻辑（human）
3. 在 `protect_and_fill_required_aspects` 加载表 B：必填缺失时先查 (category, aspect) → source policy 用源值、neutral policy 用中性值、human policy 或未命中仍落 human
4. 加载失败/文件缺失 → 全部落回原安全行为（表是增强不是依赖）
5. 新增测试：表 A 命中/未命中、表 B 三种 policy、YAML 缺失时的降级、"臆造防线"（无 evidence 的 target 不被应用）
6. `py_compile` + `pytest tests/test_semantic_rewrite.py tests/test_listing_fact_sheet.py tests/test_claim_diff_engine.py` 全绿

## 4. ★硬停人工放行点★

阶段 B 完成后**停止一切执行**，向用户交付：
- `config/semantic_rewrite_maps.yaml`（含你填的建议值 + evidence）
- `logs/humanqueue_drain_review.md`：逐模式列 [源值 → 建议 target → 依据 → 影响 SKU 数]，请用户逐行 **批准 / 修改 / 打回 human**

**你不得自行认定表已生效。** 用户会亲自审这张表（这是"人工审映射表"的本意），改定后把批准信号给你，你才进入阶段 C。等待期间不跑任何 apply。

## 5. 阶段 C：应用（收到用户批准后）

1. 以用户审定的 `config/semantic_rewrite_maps.yaml` 为准
2. 对 59 个目标 SKU 逐条走 semantic_rewrite 单 SKU 协议（plan→apply(留backup)→连续两次 postfix dry 0 CRITICAL→不收敛即回滚→仍不行留 human）
3. 每 SKU 落 `logs/semantic_rewrite_run_progress.jsonl`（phase="HQ_DRAIN"）
4. 从 human_queue 移除已 DONE 的行（保留未解析的），更新文件
5. 批末抽 2 条 DONE 用事实表引擎独立复核

## 6. 产出物

- `config/semantic_rewrite_maps.yaml`（版本化策展表，用户审定后的最终态）
- `logs/humanqueue_drain_candidates.yaml`（阶段 A）
- `logs/humanqueue_drain_review.md`（阶段 B 硬停交付）
- `logs/humanqueue_drain_summary.md`（阶段 C 后：59 中 DONE/仍 human 分解、残余 23 清单、表覆盖率、测试结果粘贴）
- progress 续写（phase=HQ_DRAIN）

## 7. 验收标准（交接方分两次）

**阶段 B 硬停验收**：候选表每条有 evidence；机制测试全绿且既有断言零删改；YAML 缺失降级正确；无臆造 target；无 commit / .env / 守护进程改动。
**终验**：对本轮 DONE 独立双引擎复核（offer 面 0 语义 CRITICAL、listing_id 未变、backup 在）；human_queue 净减少数 = DONE 数；残余 23 + 未解析条目有明确原因；`config/semantic_rewrite_maps.yaml` 与实际应用一致。

## 8. 开工自检清单

- [ ] 已读本书 + 立项书；理解"最大桶是 required_no_source 不是复合材质"
- [ ] 明确阶段 B 后硬停等用户审表，阶段 C 不得抢跑
- [ ] 明确策展禁臆造（源可溯或 eBay 官方中性值二选一）
- [ ] 只碰 config/ 新文件 + semantic_rewrite.py 受限接入 + 新增测试
- [ ] venv + PYTHONPATH；apply 时 SEMANTIC_REWRITE_APPLY_ENABLED=1；audit 验证 AUDIT_SEMANTIC_FACT_SHEET=1
- [ ] 禁 commit / 改既有断言 / 碰 .env 与守护进程

确认后从阶段 A 开始。
