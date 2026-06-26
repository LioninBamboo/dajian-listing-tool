# GigaCloud (大建) Open API 2.0 技术文档

> 最后更新: 2026-02-05  
> 基于官方文档: https://www.gigab2b.com/index.php?route=information/open_api

---

## 1. 概述

GigaCloud B2B 平台提供 Open API 能力，支持 Buyer 和 Seller 通过 API 获取产品、订单、库存等数据。

### 1.1 域名

| 环境 | 域名 |
|------|------|
| **生产环境** | `https://openapi.gigab2b.com` |
| 测试环境 | `https://openapi-sandbox.gigab2b.com` |

### 1.2 认证凭证

在 B2B 平台申请获取：
- `client-id`: API 应用的 Client ID
- `client-secret`: API 应用的 Client Secret

---

## 2. 签名算法

### 2.1 签名规则

1. **构建待签名字符串 (msg)**:
   ```
   msg = clientId + "&" + apiPath + "&" + timestamp + "&" + nonce
   ```

2. **构建签名密钥 (key)**:
   ```
   key = clientId + "&" + clientSecret + "&" + nonce
   ```

3. **计算签名**:
   ```
   sign = base64( hmac_sha256_hex(msg, key) )
   ```
   - 使用 HMAC-SHA256 加密
   - 结果转为 **hex 字符串**（64 字符）
   - 再进行 **Base64 编码**

### 2.2 Python 签名示例

```python
import hmac
import hashlib
import base64
import time

def generate_signature(client_id: str, client_secret: str, api_path: str) -> tuple:
    """生成 API 签名
    
    Returns:
        (timestamp, nonce, sign)
    """
    timestamp = str(int(time.time() * 1000))  # 毫秒时间戳
    nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=10))  # 10位随机字符
    
    # 构建签名字符串
    msg = f"{client_id}&{api_path}&{timestamp}&{nonce}"
    key = f"{client_id}&{client_secret}&{nonce}"
    
    # HMAC-SHA256 -> hex -> base64
    hmac_hex = hmac.new(key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
    sign = base64.b64encode(hmac_hex.encode()).decode()
    
    return timestamp, nonce, sign
```

---

## 3. 公共请求头

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| Content-Type | string | 是 | 固定值: `application/json` |
| client-id | string | 是 | API 应用的 Client ID |
| timestamp | string | 是 | 毫秒时间戳 (UTC)，只处理 20 分钟内的请求 |
| nonce | string | 是 | 10 位随机字符串 |
| sign | string | 是 | 签名值 |

---

## 4. 公共响应结构

```json
{
  "success": true,
  "code": "200",
  "data": { ... },
  "requestId": "xxx",
  "msg": "success",
  "subMsg": null,
  "recommend": null
}
```

| 字段 | 类型 | 描述 |
|------|------|------|
| success | boolean | 请求是否成功 |
| code | string | 状态码，200 表示成功 |
| data | object | 响应数据 |
| requestId | string | 请求唯一标识 |
| msg | string | 信息描述 |
| subMsg | string | 二级错误描述 |
| recommend | string | 错误诊断链接 |

---

## 5. Buyer 接口

### 5.1 产品列表查询

**POST** `/b2b-overseas-api/v1/buyer/product/skus/v1`

查询 Buyer 收藏夹内或国货库存的产品列表。

#### 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| page | integer | 否 | 页码，默认 1，最小 1 |
| pageSize | integer | 否 | 每页数量，**最小 100**，最大 1000，默认 5000 |
| sort | integer | 否 | 排序：1=updateTime.asc, 2=updateTime.desc, 3=datePosted.asc, 4=datePosted.desc |
| firstArrivalDate | string | 否 | 首次到库时间，格式: `yyyy-MM-dd` |
| lastUpdatedAfter | string | 否 | 最后更新时间，格式: `yyyy-MM-dd HH:mm:ss` |
| queryTimeType | integer | 否 | 时间类型：1=最后更新时间, 2=收藏时间 |
| startTime | string | 否 | 开始时间，格式: `yyyy-MM-dd HH:mm:ss` |
| endTime | string | 否 | 结束时间，格式: `yyyy-MM-dd HH:mm:ss` |

#### 请求示例

```json
{
  "page": 1,
  "pageSize": 200,
  "sort": 4,
  "queryTimeType": 2,
  "startTime": "2025-07-01 00:00:00",
  "endTime": "2026-01-01 00:00:00"
}
```

#### 响应示例

