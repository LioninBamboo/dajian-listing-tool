# Marketplace Insights Access Request

Submission path:
- https://developer.ebay.com/my/support/tickets?tab=app-check
- https://developer.ebay.com/support/developer-technical-support
- https://developer.ebay.com/grow/application-growth-check

Suggested ticket type:
- `Application Growth Check` or `Developer Technical Support`

Suggested subject:
- `Request access to Buy Marketplace Insights API for production repricing workflow`

Suggested body:

```text
Hello eBay Developer Support,

I am requesting production access / enablement for the Buy Marketplace Insights API for my eBay selling automation workflow.

Use case:
- I maintain a production listing tool that reprices active eBay listings based on market data.
- I need sold-data (not only active-listing prices) so pricing decisions reflect actual transaction behavior.
- The workflow is used only for my own seller operations and internal pricing analytics.

Requested capability:
- Access to the Buy Marketplace Insights endpoint:
  /buy/marketplace_insights/v1_beta/item_sales/search

Current issue:
- My production workflow can authenticate successfully, but Marketplace Insights calls return:
  HTTP 403
  "Insufficient permissions to fulfill the request."

Observed evidence:
- Date: 2026-03-30
- Environment: Production
- Endpoint: /buy/marketplace_insights/v1_beta/item_sales/search
- Auth mode used by the workflow: user token
- Current fallback behavior: my repricer must fall back to Browse API active-listing data, which is less accurate than sold-data for competitive pricing.

Business justification:
- I need sold-data to keep pricing competitive while preserving margin floors.
- Active-listing data alone can be biased by unsold high-price listings.
- Marketplace Insights would improve pricing accuracy, listing health decisions, and inventory turnover.

Please let me know:
- Whether this application can be enabled for Marketplace Insights,
- Whether any additional review, whitelisting, or application growth check is required,
- Whether there are specific scopes, keyset settings, or account prerequisites I must complete.

I can provide any additional information you need, including app ID / keyset details, example request IDs, and screenshots of the 403 response.

Thank you.
```

Recommended attachments / evidence:
- `reports/reprice_report_20260330_0836.json`
  - Shows `marketplace_insights_access.available = false`
  - Shows `reason = permission_denied`
  - Shows `status_code = 403`
  - Shows `endpoint = /buy/marketplace_insights/v1_beta/item_sales/search`
  - Shows the repricer had to use `BROWSE_FALLBACK` for 402 listings

Local evidence summary:

```json
{
  "timestamp": "2026-03-30T08:36:32.016744",
  "market_mode": "auto",
  "marketplace_insights_access": {
    "available": false,
    "reason": "permission_denied",
    "status_code": 403,
    "auth_mode": "user",
    "endpoint": "/buy/marketplace_insights/v1_beta/item_sales/search",
    "error": "Insufficient permissions to fulfill the request."
  },
  "summary_market_sources": {
    "BROWSE_FALLBACK": 402
  }
}
```

Notes:
- The support portal requires an authenticated eBay Developer account session to submit.
- If the ticket is filed under `Application Growth Check`, include the production app / keyset identifier from your Developer Portal.

---

# Application Growth Check Draft

Use this draft for the `Application Growth Check` form that eBay Developer Support requested on 2026-03-31.

Important positioning:
- This request is primarily for **production access / enablement** to a restricted API, not a generic rate-limit increase.
- If the form only provides `Increase My Call Limit` as the closest purpose option, select it and explain clearly in `Application Details` that the real request is **Marketplace Insights API production access for a live repricing workflow**.

## Suggested field values

### Application Title / Summary

```text
Production access request for Buy Marketplace Insights API for live eBay repricing workflow
```

### Application Details

