from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
import uvicorn
import json
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import sys
import os
# Add root to sys.path to allow importing src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import collection database
from src.db.collection_db import get_db, init_db
from src.db.collection_models import CollectedProduct

app = FastAPI()

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    import logging
    # Force add handler because Uvicorn already configured logging
    root_logger = logging.getLogger()
    handler = logging.FileHandler('debug_server.log', mode='w', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    
    logging.info("Server starting up (handler forced)...")
    init_db()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import logging
    logging.error(f"[CRITICAL] Global exception: {exc}")
    import traceback
    logging.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": f"Global Error: {str(exc)}"},
    )

# --- Serving Static Dashboard ---
from fastapi.staticfiles import StaticFiles
import os
from pathlib import Path

# Robust Path Resolution
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "src" / "web"

if not WEB_DIR.exists():
    os.makedirs(WEB_DIR, exist_ok=True)
    # Create a dummy index if missing to avoid 404 loop
    with open(WEB_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write("<h1>Dashboard Not Found (Re-create dashboard.html as index.html)</h1>")

app.mount("/dashboard", StaticFiles(directory=str(WEB_DIR), html=True), name="static")

# --- Config ---
BRAND_NAME = "AquaVerve"

# --- Models ---
class ProductPayload(BaseModel):
    sku: str
    title: str
    price: float
    shipping: float = 0.0 # NEW: Support Shipping Cost
    stock: int = 0
    description: str = ""
    images: List[str] = []
    videos: List[str] = []
    attributes: Dict[str, Any] = {}
    url: str | None = None

# --- Mock eBay Client (As requested) ---
class MockEbayClient:
    def create_or_replace_inventory_item(self, sku: str, product: Dict[str, Any]):
        print(f"[MockEbay] Creating Inventory Item for {sku}...")
        print(f"           Brand: {product.get('aspects', {}).get('Brand')}")
        print(f"           Title: {product.get('title')}")
        return {"sku": sku, "status": "created"}
    
    def create_offer(self, sku: str, price: float):
        print(f"[MockEbay] Creating Offer for {sku} at ${price}...")
        return {"offerId": f"OFFER-{sku}-123", "status": "created"}
    
    def publish_offer(self, offer_id: str):
        print(f"[MockEbay] Publishing Offer {offer_id}...")
        return {"listingId": f"LISTING-{offer_id}-999"}

ebay_client = MockEbayClient()

# Database is now SQLite - no more in-memory dict! 

class PublishRequest(BaseModel):
    sku: str

# --- Background Logic (Analysis Only) ---
def analyze_product_task(sku: str):
    """Background task to analyze product pricing and run AI optimization"""
    print(f" Analyzing SKU: {sku}...")
    
    # Create new database session for background task
    from src.db.collection_db import SessionLocal
    db = SessionLocal()
    
    try:
        product = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not product:
            print(f"[ERROR] Product {sku} not found in database")
            return
        
        # 1. Pricing Calculation
        from src.services.pricing_engine import PricingEngine
        
        # Check if product has dimensions for oversize calculation
        specs = product.specs or {}
        attributes = product.attributes or {}
        is_oversize = 'Dimensions' in specs or 'Dimensions' in attributes or 'oversize' in str(attributes).lower()
        
        dajian_costs = PricingEngine.calculate_dajian_cost(
            product_price=product.price,
            shipping_cost=product.shipping,
            is_oversize=is_oversize
        )
        safe_price = PricingEngine.calculate_selling_price(dajian_costs["total_dajian_cost"], 0.15)
        min_price = PricingEngine.calculate_selling_price(dajian_costs["total_dajian_cost"], 0.08)
        
        market_price = None  # Placeholder for future market research
        decision = PricingEngine.determine_final_price(safe_price, min_price, market_price)
        
        # Save pricing breakdown
        product.cost_breakdown = dajian_costs
        product.suggested_price = decision["price"]
        
        # 2. AI Optimization (Switched to Qwen)
        from qwen_optimizer import QwenOptimizer
        import os
        QWEN_KEY = os.getenv("QWEN_API_KEY")
        
        if not QWEN_KEY:
            # Mock optimization
            product.optimization = {
                "title": f"MOCK - {product.title}"[:80],
                "description": product.description,
                "aspects": {"Brand": [BRAND_NAME]}
            }
        else:
            qwen = QwenOptimizer(api_key=QWEN_KEY)
            opt_data = qwen.optimize_product_full(
                original_title=product.title,
                original_description=product.description,
                attributes=attributes,
                images=product.images or []
            )
            product.optimization = opt_data
        
        # Update status
        product.status = "READY"
        product.logs = (product.logs or []) + [f"Analysis complete at {datetime.utcnow().isoformat()}"]
        
        db.commit()
        print(f"?Analysis Complete for {sku}")
        
    except Exception as e:
        logging.error(f"[ERROR] Analysis Failed for {sku}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        if product:
            product.status = "ERROR"
            product.logs = (product.logs or []) + [f"Error: {str(e)}"]
            db.commit()
    finally:
        db.close()

# --- API Endpoints ---

@app.get("/api/products")
async def get_products(db: Session = Depends(get_db)):
    """List all collected products"""
    products = db.query(CollectedProduct).order_by(CollectedProduct.created_at.desc()).all()
    return {"status": "success", "products": [p.to_dict() for p in products]}

from fastapi import Request

@app.post("/api/collect")
async def collect_product(
    payload: ProductPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Receives product from browser extension and saves to database.
    """
    try:
        raw_body = await request.json()
        print(f" RAW BODY RECEIVED: {json.dumps(raw_body, indent=2)}")
        
        print(f" Received Product: {payload.sku} ({len(payload.images)} imgs, {len(payload.videos)} vids, Shipping: ${payload.shipping})")
        
        # Check if product already exists
        existing = db.query(CollectedProduct).filter_by(sku=payload.sku).first()
        
        if existing:
            # Update existing product
            existing.title = payload.title
            existing.price = payload.price
            existing.shipping = payload.shipping
            existing.stock = payload.stock
            existing.description = payload.description
            existing.images = payload.images
            existing.videos = payload.videos
            existing.attributes = payload.attributes
            existing.url = payload.url
            existing.status = "COLLECTED"
            existing.logs = (existing.logs or []) + [f"Updated from extension at {datetime.utcnow().isoformat()}"]
            db.commit()
            print(f" Updated existing product: {payload.sku}")
        else:
            # Create new product
            new_product = CollectedProduct(
                sku=payload.sku,
                title=payload.title,
                price=payload.price,
                shipping=payload.shipping,
                stock=payload.stock,
                description=payload.description,
                images=payload.images,
                videos=payload.videos,
                attributes=payload.attributes,
                specs={},  # Will be populated from attributes if available
                url=payload.url,
                status="COLLECTED",
                logs=[f"Received from extension at {datetime.utcnow().isoformat()}"]
            )
            db.add(new_product)
            db.commit()
            print(f"?Created new product: {payload.sku}")
        
        # Schedule background optimization
        print(f"?Scheduling background optimization for {payload.sku}...")
        background_tasks.add_task(analyze_product_task, payload.sku)
        
        return {
            "status": "success",
            "message": "Product collected successfully",
            "sku": payload.sku
        }
        
    except Exception as e:
        print(f"[ERROR] Error collecting product: {e}")
        import traceback
        traceback.print_exc()
        
        # Return JSON error instead of raising exception
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Failed to collect product: {str(e)}",
                "error": str(e)
            }
        )


@app.post("/api/publish/{sku}")
async def publish_product(sku: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Trigger Final eBay Listing (Confirm & List workflow)
    Uses real eBay API with video upload support
    """
    # Imports moved to try block
    
    product = db.query(CollectedProduct).filter_by(sku=sku).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    if product.status == "PUBLISHED":
        return {"status": "error", "message": "Already published", "listing_id": product.listing_id}

    # Internal imports to avoid circular deps if any, and ensure availability
    import os
    from src.clients.real_ebay_client import create_real_ebay_client
    from src.services.ebay_video_uploader import upload_video_background

    # Check if optimization is ready
    if not product.optimization or not product.cost_breakdown:
        return {"status": "error", "message": "AI Analysis not complete yet. Please wait."}

    # Check eBay authorization
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    
    try:
        ebay_client = create_real_ebay_client(environment)
        
        if not ebay_client.oauth.is_authorized():
            raise HTTPException(status_code=401, detail="eBay not authorized. Please authorize at /ebay/auth")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize eBay client: {e}")

    # Proceed to List
    try:
        final_price = product.suggested_price
        opt_data = product.optimization
        
        # Get category suggestions if not already set
        category_id = opt_data.get("categoryId")
        if not category_id:
            suggestions = ebay_client.get_category_suggestions(opt_data.get("title", product.title))
            if suggestions:
                category_id = suggestions[0].get("category", {}).get("categoryId")
                print(f" Using suggested category: {category_id}")
        
        # 1. Create Inventory Item
        print(f" Creating inventory item for {sku}...")
        ebay_client.create_or_replace_inventory_item(
            sku=product.sku,
            product={
                "title": opt_data.get("title", product.title),
                "description": opt_data.get("description", product.description),
                "image_urls": product.images or [],
                "price": final_price,
                "quantity": product.stock if product.stock > 0 else 1,
                "condition": "NEW",
                "aspects": opt_data.get("aspects", {"Brand": [BRAND_NAME]})
            }
        )
        
        # 2. Handle video upload (async)
        if product.videos and len(product.videos) > 0:
            video_url = product.videos[0]
            video_title = opt_data.get("title", product.title)[:80]
            
            print(f" Scheduling async video upload for {sku}...")
            background_tasks.add_task(
                upload_video_background,
                video_url,
                sku,
                video_title,
                environment
            )
            
            # Mark video as processing
            if not product.optimization:
                product.optimization = {}
            product.optimization["video_status"] = "PROCESSING"
            db.commit()
        
        # 3. Create Offer
        print(f" Creating offer for {sku}...")
        offer = ebay_client.create_offer(
            sku=product.sku,
            price=final_price,
            category_id=category_id
        )
        
        # 4. Publish (DISABLED - Save as Draft Mode)
        if offer and offer.get("offerId"):
            # SKIPPING LIVE PUBLISH AS REQUESTED
            # print(f" Publishing offer {offer['offerId']}...")
            # listing = ebay_client.publish_offer(offer["offerId"])
            
            product.listing_id = "DRAFT-OFFER-" + offer["offerId"]
            product.status = "READY_TO_PUBLISH" # specific status for drafts
            product.published_at = datetime.utcnow() # timestamp of draft creation
            product.logs = (product.logs or []) + [
                f"Saved as DRAFT (offer created): {offer['offerId']}",
                f"Category ID: {category_id}" if category_id else "No category"
            ]
            
            if product.videos:
                product.logs.append("Video upload in progress...")
            
            db.commit()
            
            print(f"?Product {sku} saved as DRAFT. Offer ID: {offer['offerId']}")
            
            return {
                "status": "success",
                "message": "Product saved as Draft (Offer created)",
                "listing_id": None, 
                "offer_id": offer["offerId"],
                "video_processing": bool(product.videos)
            }
        else:
            raise Exception("Failed to create eBay offer.")

    except Exception as e:
        error_msg = str(e) if str(e) else "Unknown error occurred"
        product.logs = (product.logs or []) + [f"Publish failed: {error_msg}"]
        db.commit()
        logging.error(f"[ERROR] Failed to publish {sku}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        
        # Check for common issues
        if "authorized" in error_msg.lower() or "token" in error_msg.lower():
            return {"status": "error", "message": "eBay authorization expired. Please re-authorize at /ebay/auth"}
        elif "refresh" in error_msg.lower():
            return {"status": "error", "message": "eBay token expired. Please re-authorize at /ebay/auth"}
        else:
            return {"status": "error", "message": f"Failed to publish: {error_msg}"}

# --- End of Server Logic ---

@app.get("/history")
async def history_page():
    """Serve the history dashboard"""
    return FileResponse(str(WEB_DIR / "history.html"))

# --- eBay OAuth Endpoints ---

@app.get("/ebay/auth")
async def ebay_auth_start():
    """Start eBay OAuth authorization flow"""
    from src.services.ebay_auth import EbayOAuthService
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    auth_url = oauth.get_authorization_url(state="ebay_auth_state")
    
    # Redirect to eBay authorization page
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=auth_url)

@app.get("/ebay/callback")
async def ebay_auth_callback(
    code: Optional[str] = None,
    ebayktn: Optional[str] = None,  # eBay's actual parameter name
    tknexp: Optional[str] = None,
    username: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None
):
    """Handle eBay OAuth callback"""
    from src.services.ebay_auth import EbayOAuthService
    from fastapi.responses import HTMLResponse
    from fastapi import Request
    import os
    
    # Log all query parameters for debugging
    print(f" OAuth Callback received:")
    print(f"   code: {code[:20] if code else 'None'}...")
    print(f"   ebayktn: {ebayktn[:20] if ebayktn else 'None'}...")
    print(f"   tknexp: {tknexp}")
    print(f"   username: {username}")
    print(f"   state: {state}")
    print(f"   error: {error}")
    print(f"   error_description: {error_description}")
    
    # eBay uses 'ebayktn' instead of 'code' for some flows
    # Use whichever is present
    auth_code = code or ebayktn
    
    # Check for errors from eBay
    if error:
        error_html = f"""
            <html>
                <head><title>eBay Authorization Failed</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>[ERROR] eBay Authorization Failed</h1>
                    <p><strong>Error:</strong> {error}</p>
                    <p><strong>Description:</strong> {error_description or 'No description'}</p>
                    <p>Please try again or contact support.</p>
                </body>
            </html>
        """
        return HTMLResponse(content=error_html, status_code=400)
    
    # Check if code is present
    if not auth_code:
        error_html = f"""
            <html>
                <head><title>eBay Authorization Error</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>[ERROR] Authorization Code Missing</h1>
                    <p>The authorization code was not received from eBay.</p>
                    <p><strong>Received parameters:</strong></p>
                    <ul style="text-align: left; display: inline-block;">
                        <li>code: {code or 'None'}</li>
                        <li>ebayktn: {ebayktn or 'None'}</li>
                        <li>tknexp: {tknexp or 'None'}</li>
                        <li>username: {username or 'None'}</li>
                    </ul>
                    <p>Please try the authorization process again.</p>
                    <a href="/ebay/auth">Try Again</a>
                </body>
            </html>
        """
        return HTMLResponse(content=error_html, status_code=400)
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    try:
        # Exchange code for token
        print(f" Exchanging code for token...")
        print(f"   Using auth_code: {auth_code[:20]}...")
        token_data = oauth.exchange_code_for_token(auth_code)
        print(f"?Token received successfully!")
        
        # Return success page
        return HTMLResponse(content=f"""
            <html>
                <head><title>eBay Authorization Success</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>?eBay Authorization Successful!</h1>
                    <p>Your eBay account <strong>{username or 'Unknown'}</strong> has been successfully authorized.</p>
                    <p>Token has been saved and will be automatically refreshed.</p>
                    <p>Token expires: {tknexp or 'Unknown'}</p>
                    <p>You can now close this window.</p>
                    <script>
                        setTimeout(() => {{
                            window.close();
                        }}, 3000);
                    </script>
                </body>
            </html>
        """)
    except Exception as e:
        print(f"[ERROR] Token exchange failed: {e}")
        import traceback
        traceback.print_exc()
        
        error_html = f"""
            <html>
                <head><title>eBay Authorization Error</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>[ERROR] Authorization Failed</h1>
                    <p><strong>Error:</strong> {str(e)}</p>
                    <p><strong>Auth Code Used:</strong> {auth_code[:20] if auth_code else 'None'}...</p>
                    <p>Please check the server logs for details.</p>
                    <a href="/ebay/auth">Try Again</a>
                </body>
            </html>
        """
        return HTMLResponse(content=error_html, status_code=400)

@app.get("/api/ebay/auth/status")
async def ebay_auth_status():
    """Check eBay authorization status"""
    from src.services.ebay_auth import EbayOAuthService
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    is_authorized = oauth.is_authorized()
    
    return {
        "authorized": is_authorized,
        "environment": environment
    }

@app.get("/api/ebay/policies")
async def get_ebay_policies():
    """Get cached eBay policies"""
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_policy_manager import EbayPolicyManager
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        raise HTTPException(status_code=401, detail="Not authorized. Please authorize eBay first.")
    
    policy_manager = EbayPolicyManager(oauth)
    
    return {
        "policies": policy_manager.get_all_policies(),
        "defaults": {
            "fulfillment": policy_manager.get_default_fulfillment_policy_id(),
            "return": policy_manager.get_default_return_policy_id(),
            "payment": policy_manager.get_default_payment_policy_id()
        }
    }

@app.post("/api/ebay/policies/refresh")
async def refresh_ebay_policies():
    """Fetch and cache eBay policies from API"""
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_policy_manager import EbayPolicyManager
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        raise HTTPException(status_code=401, detail="Not authorized. Please authorize eBay first.")
    
    policy_manager = EbayPolicyManager(oauth)
    policy_manager.fetch_and_cache_all_policies()
    
    return {"status": "success", "message": "Policies refreshed"}

@app.get("/")
def health_check():
    return {"status": "running", "service": "Ebay Copilot Server"}

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
