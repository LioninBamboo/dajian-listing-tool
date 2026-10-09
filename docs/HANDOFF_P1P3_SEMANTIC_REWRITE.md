# 交接任务书 #4：语义改写管线 P1→P3（实弹执行，全阶段）

> 交接方：Claude（2026-07-13，P0 已验收通过）
> 执行方：你（AI 执行模型，即 P0 的作者——工具是你写的，现在你来开它）
> 规格母文件：`docs/PROJECT_SEMANTIC_REWRITE.md`（3.4 节流、第 4 节阶段表、第 5 节风险对策）。冲突时以本任务书为准（本书含用户最新授权）。
> **用户已授权贯通执行 P1→P3，但 P1 结束后有一个硬停验收点（见 §3.4）——那是唯一需要等指令的地方。**

---

## 0. 一句话任务

用你在 P0 交付的 `semantic_rewrite` 工具，金丝雀 15 条实弹改写并验证（P1）→ 停下等验收 → 按日限批量清完全部语义 CRITICAL 队列（P2）→ 把管线接入每日调度闭环（P3）。

## 1. 授权与铁律

**本阶段新增授权**（相对 P0）：
- 允许设置 `SEMANTIC_REWRITE_APPLY_ENABLED=1` 执行 `--apply`
- 允许写 collected_products（apply_rewrite 的设计内写入）与 `logs/semantic_rewrite_backups/`
- P3 允许修改 `scheduler_daemon.py`（仅按 §5 的机械模式）+ 新增测试

**继续禁止**：
- git commit / stage / push
- 修改既有测试断言（只许新增）
- 杀/重启调度守护进程（P3 代码落地后在下次自然重启生效，写明即可）
- 碰价格 / 库存 / promotion / 类目重判（类目由规则层管，改写只动 title/aspects/description）
- 10:50–12:15 窗口内发起批次；开跑前确认无其他 audit/rewrite 进程

**红线（触发即全停 + 记录）**：
1. 任一 SKU 改写后 postfix 不收敛且**回滚也失败**（live 处于坏状态）→ 全停，把该 SKU 的 backup 路径和错误写清楚
2. 单批内回滚 >2 次 → 全停（说明工具或数据有系统性问题）
3. 描述缩水 >60% 的计划被推送（工具应拦，若发现拦截失效即全停）
4. 任何 listing_id 变化（禁 end+relist；republish 必须原 offer）

## 2. 通用单 SKU 执行协议（P1/P2 共用）

1. `plan_rewrite` → 若 SkipResult 或 `needs_human` → 记入 `logs/semantic_rewrite_human_queue.txt`（追加，带原因），跳过
2. `pushable=True` → `apply_rewrite`（自动留 backup）
3. **postfix 收敛**：连续两次 dry 审计（`--live --sku <SKU>`，`AUDIT_SEMANTIC_FACT_SHEET=1`），两次均 0 CRITICAL 才算 DONE
4. 不收敛 → 立即用 `scripts/semantic_rewrite_rollback.py --sku <SKU>` 回滚 → 回滚后再 dry 验证恢复原状 → 记入 human_queue（原因=postfix_failed）
5. 每 SKU 一行进度：`logs/semantic_rewrite_run_progress.jsonl`
   `{"phase": "P1|P2", "sku": "...", "result": "DONE|SKIP|HUMAN|ROLLED_BACK", "listing_id": "...", "violations_fixed": N, "notes": "..."}`

## 3. P1 金丝雀

