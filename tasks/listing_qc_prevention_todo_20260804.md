# Listing QC 前置预防执行清单（2026-08-04）

状态约定：`[ ]` 未开始，`[~]` 进行中，`[x]` 已完成，`[!]` 阻断/需人工确认。

## Phase 0：基线和文档

- [x] P0-01 保留既有 `tasks/plan.md`，新增本轮专用计划。
- [x] P0-02 记录当前基线：原 30 条 CRITICAL/23 SKU 定向复审为 0 CRITICAL、0 transport；仍有 48 条非 critical。
- [x] P0-03 明确本轮不做批量 live 改库存、改价、结束或重刊。

## Phase 1：共享 QC 门面

- [x] P1-01 先写 `pass / blocked / unavailable / exception` 单元测试。
- [x] P1-02 新增共享 QC service，统一结构化质量门与 FactSheet 结果。
- [x] P1-03 保留 source/candidate fingerprint、ruleset/factsheet version 和证据字段。
- [x] P1-04 运行共享门面目标测试。

## Phase 2：生成和 READY 前置阻断

- [x] P2-01 接入 `daily_tasks.analyze_collected_products`。
- [x] P2-02 FactSheet blocker 传入 AI 重试提示，最终失败不得写 READY。
- [x] P2-03 接入 `scripts/audit_fix_ready_drafts.py`，删除重复的 FactSheet 字符串拼装。
- [x] P2-04 新增生成路径事实冲突与 unavailable 回归测试。

## Phase 3：发布前统一门

- [x] P3-01 接入 `batch_publish.py`。
- [x] P3-02 dry-run 持久化统一 QC 证据。
- [x] P3-03 验证 allowlist 与真实 dry-run 集合恒等：12/12、状态全为 `dry_run`，集合差集为空；证据见 `logs/pending_listing_publish_allowlist_20260804_plan_p3.txt`、`logs/publish_results_20260804_192042.json`。

## Phase 4：live 写入安全

- [x] P4-01 发布前 protected-field snapshot。
- [x] P4-02 API 异常后回读并识别 partial write。
- [x] P4-03 listing/offer ID、availability、images/videos、condition/package 对账。
- [x] P4-04 完成标准 live detect-only：23 个已发布 SKU，transport=0；发现 1 CRITICAL、16 HIGH、34 MEDIUM，未执行 live 写入；证据见 `logs/listing_qc_plan_detect_only_20260804.json`。

## Phase 5：FactSheet 溯源

- [x] P5-01 缓存增加版本和 provenance 字段。
- [x] P5-02 版本不匹配按 miss 处理。
- [x] P5-03 完成只读跨系统对账器并实跑：44 个 SKU，source URL=44/44、seller host evidence=44/44、GIGA live inventory=44/44、eBay quantity evidence=23/23；GIGA/eBay 库存状态冲突均为 0；9 条带到货证据；证据见 `logs/listing_qc_cross_system_20260804_plan.json`。

## Phase 6：验证和交付

- [x] P6-01 运行共享门面、FactSheet、quality gate、daily tasks 相关测试。
- [x] P6-02 运行全量相关回归测试并记录耗时/结果。
- [x] P6-03 对 READY/CRITICAL 目标完成 dry-run 审计并保存统一证据：12 条 allowlist 全部结构化通过；历史 23 SKU 定向复审为 0 CRITICAL/0 transport；统一报告状态为 `verified_with_followups`，见 `logs/listing_qc_plan_evidence_20260804.json`。
- [x] P6-04 已生成执行摘要：`logs/listing_qc_prevention_execution_summary_20260804.md`。

## Phase 7：经验沉淀与规则晋级

- [x] P7-01 将“案例库 + 规则库 + 回归库 + 晋级流程”纳入总计划。
- [x] P7-02 新增 `qc_experience` / `qc_rule_catalog` 的登记模型和 SQLite 迁移演练。
- [x] P7-03 让共享 QC 结果返回 `rule_ids`、`experience_ids`、`evidence_refs`。
- [x] P7-04 已建立首批 6 类人工修复案例种子目录；已完成真实 `ebay_collection.db` 备份、迁移和完整性校验。
- [x] P7-05 已完成历史样本回放：6/6 案例可复现，内容历史问题可阻断，clean control 未复发。
- [x] P7-06 已生成经验复发日报：enforced 规则保持 enforced，库存/部分写入证据不足项转人工/补证据，不自动晋级或批量 live。

### Phase 7 的安全边界

- [x] 经验登记不直接修改 live listing。
- [x] candidate 规则不能自动放行，只能 shadow 或 manual review。
- [x] 库存经验不能写入内容白名单；GIGA/eBay 数量冲突必须走库存对账。
- [x] 每条 enforced 规则必须有回归测试和版本号。

## 本轮停止条件

- [ ] 发现需要修改 scope 外 live listing。
- [ ] 发现 eBay API 写入状态无法通过回读判定。
- [ ] 测试失败但无法确认是本轮改动引起时，不得继续 live 写入。
- [ ] FactSheet unavailable 时不得放行。
