# GIGA / 大建 Open API — 履约与订单接口（沉淀文档）

> 最后更新: 2026-08-11
> 来源: GigaCloud 官方接口文档 PDF（2025-08 起新增履约能力）
> - 仓库地址查询
> - 发货物流查询
> - 订单状态查询
> - 订单导入-一件代发
> - 订单导入-上门取货上传 label
>
> 认证、签名、公共请求头与现有 `docs/DAJIAN_API_REFERENCE.md` / `src/clients/dajian_client.py` **同一套**。
> 生产域名: `https://openapi.gigab2b.com`

---

## 0. 与本仓库的关系

| 现状 | 说明 |
|------|------|
| 已实现 | 产品列表/详情/价格/库存（`DaJianClient`） |
| **未实现** | 本文 5 个履约接口 |
| eBay 价值 | 把 eBay 成交单自动推到 GIGA 一件代发，再回写运单号到 eBay |
| Wayfair 价值 | 文档示例渠道含 Wayfair；同一套 API 也可服务 Wayfair 店（独立仓库另做） |

**通用约定**

- Method: `POST`，Body: JSON
- 公共头: `Content-Type`, `timestamp`, `nonce`, `sign`, `client-id`
- 公共响应: `success`, `code`, `data`, `requestId`, `msg`, `subMsg`, `recommend`
- 通用错误码: `200 / 401 / 404 / 500 / 400001…400010`（签名、限流、时间戳等）
- 限流（查询类）: **10 秒内 20 次**（订单导入接口文档写「暂无」单独限流说明）

---

## 1. 仓库地址查询

| 项 | 值 |
|----|-----|
| 名称 | 仓库地址查询 |
| 路径 | `/b2b-overseas-api/v1/buyer/warehouse/query-address/v1` |
| 场景 | 用 `warehouseCode` 查 GIGA 仓库完整地址（库存接口可拿到 code） |
| 限流 | 10 秒 / 20 次 |

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| warehouseCodes | string[] | 是 | B2B 仓库 code，单次最多 **200** |

```json
{ "warehouseCodes": ["CA3"] }
```

### 响应 data[]

| 字段 | 说明 |
|------|------|
| warehouseCode | 仓库 code |
| address | 街道地址 |
| country / state / city / zipCode | 国家/州/城市/邮编 |

### 示例

```json
{
  "success": true,
  "code": "200",
  "data": [{
    "warehouseCode": "CA3",
    "address": "10850 Business Dr",
    "country": "US",
    "state": "CA",
    "city": "Fontana",
    "zipCode": "92337"
  }]
}
```

### 业务错误

| code | 含义 |
|------|------|
| B52004 | 仓库 code 错误 / 数据不存在 |
| B52002 | 单次超过 200 个 code |

---

## 2. 发货物流查询

| 项 | 值 |
|----|-----|
| 名称 | 发货物流查询 |
| 路径 | `/b2b-overseas-api/v1/buyer/order/track-no/v1` |
| 场景 | 按 **发货订单号** 查包裹运单、承运商、发货仓；德国站还可查退货运单 |
| 限流 | 10 秒 / 20 次 |
| 迭代 | 2026-08-06 增加 `shipFromInfo`（发货国家 + 仓库） |

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| orderNo | string[] | 是 | 发货订单号，单次最多 **100** |

```json
{ "orderNo": ["DSR202507241513"] }
```

### 响应 data[] 要点

| 字段 | 说明 |
|------|------|
| orderNo | 发货订单号 |
| shipTrackInfo[] | 正向运单 |
| └ sku / skuQty | SKU 与数量（combo 时 sku 可能是子 SKU） |
| └ isCombo / comboSku | 是否组合品、父 SKU |
| └ trackingNum / carrierName | 运单号、承运商（如 FedEx） |
| └ shipFromInfo.country / warehouseCode | 发货国、发货仓 |
| returnTrackInfo[] | 德国站退货运单（一件代发且买了 Return Label 时才有） |

### 业务错误

| code | 含义 |
|------|------|
| B40001 | orderNo 为空 |
| B40004 | orderNo 不存在 |
| B40002 | 单次超过 100 个 |

---

## 3. 订单状态查询

