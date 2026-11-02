# SKU Field Edit / 单 SKU 字段编辑器

Operator-only CLI that edits **one SKU** (or an explicit `--skus` list, max 50) on Main, GrovePop, or Aqua. It replaces ad-hoc live-edit scripts.

操作员专用 CLI：编辑 **单个 SKU**（或显式 `--skus` 列表，最多 50）。用于替代临时写库/写 eBay 脚本。

## Safety / 安全约定

- **Dry-run is the default.** No `--apply` means zero DB writes, zero eBay writes, and no backup. The report is still written.
- **默认 dry-run。** 不加 `--apply` 时不写库、不写 eBay、不做备份；仍会写报告。
- `--apply` writes the local `collected_products` row only. `--push-live` is required to touch eBay, and only after a successful DB write + readback.
- `--apply` 只写本地库。要推 eBay 必须同时给 `--push-live`，且发生在库写成功并回读通过之后。
- Automatic online SQLite backup before the first write. On Main this is a full copy of `ebay_collection.db` (about 200 MB). Keep a short retention window under `deploy_backup/sku_edit_*`.
- 第一次写入前做在线 SQLite 备份。Main 库整库约 200 MB；`deploy_backup/sku_edit_*` 请做短期留存。
- Text edits are **deletion-only**, except the local-draft source-checked write mode below.
- 文本编辑只允许删除。唯一例外是下面的本地草稿来源校验写入（`--set-title` / `--set-aspect`）。
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

# Local-draft source-checked title rewrite (dry-run default)
python scripts/sku_field_edit.py --store main --sku W2615P429613 \
  --set-title 'Wall Mounted Narrow Bathroom Cabinet 12" with 2 Open Shelves, Sintered Stone Top'

# Same write, plus a sourced item specific
python scripts/sku_field_edit.py --store main --sku W2615P429613 \
  --set-title 'Wall Mounted Narrow Bathroom Cabinet 12" with 2 Open Shelves, Sintered Stone Top' \
  --set-aspect "Mounting=Wall Mounted" --apply --note "source-checked draft title"
```

### Local-draft source-checked writes / 本地草稿来源校验写入

`--set-title` and `--set-aspect` replace draft `optimization` fields when the new text is grounded in the stored GIGA source (row `title`, HTML description, attributes, specs). This is the only add/replace path.

`--set-title` / `--set-aspect` 用 GIGA 源文本校验后改草稿 `optimization`。这是唯一允许新增/替换标题或 item specific 的路径。

- **Local drafts only.** Statuses such as READY / COLLECTED / DRAFT are allowed. PUBLISHED, ENDED, or any row with a `listing_id` is refused (`published_not_editable` / `listing_id_present`).
- **仅本地草稿。** READY / COLLECTED / DRAFT 等可写。PUBLISHED、ENDED、或带 `listing_id` 的行一律拒绝。
- **One SKU per invocation.** `--push-live` is refused (`source_write_live_forbidden`); no eBay API calls.
- **每次只改一个 SKU。** 禁止 `--push-live`，不调 eBay。
- Title rules: length ≤ 80, non-empty, no HTML, existing banned-term QC, every content token must appear in source (word boundary, simple plural/stem, unit-aware `12"` / `12 in` / `12-inch`). `in` is inches only at end of text or before punctuation / `x` / a dimension word — not in `N-in-1` / `N in 1` or `12 in White`. Neutral connectors (`with`, `and`, `for`, `of`, `the`, `a`, `in`, `&`, `-`, `,`) are configurable via `SKU_FIELD_EDIT_TITLE_CONNECTORS`; `set` is not a connector. Compound leftovers are allowlisted only (`countertop`/`tabletop`→`top`, `bookshelf`→`shelf`). `Set of N` / `N-Piece` / `N Pack` / `N-Pcs` in a title must match the same count in source (`Set of N` or `N pcs/pieces/pack`); a bare number elsewhere is not evidence. Attribute/spec values `No`/`None`/`False`/`N/A`/empty/`0` do not evidence their key words. Then FactSheet / claim-guard runs; unsourced claims block. FactSheet `unavailable` is reported as `fact_sheet_unavailable` (not `pass`) and blocks `--apply` unless `--allow-factsheet-unavailable`.
- 标题：≤80、非空、无 HTML、走已有违禁词；每个内容词必须在源里（词边界、简单复数/词干、单位等价）。`in` 仅在句末或标点/`x`/尺寸词前视为英寸。连接词可通过环境变量配置（不含 `set`）。复合词仅白名单。套装数量短语必须在源里有对应 count phrase。否定属性值不能给键名当证据。然后再跑 FactSheet / claim-guard；`unavailable` 记 `fact_sheet_unavailable`，`--apply` 默认拦截。
- Aspect `NAME=VALUE` is set only when VALUE matches the same or equivalent source attribute/spec NAME (small synonym map: Material / Frame Material / Main Material, Color / Colour), or the matching dimension for size keys. Free text is not enough for material / color / type. Protected keys refused: Brand, MPN, UPC, EAN, ISBN, Manufacturer Part Number.
- item specific 必须对上同名或等价源字段（材质/颜色/类型不能只靠描述里的闲散词）；尺寸键可对同维字段或换算后的测量。Brand/MPN/UPC/EAN/ISBN 拒绝写入。
- Same safety as the rest of the tool: dry-run default, explicit `--apply`, maintenance / DB lock, online backup + `field_edit_report` JSON, readback, non-zero exit on refusal. Never touches image URLs (GigaB2B `x-cc` / `x-cu` / `x-ct` / `x-cs` stay intact).
- 安全约定与现有模式相同：默认 dry-run、`--apply` 才写、维护锁、备份+JSON 报告、回读、拒绝非 0 退出。绝不改图片 URL。

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
