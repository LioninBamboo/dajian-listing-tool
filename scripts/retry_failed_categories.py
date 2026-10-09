"""
Retry the 7 failed category fixes by first updating inventory item aspects,
then changing the offer category.

Failures were:
- 3x Dining Sets (107578): missing "Set Includes"
- 1x Tables (38204): missing "Type"
- 1x Chairs (54235): missing "Type"
- 1x Shower Doors (147147): "Glass Thickness" multiple values
- 1x MIG Welders (124822): "Welding Process" multiple values
"""
import sys, os, logging, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient
from src.utils.ebay_quantity import coerce_nonnegative_quantity

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# SKU -> (offer_id, new_category_id, aspect_fixes)
# aspect_fixes: dict of {aspect_name: value_to_set} for missing aspects
#               or {aspect_name: None} to force single-value
FAILED_FIXES = {
    "W1675P307141": ("130282364011", "147147", {}),  # Installation multi-value
}

# Aspects that eBay requires to be single-value
SINGLE_VALUE_ASPECTS = {
    "Glass Thickness", "Welding Process", "Welded Material", "Type", "Brand",
    "Color", "Style", "Material", "Shape", "Pattern", "Finish", "Department",
    "Power Source", "Voltage", "Installation", "Door Type",
}


def fix_inventory_aspects(client: RealEbayClient, sku: str, aspect_fixes: dict):
    """
    Fetch inventory item, fix aspects (add missing or deduplicate multi-value), PUT back.
    Returns True if updated or no update needed.
    """
    item = client.get_inventory_item(sku)
    if not item:
        print(f"  [ERROR] Cannot fetch inventory item for {sku}")
        return False

    aspects = item.get("product", {}).get("aspects", {})
    changed = False

    # Add missing aspects
    for aspect_name, value in aspect_fixes.items():
        if aspect_name not in aspects or not aspects[aspect_name]:
            aspects[aspect_name] = [value]
            print(f"  [FIX] Added {aspect_name}={value}")
            changed = True

    # Fix multi-value aspects that should be single
    for aspect_name in list(aspects.keys()):
        vals = aspects[aspect_name]
        if aspect_name in SINGLE_VALUE_ASPECTS and isinstance(vals, list) and len(vals) > 1:
            aspects[aspect_name] = [vals[0]]
            print(f"  [FIX] {aspect_name}: kept first value '{vals[0]}' from {vals}")
            changed = True

    if not changed:
        print(f"  [INFO] No aspect changes needed for {sku}")
        return True

    # Rebuild product payload for PUT
    product_data = item.get("product", {})
    product_data["aspects"] = aspects

    availability = item.get("availability") or {}
    ship_to_location = availability.get("shipToLocationAvailability") or {}
    preserved_quantity = coerce_nonnegative_quantity(
        ship_to_location.get("quantity"),
        fallback=1,
    )
    payload = {
        "condition": item.get("condition", "NEW"),
        "availability": {
            "shipToLocationAvailability": {
                "quantity": preserved_quantity,
            }
        },
        "product": product_data,
    }
    # Sanitize packageWeightAndSize to avoid eBay 25709 errors
    pkg = item.get("packageWeightAndSize")
    if pkg:
        pkg = dict(pkg)
        # Validate weight
        w = pkg.get("weight", {})
        if w:
            try:
                val = float(w.get("value", 0))
                if val <= 0 or val > 2000:
                    pkg.pop("weight", None)
                else:
                    pkg["weight"] = {"value": round(val, 2), "unit": w.get("unit", "POUND")}
            except (ValueError, TypeError):
                pkg.pop("weight", None)
        # Validate dimensions
        for dim_key in ("dimensions", "packageDimensions"):
            d = pkg.get(dim_key, {})
            if d:
                cleaned = {"unit": d.get("unit", "INCH")}
                valid = True
                for axis in ("length", "width", "height"):
                    try:
                        v = float(d.get(axis, 0))
                        if v <= 0 or v > 999:
                            valid = False
                            break
                        cleaned[axis] = round(v, 2)
                    except (ValueError, TypeError):
                        valid = False
                        break
                if valid:
                    pkg[dim_key] = cleaned
                else:
                    pkg.pop(dim_key, None)
        if pkg:
            payload["packageWeightAndSize"] = pkg

    # PUT inventory item
    token = client.oauth.get_valid_token()
    url = f"{client.base_url}/sell/inventory/v1/inventory_item/{sku}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    resp = client.session.put(url, headers=headers, json=payload, timeout=60)
    if resp.status_code in (200, 204):
        print(f"  [OK] Inventory item {sku} aspects updated")
        return True
    else:
        print(f"  [ERROR] Failed to update inventory item {sku}: {resp.status_code} - {resp.text[:300]}")
        return False


def main():
    env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(env)
    policy_mgr = EbayPolicyManager(oauth)
    client = RealEbayClient(oauth, policy_mgr)

    ok_count = 0
    fail_count = 0

    for sku, (offer_id, new_cat, aspect_fixes) in FAILED_FIXES.items():
        print(f"\n[{sku}] Fixing aspects + category → {new_cat}")

        # Step 1: Fix inventory item aspects
        if not fix_inventory_aspects(client, sku, aspect_fixes):
            fail_count += 1
            continue

        time.sleep(2)  # Brief pause between API calls

        # Step 2: Update offer category
        success = client.update_offer_category(offer_id, new_cat)
        if success:
            print(f"  [OK] {sku}: offer {offer_id} → category {new_cat}")
            ok_count += 1
        else:
            print(f"  [FAIL] {sku}: offer update failed for {offer_id}")
            fail_count += 1

        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"DONE: {ok_count} fixed, {fail_count} failed out of {len(FAILED_FIXES)}")


if __name__ == "__main__":
    main()
