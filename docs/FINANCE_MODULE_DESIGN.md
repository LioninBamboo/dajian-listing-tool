# 财务板块设计（eBay + GIGA 履约）

> 版本: 2026-08-11
> 目标: 在现有刊登/改价/履约能力上，补齐**可核对、可复盘、可告警**的财务视图。
> 原则: 成本以 **GIGA 订单**为准；收入以 **平台实收**为准；利润一单一结、可追溯。

---

## 0. 你已有什么 / 缺什么

| 已有 | 用途 | 缺口 |
|------|------|------|
| `PricingEngine.calculate_dajian_cost` | GIGA 采购成本（货+运+保险+支付宝） | 未按「真实出库单」锁定成本 |
| `cost_breakdown` / `suggested_price` | SKU 级建议成本与售价 | 不是订单级 PnL |
| eBay Fulfillment 订单 | 成交价、税费、地址 | 未入库为财务事实表 |
| `giga_fulfillment_orders` | 推单/运单状态 | 无 GIGA 实付金额、无 eBay 手续费实收 |
| `platform_fee_profile` | eBay 费率抽象 | 未接到订单结算 |
| 健康检查「潜在亏损」 | 在售价 vs 成本 | 不等于已售订单利润 |

**财务板块要补的核心：订单级损益（Order PnL）+ 日报/看板 + 对账。**

---

## 1. 财务对象模型（建议）

```text
                    ┌─────────────────────┐
                    │  finance_order      │  ← 一单一行（平台成交）
                    │  (ebay_order_id)    │
                    └─────────┬───────────┘
                              │ 1:N
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
     finance_order_line   finance_cost_leg   finance_fee_leg
     (SKU 行)             (GIGA 采购)        (平台/广告/支付)
```

### 1.1 `finance_order`（订单头）

| 字段 | 说明 |
|------|------|
| platform | `ebay` / 预留 `wayfair` |
| platform_order_id | eBay Order Number（如 `13-15010-93245`） |
| order_date / paid_date / shipped_date | 时间轴 |
| currency | USD |
| gross_sales | 商品成交额（line 合计，不含税或分列） |
| shipping_charged | 向买家收的运费 |
| tax_collected | eBay 代收税（通常不计入卖家毛利） |
| seller_credits | 退款/部分退款 |
| status | `paid` / `shipped` / `cancelled` / `refunded` |
| giga_order_no | 关联履约单 |
| pnl_status | `estimated` / `locked` / `reconciled` |

### 1.2 `finance_order_line`（订单行）

| 字段 | 说明 |
|------|------|
| sku | GIGA / eBay SKU |
| ebay_item_number | Item Number（`legacyItemId`） |
| ebay_transaction_id | Transaction ID（`lineItemId`） |
| qty | 数量 |
| unit_sell_price | 卖家侧成交单价 |
| line_gross | 行销售额 |
| unit_giga_cost | **该行锁定**的 GIGA 单位全成本 |
| line_cogs | qty × unit_giga_cost |
| estimated_ebay_fee | 预估 FVF+广告分摊+固定费分摊 |
| estimated_net | 预估净利 |

### 1.3 成本腿（COGS）— 必须以 GIGA 订单为准

```text
GIGA 订单基数 = 商品价 + 运费          ← 不是 eBay 售价
+ 退货保险 2%
+ 物流保险 3.2% / 5%
+ 支付宝 0.83%（付 GIGA）
= unit_giga_cost（已在 PricingEngine）
```

**锁定规则（重要）**

| 时机 | 成本来源 |
|------|----------|
| 刊登/在售 | `collected_products.cost_breakdown`（可刷新） |
| **订单 paid** | 快照一份 `unit_giga_cost` 写入 line，**之后不随改价变动** |
| GIGA 实付到账后 | 若有真实扣款明细，覆盖为 `locked` 实成本 |

### 1.4 费用腿（Fees）

