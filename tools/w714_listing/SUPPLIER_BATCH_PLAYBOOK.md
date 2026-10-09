# 单供应商批量刊登 Playbook（收藏闸门）

大建 OpenAPI 的库存 / 价格 / 详情 **只对 Buyer 收藏夹（或国货仓）内 SKU 可用**。  
浏览器插件采集会自动点心形收藏；纯页面采集（如 `w714_page_collect.py`）不会。

## 推荐流水线

1. **Excel 筛选** → 产出 SKU 清单  
2. **收藏闸门（必做）**  
   ```bash
   python scripts/supplier_favorite_api_gate.py --sku-list tools/xxx/skus.txt --require-all --json-out tools/xxx/favorite_gate.json
   ```
   - `readable`：可走 API 库存/价格  
   - `blocked`：未收藏或权限拒绝 → **用插件打开商品页点心形**，再跑闸门  
3. **采集**  
   - 优先：插件采集（自动收藏）  
   - 备选：页面采集（仅在收藏闸门通过后）  
4. **分析 → scrub → audit → dry-run → live**  
   - 刊登数量走 `resolve_publish_quantity`（优先 API 库存，失败再回退本地 `CollectedProduct.stock`）  
5. **库存同步**  
   - API 未知 / Region 限制：**跳过，不清零**（已修）  
   - 明确无货才可归零  

## 与 W714 批次编排器

```bash
python scripts/w714_run_batch.py --batch B1 --require-favorite-api
```

`--require-favorite-api` 会在 collect 前跑硬闸门；任一 SKU blocked 则整批中止。

## 没有「加收藏」官方 API

收藏只能：插件心形、或人工在 GIGA 页面点收藏。闸门脚本只做 **可读性校验**，不会代你收藏。
