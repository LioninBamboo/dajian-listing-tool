# 立项书：源事实驱动自动改写管线（Semantic Rewrite Pipeline）

> 立项日期：2026-07-13
> 发起：巡检发现全库 472 SKU 带语义层 CRITICAL 声明（源不支持的材质/容量/认证/数量），规则层修复已收官，语义层目前 report-only。
> 状态：**已立项，待开工**（Phase 0 可交接执行，Phase 1 起需用户参与金丝雀放行）

---

## 1. 项目目标

把语义护栏（`src/utils/listing_fact_sheet.py`）检出的 CRITICAL 内容债务，从"每日邮件报告"升级为**自动改写闭环**：

```
语义 CRITICAL 队列 → 源事实表 → 确定性重建(文案+aspects+标题) → 三层验证 → republish → postfix 收敛验证
```

**成功标准（项目级）**：
- 全库语义 CRITICAL SKU 数从 ~470 降到 < 20（残余为需人工判断的边界案例）
- 每日审计的语义 CRITICAL 新增速率 ≈ 0（新刊登被 READY 门拦住，存量被本管线清零）
- 改写后 30 天内，被改写 SKU 的退款/SNAD 纠纷率不高于库均值
- 零"改写引入新幻觉"事故（三层验证兜底）

## 2. 范围

**In**：巡检语义 CRITICAL 队列（以开工当日最新每日审计报告重新推导，基线参考 `logs/patrol_semantic_critical_skus.txt` 463 条，FP 调优后预计 ~430）；描述、aspects、标题三个面的source-faithful 重建；HIGH 级功能过度声明（waterproof/foldable 族）在同一次重建中顺带清理。

**Out**：MEDIUM 级措辞（不碰）；价格/库存/promotion；类目（规则层已管）；视频；源已下架的 SKU（qty 0，跳过——改了也没流量）；语义层检测本身的进一步调优（缺陷另行提单）。

## 3. 架构设计（核心决策已定，不接受执行期更改）

### 3.1 生成方式：确定性重建，不用 LLM 写文案

这是本项目最重要的决策。已被 W3636P456662 修复实战验证（见 `docs/LISTING_QUALITY_GATE.md` 语义护栏章节）：

- **描述**：`build_structured_description_from_source`（audit 脚本内的店铺标准模板构建器：AQUAVERVE banner + 金边栏目 + 规格表 + 加州 footer），KEY FEATURES **逐字取自源 characteristics**——LLM 不参与生成，只参与验证。
- **aspects**：确定性修正表（见 3.3）。
- **标题**：仅当标题本身含违规声明时才动；用源标题 + `normalize_listing_title_for_ebay` 重建（W3636 模式），不做 SEO 创作。
- 理由：这批是内容债务 listing，安全 >> 文案花活；LLM 生成会重新引入本项目要消灭的东西。CRO 侧的标题热词优化管线（10:45）与本管线正交，改写完成的 SKU 冷却 14 天后才允许进 CRO 标题队列。

### 3.2 单 SKU 处理流（复刻 W3636 实战路径）

1. **源刷新**：`refresh_skus`（`src/services/source_refresh.py`）现抓该 SKU detailInfo，修快照（顺带拿新鲜签名媒体 URL）
2. **事实表**：`fact_sheet_for_content` 取源/live 两张表（hash 缓存），`compare_fact_sheets` 得违规清单
3. **重建**：3.1 的三个面；`suspect_source_dimensions` 守卫尊重（源尺寸不可信时不写尺寸）
4. **三层验证（全过才许推）**：
   - claim 引擎 `detect_claim_violations` = 0 CRITICAL
   - 事实表 diff（新内容 vs 源）= 0 CRITICAL
   - 质量门要件：KEY FEATURES+bullets、描述含 L/W/H 数字、assembly copy、标题 ≤80 word-safe、模板标记齐全（banner/footer/栏目）
5. **推送**：`_put_inventory_product_only` → `update_offer_category(listing_description=...)` → `publish_offer`（原 offer 原 listing_id，禁止 end+relist）
6. **postfix**：连续两次 dry 审计（AUDIT_SEMANTIC_FACT_SHEET=1）0 CRITICAL 才算收敛；不收敛 → 回滚到推送前快照并标记人工
7. **留痕**：DB logs 追加、改写前内容存 `logs/semantic_rewrite_backups/{sku}.json`（回滚依据）

### 3.3 aspects 确定性修正表