| 项 | 值 |
|----|-----|
| 名称 | 订单状态查询 |
| 路径 | `/b2b-overseas-api/v1/buyer/order/status/v1` |
| 场景 | 按发货订单号查状态、是否可取消 |
| 限流 | 10 秒 / 20 次 |

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| orderNo | string[] | 是 | 发货订单号集合 |

```json
{ "orderNo": ["SA250802XT0010"] }
```

### 响应 data[]

| 字段 | 说明 |
|------|------|
| orderNo | 发货订单号 |
| orderStatus | 状态码字符串（见下表） |
| canCancel | `"0"` 不可取消 / `"1"` 可取消 |

### orderStatus 枚举（文档说明）

| 值 | 含义（摘要） |
|----|----------------|
| 1 | Unpaid 未支付 |
| 2 | Being Processed 处理中 |
| 4 | On Hold 暂停 |
| 10 | Cancelled 已取消 |
| 20 | Completed 已完成 |
| 100 | Check (Shipping Label) 查验面单相关 |
| 150 | Package Label Pending 等面单 |
| 207 | Awaiting Pickup 待取货 |
| 208 | Box Pending 待装箱等 |
| 210 | Cancelled/Other | 其他取消类 |

> 实现时以接口实际返回为准，客户端应做 **未知状态透传 + 日志**，不要写死只认 1/2/20。

### 业务错误

| code | 含义 |
|------|------|
| B10001 | orderNo 为空 |
| B10004 | 订单不存在 |
| B10002 | 单次超过 100 个 |

---

## 4. 订单导入 — 一件代发（Dropship）

| 项 | 值 |
|----|-----|
| 名称 | 订单导入-一件代发 |
| 路径 | `/b2b-overseas-api/v1/buyer/order/dropShip-sync/v1` |
| 场景 | 把**第三方平台成交单**同步到 GIGA 一件代发 |
| 限制 | 加拿大大额 Buyer 暂无法使用；限流文档写「暂无」 |

### 关键必填字段

| 字段 | 说明 |
|------|------|
| orderDate | 订单日期 |
| orderNo | 发货订单号（字母数字 `._-`，唯一） |
| shipName / shipPhone / shipAddress1 / shipCity / shipCountry / shipZipCode | 收件人 |
| orderLines[] | 明细：itemPrice, qty, sku（GIGA item code） |
| orderTotal | 订单总金额（常用） |

### eBay 相关字段（必须与 eBay 模板一致）

GIGA 美国站 eBay 订单模板（`eBayOrderTemplateUS`）要求：

| 模板列 | GIGA API 字段 | eBay 来源（Fulfillment / Orders 报告） |
|--------|---------------|----------------------------------------|
| `*eBayItemNumber` | `orderLines[].ebayItemCode` | **Item Number** = `legacyItemId` |
| `*eBayTransactionID` | `ebayTransactionID` | **Transaction ID** = `lineItemId` |
| `*OrderId` | （我方映射用） | **Order Number** = `orderId` |
| `*B2BItemCode` | `orderLines[].sku` | Custom Label / Inventory SKU |

> **禁止**用 Order Number 去尾数字当 Transaction ID。
> 错误填写会导致 eBay 无法识别订单，出问题后**无法享受订单缺陷率豁免**。

### 其他重要字段

| 字段 | 说明 |
|------|------|
| hasOtherLabel | 是否有品牌标/Packing Slip 外的贴标要求 |
| valueAddedServices.returnLabelService | 是否买退货面单（德国等） |
| valueAddedServices.deliveryService | 美仓配送类型：普通 `DSR`；LTL 如 `NSR,TRHD,ROC,WG` |
| currencyCode | USD/CAD/GBP/EUR/JPY 等 |
| shipState | 美/德/英等对省州必填规则不同 |
| shipPhone | DE/UK 等对长度/数字有额外规则 |

### 响应

成功时 `data` 多为 null，靠 `success` + `code=200` 判断；业务错误以 `B11xxx` 为主（字段校验、PO Box 禁止、SKU 不存在、orderNo 重复等）。

### 请求体结构（精简）

