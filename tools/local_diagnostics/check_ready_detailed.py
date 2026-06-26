from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from src.clients.dajian_client import DaJianClient


def parse_json(raw):
    if not raw:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def check_ready() -> None:
    conn = sqlite3.connect(ROOT / "ebay_collection.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM collected_products WHERE status = 'READY'").fetchall()

    if not rows:
        logger.info("没有状态为 READY 的产品。")
        return

    skus = [row["sku"] for row in rows]
    logger.info(f"找到 {len(skus)} 个 READY 产品: {skus}")

    dajian = DaJianClient(
        client_id=os.getenv("DAJIAN_API_KEY"),
        client_secret=os.getenv("DAJIAN_API_SECRET"),
    )

    gc_details = {}
    for detail in dajian.get_product_details(skus):
        if detail.get("sku"):
            gc_details[detail["sku"]] = detail

    for row in rows:
        product = dict(row)
        for field in ("optimization", "cost_breakdown", "images", "videos", "specs", "attributes", "logs"):
            if product.get(field):
                product[field] = parse_json(product[field])

        sku = product["sku"]
        optimization = product.get("optimization", {})
        videos = product.get("videos", [])

        logger.info("\n=========================================")
        logger.info(f"SKU: {sku}")
        logger.info(f"标题: {product.get('title')}")
        logger.info(f"类目ID (预备): {optimization.get('categoryId')}")
        logger.info(f"采集到的视频数量: {len(videos)}")

        gc_data = gc_details.get(sku, {})
        gc_attrs_raw = gc_data.get("attributes") or []
        gc_desc = gc_data.get("description") or ""

        db_attrs = product.get("attributes", {})
        gc_attrs = {}
        for attr in gc_attrs_raw:
            if isinstance(attr, dict):
                gc_attrs[attr.get("name", "")] = attr.get("value", "")

        logger.info(f"大建云仓参数数量: {len(gc_attrs)}, 数据库参数数量: {len(db_attrs)}")

        db_desc = optimization.get("description", product.get("description", ""))
        logger.info(f"大建云仓描述长度: {len(gc_desc)}, 数据库(优化后)描述长度: {len(db_desc)}")


if __name__ == "__main__":
    check_ready()