| 类型 | eBay 预估 | 以后实收 |
|------|-----------|----------|
| 成交费 FVF | 13.25% × 折扣后成交额 | eBay 月结/CSV |
| 店铺折扣影响 | 买家 5% 折扣下的净收模型 | 同上 |
| 广告 | 默认 5% 或实际 campaign | 推广报告 |
| 固定费 | $0.30 / 单 | 同上 |
| 支付宝 | 已在 COGS | GIGA 账单 |
| Wayfair Net-30 2% | 仅 Wayfair 渠道 | Wayfair 汇款单 |

**第一阶段用「预估费」**即可出日报；第二阶段导入平台结算 CSV 做差异。

---

## 2. 利润公式（统一口径）

### 单行预估净利

```text
line_net ≈ line_gross_after_store_discount
         − ebay_fvf − ad_share − fixed_fee_share
         − line_cogs
```

与现有死线一致的「最差净收」：

```text
worst_net = sell_price × 0.95 × (1 − 0.1325 − ad_rate) − 0.30
profit    = worst_net − unit_giga_cost
```

### 订单预估净利

```text
order_net = Σ line_net − 退款 − 额外运费差
```

### 日/周汇总

```text
GMV          = Σ gross_sales
COGS         = Σ line_cogs
Fees_est     = Σ fees
Net_est      = GMV − COGS − Fees_est
Margin%      = Net_est / GMV
```

---

## 3. 页面结构（Streamlit「💰 财务」）

建议新页：`src/web/pages/finance_dashboard.py`，挂到 `app.py`。

### Tab A — 总览（今日/本周/本月）

- KPI 卡：GMV、预估净利、利润率、订单数、亏损单数
- 趋势：近 30 天 GMV / Net
- 告警条：亏损单、成本缺失、未关联 GIGA 履约

### Tab B — 订单损益明细

| 列 | 内容 |
|----|------|
| 订单号 | eBay Order Number |
| SKU / 标题缩略 | |
| 成交价 | 实卖 |
| GIGA 成本 | 锁定 unit_giga_cost |
| 预估平台费 | |
| 预估净利 | 红/绿 |
| 履约状态 | paid / pushed / shipped |
| Item# / Trans# | 可点开核对缺陷率字段 |

筛选：日期、仅亏损、仅未推 GIGA、按 SKU。

### Tab C — SKU 盈利排行

- 按已售：贡献净利 Top / 亏损 Top
- 在售：当前价 vs 安全底（复用 health / PricingEngine）

### Tab D — 对账中心（Phase 2+）

- 导入 eBay 订单报告 CSV（你已有格式）
- 导入 GIGA 账单 / 扣款（若有）
- 差异：预估费 vs 结算费、预估成本 vs 实付

### Tab E — 费率配置

- 只读展示当前 `PricingEngine` / `PlatformFeeProfile` 常量
- 可编辑：广告默认率、是否按 SKU 关广告（与 repricing_guard 对齐）

---

## 4. 数据管道（怎么灌数据）

```text
① eBay Fulfillment 拉单（已有 giga_dropship.fetch_ebay_orders）
      ↓
② upsert finance_order + lines
      ↓ 成本快照
③ PricingEngine.calculate_dajian_cost(price, shipping) from collected_products
      ↓
④ 关联 giga_fulfillment_orders（giga_order_no / tracking）
      ↓
⑤ 预估 fees → 写 estimated_net
      ↓
⑥ Streamlit / 日报邮件读汇总表
```

**入口建议**

| 入口 | 作用 |
|------|------|
| `scripts/finance_sync_orders.py` | 拉单+算 PnL |
| `scheduler task_finance_sync` | 每日 09:15 + 每 4h 自动刷新 |
| `daily_tasks` 全量末段 | 发日报前再刷一次，保证邮件数字新 |
| `giga_dropship_push` 成功时 | 回写 `giga_order_no` 到 finance_order |
| `giga_dropship_sync` 发货时 | 更新 shipped / tracking_json |
| 可选：导入 eBay Orders CSV | 与 API 互补（缺字段时） |

---

## 5. 与现有模块接点（不重复造轮）