```json
{
  "success": true,
  "code": "200",
  "data": {
    "pageInfo": {
      "page": 1,
      "totalPage": 20,
      "pageSize": 100,
      "totalNum": 1919
    },
    "records": [
      {
        "sku": "N710P401337K",
        "productName": "55 Inch Mirror Medicine Cabinet...",
        "updateTime": "2025-07-23 18:51:43",
        "firstArrivalDate": "2025-07-23",
        "addedTime": "2025-01-01 00:51:43"
      }
    ]
  },
  "requestId": "17db2e892b68ff9",
  "msg": "success"
}
```

### 5.2 产品详情查询

**POST** `/b2b-overseas-api/v1/buyer/product/detailInfo/v1`

通过 SKU 或产品名称查询产品详情。仅支持查询 Buyer 收藏夹内或国货库存的产品。

- **限流规则**: 10秒内20次
- **更新时间**: 2025-12-16 (toBePublished 改为 skuAvailable)

#### 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| skus | string[] | 否* | SKU列表，最多200个 |
| productNames | string[] | 否* | 产品名称列表，最多200个 |

> *skus 和 productNames 二选一，只能选一个作为入参查询

#### 请求示例

```json
{
  "skus": [
    "W10172S00004",
    "W10172S00020",
    "B215P204169"
  ]
}
```

#### 响应字段

| 字段 | 类型 | 描述 |
|------|------|------|
| sku | string | 平台产品编码 |
| mpn | string | 商家自定义商品编码 |
| weightUnit | string | 单位重量 (lb) |
| lengthUnit | string | 长度单位 (in) |
| weight | number | 重量 |
| length | number | 长度 |
| width | number | 宽度 |
| height | number | 高度 |
| weightKg / lengthCm / widthCm / heightCm | number | 公制单位尺寸 |
| productName | string | 产品名称 |
| description | string | 产品描述 |
| characteristicx | string[] | 产品特点 (1-4) |
| imageUrls | string[] | 产品图片URL列表 |
| mainImageUrl | string | 主图URL |
| categoryCode | integer | 分类代码 |
| category | string | 分类名称 |
| comboFlag | boolean | 是否combo产品 |
| overSizeFlag | boolean | 是否超大尺寸 |
| partFlag | boolean | 是否配件 |
| upc | string | UPC码 |
| customized | string | 是否定制 (Yes/No) |
| placeOfOrigin | string | 产地 |
| lithiumBatteryContained | string | 是否含锂电池 (Yes/No) |
| assembledLength / assembledWidth / assembledHeight | string | 组装尺寸 |
| attributes | object | 产品属性 {key: value} |
| whiteLabel | string | 是否白牌 (Yes/No) |
| comboInfo | array | combo信息 [{weight, length, width, height, sku, qty}] |
| qty | integer | 数量 |
| firstArrivalDate | string | 首次到库时间 (yyyy-MM-dd) |
| sellerInfo | object | 卖家信息 |
| sellerInfo.sellerStore | string | 店铺名 |
| sellerInfo.sellerType | string | 店铺类型 (ONSITE/GENERAL) |
| sellerInfo.gigaIndex | string | 店铺评分 |
| sellerInfo.sellerReturnRate | string | 退退品率 |
| skuAvailable | boolean | 是否可购买 (true=可购买) |

图片 URL 处理注意：

- `imageUrls` 和 `mainImageUrl` 可能包含 GigaB2B 签名 query 参数。
- 项目采集阶段必须原样保存这些 URL。
- 推送 eBay 前只能删除 `x-oss-process` 等图片处理参数，不能删除 `x-cc` / `x-cu` / `x-ct` / `x-cs`。
- 如果本地 `imageUrls` 有多张但 eBay live 只剩 1 张，应优先排查刊登或 inventory revise 的 URL 清洗逻辑。

#### 响应示例

```json
{
  "success": true,
  "code": "200",
  "data": [
    {
      "sku": "W10172S00004",
      "mpn": "W10172S00004",
      "weightUnit": "lb",
      "lengthUnit": "in",
      "weight": null,
      "length": null,
      "width": null,
      "height": null,
      "productName": "test name",
      "description": "",
      "imageUrls": ["https://..."],
      "mainImageUrl": "https://...",
      "categoryCode": 10915,
      "category": "Beds, Frames & Bases",
      "attributes": {
        "Main Color": "Alabaster",
        "Main Material": "ABS+PC"
      },
      "whiteLabel": "No",
      "comboInfo": [
        {
          "weight": 21,
          "length": 21,
          "width": 21,
          "height": 21,
          "sku": "W101722P197236",
          "qty": 2
        }
      ],
      "firstArrivalDate": "2024-11-01",
      "sellerInfo": {
        "sellerStore": "W10172店铺名称xx",
        "sellerType": "GENERAL",
        "gigaIndex": "96.53",
        "sellerReturnRate": "Low"
      },
      "skuAvailable": false
    }
  ],
  "requestId": "f1bda0b69e47175a",
  "msg": "success"
}
```

