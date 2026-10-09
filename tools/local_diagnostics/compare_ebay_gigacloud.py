from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / f"compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def parse_json(raw):
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def normalize_aspect_value(val):
    if val is None:
        return ""
    if isinstance(val, list):
        val = val[0] if val else ""
    text = str(val).strip().lower()
    text = re.sub(r"\s*(in|inch|inches|lbs|lb|pounds|oz|ounces|cm|mm|kg|g|ft|feet|m|meter)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_numeric(val):
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else ""
    match = re.search(r"[\d]+\.?[\d]*", str(val).strip())
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


GENERIC_WORDS = frozenset(
    {
        "the", "and", "with", "for", "from", "size", "plus", "in", "on", "of", "by", "to", "is",
        "at", "all", "this", "that", "are", "was", "has", "can", "not", "but", "you", "your",
        "our", "its", "also", "more", "most", "made", "make", "each", "every", "any", "some",
    }
)

ASPECT_MAP = {
    "item length": ["assembled length (in.)", "length"],
    "item width": ["assembled width (in.)", "width"],
    "item height": ["assembled height (in.)", "height"],
    "item weight": ["product weight (lbs.)", "weight"],
    "color": ["variant", "color"],
    "material": ["main material", "material"],
    "brand": ["brand"],
    "type": ["type"],
    "style": ["style"],
    "room": ["room", "room type"],
    "features": ["features"],
    "model": ["model"],
    "mpn": ["mpn"],
    "upc": ["upc"],
    "pattern": ["pattern"],
    "assembly required": ["assembly required"],
    "country of origin": ["country of origin", "origin"],
}
DIMENSION_ASPECTS = ["Item Length", "Item Width", "Item Height", "Item Weight"]
TEXT_ASPECTS = ["Brand", "Color", "Material", "Type", "Style", "Room", "Pattern"]
IMPORTANT_GIGACLOUD_KEYS = [
    "main material",
    "variant",
    "assembly required",
    "assembled length (in.)",
    "assembled width (in.)",
    "assembled height (in.)",
    "product weight (lbs.)",
]


def compare_aspects(ebay_aspects, dajian_attrs, dajian_specs, opt_aspects):
    issues = []
    gc_combined = {}
    for key, value in (dajian_attrs or {}).items():
        gc_combined[key.lower()] = value
    for key, value in (dajian_specs or {}).items():
        gc_combined[key.lower()] = value

    opt_aspects = opt_aspects or {}
    for dim_name in DIMENSION_ASPECTS:
        ebay_val = ebay_aspects.get(dim_name)
        if ebay_val is None:
            continue
        ebay_num = extract_numeric(ebay_val)
        gc_num = None
        gc_raw = ""
        for candidate in ASPECT_MAP.get(dim_name.lower(), [dim_name.lower()]):
            raw_val = gc_combined.get(candidate)
            if raw_val:
                gc_num = extract_numeric(raw_val)
                gc_raw = str(raw_val)
                break
        if ebay_num is not None and gc_num is not None:
            diff = abs(ebay_num - gc_num)
            tolerance = max(gc_num * 0.02, 0.5)
            if diff > tolerance:
                issues.append(
                    {
                        "field": f"Item Specifics - {dim_name}",
                        "ebay_value": ebay_val,
                        "dajian_value": gc_raw,
                        "issue": f"数值偏差: eBay={ebay_num}, 大建={gc_num}, 差值={diff:.2f}",
                        "severity": "HIGH" if diff > 5 else "MEDIUM",
                    }
                )
        opt_val = opt_aspects.get(dim_name)
        if opt_val:
            opt_num = extract_numeric(opt_val)
            if ebay_num is not None and opt_num is not None:
                diff = abs(ebay_num - opt_num)
                tolerance = max(opt_num * 0.02, 0.5)
                if diff > tolerance:
                    issues.append(
                        {
                            "field": f"Item Specifics - {dim_name} (vs 优化草稿)",
                            "ebay_value": ebay_val,
                            "opt_value": opt_val,
                            "issue": f"数值偏差: eBay={ebay_num}, 优化={opt_num:.2f}, 差值={diff:.2f}",
                            "severity": "MEDIUM",
                        }
                    )

    for aspect_name in TEXT_ASPECTS:
        ebay_val = ebay_aspects.get(aspect_name)
        if ebay_val is None:
            continue
        ebay_norm = normalize_aspect_value(ebay_val)
        for candidate in ASPECT_MAP.get(aspect_name.lower(), [aspect_name.lower()]):
            gc_val = gc_combined.get(candidate)
            if not gc_val:
                continue
            gc_norm = normalize_aspect_value(gc_val)
            if ebay_norm and gc_norm and ebay_norm != gc_norm and gc_norm not in ebay_norm and ebay_norm not in gc_norm:
                issues.append(
                    {
                        "field": f"Item Specifics - {aspect_name}",
                        "ebay_value": ebay_val,
                        "dajian_value": gc_val,
                        "issue": f"内容偏差: eBay='{ebay_norm}', 大建='{gc_norm}'",
                        "severity": "HIGH",
                    }
                )
            break

    for gc_key in IMPORTANT_GIGACLOUD_KEYS:
        gc_val = gc_combined.get(gc_key)
        if not gc_val:
            continue
        mapped = []
        for ebay_name, gc_keys in ASPECT_MAP.items():
            if gc_key in gc_keys:
                mapped.append(ebay_name.title())
                break
        if mapped and not any(ebay_aspects.get(name) is not None for name in mapped):
            issues.append(
                {
                    "field": f"Item Specifics - 缺失: {mapped[0]}",
                    "ebay_value": "(缺失)",
                    "dajian_value": gc_val,
                    "issue": "大建有此参数但 eBay Item Specifics 缺失",
                    "severity": "MEDIUM",
                }
            )

    return issues


def compare_title(ebay_title, db_title, opt_title):
    issues = []
    if not ebay_title or not db_title:
        return issues
    db_kw = set(re.findall(r"[a-zA-Z]{3,}", db_title.lower())) - GENERIC_WORDS
    ebay_kw = set(re.findall(r"[a-zA-Z]{3,}", ebay_title.lower())) - GENERIC_WORDS
    missing = {word for word in db_kw - ebay_kw if len(word) >= 4}
    if len(missing) > 2:
        issues.append(
            {
                "field": "标题",
                "ebay_value": ebay_title[:80],
                "dajian_value": db_title[:80],
                "issue": f"缺少大建关键词: {', '.join(sorted(missing)[:8])}",
                "severity": "LOW" if len(missing) <= 4 else "MEDIUM",
            }
        )
    if opt_title:
        opt_kw = set(re.findall(r"[a-zA-Z]{3,}", opt_title.lower())) - GENERIC_WORDS
        missing_opt = {word for word in opt_kw - ebay_kw if len(word) >= 4}
        if len(missing_opt) > 3:
            issues.append(
                {
                    "field": "标题 (vs 优化草稿)",
                    "ebay_value": ebay_title[:80],
                    "opt_value": opt_title[:80],
                    "issue": f"缺少优化关键词: {', '.join(sorted(missing_opt)[:8])}",
                    "severity": "LOW",
                }
            )
    return issues


def compare_description(ebay_desc, db_desc, opt_desc):
    issues = []
    if not ebay_desc:
        issues.append({"field": "描述", "ebay_value": "(空)", "dajian_value": "(有内容)" if db_desc else "(空)", "issue": "eBay 描述为空", "severity": "HIGH"})
        return issues
    if not db_desc:
        return issues
    feature_matches = re.findall(r"<li[^>]*>(.*?)</li>", db_desc, re.DOTALL)
    if not feature_matches:
        feature_matches = [line.strip() for line in db_desc.split("\n") if line.strip() and len(line.strip()) > 10]
    if feature_matches:
        ebay_text = ebay_desc.lower()
        missing = []
        for feature in feature_matches[:10]:
            feature_text = re.sub(r"<[^>]+>", "", feature).strip()
            if len(feature_text) < 5:
                continue
            feature_words = set(re.findall(r"[a-zA-Z]{3,}", feature_text.lower())) - GENERIC_WORDS
            if feature_words and not any(word in ebay_text for word in feature_words if len(word) >= 4):
                missing.append(feature_text[:60])
        if len(missing) >= 3:
            issues.append(
                {
                    "field": "描述",
                    "ebay_value": f"({len(ebay_desc)} chars)",
                    "dajian_value": f"({len(db_desc)} chars)",
                    "issue": f"缺少大建特性: {', '.join(missing[:5])}",
                    "severity": "MEDIUM",
                }
            )
    return issues


def compare_images(ebay_images, db_images):
    issues = []
    ebay_count = len(ebay_images or [])
    db_count = len(db_images or [])
    if ebay_count == 0 and db_count > 0:
        issues.append({"field": "图片", "ebay_value": f"{ebay_count} 张", "dajian_value": f"{db_count} 张", "issue": "eBay 无图片但大建有图片", "severity": "HIGH"})
    elif db_count >= 4 and ebay_count < db_count * 0.5:
        issues.append({"field": "图片", "ebay_value": f"{ebay_count} 张", "dajian_value": f"{db_count} 张", "issue": f"图片严重缺失: eBay 仅 {ebay_count} 张 vs 大建 {db_count} 张 (<50%)", "severity": "MEDIUM" if ebay_count >= 1 else "HIGH"})
    elif db_count >= 6 and ebay_count < db_count * 0.7:
        issues.append({"field": "图片", "ebay_value": f"{ebay_count} 张", "dajian_value": f"{db_count} 张", "issue": f"图片不足: eBay {ebay_count} 张 vs 大建 {db_count} 张 (<70%)", "severity": "LOW"})
    return issues


def main() -> dict:
    logger.info("=" * 70)
    logger.info("开始 eBay vs 大建云仓 内容对比 (100条在刊登)")
    logger.info("=" * 70)

    conn = sqlite3.connect(ROOT / "ebay_collection.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT sku, title, price, shipping, stock, listing_id, attributes, specs, description, images, optimization, suggested_price "
        "FROM collected_products WHERE status = 'PUBLISHED' ORDER BY published_at DESC LIMIT 100"
    )
    db_rows = cur.fetchall()
    conn.close()
    if not db_rows:
        logger.error("没有找到任何已刊登产品!")
        return {}

    logger.info(f"从数据库读取了 {len(db_rows)} 个已刊登产品")

    from src.clients.real_ebay_client import create_real_ebay_client
    from src.clients.dajian_client import DaJianClient

    try:
        ebay_client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        logger.info("eBay API 客户端初始化成功")
    except Exception as exc:
        logger.error(f"eBay API 客户端初始化失败: {exc}")
        return {}

    dajian = DaJianClient(client_id=os.getenv("DAJIAN_API_KEY"), client_secret=os.getenv("DAJIAN_API_SECRET"))
    if not dajian.test_connection():
        logger.error("大建云仓 API 连接失败!")
        return {}
    logger.info("大建云仓 API 客户端初始化成功")

    skus = [row["sku"] for row in db_rows]
    logger.info("批量获取大建云仓产品详情...")
    gc_details, gc_prices, gc_inventory = {}, {}, {}
    for batch_start in range(0, len(skus), 200):
        batch = skus[batch_start:batch_start + 200]
        try:
            details = dajian.get_product_details(batch)
            for detail in details:
                if detail.get("sku"):
                    gc_details[detail["sku"]] = detail
            logger.info(f"  获取大建详情: {len(details)} 个 (批次 {batch_start // 200 + 1})")
        except Exception as exc:
            logger.error(f"获取大建详情失败: {exc}")
        try:
            prices = dajian.get_product_prices(batch)
            for price in prices:
                if price.get("sku"):
                    gc_prices[price["sku"]] = price
        except Exception as exc:
            logger.error(f"获取大建价格失败: {exc}")
        try:
            inventory = dajian.get_inventory(batch)
            for item in inventory:
                if item.get("sku"):
                    gc_inventory[item["sku"]] = item
        except Exception as exc:
            logger.error(f"获取大建库存失败: {exc}")
        time.sleep(1)

    logger.info("逐个获取 eBay 在刊登数据...")
    ebay_inv_map = {}
    for idx, row in enumerate(db_rows, start=1):
        try:
            inv_item = ebay_client.get_inventory_item(row["sku"])
            if inv_item:
                ebay_inv_map[row["sku"]] = inv_item
            time.sleep(0.3)
        except Exception as exc:
            logger.warning(f"  获取 eBay inventory {row['sku']} 失败: {exc}")
        if idx % 20 == 0:
            logger.info(f"  已获取 {idx}/{len(db_rows)} 个 eBay inventory")

    logger.info(f"成功获取 eBay inventory: {len(ebay_inv_map)} 个")

    logger.info("逐个获取 eBay offer (listingDescription)...")
    ebay_offer_map = {}
    token = ebay_client.oauth.get_valid_token()
    for idx, row in enumerate(db_rows, start=1):
        try:
            response = ebay_client.session.get(
                f"{ebay_client.base_url}/sell/inventory/v1/offer",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"sku": row["sku"], "marketplace_id": "EBAY_US", "limit": 5},
                timeout=30,
            )
            if response.status_code == 200:
                offers = response.json().get("offers", [])
                if offers:
                    best_offer = next((offer for offer in offers if (offer.get("listing") or {}).get("listingStatus") == "ACTIVE"), offers[0])
                    ebay_offer_map[row["sku"]] = best_offer
            if idx % 50 == 0:
                try:
                    token = ebay_client.oauth.get_valid_token()
                except Exception:
                    pass
            time.sleep(0.3)
        except Exception as exc:
            logger.warning(f"  获取 eBay offer {row['sku']} 失败: {exc}")
        if idx % 20 == 0:
            logger.info(f"  已获取 {idx}/{len(db_rows)} 个 eBay offer")

    logger.info(f"成功获取 eBay offer: {len(ebay_offer_map)} 个")
    logger.info("=" * 70)
    logger.info("开始逐个对比...")
    logger.info("=" * 70)

    all_results = []
    total_issues = 0
    products_with_issues = 0
    for row in db_rows:
        sku = row["sku"]
        listing_id = row["listing_id"]
        db_title = row["title"]
        db_price = float(row["price"] or 0)
        db_shipping = float(row["shipping"] or 0)
        db_suggested_price = float(row["suggested_price"] or 0)
        db_attrs = parse_json(row["attributes"])
        db_specs = parse_json(row["specs"])
        db_desc = row["description"] or ""
        db_images = parse_json(row["images"])
        db_opt = parse_json(row["optimization"])
        opt_title = db_opt.get("title", "")
        opt_desc = db_opt.get("description", "")
        opt_aspects = db_opt.get("aspects", {})

        ebay_inv = ebay_inv_map.get(sku, {})
        ebay_product = (ebay_inv.get("product") or {}) if ebay_inv else {}
        ebay_offer = ebay_offer_map.get(sku, {})
        ebay_title = ebay_product.get("title", "")
        ebay_desc = ebay_offer.get("listingDescription", "") or ebay_product.get("description", "")
        ebay_aspects = ebay_product.get("aspects", {})
        ebay_images = ebay_product.get("imageUrls", [])

        gc_detail = gc_details.get(sku, {})
        gc_price_info = gc_prices.get(sku, {})
        gc_current_price = float(gc_price_info.get("price", 0) or 0)
        gc_current_shipping = float(gc_price_info.get("shippingFee", 0) or 0)
        gc_attrs_dict = {}
        for attr in gc_detail.get("attributes", []) or []:
            if isinstance(attr, dict):
                name = attr.get("attributeName", attr.get("name", ""))
                value = attr.get("attributeValue", attr.get("value", ""))
                if name:
                    gc_attrs_dict[name] = value
        if not gc_attrs_dict:
            gc_attrs_dict = db_attrs or {}

        product_issues = []
        if ebay_aspects:
            product_issues.extend(compare_aspects(ebay_aspects, gc_attrs_dict, db_specs, opt_aspects))
        else:
            product_issues.append({"field": "Item Specifics", "ebay_value": "(无)", "dajian_value": f"{len(gc_attrs_dict)} 个参数", "issue": "eBay 在刊登没有 Item Specifics", "severity": "HIGH"})

        product_issues.extend(compare_title(ebay_title, db_title, opt_title))
        product_issues.extend(compare_description(ebay_desc, db_desc, opt_desc))
        product_issues.extend(compare_images(ebay_images, db_images))

        ebay_offer_price = None
        if ebay_offer:
            pricing = ebay_offer.get("pricingSummary") or {}
            try:
                ebay_offer_price = float((pricing.get("price") or {}).get("value", 0) or 0)
            except (ValueError, TypeError):
                ebay_offer_price = None
        if ebay_offer_price and db_suggested_price:
            diff = abs(ebay_offer_price - db_suggested_price)
            if diff > db_suggested_price * 0.1:
                pct = diff / db_suggested_price * 100
                product_issues.append({"field": "价格", "ebay_value": f"${ebay_offer_price:.2f}", "dajian_value": f"建议 ${db_suggested_price:.2f}", "issue": f"价格偏差: eBay=${ebay_offer_price:.2f}, 建议=${db_suggested_price:.2f}, 差={diff:.2f} ({pct:.0f}%)", "severity": "HIGH" if pct > 20 else "MEDIUM"})
        if gc_current_price > 0 and db_price > 0:
            diff = abs(gc_current_price - db_price)
            if diff > 0.01:
                pct = diff / db_price * 100
                product_issues.append({"field": "大建源价变动", "ebay_value": f"DB记录 ${db_price:.2f}", "dajian_value": f"当前 ${gc_current_price:.2f}", "issue": f"大建价格已变化: DB=${db_price:.2f} -> 当前=${gc_current_price:.2f} ({pct:.0f}%)", "severity": "HIGH" if pct > 10 else ("MEDIUM" if pct > 5 else "LOW")})
        if gc_current_shipping > 0 and db_shipping > 0:
            diff = abs(gc_current_shipping - db_shipping)
            if diff > 0.01:
                product_issues.append({"field": "大建运费变动", "ebay_value": f"DB记录 ${db_shipping:.2f}", "dajian_value": f"当前 ${gc_current_shipping:.2f}", "issue": f"大建运费已变化: DB=${db_shipping:.2f} -> 当前=${gc_current_shipping:.2f}", "severity": "MEDIUM" if diff > 5 else "LOW"})

        if not product_issues:
            continue
        products_with_issues += 1
        total_issues += len(product_issues)
        max_severity = max(product_issues, key=lambda item: {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(item["severity"], 0))["severity"]
        all_results.append({"sku": sku, "listing_id": listing_id, "ebay_url": f"https://www.ebay.com/itm/{listing_id}" if listing_id else "", "db_title": db_title[:60], "ebay_title": ebay_title[:60] if ebay_title else "(未获取)", "dajian_product_name": ((gc_detail.get("productName") or "")[:60] or db_title[:60]), "discrepancies": product_issues, "max_severity": max_severity})

    logger.info("=" * 70)
    logger.info("对比完成")
    logger.info("=" * 70)
    all_results.sort(key=lambda row: {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(row["max_severity"], 0), reverse=True)

    sev_counts = defaultdict(int)
    field_counts = defaultdict(int)
    for row in all_results:
        for discrepancy in row["discrepancies"]:
            sev_counts[discrepancy["severity"]] += 1
            field_counts[discrepancy["field"]] += 1

    lines = [
        "=" * 70,
        "  eBay vs 大建云仓 内容对比报告",
        f"  检查数量: {len(db_rows)} 条在刊登",
        f"  发现偏差: {products_with_issues} 个产品, {total_issues} 条偏差",
        f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        "",
        "偏差严重度统计:",
    ]
    for severity in ["HIGH", "MEDIUM", "LOW"]:
        lines.append(f"  {severity}: {sev_counts.get(severity, 0)} 条")
    lines.append("")
    lines.append("偏差类型统计:")
    for field, count in sorted(field_counts.items(), key=lambda item: -item[1]):
        lines.append(f"  {field}: {count} 条")
    lines.extend(["", "=" * 70, "  详细偏差列表", "=" * 70])

    for row in all_results:
        lines.extend(
            [
                "",
                f"SKU: {row['sku']}",
                f"  eBay链接: {row['ebay_url']}",
                f"  DB标题: {row['db_title']}",
                f"  eBay标题: {row['ebay_title']}",
                f"  大建名称: {row['dajian_product_name']}",
                f"  偏差数: {len(row['discrepancies'])}, 最高严重度: {row['max_severity']}",
                "  ---",
            ]
        )
        for discrepancy in row["discrepancies"]:
            lines.append(f"  [{discrepancy['severity']}] {discrepancy['field']}")
            lines.append(f"    eBay值: {discrepancy['ebay_value']}")
            lines.append(f"    大建值: {discrepancy.get('dajian_value', discrepancy.get('opt_value', ''))}")
            lines.append(f"    问题: {discrepancy['issue']}")

    report_text = "\n".join(lines)
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"compare_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report_path.write_text(report_text, encoding="utf-8")
    json_path = report_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "summary": {
                    "checked": len(db_rows),
                    "products_with_issues": products_with_issues,
                    "total_discrepancies": total_issues,
                    "severity_counts": dict(sev_counts),
                    "field_counts": dict(field_counts),
                },
                "results": all_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(report_text)
    logger.info(f"报告已保存到: {report_path}")
    logger.info(f"JSON已保存到: {json_path}")
    return {"checked": len(db_rows), "products_with_issues": products_with_issues, "total_issues": total_issues, "report_path": str(report_path)}


if __name__ == "__main__":
    main()

