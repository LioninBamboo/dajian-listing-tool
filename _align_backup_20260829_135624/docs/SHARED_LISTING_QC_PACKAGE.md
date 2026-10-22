# 三店审计脚本共享化（中期）

## 为什么要做

当前 `scripts/audit_fix_active_listings.py` 在主店 / GrovePop / AutoParts 三仓各有一份拷贝。
今天已经出现过：主店先修好邮件分栏 / post_fix / category manifest，子店仍跑旧逻辑；
店名标题、F150 别名、Toyota 模型表等补丁也容易漏同步。

共享化不是“立刻 monorepo”，而是先把**真共享且变更频繁**的审计内核抽出去，减少漂移。

## 建议形态（务实）

1. **新建共享包目录**（任选其一，推荐 A）
   - A. 独立小仓 `dajian-listing-qc`（pip editable install 到三店 `.venv`）
   - B. 三店各自 `vendor/listing_qc/` 用 git subtree / 定期同步脚本（更轻，但易忘）
2. **先抽这些模块**（高价值、低店铺耦合）
   - residual / fix-key 白名单常量
   - email subject/heading 的 store label 拼装
   - FactSheet / ClaimDiff 校验与 semantic rebuild 闸门
   - Motors fitment QC（`fitment_qc.py`）— 仅 AutoParts 强依赖，但也应单源
3. **留在各店的部分**
   - `store_profile` / 政策 ID / mutex / 计划任务名
   - scheduler 日程与 `--limit` 运维参数
   - Motors Trading 通道专用脚本入口

## 落地顺序

1. 把 `SCHEDULED_SOURCE_ASPECT_AUTOFIX_*`、`_residual_issues_for_item`、`_store_email_label` 抽到共享包并加单测
2. 三店 `audit_fix_active_listings.py` 改为 `from listing_qc import ...`
3. CI：任一店改共享包 API 时，三店跑同一组 contract tests
4. 再考虑描述生成 / publish gate 等更大块迁移

## 红线

- 共享包**不得**默认带裸 `--fix`
- `categoryId` 永不进入自动写白名单
- 店名必须来自各店 `get_store_profile().brand_name`，禁止写死 `eBay/GIGA`
