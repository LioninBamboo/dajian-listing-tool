"""Listing Optimizer Service"""
import time
import re
import html
from datetime import datetime, timedelta
from typing import Any
from ..clients.dajian_client import DaJianClient
from ..clients.ebay_trading_client import EbayTradingClient
from ..db.database import Database
from ..db.models import Product

from ..services.market_research import MarketResearch
from ..services.gemini_optimizer import GeminiOptimizer

class ListingOptimizer:
    """ Listing Optimizer: Traffic Maximization Service """
    
    def __init__(
        self,
        dajian_client: DaJianClient,
        ebay_trading: EbayTradingClient,
        gemini: GeminiOptimizer, # Added Gemini
        db: Database
    ):
        self.dajian = dajian_client
        self.ebay = ebay_trading
        self.gemini = gemini
        self.db = db
        self.market_research = MarketResearch() # Initialize internal service
        
    def run_daily_heartbeat(self, limit: int = 50):
        """
        Daily Heartbeat: 'Touch' listings to keep them active.
        Strategy: Sync stock/price. Even if no change, we can send a trivial revision?
        Actually, best practice is to sync real stock. If stock hasn't changed, 
        eBay might ignore it, but we can check.
        """
        print("="*60)
        print("💓 Running Daily Heartbeat (Revise/Sync)")
        print("="*60)
        
        listed_products = self.db.get_products_by_status('listed', limit=limit)
        
        for product in listed_products:
            sku = product['sku']
            item_id = product.get('ebay_item_id')
            
            if not item_id or item_id == "Unknown":
                print(f"⏩ Skipping {sku}: No Item ID")
                continue
                
            try:
                # 1. Fetch latest data from Dajian
                latest = self.dajian.get_product_detail(sku)
                if not latest:
                    print(f"⚠️ Failed to fetch Dajian data for {sku}")
                    continue
                    
                new_stock = latest.get('stock', 0)
                new_price = latest.get('price', 0)
                
                # 2. Revise Item on eBay
                # Always send a revision for Stock to ensure "Heartbeat"
                print(f"🔄 Touching {sku} (Item: {item_id})...")
                
                # Construct Revision XML
                # We update Quantity and StartPrice
                xml_body = f"""
                    <Quantity>{new_stock}</Quantity>
                    <StartPrice>{new_price}</StartPrice>
                """
                
                response = self.ebay.revise_item(item_id, xml_body)
                
                if "<Ack>Success</Ack>" in response or "<Ack>Warning</Ack>" in response:
                    print(f"  ✓ Heartbeat Success: {sku}")
                    self.db.update_sync_status(
                        sku, 
                        'listed', 
                        dajian_stock=new_stock,
                        dajian_price=new_price
                    )
                    self.db.log_sync(sku, 'heartbeat', 'success', 'Daily revision success')
                else:
                    print(f"  ✗ Heartbeat Failed: {sku}")
                    # Log error but don't fail hard
                    err = self._extract_error(response)
                    self.db.log_sync(sku, 'heartbeat', 'failed', err)
                    
                time.sleep(1) # Rate limit
                
            except Exception as e:
                print(f"  ✗ Exception for {sku}: {e}")
                self.db.log_sync(sku, 'heartbeat', 'failed', str(e))
                
    def run_stale_refresher(self, age_days: int = 60, limit: int = 10):
        """
        Stale Refresher: End & Sell Similar for old items.
        
        [Enhanced]: Now includes Market Research & Title Optimization!
        """
        print("="*60)
        print(f"♻️ Running Stale Refresher (Age > {age_days} days)")
        print("="*60)
        
        with self.db.SessionLocal() as session:
            # Find products listed longer than age_days ago
            # We use created_at or listing_created_at
            cutoff = datetime.utcnow() - timedelta(days=age_days)
            
            # Query for products that match criteria
            candidates = session.query(Product).filter(
                Product.sync_status == 'listed',
                Product.ebay_item_id.isnot(None),
                Product.listing_created_at < cutoff
            ).limit(limit).all()
            
            if not candidates:
                print("No stale candidates found.")
                return

            for product in candidates:
                sku = product.sku
                item_id = product.ebay_item_id
                original_title = product.dajian_title
                
                print(f"♻️ Cycling Stale Listing: {sku} (Age: {product.listing_created_at})")
                
                # --- [NEW] Market Research & Optimization ---
                print(f"  🔎 Researching market for: {original_title[:30]}...")
                try:
                    # 1. Search Market
                    search_results = self.market_research.search_market(original_title[:50])
                    
                    # 2. Optimize Title with Market Data
                    optimized_title = self.gemini.optimize_title(
                        original_title,
                        category=product.dajian_category or "",
                        search_results=search_results
                    )
                    print(f"  ✨ New Optimized Title: {optimized_title}")
                    
                    # Update Product record immediately
                    product.optimized_title = optimized_title
                    
                except Exception as e:
                    print(f"  ⚠️ Research/Optimization failed, using old data: {e}")
                # --------------------------------------------
                
                # 3. End Item
                print(f"  Ending Item {item_id}...")
                end_resp = self.ebay.end_item(item_id, reason="NotAvailable")
                
                if not ("<Ack>Success</Ack>" in end_resp or "<Ack>Warning</Ack>" in end_resp):
                    print(f"  Failed to end item: {self._extract_error(end_resp)}")
                    continue
                    
                # 4. Relist
                print(f"  Marking for Re-listing...")
                product.sync_status = 'optimized' # Will be picked up by Pipeline for "Listing"
                product.ebay_item_id = None 
                product.last_refreshed_at = datetime.utcnow()
                session.commit()
                
                self.db.log_sync(sku, 'cycle', 'success', f'Ended {item_id}, Title optimized, Queued for relist')
                
    def _extract_error(self, xml_response: str) -> str:
        if "<LongMessage>" in xml_response:
             return xml_response.split("<LongMessage>")[1].split("</LongMessage>")[0]
        elif "<ShortMessage>" in xml_response:
             return xml_response.split("<ShortMessage>")[1].split("</ShortMessage>")[0]
        return "Unknown Error"