1. **队列推导**（开跑当日现推导）：`scripts/semantic_rewrite.py --derive-queue` → `logs/semantic_rewrite_queue.txt`；再按"近 7 天有订单的 SKU 沉底"重排（查 `order_recheck_log` 与 Trading GetOrders 近 7 天，无订单在前）
2. 取队列前 15 条（CRITICAL 最重优先，`W465P369984` 若在队列中跳过——已知 needs_human）
3. 按 §2 协议逐条执行（串行，不并发）
4. **产出金丝雀报告** `logs/semantic_rewrite_canary_report.md`：逐条 before/after（复用 P0 预览格式）+ 收敛结果 + human_queue 增量
5. **硬停**：P1 结束后停止一切执行，输出报告路径。**不得自行开始 P2**——等交接方验收（验收会独立复核 15 条 live 内容）后由用户/交接方给继续指令。

## 4. P2 铺量（收到继续指令后）

1. 重新 `--derive-queue`（P1 后债务面变了），排除 human_queue 与已 DONE
2. 分批：每批 ≤25，**每日总量 ≤80**（立项书 3.4）；每批完成即在 progress 里落 batch 汇总行
3. 每批协议 = §2 + 批末抽 2 条用事实表引擎独立复核 after 内容（模拟交接方验收，写入批汇总）
4. 队列清空或只剩 human_queue 时 P2 完成 → 写 `logs/semantic_rewrite_p2_summary.md`（总量/DONE/SKIP/HUMAN/ROLLED_BACK 统计 + human_queue 全量清单）

## 5. P3 每日闭环（P2 完成后）

1. 新增 `task_semantic_rewrite` 到 `scheduler_daemon.py`，**严格复刻既有机械模式**（参考今天 task_source_refresh 的加法）：TASK_TIMEOUT（建议 3600）、`schedule.every().day.at("12:30")`、CLI `--task` map、`recover_missed_tasks` 条目（priority 145，listing_audit 之后）、setup_schedule 日志行
2. 任务命令：`scripts/semantic_rewrite.py --from-daily-audit --limit 40 --apply --email`（需要你在 CLI 加 `--from-daily-audit`：读当日最新 audit 报告推导增量队列；`--email` 发执行摘要——邮件复用 `src/utils/email_sender.send_email`）
3. 依旧尊重双闸：调度任务的 apply 依赖 .env 中 `SEMANTIC_REWRITE_APPLY_ENABLED=1`——**你不改 .env**，在 summary 里写明"用户在 .env 加此行后调度生效"
4. 新增调度测试（参考 tests/test_scheduler_recovery.py 的 stub 模式，把 task_semantic_rewrite 加进 stub 列表——这是新增测试行为，允许）
5. `py_compile` + 全套相关切片测试绿

## 6. 产出物（验收依据）

- `logs/semantic_rewrite_run_progress.jsonl`（P1/P2 每 SKU 一行）
- `logs/semantic_rewrite_canary_report.md`（P1 硬停时交付）
- `logs/semantic_rewrite_p2_summary.md`、`logs/semantic_rewrite_human_queue.txt`
- P3：代码改动清单 + 测试结果贴入 `logs/semantic_rewrite_p3_summary.md`

## 7. 验收标准（交接方分两次验收）

**P1 验收（硬停点）**：15 条逐一——live 内容独立双引擎复核 0 CRITICAL、listing_id 未变、模板标记齐全、backup 文件存在、postfix 记录两连清；human_queue 原因合理；无 commit。
**终验（P2+P3 后）**：全库审计语义 CRITICAL SKU < 20（残余均在 human_queue 且有原因）；progress 总账对得上队列；调度改动过测试且守护进程未被动过；无 commit；红线记录核查。

## 8. 开工自检清单

- [ ] 已读本书全文 + 立项书 3.4/4/5 节
- [ ] 明确 P1 后硬停等指令，P2/P3 不得抢跑
- [ ] 明确四条红线与回滚义务（backup→rollback→验证恢复→human_queue）
- [ ] venv + PYTHONPATH；apply 时设 SEMANTIC_REWRITE_APPLY_ENABLED=1；audit 验证时 AUDIT_SEMANTIC_FACT_SHEET=1
- [ ] 禁 commit、禁改既有断言、禁碰守护进程与 .env

确认后从 P1 队列推导开始。
