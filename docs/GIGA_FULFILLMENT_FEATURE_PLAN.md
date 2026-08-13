# GIGA 履约 API — 可开发功能计划（本 eBay 仓库）

> 基于 `docs/GIGA_FULFILLMENT_API.md`
> 目标：在现有「采集 → 刊登 → 改价 → 库存同步」之上，补齐 **eBay 出单 → GIGA 履约 → 运单回写** 闭环。
> 说明：Wayfair 也可用同一套 GIGA 接口，但本计划默认 **eBay 优先**；Wayfair 作为并行渠道备注。

---

## 1. 能力地图

| API | eBay 能做什么 | 优先级 |
|-----|----------------|--------|
| 仓库地址查询 | 把库存分布里的 `warehouseCode` 解析成可展示/可对账的发货地址；校验 eBay item location | P1 |
| 订单导入-一件代发 | eBay 订单自动/半自动推到 GIGA 代发 | **P0** |
| 订单状态查询 | 履约看板、可取消判断、卡单告警 | **P0** |
| 发货物流查询 | 取 tracking + carrier，回写 eBay 完成发货 | **P0** |
| 上门取货上传 label | 自有面单场景（少数） | P2 |

---

## 2. 端到端主流程（建议做成「履约管线」）

```text
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ eBay 订单   │────▶│ 映射 + 校验      │────▶│ GIGA dropship   │
│ Fulfillment │     │ SKU/地址/渠道    │     │ dropShip-sync   │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                      │
                      ┌──────────────────┐            │
                      │ 状态轮询         │◀───────────┘
                      │ order/status     │
                      └────────┬─────────┘
                               │ Completed / 有运单
                      ┌────────▼─────────┐
                      │ track-no 查运单  │
                      └────────┬─────────┘
                               │
                      ┌────────▼─────────┐
                      │ eBay 标记已发货  │
                      │ + tracking 回写  │
                      └──────────────────┘
```

**订单号策略（务必先定）**

- GIGA `orderNo` 必须全局唯一、符合字母数字规则。
- 建议：`EB{ebayOrderId末段}` 或 `EB{legacyOrderId}`，落库映射表防重复推送。

**SKU 映射**

- eBay Custom SKU / Inventory SKU 应等于 GIGA item code（本仓库现状基本如此）。
- 推单前：`get_inventory` 确认有货；多仓时按策略选仓（或交给 GIGA 默认）。

---

## 3. 分阶段交付

### Phase 0 — 客户端与契约（0.5–1 天）

- [x] `DaJianClient` 增加 5 个方法 + 单测（mock 签名与限流切片）
  - `query_warehouse_addresses` / `query_order_tracking` / `query_order_status`
  - `import_dropship_order` / `import_pickup_label_order`（支持 `dry_run=`）
  - **一件代发不要求 warehouse**：GIGA 选仓发货
- [x] CLI：`scripts/giga_fulfillment_dry_run.py`（warehouse/status/track 实查；dropship-sample 只校验）
- [x] 本地表：`giga_fulfillment_orders`
  - ebay_order_id, giga_order_no, status, last_tracking, payload_json, error, timestamps
- [x] 配置：`ENABLE_GIGA_DROPSHIP_PUSH=1`、`ENABLE_GIGA_EBAY_FULFILL=1`；未显式开启时 dry-run

**验收**：用 sandbox/生产只读接口跑通「仓地址 + 状态/物流查询」（有历史单时）。

### Phase 1 — 半自动一件代发（eBay 核心，2–4 天）

- [x] 从 eBay Fulfillment API 拉 `NOT_STARTED` / `IN_PROGRESS` 且 `PAID` 订单
- [x] CLI：`scripts/giga_dropship_push.py --days N --dry-run` / `--order …` / `--apply`
- [x] 组装 `dropShip-sync` payload（`src/services/giga_dropship.py`，**无 warehouseCode**）
- [x] 本地表 `giga_fulfillment_orders`（dry_run_ready / pushed / push_failed…）
- [x] 真推可走 scheduler `task_giga_dropship_push`（默认 dry-run；仅 `ENABLE_GIGA_DROPSHIP_PUSH=1` 时 `--apply`）
- [x] 日报：财务摘要含未推 GIGA / 履约阶段（`render_finance_email_html`）