```text
I am requesting production access / enablement for the Buy Marketplace Insights API for my live eBay seller automation application.

Application overview:
- Internal production listing and operations tool used only for my own seller account and internal pricing analytics.
- Stack: local Streamlit admin UI + FastAPI service + SQLite data store + eBay OAuth user token flow.
- The application manages listing publish, inventory sync, title optimization, sales-health checks, and smart repricing.

Current production footprint:
- 442 active/published eBay listings in the local production database.
- The application has been publishing and maintaining live listings since January 2026.
- Recent smart repricing runs processed 361-404 live listings, with the 2026-03-30 run processing 402 listings.

Requested API capability:
- Buy Marketplace Insights API
- Endpoint needed: /buy/marketplace_insights/v1_beta/item_sales/search
- Intended usage: retrieve sold-item market data for pricing decisions in my own seller workflow.

Why this access is needed:
- My repricer currently falls back to Browse API active-listing data when Marketplace Insights is unavailable.
- Active-listing prices can be biased by unsold high-price listings, which reduces pricing accuracy.
- Sold-data is needed to improve repricing quality, protect margin floors, and support healthier inventory turnover.

Observed production issue:
- Date observed: 2026-03-30
- Environment: Production
- Auth mode: user token
- Result: HTTP 403 "Insufficient permissions to fulfill the request."
- Evidence from local report: marketplace_insights_access.available=false, reason=permission_denied, status_code=403.
- On that run, all 402 repriced listings had to use Browse fallback instead of sold-data.

How the data is used:
- The data is used only inside my own seller operations workflow.
- It is not exposed as a public analytics product, not resold, and not shared with third parties.
- The application uses sold-data only to evaluate pricing for the listings that I manage.

Expected call volume:
- Marketplace Insights calls are grouped by category/keyword, not per listing.
- Initial expected Marketplace Insights usage: about 60-120 calls/day, peak below 250/day.
- Overall eBay API traffic for the live application is approximately 1,200-2,000 calls/day, with peak days remaining below the default 5,000 calls/day limit.

Requested outcome:
- Enable production access for Marketplace Insights on this application/keyset.
- Confirm whether any additional whitelist, restricted-API review, or policy acknowledgement is required.

I can provide screenshots, request examples, and the repricing report showing the 403 response and Browse fallback behavior if needed.
```

Short version for the portal limit (`1752` characters):

```text
I request production access to the Buy Marketplace Insights API for my live eBay seller automation app.

This is an internal tool used only for my own seller operations and pricing analytics. It runs a local Streamlit UI + FastAPI service with SQLite and eBay OAuth user tokens. The workflow manages listing publish, inventory sync, title optimization, sales health checks, and smart repricing.

Current production usage:
- 442 active/published eBay listings in my production database
- Live publishing and maintenance since January 2026
- Recent repricing runs processed 361-404 live listings; the 2026-03-30 run processed 402 listings

Requested endpoint:
- /buy/marketplace_insights/v1_beta/item_sales/search

Business need:
I need sold-item data to price my own live listings more accurately. Browse API active-listing prices are only a fallback and can be biased by unsold high-price listings. Sold-data will improve repricing accuracy, margin protection, and inventory turnover.

Observed issue in production:
On 2026-03-30, Marketplace Insights calls in Production with a user token returned HTTP 403: "Insufficient permissions to fulfill the request." My local repricing report shows permission_denied and all 402 repriced listings had to fall back to Browse data.

Usage and volume:
The data is for internal use only, not resold or shared with third parties. Calls are grouped by category/keyword, not per listing. Expected Marketplace Insights usage is about 60-120 calls/day, peak below 250/day. Overall eBay API traffic is about 1,200-2,000 calls/day, below the default 5,000/day limit.

Please enable Marketplace Insights access for this production application/keyset and let me know if any additional review or whitelist step is required.
```

### Products

Recommended selection:
- Choose the product group that contains `Marketplace Insights API`.
- If the dropdown is grouped more broadly, select `Buy APIs`.

### Purpose of Request

Recommended selection:
- Prefer an option equivalent to `Access restricted API` or `Marketplace Insights access` if present.
- If the only close option is `Increase My Call Limit`, select that and rely on the details above to clarify that this is a **restricted API enablement request**, not a general call-limit increase request.

### eBay Partner Network member

```text
No
```

Only choose `Yes` if you actually participate in eBay Partner Network.

### Application ID

```text
xiaoting-AquaVerv-PRD-947e7b8cc-97e0e078
```

### Application URL

Recommended value:

```text
http://localhost:8501
```

If the form rejects localhost URLs, leave this field blank and include this note in the details:

```text
Internal self-use application hosted locally (localhost), no public customer-facing URL.
```

### Call Volume Estimate

```text
Marketplace Insights API usage is grouped by category/keyword rather than per listing. Initial expected Marketplace Insights traffic is about 60-120 calls/day, with peak days below 250/day. Overall production eBay API traffic for the live application is approximately 1,200-2,000 calls/day across listing publish, inventory sync, title optimization, health checks, and repricing, with peak days remaining below the default 5,000 calls/day limit.
```

### CC

Leave blank unless you want another mailbox copied on the request.

### Attach Documents

