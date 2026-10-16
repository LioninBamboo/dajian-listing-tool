"""
Server-side publish dry-run.

Exercises the full publish aspect-preparation chain as it runs inside
POST /api/publish/{sku}, but without starting an HTTP server or calling
the real eBay API.

Usage:
    python scripts/dry_run_server_publish.py [SKU]

If SKU is omitted the script picks the first READY/COLLECTED product from
the local DB.  If no such product exists it falls back to a built-in
fixture so the pipeline can always be verified.
"""

import sys
import os
import json
import traceback

# ------------------------------------------------------------------
# path setup so relative imports resolve from project root
# ------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ------------------------------------------------------------------
# minimal fixture used when no real product is available
# ------------------------------------------------------------------
FIXTURE_PRODUCT = {
    "sku": "DRY-RUN-001",
    "title": "Dajian 6-Piece Dining Set w/ Extendable Table & Upholstered Chairs",
    "price": 599.00,
    "suggested_price": 799.00,
    "description": "<div><p>High-quality dining set. Seats 6 comfortably.</p></div>",
    "optimization": {
        "title": "6-Piece Dining Set Extendable Table Upholstered Chairs Wood Frame",
        "categoryId": "107578",
        "categoryName": "Dining Sets",
        "aspects": {
            "Brand": ["Dajian"],
            "Style": ["Modern", "Contemporary"],
            "Material": ["Wood"],
            "Color": ["Gray", "Walnut"],
            "Features": ["Foldable", "Extendable"],
            "Set Includes": ["See Description"],
            "Assembly Required": ["Yes"],
            "Load Capacity": [
                "This table has a maximum load capacity of 330 lbs per chair and 220 lbs for"
                " the main table surface under normal household conditions"
            ],
        },
    },
    "attributes": {
        "assembledLength": 63,
        "assembledWidth": 36,
        "assembledHeight": 30,
        "assembledWeight": 185,
    },
    "cost_breakdown": {"dajian_cost": 320.0, "selling_price": 799.0},
    "status": "READY",
}


# ------------------------------------------------------------------
# helpers copied from server.py bootstrap (avoid FastAPI import)
# ------------------------------------------------------------------
def _load_product_from_db(sku: str | None):
    """Return a plain dict mirroring what publish_product() reads from the DB."""
    try:
        from src.db.collection_db import SessionLocal
        from src.db.collection_models import CollectedProduct

        db = SessionLocal()
        try:
            query = db.query(CollectedProduct)
            if sku:
                product = query.filter_by(sku=sku).first()
            else:
                product = (
                    query.filter(CollectedProduct.status.in_(["READY", "COLLECTED"]))
                    .order_by(CollectedProduct.created_at.desc())
                    .first()
                )
            if not product:
                return None
            return {
                "sku": product.sku,
                "title": product.title or "",
                "price": product.price or 0.0,
                "suggested_price": product.suggested_price or product.price or 0.0,
                "description": product.description or "",
                "optimization": product.optimization or {},
                "attributes": product.attributes or {},
                "cost_breakdown": product.cost_breakdown or {},
                "status": product.status,
            }
        finally:
            db.close()
    except Exception as exc:
        print(f"[WARN] Could not load from DB: {exc}")
        return None


