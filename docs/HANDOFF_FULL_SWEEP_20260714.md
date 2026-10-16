# 交接任务书 #7：语义改写全量追赶（一次性、危险优先、不限量）

> 交接方：Claude（2026-07-14）
> 执行方：你（semantic_rewrite 工具作者）
> 目的：把全库 ~428 条语义 CRITICAL（"吹牛文案"）里**能安全自动修的**一次性清完，而不是每天 40 个慢慢来。
> 安全前提（已由前几轮验证）：工具**不可能推送不干净的内容**——改不干净的一律拒推、进人工队列，live 页面原样不动。所以"跑快"唯一代价是 eBay 当晚多几百次改动，**没有风险下限**。

---

## 0. 与每日 12:30 任务的关系

- 这是**一次性追赶**，独立于每日 `--limit 40` 的常规任务。常规任务继续跑，接住新增。
- 今天交接方已落地 3 处工具改进（未 commit，在工作树，本次自动生效）：① 事实表 grounding ② 二级材质字段清理 ③ foldable 词形回归修复。所以本次自动过审率高于今天的 19/40。

## 1. 铁律（违反任一即失败）

1. 禁 git commit/stage/push；禁改 `.env`（双闸已开，直接可 apply）；禁碰守护进程；禁改代码（本次只执行不开发，发现 bug → 记录+跳过该 SKU，不改）。
2. live 写仅限本次队列内 SKU；republish 必须原 offer 原 listing_id，禁 end+relist。
3. **四条红线**（沿用）触发即全停并记录：① 某 SKU postfix 不收敛且回滚也失败（live 坏态）② 单批回滚 >2 ③ 缩水拦截失效 ④ 任何 listing_id 变化。
4. 避开 10:50–12:15 与 12:30（每日任务）窗口。
5. 全程双引擎自检不得省（见 §3）。

## 2. 队列构建（危险优先）

1. 用最新每日全库审计报告（`logs/listing_audit_fix_2026*.json` 里 `total_published>500` 的最新一份）推导语义 CRITICAL SKU 全集。
2. **排除**：已 DONE 的（progress 里 result=DONE）、已在 `logs/semantic_rewrite_human_queue.txt` 的、源 `skuAvailable=False` 的（下架不出单，改了没流量）。
3. **危险优先排序**（先修最会导致退款的）：按每 SKU 的 CRITICAL 类型加权降序 —— semantic_material / semantic_certification / semantic_dimension 权重最高，semantic_count / semantic_capacity 次之，其余最后。
4. 写 `logs/full_sweep_queue.txt`。**不设日限**——本次目标是一晚跑完全部可跑的。

## 3. 单 SKU 协议（串行，与前几轮一致）

1. `plan_rewrite` → SkipResult / needs_human → 记 `human_queue`（带原因），跳过
2. pushable=True → `apply_rewrite`（自动留 backup 到 `logs/semantic_rewrite_backups/`）
3. **postfix 收敛**：连续两次 dry 审计（`--live --sku <SKU>`，`AUDIT_SEMANTIC_FACT_SHEET=1`），两次均 0 语义 CRITICAL 才判 DONE
4. 不收敛 → 立即 `scripts/semantic_rewrite_rollback.py --sku <SKU>` 回滚 → 回滚后 dry 验证恢复 → 记 human_queue（postfix_failed）
5. 每 SKU 落 `logs/semantic_rewrite_run_progress.jsonl`，phase="FULL_SWEEP"
6. 分批处理（每 25 一批只为留痕/抽检，不是节流）；**每批末抽 2 条 DONE 用事实表引擎独立复核**（`compare_fact_sheets` 传 live_text/source_text），结果写批汇总 `logs/full_sweep_batches.jsonl`
7. DONE 的从 human_queue 移除（若在）；human 的追加

## 4. 产出物（终验依据，缺失即失败）

- `logs/full_sweep_queue.txt`（危险优先队列）
- `logs/semantic_rewrite_run_progress.jsonl`（phase=FULL_SWEEP 每 SKU 一行）
- `logs/full_sweep_batches.jsonl`（每批统计 + 抽检）
- `logs/full_sweep_summary.md`：总账（处理数 / DONE / HUMAN / ROLLED_BACK / SKIP）、危险类型清零对比（跑前 vs 跑后各类型 CRITICAL SKU 数）、human_queue 分桶残余清单、四条红线记录（无则写"无"）、明确声明未 commit/未改代码/未动 .env 与守护进程

## 5. 验收标准（交接方终验）

1. 对本次 DONE 抽样 15-20 条独立双引擎复核（offer 面 0 语义 CRITICAL、listing_id 未变、backup 在）。
2. human_queue 净变化对得上（新增 human = 本次判 human 的；DONE 不在 queue）。
3. 跑前 vs 跑后全库语义 CRITICAL SKU 数应大幅下降（预期从 ~428 降到 ~150 上下；残余均在 human_queue 且有原因）。
4. 无 commit / .env / 守护进程改动；四条红线未破或破了有完整记录。

## 6. 自检清单

- [ ] 已读本书；理解工具不可能推送不干净内容 → 跑快零风险下限
- [ ] 危险优先排序、不设日限、排除下架/已 DONE/已 human
- [ ] 每 SKU 走 plan→apply(备份)→两次 postfix→不收敛即回滚→仍不行留 human
- [ ] 四条红线；每批抽检 2 条独立复核
- [ ] venv + PYTHONPATH；apply 用 SEMANTIC_REWRITE_APPLY_ENABLED=1（.env 已有，勿再改）；审计 AUDIT_SEMANTIC_FACT_SHEET=1
- [ ] 禁 commit / 改代码 / 改 .env / 碰守护进程；避开 10:50–12:15 与 12:30

确认后从 §2 队列构建开始，通宵跑完，交 summary。
