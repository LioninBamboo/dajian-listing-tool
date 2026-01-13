"""
eBay Video Uploader Service

Handles async video upload to eBay Media API with status tracking
"""

import asyncio
import requests
from typing import Optional
from src.services.ebay_auth import EbayOAuthService
from src.db.collection_db import get_db
from src.db.collection_models import CollectedProduct


class EbayVideoUploader:
    """Async video uploader for eBay Media API"""
    
    def __init__(self, oauth_service: EbayOAuthService):
        """
        Initialize Video Uploader
        
        Args:
            oauth_service: EbayOAuthService for authentication
        """
        self.oauth = oauth_service
        self.base_url = oauth_service.api_base
    
    async def upload_video_async(self, video_url: str, sku: str, title: str):
        """
        Upload video asynchronously and track status
        
        Args:
            video_url: URL of video to upload
            sku: Product SKU
            title: Video title
        """
        print(f"📹 Starting async video upload for {sku}...")
        
        try:
            # 1. Initiate upload
            video_id = self._upload_video(video_url, title)
            
            # 2. Update product record with video ID and status
            self._update_product_video_status(sku, video_id, "PROCESSING")
            
            # 3. Poll status in background
            await self._poll_video_status(video_id, sku)
            
        except Exception as e:
            print(f"❌ Video upload failed for {sku}: {e}")
            self._update_product_video_status(sku, None, "FAILED")
    
    def _upload_video(self, video_url: str, title: str) -> str:
        """
        Upload video to eBay Media API
        
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
            "description": title,
            "videoUrl": video_url
        }
        
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        
        result = response.json()
        video_id = result.get("videoId")
        
        print(f"✅ Video upload initiated: {video_id}")
        
        return video_id
    
    async def _poll_video_status(self, video_id: str, sku: str, max_attempts: int = 60):
        """
        Poll video processing status
        
        Args:
            video_id: eBay video ID
            sku: Product SKU
            max_attempts: Max polling attempts (60 = 30 minutes at 30s intervals)
        """
        for attempt in range(max_attempts):
            await asyncio.sleep(30)  # Wait 30 seconds between checks
            
            try:
                status = self._get_video_status(video_id)
                status_value = status.get("status", "UNKNOWN")
                
                print(f"📹 Video {video_id} status: {status_value} (attempt {attempt + 1}/{max_attempts})")
                
                if status_value == "LIVE":
                    # Video is ready
                    self._update_product_video_status(sku, video_id, "LIVE")
                    print(f"✅ Video {video_id} is now LIVE")
                    break
                elif status_value == "FAILED":
                    # Video processing failed
                    self._update_product_video_status(sku, video_id, "FAILED")
                    print(f"❌ Video {video_id} processing failed")
                    break
                elif status_value in ["PENDING", "PROCESSING"]:
                    # Still processing, continue polling
                    continue
                else:
                    # Unknown status
                    print(f"⚠️ Unknown video status: {status_value}")
                    
            except Exception as e:
                print(f"⚠️ Error checking video status: {e}")
                continue
        
        # If we exhausted all attempts
        if attempt == max_attempts - 1:
            print(f"⏱️ Video {video_id} status check timed out")
            self._update_product_video_status(sku, video_id, "TIMEOUT")
    
    def _get_video_status(self, video_id: str) -> dict:
        """Get video processing status from eBay"""
        url = f"{self.base_url}/sell/media/v1/video/{video_id}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        return response.json()
    
    def _update_product_video_status(self, sku: str, video_id: Optional[str], status: str):
        """Update product record with video status"""
        db = next(get_db())
        
        try:
            product = db.query(CollectedProduct).filter_by(sku=sku).first()
            
            if product:
                # Store video status in optimization data
                if not product.optimization:
                    product.optimization = {}
                
                product.optimization["video_id"] = video_id
                product.optimization["video_status"] = status
                
                db.commit()
                print(f"✅ Updated video status for {sku}: {status}")
        finally:
            db.close()
    
    def get_video_status_for_sku(self, sku: str) -> Optional[dict]:
        """
        Get video status for a product
        
        Args:
            sku: Product SKU
            
        Returns:
            {"video_id": "...", "status": "PENDING|PROCESSING|LIVE|FAILED"}
        """
        db = next(get_db())
        
        try:
            product = db.query(CollectedProduct).filter_by(sku=sku).first()
            
            if product and product.optimization:
                return {
                    "video_id": product.optimization.get("video_id"),
                    "status": product.optimization.get("video_status", "UNKNOWN")
                }
            
            return None
        finally:
            db.close()


# Background task helper
async def upload_video_background(video_url: str, sku: str, title: str, environment: str = "SANDBOX"):
    """
    Background task for video upload
    
    Args:
        video_url: Video URL
        sku: Product SKU
        title: Video title
        environment: eBay environment
    """
    oauth = EbayOAuthService(environment)
    uploader = EbayVideoUploader(oauth)
    
    await uploader.upload_video_async(video_url, sku, title)


if __name__ == "__main__":
    # Test video uploader
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    oauth = EbayOAuthService()
    
    if not oauth.is_authorized():
        print("❌ Not authorized. Please run OAuth flow first.")
        exit(1)
    
    uploader = EbayVideoUploader(oauth)
    
    # Example: Check video status for a SKU
    status = uploader.get_video_status_for_sku("TEST-SKU")
    print(f"Video status: {status}")
