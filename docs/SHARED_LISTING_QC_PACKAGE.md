# 三店审计脚本共享化

## 落地形态（已选）

主店仓内包：`packages/listing_qc`（非独立仓）。三店 `.venv` 通过 editable 安装：

```text
# Dajian / AquaVerve
pip install -e ./packages/listing_qc

# GrovePop / AutoParts（相对兄弟目录）
pip install -e ../Dajian_Listing_Tool/packages/listing_qc
```

`requirements.txt` 已写入对应 `-e` 行。

## 第一刀已迁入

| 符号 | 包内模块 |
|------|----------|
| `SCHEDULED_SOURCE_ASPECT_AUTOFIX_*` / video autofix 常量 | `listing_qc.autofix_whitelist` |
| residual / fix-key 匹配 / actionable severities | `listing_qc.residual` |
| 邮件店名标签（注入 `brand_name`，不读各店 yaml） | `listing_qc.store_label` |
| APC 类目自动 Yes（排除 20518 gabion） | `listing_qc.assembly_category_policy` |

三店 `scripts/audit_fix_active_listings.py` 与主店/GrovePop `scheduler_daemon.py` 已改为 `from listing_qc import ...`。

## Contract tests

```bash
# 任一店 venv 在安装 -e 后：
pytest packages/listing_qc/tests/test_contract_listing_qc.py
# 或从子店：
pytest ../Dajian_Listing_Tool/packages/listing_qc/tests/test_contract_listing_qc.py
```

## 第二刀（未迁）

FactSheet 全量、描述生成、`fitment_qc` — 避免一次过大。

## 红线

- 共享包**不得**默认带裸 `--fix`
- `categoryId` 永不进入自动写白名单
- 店名必须来自各店 `get_store_profile().brand_name` 注入，禁止写死店名；fallback 仅 `eBay/GIGA`
- APC 自动 Yes 是专用小任务（`--accept-package-conflict-as Yes` + 类目策略），**不**并入 12:10 全量白名单门控之外的无门控批改