| 模块 | 财务用法 |
|------|----------|
| `PricingEngine` | 唯一 COGS 公式 |
| `platform_fee_profile` | 多平台费率扩展 |
| `giga_dropship` | 订单/Item/Transaction 真源 |
| `sales_health_check` | 在售亏损预警（事前） |
| finance 订单 PnL | 已售盈亏（事后） |
| `pricing_history` | 售价轨迹，辅助解释「为何这单赚/亏」 |

---

## 6. 分阶段交付

### Phase F0 — 最小可用（约 2–3 天）

- [x] 表：`finance_orders` / `finance_order_lines`（`src/services/finance_orders.py`）
- [x] `scripts/finance_sync_orders.py --days 30`：从 eBay 拉单 + 快照成本 + 预估净利
- [x] Streamlit：`💰 财务`（`src/web/pages/finance_dashboard.py`）
- [x] 亏损单筛选 + 红字提示；Item# / Trans# 行明细

**验收**：`python scripts/finance_sync_orders.py --days 30` 后，Streamlit「💰 财务」能看到订单 GMV/COGS/净利。

### Phase F1 — 履约联动（约 1–2 天）

- [x] 与 `giga_fulfillment_orders` 关联（`refresh_giga_links` + query LEFT JOIN）
- [x] 状态：未推 GIGA / 已推未付 / 处理中 / 已发货 / 异常（`classify_giga_fulfill_stage`）
- [x] 日报邮件一节「财务摘要」（`render_finance_email_html` → `daily_tasks.send_daily_summary_email`）
- [x] Streamlit「💰 财务」展示 GIGA 阶段 KPI + 筛选
- [x] 总览时间切片：今日 / 近7天 / 近30天 / 本月 / 全部（`resolve_finance_date_range`）
- [x] 独立 KPI「利润率(估)」；总览仅 PAID；退款单数/原 GMV 只读提示



### Phase F2 — 对账（约 3–5 天）

- [ ] 导入 eBay Orders CSV / 费用报告
- [ ] 预估 vs 实际差异报表
- [ ] 退款单冲减

### Phase F3 — 经营分析（可选）

- [ ] 按类目/仓库/广告率的利润贡献
- [ ] 「若统一提到安全底」模拟提价收益
- [ ] 导出会计用 CSV（按月）

---

## 7. 界面草图（订单行）

```text
订单 13-15010-93245 | PAID | 未发货
────────────────────────────────────────────
SKU W5635P502449
Item# 366518074803 | Trans# 10085330527613
成交 $173.47
GIGA 成本(估) $xxx.xx  (货+运+保险+支付宝)
eBay 费(估)   $yy.yy
预估净利      $zz.zz   ← 绿/红
GIGA 单号     13-15010-93245 (或历史 R2)
```

---

## 8. 风险与规则

1. **售价变动不影响已售成本快照**（避免改价后历史利润被改写）。
2. **税**：eBay Collect and Remit 默认不计入卖家收入。
3. **退款**：全额退 → 净利归零并标 refunded；部分退按比例冲。
4. **多行订单**：每行独立 COGS；Transaction ID 每行不同（API 订单级字段用主行或拆单策略需固定文档）。
5. **无权当会计系统**：先做经营利润，不做完整总账/发票。

---

## 9. 建议你拍板的 3 个选择

| # | 问题 | 推荐默认 |
|---|------|----------|
| 1 | 第一期是否只要 **eBay**（Wayfair 预留）？ | 是，只做 eBay |
| 2 | 广告费用 **固定 5%** 还是尽量读真实 campaign？ | 先固定 5%，看板标注「估」 |
| 3 | 财务页优先 **Streamlit** 还是只要 **日报邮件**？ | Streamlit + 日报各一截 |

---

## 10. 下一步（若你说「按这个做」）

按 **Phase F0** 开工：

1. 建表 + `finance_sync_orders.py`
2. 复用 `fetch_ebay_orders` + `PricingEngine`
3. Streamlit「💰 财务」订单明细 + KPI

不做大而全 ERP，先让你每天能回答：

> 今天卖了多少、赚了多少、哪几单在亏、哪几单还没推 GIGA。
