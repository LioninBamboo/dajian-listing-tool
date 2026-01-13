"""
Real eBay API Client

Implements complete eBay listing workflow using:
- Inventory API: Product upload
- Account API: Policy management  
- Offer API: Listing creation and publishing
- Media API: Video upload
"""

import requests
import json
import os
import logging
from typing import Dict, List, Optional, Any
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager

# Create session with proxy bypass for eBay
def create_ebay_session():
    """Create requests session with eBay-specific settings"""
    session = requests.Session()
    # Disable proxy for eBay domains
    session.trust_env = False
    return session


class RealEbayClient:
    """Real eBay API Client for product listing"""
    
    def __init__(self, oauth_service: EbayOAuthService, policy_manager: EbayPolicyManager):
        """
        Initialize Real eBay Client
        
        Args:
            oauth_service: EbayOAuthService for authentication
            policy_manager: EbayPolicyManager for policy IDs
        """
        self.oauth = oauth_service
        self.policy_manager = policy_manager
        self.base_url = oauth_service.api_base
        self.marketplace_id = "EBAY_US"
        self.session = create_ebay_session()
    
    def create_or_replace_inventory_item(self, sku: str, product: Dict) -> Dict:
        """
        Create or update inventory item
        
        PUT /sell/inventory/v1/inventory_item/{sku}
        
        Args:
            sku: Product SKU
            product: Product data dict with:
                - title: str
                - description: str (HTML)
                - image_urls: List[str]
                - video_urls: List[str] (optional)
                - price: float
                - quantity: int
                - condition: str (default: "NEW")
                - aspects: Dict[str, List[str]]
                
        Returns:
            Response dict
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US"
        }
        
        # Build payload
        # eBay limits: title 80 chars, description 4000 chars, images 12, video 1
        description = product.get("description", "")
        
        # Warn if description is too long (should not happen with updated prompt)
        if len(description) > 4000:
            print(f"[WARN] Description is {len(description)} chars (limit: 4000)")
            print(f"   This should not happen - Gemini prompt needs adjustment")
            description = description[:3997] + "..."  # Fallback safety
        
        # Validate aspects format (must be list of strings)
        aspects = product.get("aspects", {})
        cleaned_aspects = {}
        for k, v in aspects.items():
            if isinstance(v, str):
                cleaned_aspects[k] = [v]
            elif isinstance(v, list):
                cleaned_aspects[k] = [str(i) for i in v]
            else:
                cleaned_aspects[k] = [str(v)]
                
        payload = {
            "condition": product.get("condition", "NEW"),
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": product.get("quantity", 1)
                }
            },
            "product": {
                "title": product["title"][:80],  # eBay limit
                "description": description,
                "imageUrls": product.get("image_urls", [])[:12],  # eBay max 12
                "aspects": cleaned_aspects
            }
        }
        # Note: Country comes from merchantLocationKey in offer, not inventory item
        
        # Add video if available
        if product.get("video_urls"):
            payload["product"]["videoIds"] = product["video_urls"][:1]  # eBay max 1 video
        
        response = self.session.put(url, headers=headers, json=payload)
        
        if response.status_code == 204:
            logging.info(f"[SUCCESS] Inventory item created/updated: {sku}")
            return {"status": "success", "sku": sku}
        else:
            logging.error(f"[ERROR] eBay API Error {response.status_code}")
            logging.error(f"   Response: {response.text}")
            response.raise_for_status()
            return response.json()
    
    def create_offer(self, sku: str, price: float, category_id: Optional[str] = None) -> Dict:
        """
        Create offer for inventory item
        
        POST /sell/inventory/v1/offer
        
        Args:
            sku: Product SKU
            price: Listing price
            category_id: eBay category ID (optional, will use suggested if not provided)
            
        Returns:
            {"offerId": "...", "status": "..."}
        """
        url = f"{self.base_url}/sell/inventory/v1/offer"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US"
        }
        
        # Try to get policy IDs (optional)
        try:
            fulfillment_policy_id = self.policy_manager.get_default_fulfillment_policy_id()
            return_policy_id = self.policy_manager.get_default_return_policy_id()
            payment_policy_id = self.policy_manager.get_default_payment_policy_id()
        except:
            # If policies not available, set to None
            # eBay will use account default policies
            fulfillment_policy_id = None
            return_policy_id = None
            payment_policy_id = None
            print("[WARN] No cached policies found, eBay will use account defaults")
        
        payload = {
            "sku": sku,
            "marketplaceId": self.marketplace_id,
            "format": "FIXED_PRICE",
            "merchantLocationKey": "DAJIAN_LA_WAREHOUSE",  # Los Angeles, CA
            "pricingSummary": {
                "price": {
                    "value": str(price),
                    "currency": "USD"
                }
            }
        }
        
        # Add policies if available
        if all([fulfillment_policy_id, return_policy_id, payment_policy_id]):
            payload["listingPolicies"] = {
                "fulfillmentPolicyId": fulfillment_policy_id,
                "returnPolicyId": return_policy_id,
                "paymentPolicyId": payment_policy_id
            }
            print(f"[OK] Using cached policies")
        else:
            # Use hardcoded user's policies as fallback
            payload["listingPolicies"] = {
                "fulfillmentPolicyId": "321897899021",
                "returnPolicyId": "321896608021",
                "paymentPolicyId": "321896606021"
            }
            print(f"[INFO] Using user's configured policies")
        
        # Add category if provided
        if category_id:
            payload["categoryId"] = category_id
        
        try:
            response = self.session.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            logging.info(f"[SUCCESS] Offer created: {result.get('offerId')}")
            return result
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                try:
                    error_data = e.response.json()
                    # Check if error is "Offer entity already exists" (ID 25002)
                    errors = error_data.get("errors", [])
                    for error in errors:
                        if error.get("errorId") == 25002:
                            # Extract offerId from parameters
                            for param in error.get("parameters", []):
                                if param.get("name") == "offerId":
                                    offer_id = param.get("value")
                                    print(f"[INFO] Offer already exists: {offer_id}")
                                    return {"offerId": offer_id, "status": "EXISTING"}
                except:
                    pass
            
            # If not handled, re-raise
            import sys
            sys.stderr.write(f"\n[ERROR] Create Offer Failed: {e.response.status_code}\n")
            sys.stderr.write(f"[ERROR] Response Body: {e.response.text}\n")
            logging.error(f"[ERROR] Create Offer Failed: {e.response.status_code}")
            logging.error(f"[ERROR] Response Body: {e.response.text}")
            raise
    
    def publish_offer(self, offer_id: str) -> Dict:
        """
        Publish offer to eBay
        
        POST /sell/inventory/v1/offer/{offerId}/publish
        
        Args:
            offer_id: Offer ID to publish
            
        Returns:
            {"listingId": "...", "status": "..."}
        """
        url = f"{self.base_url}/sell/inventory/v1/offer/{offer_id}/publish"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # No payload needed - Country comes from merchantLocationKey in offer
        response = self.session.post(url, headers=headers)
        
        if response.status_code != 200:
            logging.error(f"[ERROR] Publish failed: {response.status_code}")
            logging.error(f"[ERROR] Response: {response.text}")
            print(f"[DEBUG] Response status: {response.status_code}")
            print(f"[DEBUG] Response body: {response.text}")
        
        response.raise_for_status()
        
        result = response.json()
        listing_id = result.get("listingId")
        
        print(f"[OK] Listing published: {listing_id}")
        
        return result
    
    def upload_video(self, video_url: str, title: str, description: str = "") -> str:
        """
        Upload video to eBay Media API
        
        POST /sell/media/v1/video
        
        Args:
            video_url: URL of video to upload
            title: Video title
            description: Video description
            
        Returns:
            video_id: eBay video ID
        """
        url = f"{self.base_url}/sell/media/v1/video"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "title": title,
            "description": description or title,
            "videoUrl": video_url
        }
        
        response = self.session.post(url, headers=headers, json=payload)
        response.raise_for_status()
        
        result = response.json()
        video_id = result.get("videoId")
        
        video_id = result.get("videoId")
        
        print(f"[OK] Video upload initiated: {video_id}")
        
        return video_id
    
    def get_video_status(self, video_id: str) -> Dict:
        """
        Get video processing status
        
        GET /sell/media/v1/video/{videoId}
        
        Args:
            video_id: eBay video ID
            
        Returns:
            {"status": "PENDING|PROCESSING|LIVE|FAILED", ...}
        """
        url = f"{self.base_url}/sell/media/v1/video/{video_id}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        
        return response.json()
    
    def get_category_suggestions(self, title: str, limit: int = 3) -> List[Dict]:
        """
        Get category suggestions based on title
        
        GET /commerce/taxonomy/v1/category_tree/0/get_category_suggestions
        
        Args:
            title: Product title
            limit: Max number of suggestions
            
        Returns:
            List of category suggestions
        """
        url = f"{self.base_url}/commerce/taxonomy/v1/category_tree/0/get_category_suggestions"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        params = {
            "q": title
        }
        
        response = self.session.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        result = response.json()
        suggestions = result.get("categorySuggestions", [])
        
        return suggestions[:limit]


# Factory function for easy initialization
def create_real_ebay_client(environment: str = "SANDBOX") -> RealEbayClient:
    """
    Create a real eBay client with all dependencies
    
    Args:
        environment: "SANDBOX" or "PRODUCTION"
        
    Returns:
        Configured RealEbayClient instance
    """
    oauth = EbayOAuthService(environment)
    policy_manager = EbayPolicyManager(oauth)
    
    return RealEbayClient(oauth, policy_manager)


if __name__ == "__main__":
    # Test real eBay client
    client = create_real_ebay_client()
    
    if not client.oauth.is_authorized():
        print("[!] Not authorized. Please run OAuth flow first.")
        print(f"Authorization URL: {client.oauth.get_authorization_url()}")
        exit(1)
    
    print("[OK] eBay client initialized and authorized")
    
    # Test category suggestions
    suggestions = client.get_category_suggestions("Dining Chair")
    print(f"\n📂 Category Suggestions for 'Dining Chair':")
    for s in suggestions:
        print(f"  - {s.get('category', {}).get('categoryName')} (ID: {s.get('category', {}).get('categoryId')})")