def _run_server_publish_chain(product: dict) -> dict:
    """
    Replicate the aspect-preparation steps from POST /api/publish/{sku}.

    Returns a dict describing what would be sent to eBay Inventory API:
        {
            "sku": str,
            "title": str,
            "price": float,
            "category_id": str,
            "aspects_before": dict,   # after complete_publish_aspects
            "aspects_after":  dict,   # after sanitize + prepare_ebay_aspects
            "aspect_count": int,
            "warnings": list[str],
        }
    """
    from src.utils.publish_aspect_completion import complete_publish_aspects
    from src.utils.publish_autofix import sanitize_single_value_aspects, prepare_ebay_aspects
    from src.services.ebay_publisher import EbayPublisher
    from src.utils.dimension_helpers import populate_dimension_aspects
    from src.utils.publish_validation import MEASUREMENT_ASPECT_KEYS
    from src.utils.title_sanitizer import normalize_listing_title_for_ebay

    opt = product["optimization"]
    sku = product["sku"]
    title, _ = normalize_listing_title_for_ebay(
        opt.get("title") or product["title"],
        source_title=product["title"],
    )
    category_id = str(opt.get("categoryId") or "107578")
    attrs = product["attributes"]
    description = opt.get("description") or product["description"]
    aspects = dict(opt.get("aspects") or {"Brand": ["Dajian"]})

    warnings: list[str] = []

    # Step 1: force dimensions from source attributes
    populate_dimension_aspects(aspects, attrs)

    # Step 2: complete aspects (set includes, bed size, fabric, category defaults, etc.)
    aspects = complete_publish_aspects(
        aspects,
        title=title,
        category_id=category_id,
        attrs=attrs,
        description=description,
        category_required_aspects=EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
        log=lambda msg: warnings.append(msg) or print(f"  {msg}"),
    )
    aspects_before = json.loads(json.dumps(aspects))  # snapshot

    # Step 3: sanitize single-value aspects
    sanitize_single_value_aspects(aspects, log=lambda msg: warnings.append(msg) or print(f"  {msg}"))

    # Step 4: pre-flight truncation and trimming (new shared helper)
    required_aspect_names: list[str] = []  # would come from category matcher in real run
    prepare_ebay_aspects(
        aspects,
        required_aspect_names,
        log=lambda msg: warnings.append(msg) or print(f"  {msg}"),
    )
    aspects_after = json.loads(json.dumps(aspects))

    return {
        "sku": sku,
        "title": title,
        "price": product["suggested_price"],
        "category_id": category_id,
        "aspects_before": aspects_before,
        "aspects_after": aspects_after,
        "aspect_count": len(aspects_after),
        "warnings": [w for w in warnings if w.startswith("[")],
    }


def main(argv: list[str]) -> int:
    sku = argv[1] if len(argv) > 1 else None
    print("=" * 60)
    print("SERVER-SIDE PUBLISH DRY-RUN")
    print("=" * 60)

    product = _load_product_from_db(sku)
    if product:
        print(f"[DB] Loaded SKU={product['sku']} status={product['status']}")
    else:
        print("[FIXTURE] No DB product found — using built-in fixture")
        product = FIXTURE_PRODUCT
        if sku:
            print(f"[WARN] SKU '{sku}' not found in DB")

    print(f"\nSKU      : {product['sku']}")
    print(f"Title    : {product['title'][:70]}...")
    print(f"Category : {product['optimization'].get('categoryId', '(none)')}")
    print(f"Price    : ${product['suggested_price']:.2f}")
    print()

    try:
        result = _run_server_publish_chain(product)
    except Exception:
        print("[ERROR] Chain failed:")
        traceback.print_exc()
        return 1

    print()
    print("─" * 60)
    print(f"ASPECTS BEFORE prepare_ebay_aspects ({len(result['aspects_before'])} keys):")
    for k, v in result["aspects_before"].items():
        print(f"  {k}: {v}")

    print()
    print(f"ASPECTS AFTER  prepare_ebay_aspects ({result['aspect_count']} keys):")
    for k, v in result["aspects_after"].items():
        flag = ""
        before_v = result["aspects_before"].get(k)
        if before_v != v:
            flag = "  ← CHANGED"
        removed = set(result["aspects_before"]) - set(result["aspects_after"])
        print(f"  {k}: {v}{flag}")
    if removed := set(result["aspects_before"]) - set(result["aspects_after"]):
        for k in removed:
            print(f"  {k}: (DROPPED — over 45-aspect limit)")

    print()
    print("─" * 60)
    value_lengths = [
        (k, idx, v)
        for k, vals in result["aspects_after"].items()
        for idx, v in enumerate(vals)
        if isinstance(v, str)
    ]
    long_vals = [(k, i, v) for k, i, v in value_lengths if len(v) > 65]
    if long_vals:
        print(f"[FAIL] {len(long_vals)} values still exceed 65 chars:")
        for k, i, v in long_vals:
            print(f"  {k}[{i}] ({len(v)} chars): {v[:80]}")
        return 1
    else:
        print(f"[OK] All {len(value_lengths)} aspect values ≤ 65 chars")

    if result["aspect_count"] > 45:
        print(f"[FAIL] {result['aspect_count']} aspects exceed eBay limit of 45")
        return 1
    else:
        print(f"[OK] Aspect count {result['aspect_count']} ≤ 45")

    print()
    print("[DRY-RUN COMPLETE] No eBay API calls made.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