```json
{
  "orderDate": "2025-07-30 12:00:00",
  "orderNo": "DS250731XT025",
  "shipName": "tom",
  "shipPhone": "12432542",
  "shipEmail": "TOM@163.com",
  "shipAddress1": "2508 APRIL",
  "shipAddress2": "OK_P",
  "shipCity": "CORRY",
  "shipCountry": "US",
  "shipState": "PA",
  "shipZipCode": "16407",
  "hasOtherLabel": true,
  "orderFrom": "TB store",
  "salesChannel": "eBay",
  "ebayTransactionID": "1234567890123",
  "orderLines": [{
    "itemPrice": 100,
    "qty": 1,
    "sku": "W10172P199674",
    "productName": "red bed",
    "ebayItemCode": "366547297749",
    "currencyCode": "USD"
  }],
  "orderTotal": 200,
  "valueAddedServices": {
    "returnLabelService": false,
    "deliveryService": "DSR"
  }
}
```

---

## 5. 订单导入 — 上门取货上传 Label

| 项 | 值 |
|----|-----|
| 名称 | 订单导入-上门取货上传 label |
| 路径 | `/b2b-overseas-api/v1/buyer/order/pickUpSellLabel-sync/v1` |
| 场景 | **买方自付运费**，已有物流 Label，把订单 + Label 同步到 GIGA；**仅美国站 Buyer** |
| 与一件代发区别 | 必须带 `shipMethod`、上传 `labelFile`（面单）；LTL 还要仓 code / BOL |

### 关键必填（相对一件代发）

| 字段 | 说明 |
|------|------|
| shipMethod | 承运商枚举：FedEx, UPS, GOFO Express, GOFO Ground, OnTrac, Amazon Shipping, USPS, LTL, ATS, UniUni 等 |
| salesChannel | Amazon, Wayfair, Walmart, Overstock, Home Depot, Lowe's, Other… |
| orderLines[].sku / qty | 同代发 |
| labelFile | **base64 面单**；快递必填；数量应与明细一致 |
| warehouseCode | 指定仓；LTL 等场景更关键 |
| bolFile | LTL + 非亚马逊时常见必填（装运提单 base64） |
| packingSlip | Home Depot 等装箱单 base64 |
| requiredShipDate | 预约发货日 `YYYY-MM-DD`（部分渠道） |

### 典型错误

| code | 含义 |
|------|------|
| B11001 | 缺 orderNo/orderDate/shipMethod/salesChannel/sku/qty/**labelFile** 等 |
| B11002 | label 解析失败、数量与明细不符、qty 超范围等 |

---

## 6. 两条履约路径对比（eBay 视角）

```text
路径 A — 一件代发（推荐主路径）
  eBay 出单
    → dropShip-sync（GIGA 出库、GIGA 物流）
    → order/status 轮询
    → track-no 取运单
    → eBay Fulfillment 回写 tracking

路径 B — 上门取货 + 自有 Label
  eBay 出单 + 卖家自行买面单
    → pickUpSellLabel-sync（上传 labelFile）
    → GIGA 仓按 label 发货
    → status / track 监控
```

当前本仓库策略是 **GIGA 美国仓采购 + 一件代发**，应优先做 **路径 A**。路径 B 适合少数「自带 UPS/FedEx 账号」场景。

---

## 7. 建议的客户端模块划分

在 `src/clients/dajian_client.py`（或新 `giga_fulfillment_client.py`）增加：

```text
query_warehouse_addresses(warehouse_codes: list[str]) -> list[dict]
query_order_tracking(order_nos: list[str]) -> list[dict]
query_order_status(order_nos: list[str]) -> list[dict]
import_dropship_order(payload: dict) -> dict
import_pickup_label_order(payload: dict) -> dict
```

约束：

- 批量切片：仓 200 / 运单与状态 100
- 尊重 10s/20 次限流
- 签名复用现有 `_generate_signature`
- 所有业务错误码写入结构化日志，便于日报

---

## 8. 源 PDF 与提取物

| 源文件 | 本地提取图（可选） |
|--------|-------------------|
| `仓库地址查询.pdf` | `docs/giga_api_extract/pages/` |
| `发货物流查询.pdf` | 同上 |
| `订单状态查询.pdf` | 同上 |
| `订单导入-一件代发.pdf` | 同上 |
| `订单导入-上门取货上传label.pdf` | 同上 |

官方总入口仍见: https://www.gigab2b.com/index.php?route=information/open_api
若线上字段与本文冲突，**以 openapi 现网响应与最新官方文档为准**。
