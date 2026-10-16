# 子店补授 sell.marketing（广告）

刷新 token **不会**长出新 scope。GrovePop / AquaRides 当前 token 调 Promoted Listings 返回 `403 errorId 1100 Insufficient permissions`。代码已经按主店广告链调度，但入驻会 SKIPPED，直到重新同意授权并把新 token 导入**对应实例**的 `ebay_tokens.db`。

三店共用同一套开发者密钥；RuName 是 `.env` 里的 `EBAY_REDIRECT_URI`（不是 URL）。回调落在云端 Streamlit（授权接受 URL），再把 token 文件拷回子店目录。

## 不要做

- 不要在主店 `Dajian_Listing_Tool` 里导入子店 token。
- 不要用 AquaVerve 账号点同意。授权页右上角必须是 `grovepop` 或 AquaRides 那个卖家。
- 不要只跑 `tools/refresh_token.py`——那只会续旧 scope。

## 每店一遍（GrovePop 然后 AquaRides）

1. 云端 Streamlit 打开「🔐 eBay 授权」→「开始授权」。或在**该子店目录**打印 URL：

```powershell
cd C:\Users\poonx\GrovePop_Listing_Tool   # AquaRides 则 AutoParts_Listing_Tool
.\.venv\Scripts\python.exe scripts\probe_marketing_scope.py --auth-url
```

2. 浏览器打开 URL，**退出已登录的主店账号**，用目标子店卖家登录并同意（scope 列表里应有 Marketing / Promoted Listings）。`--auth-url` **不会**请求 `commerce.taxonomy.readonly`（这套 RuName 的用户授权流没开通它，带上会整次 `invalid_scope`）。
3. 回调回到云端 Streamlit 后，去掉地址栏 `?code=` 再刷新，点「下载 Token 文件」。
4. 把 `ebay_token.json` 放到该子店目录，导入并复查：

```powershell
.\.venv\Scripts\python.exe scripts\probe_marketing_scope.py --import ebay_token.json
```

期望输出 `"verdict": "ok"`。若仍是 `missing_scope`，账号还没有 Promoted Listings 资格（Seller Hub → Marketing / Promoted Listings Standard 开通后再做一遍同意）。若 token 交换报 `invalid_scope`，是这套 RuName 还没给用户授权流打开 `sell.marketing`，要在 eBay Developer 后台给 RuName 勾上该 scope。

5. `git pull` 主仓库（若还没拉过 QC/广告切片），重启该店 Scheduler Daemon。

6. **同意 Promoted Listings 用户协议**（OAuth 通了仍会 409 `errorId 35067`）。用该店卖家登录后打开：
   https://useragreement.ebay.com/usragmt/agreement/PROMOTED_LISTINGS_USER_AGREEMENT
   或 Seller Hub → Marketing → Promoted Listings。GrovePop 和 AquaRides 各点一次。同意后再跑 `store_marketing.py --levers promoted --apply`。

## 复查

```powershell
.\.venv\Scripts\python.exe scripts\probe_marketing_scope.py
```

| verdict | 含义 |
|---------|------|
| `ok` | 可以开广告 |
| `missing_scope` | token 或账号仍无 Marketing 权限 |
| `invalid_scope` | 应用 RuName 未开通该 scope |
