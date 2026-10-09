import requests
import json
import time
import random

# Generate a unique SKU for this run
RUN_ID = random.randint(1000, 9999)
SKU = f"FLOW-TEST-{RUN_ID}"
BASE_URL = "http://localhost:8000"

def step_1_collect():
    print(f"\n[1/3] [STEP] Simulating Browser Extension: Collecting {SKU}...")
    
    payload = {
        "sku": SKU,
        "title": "Modern Ergonomic Office Chair with Lumbar Support",
        "price": 129.99,
        "shipping": 0.0,
        "stock": 50,
        "description": "High quality mesh office chair. Breathable back, adjustable height, 360 swivel. Perfect for home office.",
        "images": [
            "https://images.unsplash.com/photo-1592078615290-033ee584e267?q=80&w=1000&auto=format&fit=crop"
        ],
        "videos": [],
        "attributes": {
            "Brand": "ErgoLife",
            "Color": "Black",
            "Material": "Mesh"
        },
        "url": "https://supplier.com/product/123"
    }
    
    try:
        res = requests.post(f"{BASE_URL}/api/collect", json=payload)
        res.raise_for_status()
        print(f"   [SUCCESS] Product collected: {res.json()['message']}")
    except Exception as e:
        print(f"   [ERROR] Collection failed: {e}")
        exit(1)

def step_2_wait_for_ai():
    print(f"\n[2/3] [WAIT] Waiting for Qwen AI Optimization...")
    print("   Polling status every 2 seconds...", end="")
    
    max_retries = 30 # Wait up to 60 seconds
    for i in range(max_retries):
        try:
            res = requests.get(f"{BASE_URL}/api/products")
            data = res.json()
            products = data.get("products", [])
            
            # Find our product
            target = next((p for p in products if p['sku'] == SKU), None)
            
            if target:
                status = target.get('status')
                if status == 'READY':
                    print(f"\n   [SUCCESS] AI Optimization Complete!")
                    print(f"   Generated Title: {target['optimization']['title']}")
                    print(f"   Aspects: {target['optimization']['aspects']}")
                    return
                elif status == 'ERROR':
                    print(f"\n   [ERROR] AI Analysis Failed! Check logs.")
                    print(f"   Logs: {target.get('logs')}")
                    exit(1)
            
            print(".", end="", flush=True)
            time.sleep(2)
            
        except Exception as e:
            print(f"\n   [ERROR] Polling failed: {e}")
            exit(1)
            
    print("\n   [TIMEOUT] AI Optimization took too long.")
    exit(1)

def step_3_publish_draft():
    print(f"\n[3/3] [PUBLISH] Triggering Publish (Save as Draft)...")
    
    try:
        res = requests.post(f"{BASE_URL}/api/publish/{SKU}")
        data = res.json()
        
        if res.status_code == 200 and data.get("status") == "success":
            print(f"   [SUCCESS] Draft Created Successfully!")
            print(f"   Offer ID: {data.get('offer_id')}")
            print(f"   Video Processing: {data.get('video_processing')}")
        else:
            print(f"   [ERROR] Publish failed: {res.status_code}")
            print(f"   Response: {json.dumps(data, indent=2)}")
            exit(1)
            
    except Exception as e:
        print(f"   [ERROR] Publish request failed: {e}")
        exit(1)

if __name__ == "__main__":
    print(f"=== Starting End-to-End Test (SKU: {SKU}) ===")
    step_1_collect()
    step_2_wait_for_ai()
    step_3_publish_draft()
    print("\n=== [PASS] Full Flow Verification Passed! ===")