**验收**：`python scripts/giga_dropship_push.py --days 7 --dry-run` 能列出候选并写出 plan JSON。

### Phase 2 — 运单自动回写 eBay（2–3 天）

- [x] 服务：`src/services/giga_dropship_sync.py`
  - 对本地 `pushed` / `still_processing` / `fulfill_failed` 行查 GIGA `status` + `track-no`
  - 有运单 → `POST /sell/fulfillment/v1/order/{id}/shipping_fulfillment`
  - 已 FULFILLED 幂等标 `shipped`；无运单 → `still_processing`
- [x] CLI：`scripts/giga_dropship_sync.py --dry-run` / `--apply` / `--order`
- [x] 单测：`tests/test_giga_dropship_sync.py`
- [x] 挂入 `scheduler_daemon`：09:18 push / 09:22 sync / 每 4h 闭环；仅 `ENABLE_GIGA_EBAY_FULFILL=1` 时 apply
- [ ] 异常：长时间 Being Processed 邮件告警


**验收**：对已 `pushed` 且 GIGA 有 tracking 的单，`--apply` 后 eBay 显示已发货+单号。

### Phase 3 — 仓地址与体验增强（1–2 天）

- [ ] 库存同步结果附带 warehouse 地址缓存
- [ ] Streamlit「履约」页：待推送 / 处理中 / 已发货 / 失败
- [ ] 与现有库存同步、幽灵缺货恢复联动（无货不推单）

### Phase 4 — 上门取货 Label（可选，P2）

- [ ] 仅当业务需要「自有面单」时做
- [ ] 上传 base64 label；LTL 支持 BOL
- [ ] 与 Phase 1 共用状态机，仅 import 入口不同

---

## 4. 可单独上线的小功能（不必等大闭环）

| 功能 | 依赖 API | 价值 |
|------|-----------|------|
| 仓库字典缓存 | 仓地址 | 运营看懂 CA3/TX… 是哪 |
| 履约状态看板 | 状态 + 物流 | 替代人工刷 GIGA 后台 |
| 出单后「一键推 GIGA」按钮 | 代发 | 立刻省人工录入 |
| 运单到了自动邮件/站内提示 | 物流 | 客服响应快 |
| 取消窗口提示 | 状态 canCancel | 避免不可取消还去撤 |

---

## 5. 风险与规则（实现必守）

1. **幂等**：同一 eBay order 禁止重复 `orderNo` 推送（B11004 重复）。
2. **地址**：禁 PO Box（B11002）；DE/UK 电话长度。
3. **库存**：GIGA 无货时不要推；与现有 ghost/库存同步一致。
4. **限流**：批量查状态/运单按 100 切片 + 全局限速。
5. **密钥**：继续用现有 `DAJIAN_API_KEY/SECRET`，勿写入仓库。
6. **成本**：推单不改售价逻辑；采购成本仍走 `PricingEngine`（GIGA 订单价）。
7. **加拿大大额 Buyer**：代发接口不可用 — 配置层屏蔽。

---

## 6. 与现有模块接点

| 现有模块 | 接点 |
|----------|------|
| `src/clients/dajian_client.py` | 扩展履约 HTTP |
| eBay Fulfillment（订单） | 已有 OAuth；补拉单与 mark shipped |
| `inventory_sync` | 推单前库存校验 |
| `daily_tasks` / `scheduler_daemon` | 挂「履约轮询」任务 |
| Streamlit / 邮件 | 履约失败进日报 |

---

## 7. 建议排期（粗）

| 周 | 交付 |
|----|------|
| W1 | Phase 0 客户端 + 表 + dry-run 查状态/物流 |
| W2 | Phase 1 半自动推单（CLI/后台按钮） |
| W3 | Phase 2 自动回写 tracking + 告警 |
| W4 | Phase 3 看板与仓地址；按需 Phase 4 |

---

## 8. 成功标准

- 人工录入 GIGA 代发订单的比例显著下降
- eBay 订单从付款到有 tracking 的中位时间可控、可观测
- 失败单 100% 有错误码与可重试入口
- 不出现「GIGA 无货却已推单」与「有 tracking 未回写 eBay」长期堆积

---

## 9. 非目标（本阶段不做）

- 完整 Wayfair 店铺 ERP（另仓；可复用本文 API 文档）
- GIGA 退货 RMA 全自动
- 替代 GIGA 网页支付/充值流程