Recommended attachments:
- `reports/reprice_report_20260330_0836.json`
- Screenshot of the 403 response, if available
- Screenshot of the Application Growth Check page, only if you want to show the exact request context

## Facts used in the draft

- Production app ID exists and is configured in `.env`.
- Production OAuth RuName is configured.
- Local database currently contains `442` `PUBLISHED` listings.
- Recent publish dates show continued live usage in March 2026.
- `reports/reprice_report_20260330_0836.json` shows:
  - `marketplace_insights_access.available = false`
  - `reason = permission_denied`
  - `status_code = 403`
  - `auth_mode = user`
  - `endpoint = /buy/marketplace_insights/v1_beta/item_sales/search`
  - `summary.total = 402`
  - `summary.market_sources.BROWSE_FALLBACK = 402`
- Recent reprice reports processed between `361` and `404` live listings.

## Submission notes

- Check the agreement/policy checkbox before submitting.
- Keep the wording focused on:
  - live production seller workflow
  - internal-only use
  - need for sold-data rather than active-listing-only data
  - moderate, controlled call volume

---

# Follow-up Reply Draft

Use this reply for the follow-up email from eBay Developer Support asking for more details.

Important guidance:
- Keep the reply narrow and factual.
- Emphasize `internal use only`, `own seller account only`, `no external sharing`, and `derived pricing aggregates only`.
- If you do not have a public website, say so directly and offer screenshots or a short demo video instead.

## Suggested reply

```text
Hello,

Thank you for your follow-up. Please see my responses below.

1. What are your company/application? Please provide your website URL or mobile app if applicable.
My application is an internal seller operations tool used for my own eBay listing management and repricing workflow. It is not a public SaaS product and does not have a public website or mobile app. It runs as a local Streamlit + FastAPI application for internal use only. If helpful, I can provide screenshots or a short demo video of the workflow.

2. Your eBay UserID you either buy and/or sell with.
[YOUR EBAY USERID]

3. Are you an EPN partner? If so, please provide your publisher ID.
No, I am not an EPN partner.

4. What is the completed item information being used for?
The completed item information would be used only to support repricing and pricing analysis for my own live eBay listings. Specifically, I use sold-item data to estimate realistic market prices, compare sold-price behavior against active-listing prices, set safer pricing floors/ceilings, and improve inventory turnover while protecting margins.

5. Are you storing the sales data in any way?
Yes, in a limited internal way. I store only derived pricing aggregates and audit/reporting metadata needed for my repricing workflow, such as average price, median price, sample size, sold count, source, and related repricing results. I am not building or distributing a standalone completed-items database, and I do not externally expose raw sold-item datasets.

6. Are you sharing the sales data internally or externally?
The data is used only within my own internal seller workflow. It is not shared externally, resold, published, or provided to third parties.

7. What categories (list of the eBay categoryIds) do you focus on and need access to?
My current live workflow primarily focuses on home, furniture, outdoor, and related categories where I already have active listings. Current categoryIds in scope include:
175754, 175758, 38208, 38204, 20488, 54235, 177000, 107578, 79682, 115753, 32878, 20487, 131588, 16080, 116394, 183322, 139849, 262210.

8. Your contact information
Name: Xiaoting Pan
Email: [YOUR EMAIL]
Location/Time zone: Shanghai, China Time

For additional context, my production database currently contains 442 active/published listings, and my 2026-03-30 repricing run processed 402 live listings. During that run, Marketplace Insights requests returned HTTP 403 permission_denied, so the workflow had to fall back to Browse API data.

Please let me know if you would like screenshots, the repricing report, or any additional technical details.

Best regards,
Xiaoting Pan
```

## Recommended positioning for approval

- Best framing:
  - internal tool for your own seller account
  - no EPN
  - no external resale or redistribution
  - only derived aggregates stored locally for audit/repricing
  - categories already tied to live listings
- Avoid saying:
  - broad market intelligence platform
  - resale, client reporting, competitor data distribution
  - generic data warehousing of completed item records

## Risk notes

- Positive signals:
  - You have a live production workflow.
  - You already maintain hundreds of active listings.
  - Your use case is seller-operations-focused rather than data resale.
- Main risk:
  - The application is internal/local and not publicly reviewable, so they may ask for screenshots, a demo, or more evidence of legitimate seller usage.
- Practical expectation:
  - This is a reasonable reply and materially better than the initial generic request, but approval is still discretionary because Marketplace Insights is a restricted API.
