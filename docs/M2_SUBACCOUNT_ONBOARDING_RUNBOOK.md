# M2 操作手册:汽配子账号开店 + 实例B打通

> 2026-07-20 起草。前置 M1(store_profile 配置外部化)已完成(4c94ba2 + f7b6bc8)。
> 实例B已脚手架完毕:`C:\Users\poonx\AutoParts_Listing_Tool`(本地 clone,origin 指向主目录,只 pull 不开发)。

## 0. 已完成的准备(无需重做)

| 项 | 状态 |
|---|---|
| 实例B checkout | ✅ `git clone` 自主目录,HEAD 与主目录同步;更新方式:主目录 commit → 实例B `git pull` |
| `.env` | ✅ 从主实例复制;`EBAY_REFRESH_TOKEN` 已清空(子账号授权后由实例B自己的 `ebay_tokens.db` 管理);新增 `SERVER_PORT=8001` |
| `config/store_profile.local.yaml` | ✅ 已创建,**全是 CHANGEME 占位符**,填好前不得刊登 |
| venv | ✅ `python -m venv .venv` + requirements 安装 |
| 数据隔离 | ✅ 天然:实例B目录下自己的 ebay_tokens.db / ebay_collection.db / logs |

## 1. eBay 侧五步(按顺序;🧑 = 需要你人工操作)

### 步骤 1:子账号店铺就绪 🧑
- 汽配子账号完成卖家注册、店铺订阅、店名确定。
- 店名定下后 → 回填 `store_profile.local.yaml` 的 `brand_name` / `promotion_prefix` / `quality_gate.banner_marker`(小写)。

### 步骤 2:OAuth 授权(实例B)🧑+🤖
```powershell
cd C:\Users\poonx\AutoParts_Listing_Tool
.\.venv\Scripts\python.exe -c "from src.services.ebay_auth import EbayOAuthService; print(EbayOAuthService('PRODUCTION').get_authorization_url())"
```
- 打开输出的 URL,**用汽配子账号登录**并同意授权(⚠️ 别用主账号登录,token 会写错账号)。
- 回调拿到 code 后,在实例B目录执行 code 兑换(`exchange_code_for_token`),token 落入实例B的 `ebay_tokens.db`。
- 验证:`is_authorized()` 为 True 且 refresh_token 存在。

### 步骤 3:创建 business policies 🧑
- 在子账号 Seller Hub(或 Account API)创建:运费政策(汽配件的运费模板)、退货政策、收款政策。
- 然后在实例B跑:
```powershell
.\.venv\Scripts\python.exe tools\get_policy_ids.py
```
- 把三个 ID 回填 `store_profile.local.yaml` 的 `ebay:` 段。

### 步骤 4:创建 inventory location 🤖(授权完成后可代办)
- 用子账号 token 调 `POST /sell/inventory/v1/location/{key}`,key 与 `merchant_location_key` 一致(建议 `AUTOPARTS_<州>_WAREHOUSE`),地址填汽配仓库真实地址。
- 回填 `merchant_location_key`。

### 步骤 5:最小刊登验证 🤖(金丝雀,人工放行)
- 用实例B跑一条真实最小刊登(低价值 SKU):inventory item → offer → publish。
- 核对清单:листing 归属子账号 ✓ 品牌=新店名 ✓ 政策=子账号政策 ✓ 位置=新 location ✓ 描述模板 banner/footer 正确 ✓。
- 通过后 M2 收工,进入 M3(汽配采集→刊登链路)。

## 2. 风险与注意

- **授权串号**:步骤 2 登录错账号是最大风险,授权页登录前确认右上角账号名。
- **CHANGEME 未填就刊登**:brand 会写成占位符——步骤 5 金丝雀必须人工核对。
- **调度器**:M2 阶段实例B**不要**启动 scheduler_daemon(等 M5 守卫分流后再开),避免家具语义任务扫空库报警。
- 主实例完全不受影响:所有改动通过 profile 隔离,主目录照常运行。
