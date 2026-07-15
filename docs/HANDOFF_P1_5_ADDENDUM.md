# 交接任务书 #4 附录（P1.5）：金丝雀暴露的三个管线缺口修复 + 金丝雀重跑

> 背景：P1 验收结论 = **执行纪律通过，管线放行 P2 被否**。金丝雀 15 条：3 DONE（验收合格）、2 ROLLED_BACK（恢复完整）、4 apply_failed（其中 W5568P478805 曾部分写入，已由交接方恢复并补 Upholstery Fabric=Polyester）、6 needs_human。
> 本附录是 P2 的前置。完成后重新硬停，等验收。
> 纪律、红线、授权边界与任务书 #4 完全一致。允许触碰：`src/services/semantic_rewrite.py`、`scripts/semantic_rewrite*.py` + 新增测试（依旧禁改宿主文件与既有断言）。

## 缺口 1：标题重建 ↔ 类目逻辑互动（ROLLED_BACK ×2 的根因）

新标题让 audit 的类目检查（profile/canonicalize）对存量类目产生 category_mismatch（例：胶枪标题去掉 "Craft Tool" 措辞后触发）。
**修法**：plan 阶段对新标题跑与 audit 相同的类目预检（`classify_listing_profile` + `matcher.canonicalize_category`，以现类目为基准）。预检若产生类目更换建议 → **不换标题，保留原标题**（只清理标题内违规词——违规词清理后仍触发则 needs_human）。禁止改类目。
**测试**：用 W465P386389 真实数据回归——plan 产物的标题必须通过类目预检。

## 缺口 2：类目必填 aspects 保护（apply_failed ×4 的根因）

3.3 修正表删除/未补 `Upholstery Fabric` 等类目必填项 → eBay 25002。
**修法**：push 前调用 audit 的 `_fill_missing_required_aspects`（或等价：`matcher._get_category_aspects` 取必填清单）；修正表**不许删必填项**——必填且违规的，替换为源支持值（源 `Upholstery Material`/`Main Material` 映射），无源值可用则 needs_human。
**测试**：mock 必填清单含 Upholstery Fabric，断言修正后 aspects 仍含合法值。

## 缺口 3：apply 部分写入语义（W5568 事故的根因）

`publish_offer` 阶段失败 ≠ 零写入（inventory PUT 已落地）。
**修法**：apply_rewrite 分阶段记录（put_ok / offer_ok / publish_ok）；任一阶段失败即**自动回滚**（用 backup 恢复 inventory+offer 并 republish，恢复时同样过缺口 2 的必填保护），回滚后验证 live==backup；错误信息必须标明失败阶段。
**测试**：mock publish 抛错 → 断言回滚被调用且用的是 before 内容。

## 缺口 4（说明性）：needs_human 触发规则复审

6 条 needs_human 里存在疑似过度保守（如 `Material ← Oxford Fabric` 单值干净映射也进了人工）。
**要求**：在报告中列出你当前全部 needs_human 触发规则；对每条金丝雀 needs_human 给出"规则名 + 是否建议放宽 + 放宽后的守卫"。**本阶段只放宽有把握的**（如：单值、源字段直给、无逗号复合值的 Material 映射可自动），拿不准的保持 human。

## 重跑与硬停

1. 四个缺口修完、测试全绿后，对金丝雀中 12 条非 DONE（2 回滚 + 4 apply_failed + 6 needs_human 中被你放宽的部分）重跑 §2 单 SKU 协议
2. `W5568P478805` 重跑前先确认其 live 基线 = 交接方恢复后状态（title "2-Seater Loveseat Sofa..."，Upholstery Fabric=Polyester 已在）
3. 产出 `logs/semantic_rewrite_canary_round2.md`（同报告格式）+ progress 续写（phase 填 "P1.5"）
4. **硬停等验收。依旧不得抢跑 P2。**