---

### 5.3 产品价格查询

**POST** `/b2b-overseas-api/v1/buyer/product/price/v1`

通过 SKU 查询产品价格信息。仅支持查询 Buyer 收藏夹内或国货库存的产品。

- **限流规则**: 10秒内10次
- **更新时间**: 2026-01-15 (新增 exclusivePrice, discountedPrice 等字段)

#### 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| skus | string[] | 是 | SKU列表，最多200个 |

#### 请求示例

```json
{
  "skus": [
    "W690035683",
    "TEST-JT-004"
  ]
}
```

#### 响应字段

| 字段 | 类型 | 描述 |
|------|------|------|
| sku | string | 产品编码 |
| currency | string | 货币 (USD) |
| price | number | 产品原价 (有专享价时用 exclusivePrice，有折扣价时用 discountedPrice) |
| shippingFee | number | 运费 |
| shippingFeeRange | object | 运费区间 {minAmount, maxAmount} (LTL产品) |
| exclusivePrice | number | 专享价 |
| discountedPrice | number | 活动折扣价 |
| promotionFrom | string | 促销开始时间 (GMT-8) |
| promotionTo | string | 促销结束时间 (GMT-8) |
| purchaseLimit | string | 活动限购数量 |
| mapPrice | number | 最低零售价 |
| srpPrice | string | 建议零售价 (SRP) |
| futureMapPrice | number | 未来生效的 MAP 价格 |
| effectMapTime | string | MAP 价格生效时间 (GMT-8) |
| sellerInfo | object | 卖家信息 |
| spotPrice | array | 阶梯价 [{minQuantity, maxQuantity, price, discountedSpotPrice}] |
| rebatesPrice | array | 返点价 [{minQuantity, maxQuantity, price, days}] |
| marginPrice | array | 现货协议价 [{minQuantity, maxQuantity, price}] |
| skuAvailable | boolean | 是否可购买 |
| futurePrice | array | 期货协议价 [{deliveryDate, totalQuantityAvailable, depositPercentage, directSettlement, marginTransaction}] |

#### 响应示例

```json
{
  "success": true,
  "code": "200",
  "data": [
    {
      "sku": "W690035683",
      "currency": "USD",
      "price": 34,
      "shippingFee": 19.64,
      "shippingFeeRange": {
        "minAmount": 16.29,
        "maxAmount": 19.64
      },
      "exclusivePrice": null,
      "discountedPrice": null,
      "promotionFrom": null,
      "promotionTo": null,
      "mapPrice": null,
      "srpPrice": null,
      "sellerInfo": {
        "sellerStore": "MOONRIVER",
        "sellerType": "GENERAL",
        "gigaIndex": "75.65"
      },
      "spotPrice": [
        {"minQuantity": 5, "maxQuantity": 20, "price": 31},
        {"minQuantity": 21, "maxQuantity": 50, "price": 29.5}
      ],
      "rebatesPrice": [],
      "marginPrice": [
        {"minQuantity": 5, "maxQuantity": 20, "price": 31}
      ],
      "skuAvailable": true,
      "futurePrice": []
    }
  ],
  "requestId": "5c9a467556787f13",
  "msg": "success"
}
```

---

### 5.4 库存查询

**POST** `/b2b-overseas-api/v1/buyer/inventory/quantity/v2`

通过 SKU 查询 Buyer 和 Seller 的库存信息。支持查询产品促销活动的可购库存、仓租、期货库存等。

- **限流规则**: 10秒内10次
- **更新时间**: 2026-01-10 (新增上门取货 Buyer 支持查询)

#### 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| skus | string[] | 是 | SKU列表，最多200个 |

#### 请求示例

```json
{
  "skus": ["N710P401337K"]
}
```

#### 响应字段

