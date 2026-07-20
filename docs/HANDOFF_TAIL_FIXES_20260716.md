# 交接任务书 #11：三个小尾巴（选择器 bug / 空队列退出码 / footer 漏网）

> 交接方：Claude　执行方：你（semantic_rewrite / repair 工具作者）
> 规模：小（半小时级）。全部背景见 logs/_task_semantic_rewrite.log（今日 12:48 失败现场）与 footer_repush_summary.md。

## 🔒 红线（沿用，只能停不能重新解释）：回滚失败留坏态 / 单批回滚>2 / 缩水拦截失效 / listing_id 变化 → 全停等指令。

## 铁律
禁 push；禁 `git add -A`；禁改 .env；禁碰守护进程；live 只碰本书点名范围；republish 原 offer 原 listing_id；避开 10:50–12:15 与 12:30。

## 修复 1：`--from-daily-audit` 报告选择器选了最旧的
现场：今日 12:48 任务取 `listing_audit_fix_20260714_113029.json`（当时 0715/0716 报告都在）。
修 `scripts/semantic_rewrite.py` 的报告挑选逻辑：在"total_published>500 的全库报告"里**取 mtime 最新**的（疑似现在排序方向反了/取了 [0]）。加一个单测：给三个假报告文件名/mtime，断言选最新。

## 修复 2：空队列应 exit 0
现场：排除 DONE/human 后 0 条 → 打印用法提示 → exit 2 → 调度器假告警邮件。
修：`--from-daily-audit` 推导出 0 条时打印 "nothing to do (queue empty after exclusions)" 并 **exit 0**。仅此路径；无参数裸跑仍应报用法错误。加单测。

## 修复 3：footer 漏网（live 扫描，不是 DB）
背景：footer 重推队列当时从本地 opt 建，DB 描述 4000 字截断把 footer 截没 → W5368P497697 等漏网（live 仍 "Ships from California"+"Dedicated Support"）。
1. **扫 live**：全库 PUBLISHED 逐条取 offer listingDescription（复用既有客户端），凡含旧串（`Ships from California, USA` / `Dedicated Support` / `Easy Returns` / `Secure Checkout`）→ 入 `logs/footer_stragglers.txt`。预计个位数到十几条（含 W5368P497697、上轮 17 VERIFY_FAILED 里部分）。
2. 逐条走 `scripts/repair_broken_listings.py` 单 SKU 协议（backup+三层验证+live 复查+失败回滚）。verify 不过的（如已知 claim=1 的那批）跳过留清单，不硬推。
3. 产出 `logs/footer_stragglers_summary.md`（DONE/SKIP 清单 + live 复查结果）。

## 收尾 commit（只 commit 不 push）
只 stage：`scripts/semantic_rewrite.py`、新增/修改的对应测试、（若动了）`scripts/repair_broken_listings.py`。
message：`fix(semantic-rewrite): pick latest audit report, exit 0 on empty queue; footer straggler repush`
产出 `logs/tail_fixes_summary.md`（两 bug 修复+测试结果粘贴、straggler 总账、commit hash、声明未 push/未改 .env）。

## 验收（交接方）
选择器/退出码有单测且全绿；W5368P497697 live 换新 footer；straggler 清单里 skip 有原因；`git show --stat HEAD` 只含点名文件；未 push。