| 违规类型 | 修正动作 |
|----------|----------|
| semantic_material | Material ← 源 `Main Material`（规范化大小写）；删除无源依据的材质型 aspects（Pole/Stake/Floor Material 等，W3636 模式） |
| semantic_capacity | 删 Occupancy/Occupant Capacity/Seating Capacity，除非源有数 |
| semantic_certification | 删认证词（aspects+描述），复用 `__claim_diff_violations__` 清理机制 |
| semantic_count | 数字改为源值；源无数则删该声明 |
| semantic_feature (HIGH) | Features 列表剪除无源依据项；waterproof→源说 water resistant 则降级为源措辞 |
| semantic_dimension | 守卫可信时以源为准（规则层已管），不可信时不动 |

### 3.4 批次与节流

- 金丝雀 15 条 → 人工放行 → 每日 ≤ 80 条（避免单日大量 revise 影响 search standing；与 10:50–12:15 定时窗口错开）
- 有近 7 天订单的 SKU 延后到批次末尾（出单复核在护航，但少动在售旺的）
- 全程红线沿用交接 #1 三条 + 新增一条：**改写后描述长度 < 原描述 40% → 疑似重建失败，禁推、标人工**

## 4. 阶段计划

| 阶段 | 内容 | 产出 | 验收 |
|------|------|------|------|
| **P0 工具** | `scripts/semantic_rewrite.py`（--dry-run 默认 / --apply / --sku / --sku-file / --limit）+ 回滚脚本 + 单测（mock eBay/大建，覆盖 3.2 全流程与 3.3 全表） | 脚本 + 测试全绿 + 对 5 个真实 SKU 的 dry-run diff 报告（before/after 并排） | 交接方审 diff 报告，无一处新增无源声明 |
| **P1 金丝雀** | 从最新每日审计推导队列，取 CRITICAL 最重的 15 条 --apply | 15 条收敛 + before/after 报告 | **用户人工看报告放行**；30 天窗口挂 order_recheck 重点监控 |
| **P2 铺量** | 剩余队列按 3.4 节流分批 | 每批 progress.jsonl + 收敛统计 | 每批 postfix 0 CRITICAL；周维度看退款率 |
| **P3 闭环** | 每日审计的语义 CRITICAL 自动入队本管线（新增 `semantic_rewrite_queue` 表），报告改为"已入队/已改写/需人工"三态 | scheduler 任务（建议 12:30，审计后） | 连续 7 天审计新增语义 CRITICAL 自动清零 |

P0-P2 可按既有模式交接外部 AI 执行（届时按本文件写交接任务书）；**P1 的放行点必须留给用户**。

## 5. 风险与对策

| 风险 | 对策 |
|------|------|
| 重建引入新幻觉 | 生成零 LLM + 三层验证 + 金丝雀人工放行 |
| 源 characteristics 为空/太薄的 SKU | 重建器返回空 → 自动标人工，禁推（W2699 类保护） |
| 大批 revise 触发 eBay 风控/搜索降权 | 日限 80 + 错峰 + 原 offer 原 listing_id |
| 改写与 CRO 标题优化打架 | 改写 SKU 冷却 14 天才进 CRO 标题队列（查 `cro_title_rewrite` 队列表） |
| 供应商中途改源 | 每 SKU 处理前现刷源（3.2 第 1 步天然防住） |
| 回滚需求 | 推送前快照留档 + 回滚脚本是 P0 交付物的一部分 |

## 6. 规模与成本估算

- 队列 ~430 SKU；LLM 仅验证用（事实表已全库缓存，只对改写后内容各提取一次）≈ 430 次 qwen-plus 调用，成本可忽略
- 单 SKU 全流程 ~30-60s（源刷新+两次 postfix 占大头）→ P2 每日 80 条 ≈ 1.5h/天，全量 ~6 个工作日
- 人力：P1 放行一次人工审阅（15 条 before/after，约 20 分钟）

## 7. 依赖与前置

- 交接 #2 条目 1（foldable 仲裁统一）**必须先完成**——否则改写后的 postfix 可能震荡误判（阻塞 P0 验收）
- 每日 11:30 审计稳定出报告（已上线）
- `build_structured_description_from_source` 的 bullets 提取已修碎片问题（2026-07-13 已完成）

## 8. 开工方式

用户说"开工 P0"即启动。届时：
1. 以本文件第 3 节为规格写 `scripts/semantic_rewrite.py`（自研或写交接任务书 #3 externalize）
2. 队列推导命令（开工当日执行，不用陈旧清单）：
   最新 `logs/listing_audit_fix_*.json` → 过滤 `semantic_*` CRITICAL → 排除 unavailable 源 → 按 CRITICAL 数降序 → `logs/semantic_rewrite_queue.txt`
