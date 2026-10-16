# W714 → Wayfair Sofas (class 305) 提取包

筛选：本批《新品推荐表》中 **组合沙发** 且 **在库库存 > 20**，共 **82** 个 SKU。

## 文件

| 文件 | 用途 |
|------|------|
| `wayfair_sofas_305_extract.json` | 完整字段 JSON 数组（可直接给 Wayfair ERP 用） |
| `wayfair_sofas_305_extract_summary.csv` | 快速核对表 |
| `C:\Users\poonx\Aquawood Smart ERP-Wayfair\data\local_review\w714_combo_stock20_sofas_305_extract.json` | 已复制到 Wayfair 项目 |

字段规范对齐：`Aquawood Smart ERP-Wayfair\data\local_review\sofas_305_extract_prompt_portable_20260908.md`

## 关键映射

- GIGA Assembled Length → `wayfair_overall_width_in`
- GIGA Assembled Width → `wayfair_overall_depth_in`
- Height 不变 → `wayfair_overall_height_in`
- 净重 → `product_weight_lbs`；毛重 → `package_weight_lbs`
- `base_cost` = 表格单买折扣价
- `brand` = AquaWood；非皮/`mattress_included=false`/`pieces_included` 按是否含 Ottoman
- `frame_material` / `fill_material` / `cushion_construction` = `null`（页面未声明，禁止编造）

## 复跑

```bash
python scripts/w714_wayfair_sofa_extract.py
```

不影响主店 eBay 分批刊登流水线；VL 缓存与 `tools/w714_listing/cache/vl_*.json` 共用。