| 字段 | 类型 | 描述 |
|------|------|------|
| sku | string | 产品编码 |
| **buyerInventoryInfo** | object | Buyer 的库存信息 |
| └ totalBuyerAvailableInventory | integer | Buyer可用库存总量 (全款购买且未手动锁定) |
| └ totalMarginInventory | integer | Buyer保证金协议未付尾款库存总量 |
| └ totalFutureInventory | integer | Buyer期货协议未付尾款库存总量 |
| └ totalSystemLockedInventory | integer | 系统锁定库存总量 |
| └ totalBuyerLockedInventory | integer | Buyer手动锁定库存总量 |
| └ buyerInventoryDistribution | array | 按仓库分布 [{warehouseCode, buyerAvailableInventory, marginInventory, systemLockedInventory, buyerLockedInventory}] |
| └ totalStorageFee | number | 截止昨日的仓租 (已支付部分) |
| └ unpaidStorageFee | number | 未支付仓租 |
| └ currency | string | 货币单位 |
| **sellerInventoryInfo** | object | 平台产品库存信息 |
| └ sellerAvailableInventory | integer | 平台可售库存 (最大可购数量) |
| └ discountAvailableInventory | integer | 促销可购库存 |
| └ sellerInventoryDistribution | array | 按仓库分布 [{warehouseCode, availableQtyMin, availableQtyMax}] |
| └ nextArrivalInventory | object | 下次到货 {nextArrivalBegin, nextArrivalEnd, nextArrivalQtyMin, nextArrivalQtyMax} |

#### 响应示例

```json
{
  "success": true,
  "code": "200",
  "data": [
    {
      "sku": "N710P401337K",
      "buyerInventoryInfo": {
        "totalBuyerAvailableInventory": 0,
        "totalMarginInventory": 0,
        "totalFutureInventory": 0,
        "totalSystemLockedInventory": 0,
        "totalBuyerLockedInventory": 0,
        "buyerInventoryDistribution": [
          {
            "warehouseCode": "LA",
            "buyerAvailableInventory": 0,
            "marginInventory": 0,
            "systemLockedInventory": 0,
            "buyerLockedInventory": 0
          }
        ],
        "totalStorageFee": null,
        "unpaidStorageFee": null,
        "currency": "USD"
      },
      "sellerInventoryInfo": {
        "sellerAvailableInventory": 50,
        "discountAvailableInventory": 0,
        "sellerInventoryDistribution": [
          {
            "warehouseCode": "LA",
            "availableQtyMin": 10,
            "availableQtyMax": 50
          }
        ],
        "nextArrivalInventory": {
          "nextArrivalBegin": "2026-03-15",
          "nextArrivalEnd": "2026-03-20",
          "nextArrivalQtyMin": 100,
          "nextArrivalQtyMax": 100
        }
      }
    }
  ],
  "requestId": "abc123",
  "msg": "success"
}
```

---

## 6. 错误码

| 错误码 | 描述 | 解决方案 |
|--------|------|----------|
| 200 | 请求成功 | - |
| 401 | 接口不可用 | 更换可用接口或联系客服 |
| 404 | 路径不存在 | 检查接口路径 |
| 500 | 服务器错误 | 稍后重试 |
| 400001 | 请求频率过高 | 查看限流规则 |
| 400003 | 入参格式或语法错误 | 检查请求参数 |
| 400004 | 无效签名 | 检查签名算法 |
| 400005 | 缺少签名 | 添加 sign 请求头 |
| 400006 | 缺少时间戳 | 添加 timestamp 请求头 |
| 400007 | 无效时间戳 | 检查时间戳格式 |
| 400008 | 请求超时 | 时间戳超过 20 分钟，重新请求 |
| 400009 | 缺少随机值 | 添加 nonce 请求头 |
| 4000010 | 无效随机值 | nonce 需要 10 位 |
| B20002 | 字段数据不正确 | 检查具体字段格式 |

---

## 7. 限流规则

- 产品列表查询: 10 秒内 10 次

---

## 8. 项目配置

### 8.1 环境变量 (.env)

```env
DAJIAN_API_KEY=your-client-id
DAJIAN_API_SECRET=your-client-secret
DAJIAN_BASE_URL=https://openapi.gigab2b.com
DAJIAN_PROXY_URL=http://127.0.0.1:7890
```

### 8.2 代理配置

由于 GigaCloud API 服务器在海外，国内访问需要配置代理：
- HTTP 代理: `http://127.0.0.1:7890`
- 确保 Clash/代理工具已启动

---

## 9. 更新日志

| 日期 | 更新内容 |
|------|----------|
| 2026-02-05 | 初始文档，验证 API 连通性和签名算法 |
| 2026-01-22 | 官方新增 queryTimeType, startTime, endTime, addedTime 参数 |
| 2025-08-28 | 官方新增接口 |
