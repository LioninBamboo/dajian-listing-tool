# SKU Field Edit / 单 SKU 字段编辑器

Operator-only CLI that edits **one SKU** (or an explicit `--skus` list, max 50) on Main, GrovePop, or Aqua. It replaces ad-hoc live-edit scripts.

操作员专用 CLI：编辑 **单个 SKU**（或显式 `--skus` 列表，最多 50）。用于替代临时写库/写 eBay 脚本。

## Safety / 安全约定

- **Dry-run is the default.** No `--apply` means zero DB writes, zero eBay writes, and no backup. The report is still written.
- **默认 dry-run。** 不加 `--apply` 时不写库、不写 eBay、不做备份；仍会写报告。
- `--apply` writes the local `collected_products` row only. `--push-live` is required to touch eBay, and only after a successful DB write + readback.
- `--apply` 只写本地库。要推 eBay 必须同时给 `--push-live`，且发生在库写成功并回读通过之后。
- Automatic online SQLite backup before the first write.
- 第一次写入前做在线 SQLite 备份。
- Text edits are **deletion-only**. There is no `--allow-add` in v1.
- 文本编辑只允许删除。v1 没有 `--allow-add`。
- Category edits require `--allow-category` plus the high-fee / leaf / lock / required-aspect gates.
- 改类目必须 `--allow-category`，并经过高费、叶子、锁定、目标必填项门禁。
- Quantity, price, status, listing_id, images, source GIGA fields are never editable.
- 数量、价格、状态、listing_id、图片、GIGA 源字段一律不可编辑。
- Never changes `status`. In particular it never flips ERROR→READY (that would auto-publish).
- **绝不改 `status`。** 尤其不会把 ERROR 改成 READY（调度器会自动刊登）。

## Usage / 用法

```bash
# Dry-run claim strip (Main)
python scripts/sku_field_edit.py --store main --sku W465P341531 \
  --strip-claim plywood --location features

# Apply locally, do not push eBay
python scripts/sku_field_edit.py --store main --sku W465140910 \
  --set-category 303 --allow-category --apply \
  --note "Sergey 10/24 approved category remap"

# Apply + production live push
python scripts/sku_field_edit.py --store main --sku W465P341531 \
  --strip-claim plywood --location features \
  --apply --push-live --note "claim scrub"
```

`--store` must match the loaded profile (`main` = AquaVerve inventory, `gp` = GrovePop inventory, `aqua` = AquaRides trading site 100). The tool never writes `.env` or `config/store_profile*.yaml`.

`--store` 必须与已加载的店铺档案一致。工具从不改 `.env` 或店铺 yaml。

## 10/24 replays / 10/24 回放

These are the three production rows the operator bot edited with throwaway scripts. Re-run them in dry-run against a copy of the Main DB; after the original edits they should report `already_absent` / category already 303.

这是 10/24 用临时脚本改过的三行。合并前用 Main 库副本 dry-run；原改动已落地时应显示 `already_absent` / 类目已是 303。

| SKU | What happened / 当时做了什么 | Replay |
|---|---|---|
| `W714P323654` | Removed `Wood` from Frame Material (sofa, cat 38208) | `--remove-aspect-value "Frame Material=Wood"` |
| `W465140910` | Removed Features bullet claiming `concrete`; then 20487 → 303 and published | `--strip-claim concrete --location features` then `--set-category 303 --allow-category` |
| `W465P341531` | Removed Features bullet claiming `plywood` (hand truck, cat 184155) | `--strip-claim plywood --location features` |

## Exit codes / 退出码

| Code | Meaning |
|---|---|
| 0 | Done; dry-run plan is clean |
| 2 | Gate refusal; nothing written |
| 3 | Write failed and was rolled back |
| 4 | Rollback failed (`RESTORE_FAILED`) |
| 5 | Drift between snapshot and write |

## What it will never do / 明确不做

- Create, publish, end, withdraw, or delete listings
- Call `update_offer_category`, `create_or_replace_inventory_item`, or `EbayTradingClient.revise_item`
- Resolve or write quantity
- Register itself in `scheduler_daemon.py`
- Touch `start.bat` / `stop.bat` / Task Scheduler / the Windows scheduler mutex
