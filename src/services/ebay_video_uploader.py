"""
eBay Video Uploader Service - Updated for Media API v1_beta

Handles video upload to eBay Media API with status tracking
Uses the correct endpoint: https://apim.ebay.com/commerce/media/v1_beta/video

Improvements:
- Retry mechanism with exponential backoff
- SSL error handling
- Synchronous upload option for reliability
"""

import asyncio
import requests
import os
import time
import sqlite3
import json
from typing import Optional, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy.orm.attributes import flag_modified
from src.services.ebay_auth import EbayOAuthService
from src.db.collection_db import get_db
from src.db.collection_models import CollectedProduct
from src.utils.html_truncator import smart_truncate_html


def create_session_with_retry(retries=5, backoff_factor=1.0, status_forcelist=(500, 502, 503, 504)):
    """Create requests session with automatic retry for 5xx errors"""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,  # Wait 1s, 2s, 4s, 8s, 16s between retries
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
        raise_on_status=False  # Don't raise exception on retry, let us handle it
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class EbayVideoUploader:
    """Video uploader for eBay Media API v1_beta"""
    
    # Media API base URL (different from regular API)
    MEDIA_API_BASE = "https://apim.ebay.com"
    MEDIA_API_BASE_SANDBOX = "https://apim.sandbox.ebay.com"
    
    # Retry settings - increased for eBay 503 errors
    MAX_RETRIES = 5
    RETRY_DELAY = 3  # seconds
    
    def __init__(self, oauth_service: EbayOAuthService):
        """
        Initialize Video Uploader
        
        Args:
            oauth_service: EbayOAuthService for authentication
        """
        self.oauth = oauth_service
        self.session = create_session_with_retry(retries=3)
        self.last_upload_error = ""
        
        # Use correct Media API base URL
        if oauth_service.environment == "PRODUCTION":
            self.media_base_url = self.MEDIA_API_BASE
        else:
            self.media_base_url = self.MEDIA_API_BASE_SANDBOX
    
    def _clean_video_title(self, title: str) -> str:
        """Clean video title to remove markup characters that eBay doesn't allow"""
        import re
        import html
        
        if not title:
            return "Product Video"
        
        # Decode HTML entities first (e.g., &amp; -> &)
        clean = html.unescape(title)
        
        # Remove HTML tags
        clean = re.sub(r'<[^>]+>', '', clean)
        
        # Replace problematic characters
        # eBay doesn't allow: < > & " ' and other markup characters
        replacements = {
            '&': 'and',
            '<': '',
            '>': '',
            '"': '',
            "'": '',
            '™': '',
            '®': '',
            '©': '',
            '°': ' degrees',
        }
        
        for old, new in replacements.items():
            clean = clean.replace(old, new)
        
        # Remove any remaining non-ASCII characters that might cause issues
        clean = re.sub(r'[^\x20-\x7E]', '', clean)
        
        # Normalize whitespace
        clean = re.sub(r'\s+', ' ', clean).strip()
        
        return clean if clean else "Product Video"
    
    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make request with retry on failure"""
        last_error = None
        
        for attempt in range(self.MAX_RETRIES):
            try:
                if method.upper() == "GET":
                    response = self.session.get(url, timeout=120, **kwargs)
                elif method.upper() == "POST":
                    response = self.session.post(url, timeout=120, **kwargs)
                elif method.upper() == "PUT":
                    response = self.session.put(url, timeout=120, **kwargs)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                
                return response
                
            except (requests.exceptions.ConnectionError, 
                    requests.exceptions.SSLError,
                    requests.exceptions.Timeout) as e:
                last_error = e
                print(f"[Video] Request failed (attempt {attempt + 1}/{self.MAX_RETRIES}): {type(e).__name__}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY * (attempt + 1))  # Exponential backoff
                    # Recreate session to get fresh connection
                    self.session = create_session_with_retry(retries=3)
        
        raise last_error
    
    def create_video_resource(self, title: str, description: str, file_size: int) -> Tuple[str, str]:
        """
        Step 1: Create a video resource on eBay
        
        Args:
            title: Video title (max 160 chars)
            description: Video description
            file_size: Size of video file in bytes (max 157,286,400 bytes = ~150MB)
            
        Returns:
            Tuple of (video_id, upload_url)
        """
        url = f"{self.media_base_url}/commerce/media/v1_beta/video"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Clean title - remove HTML/markup characters that eBay doesn't allow
        clean_title = self._clean_video_title(title)
        clean_desc = self._clean_video_title(description) if description else clean_title
        
        payload = {
            "title": clean_title[:50],  # Max 50 chars for video title!
            "description": clean_desc[:500],
            "size": file_size,
            "classification": ["ITEM"]
        }
        
        print(f"[Video] Creating video resource: {clean_title[:50]}...")
        
        response = self._request_with_retry("POST", url, headers=headers, json=payload)
        
        if response.status_code == 201:
            # Video ID is in the Location header
            location = response.headers.get("Location", "")
            video_id = location.split("/")[-1] if location else None
            
            if video_id:
                print(f"[Video] Created video resource: {video_id}")
                return video_id, location
            else:
                raise Exception("No video ID in Location header")
        else:
            error_msg = response.text[:500]
            raise Exception(f"Failed to create video resource: {response.status_code} - {error_msg}")
    
    def upload_video_content(self, video_id: str, video_data: bytes, content_type: str = "video/mp4") -> bool:
        """
        Step 2: Upload video content.

        eBay's initial upload request expects the full binary stream to match the
        size declared in createVideo(). Resumable upload is only relevant after a
        partial transfer error; starting with a 5 MB chunk causes large videos to
        fail with "Content length does not match video size".
        
        Args:
            video_id: Video ID from createVideo
            video_data: Video file bytes
            content_type: Video MIME type
            
        Returns:
            True if upload successful
        """
        url = f"{self.media_base_url}/commerce/media/v1_beta/video/{video_id}/upload"
        
        token = self.oauth.get_valid_token()
        
        total_size = len(video_data)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(total_size),
        }

        print(f"[Video] Uploading {total_size} bytes...")
        response = self._request_with_retry("POST", url, headers=headers, data=video_data)
        if response.status_code not in [200, 201, 202, 206]:
            raise Exception(f"Video upload failed: {response.status_code} - {response.text[:200]}")
        print(f"[Video] Upload complete for {video_id}")
        return True
    
    def get_video_status(self, video_id: str) -> dict:
        """
        Step 3: Get video processing status
        
        Args:
            video_id: Video ID
            
        Returns:
            Video status dict with 'status' field (PENDING_UPLOAD, PROCESSING, LIVE, BLOCKED, etc.)
        """
        url = f"{self.media_base_url}/commerce/media/v1_beta/video/{video_id}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        try:
            response = self._request_with_retry("GET", url, headers=headers)
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}
        
        if response.status_code == 200:
            return response.json()
        else:
            return {"status": "ERROR", "error": response.text[:200]}
    
    def upload_video_from_url(self, video_url: str, title: str, description: str = None) -> Optional[str]:
        """
        Download video from URL and upload to eBay
        
        Args:
            video_url: URL of video to download
            title: Video title
            description: Video description
            
        Returns:
            video_id if successful, None otherwise
        """
        try:
            self.last_upload_error = ""
            print(f"[Video] Downloading video from URL: {video_url[:80]}...")
            
            # Download video with retry
            download_session = create_session_with_retry(retries=3)
            response = download_session.get(video_url, stream=True, timeout=120)
            response.raise_for_status()
            
            video_data = response.content
            file_size = len(video_data)
            content_type = response.headers.get("Content-Type", "video/mp4")
            
            print(f"[Video] Downloaded {file_size} bytes, type: {content_type}")
            
            # Validate it's actually a video
            if 'video' not in content_type.lower():
                self.last_upload_error = "unsupported_source: not a direct downloadable video file"
                raise Exception("Video source is not a direct downloadable video file")
            
            # Validate size (max ~150MB)
            if file_size > 157286400:
                raise Exception(f"Video too large: {file_size} bytes (max 157,286,400)")
            
            if file_size < 1000:
                self.last_upload_error = "invalid_video: downloaded file is too small"
                raise Exception(f"Video too small: {file_size} bytes (might not be valid video)")
            
            # Create video resource
            video_id, _ = self.create_video_resource(title, description or title, file_size)
            
            # Upload content
            self.upload_video_content(video_id, video_data, content_type)
            
            return video_id
            
        except Exception as e:
            if not self.last_upload_error:
                self.last_upload_error = str(e)
            print(f"[Video] Upload failed: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def upload_video_async(self, video_url: str, sku: str, title: str):
        """
        Upload video asynchronously and track status
        
        Args:
            video_url: URL of video to upload
            sku: Product SKU
            title: Video title
        """
        print(f"[Video] Starting async video upload for {sku}...")
        
        try:
            # Upload video
            video_id = self.upload_video_from_url(video_url, title)
            
            if video_id:
                # Update product record with video ID
                self._update_product_video_status(sku, video_id, "PROCESSING")
                
                # Try to add video to eBay inventory immediately
                # eBay will show the video once it's processed
                print(f"[Video] Adding video to eBay inventory for {sku}...")
                self._add_video_to_ebay_inventory(sku, video_id)
                
                # Poll status in background (optional, for status tracking)
                await self._poll_video_status(video_id, sku)
            else:
                self._update_product_video_status(sku, None, "FAILED")
            
        except Exception as e:
            print(f"[Video] Upload failed for {sku}: {e}")
            self._update_product_video_status(sku, None, "FAILED")
    
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
                status_data = self.get_video_status(video_id)
                status_value = status_data.get("status", "UNKNOWN")
                
                print(f"[Video] {video_id} status: {status_value} (attempt {attempt + 1}/{max_attempts})")
                
                if status_value == "LIVE":
                    # Video is ready
                    self._update_product_video_status(sku, video_id, "LIVE")
                    print(f"[Video] {video_id} is now LIVE")
                    break
                elif status_value in ["BLOCKED", "FAILED"]:
                    # Video processing failed
                    self._update_product_video_status(sku, video_id, "FAILED")
                    print(f"[Video] {video_id} processing failed: {status_value}")
                    break
                elif status_value in ["PENDING_UPLOAD", "PROCESSING"]:
                    # Still processing, continue polling
                    continue
                else:
                    print(f"[Video] Unknown status: {status_value}")
                    
            except Exception as e:
                print(f"[Video] Error checking status: {e}")
                continue
        
        if attempt == max_attempts - 1:
            print(f"[Video] {video_id} status check timed out")
            self._update_product_video_status(sku, video_id, "TIMEOUT")
    
    def _update_product_video_status(self, sku: str, video_id: Optional[str], status: str):
        """Update product record with video status - uses direct sqlite3 for reliability"""
        try:
            # Use direct sqlite3 for reliability (SQLAlchemy JSON field issues)
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'ebay_collection.db')
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Get current optimization
            cursor.execute('SELECT optimization FROM collected_products WHERE sku = ?', (sku,))
            row = cursor.fetchone()
            
            if row:
                opt = json.loads(row[0]) if row[0] else {}
                opt['video_id'] = video_id
                opt['video_status'] = status
                
                cursor.execute(
                    'UPDATE collected_products SET optimization = ? WHERE sku = ?',
                    (json.dumps(opt), sku)
                )
                conn.commit()
                print(f"[Video] Updated status for {sku}: {status} (video_id: {video_id[:20] if video_id else 'None'}...)")
                
                # If video is LIVE, also update eBay inventory item
                if status == "LIVE" and video_id:
                    self._add_video_to_ebay_inventory(sku, video_id)
            else:
                print(f"[Video] SKU not found in database: {sku}")
            
            conn.close()
            
        except Exception as e:
            print(f"[Video] Error updating status for {sku}: {e}")
            import traceback
            traceback.print_exc()

    def _build_inventory_video_payload(
        self,
        item_data: dict,
        video_id: str,
        *,
        fallback_title: str = "",
        fallback_description: str = "",
    ) -> dict:
        """Build a minimal Inventory API payload that safely adds one video ID."""
        product = dict((item_data or {}).get("product") or {})
        availability = dict((item_data or {}).get("availability") or {})
        ship_to = dict(availability.get("shipToLocationAvailability") or {})
        description = product.get("description") or fallback_description or ""
        if description:
            description = smart_truncate_html(description, max_length=4000, min_length=3600)
        title = str(product.get("title") or fallback_title or "")[:80]

        payload = {
            "condition": (item_data or {}).get("condition", "NEW"),
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": ship_to.get("quantity", 1) or 1,
                }
            },
            "product": {
                "title": title,
                "description": description,
                "imageUrls": list(product.get("imageUrls") or []),
                "aspects": dict(product.get("aspects") or {}),
                "videoIds": [video_id],
            },
        }

        package_weight_and_size = (item_data or {}).get("packageWeightAndSize")
        if isinstance(package_weight_and_size, dict) and package_weight_and_size:
            cleaned_pkg = dict(package_weight_and_size)
            weight = dict(cleaned_pkg.get("weight") or {})
            if weight:
                try:
                    weight_value = float(weight.get("value", 0))
                    if 0 < weight_value <= 2000:
                        cleaned_pkg["weight"] = {
                            "value": round(weight_value, 2),
                            "unit": weight.get("unit", "POUND"),
                        }
                    else:
                        cleaned_pkg.pop("weight", None)
                except (TypeError, ValueError):
                    cleaned_pkg.pop("weight", None)

            for dim_key in ("dimensions", "packageDimensions"):
                dim = dict(cleaned_pkg.get(dim_key) or {})
                if not dim:
                    continue
                cleaned_dim = {"unit": dim.get("unit", "INCH")}
                try:
                    for axis in ("length", "width", "height"):
                        axis_value = float(dim.get(axis, 0))
                        if axis_value <= 0 or axis_value > 999:
                            raise ValueError(axis)
                        cleaned_dim[axis] = round(axis_value, 2)
                except (TypeError, ValueError):
                    cleaned_pkg.pop(dim_key, None)
                else:
                    cleaned_pkg[dim_key] = cleaned_dim

            if cleaned_pkg:
                payload["packageWeightAndSize"] = cleaned_pkg

        return payload
    
    def _add_video_to_ebay_inventory(self, sku: str, video_id: str):
        """Add video ID to eBay inventory item"""
        try:
            token = self.oauth.get_valid_token()
            db = next(get_db())
            
            # Get current inventory item
            base_url = "https://api.ebay.com" if self.oauth.environment == "PRODUCTION" else "https://api.sandbox.ebay.com"
            get_url = f"{base_url}/sell/inventory/v1/inventory_item/{sku}"
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
            
            response = self._request_with_retry("GET", get_url, headers=headers)
            
            if response.status_code != 200:
                print(f"[Video] Could not get inventory item for {sku}")
                return False
            
            item_data = response.json()
            fallback_title = ""
            fallback_description = ""
            product = db.query(CollectedProduct).filter_by(sku=sku).first()
            if product:
                optimization = product.optimization or {}
                fallback_title = optimization.get("title") or product.title or ""
                fallback_description = optimization.get("description") or product.description or ""

            payload = self._build_inventory_video_payload(
                item_data,
                video_id,
                fallback_title=fallback_title,
                fallback_description=fallback_description,
            )
            
            # Update inventory item
            put_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Language": "en-US",
                "Accept": "application/json"
            }
            
            response = self._request_with_retry("PUT", get_url, headers=put_headers, json=payload)
            
            if response.status_code in [200, 204]:
                print(f"[Video] Added video {video_id} to eBay inventory item {sku}")
                return True
            else:
                print(f"[Video] Failed to update eBay inventory: {response.status_code}")
                detail = response.text[:500] if getattr(response, "text", None) else ""
                if detail:
                    print(f"[Video] Inventory update error detail: {detail}")
                return False
                
        except Exception as e:
            print(f"[Video] Error adding video to eBay: {e}")
            return False
        finally:
            try:
                db.close()
            except Exception:
                pass
    
    def upload_video_sync(self, video_url: str, sku: str, title: str) -> Optional[str]:
        """
        Upload video synchronously (blocking) - use for reliability
        
        Args:
            video_url: Video URL
            sku: Product SKU  
            title: Video title
            
        Returns:
            video_id if successful
        """
        print(f"[Video] Starting synchronous video upload for {sku}...")
        
        try:
            # Upload video
            video_id = self.upload_video_from_url(video_url, title)
            
            if video_id:
                # Update product record with video ID
                self._update_product_video_status(sku, video_id, "UPLOADED")
                print(f"[Video] Video uploaded successfully: {video_id}")
                
                # Add video to eBay inventory immediately
                # Video will show on listing once eBay finishes processing
                try:
                    print(f"[Video] Adding video to eBay inventory for {sku}...")
                    self._add_video_to_ebay_inventory(sku, video_id)
                except Exception as inventory_e:
                    print(f"[Video] Warning: failed to attach video to inventory for {sku}: {inventory_e}")
                
                return video_id
            else:
                status = (
                    "UNSUPPORTED_SOURCE"
                    if str(getattr(self, "last_upload_error", "")).startswith("unsupported_source")
                    else "FAILED"
                )
                self._update_product_video_status(sku, None, status)
                return None
            
        except Exception as e:
            print(f"[Video] Upload failed for {sku}: {e}")
            status = (
                "UNSUPPORTED_SOURCE"
                if str(getattr(self, "last_upload_error", "")).startswith("unsupported_source")
                else "FAILED"
            )
            self._update_product_video_status(sku, None, status)
            return None
    
    def get_video_status_for_sku(self, sku: str) -> Optional[dict]:
        """
        Get video status for a product
        
        Args:
            sku: Product SKU
            
        Returns:
            {"video_id": "...", "status": "PENDING_UPLOAD|PROCESSING|LIVE|FAILED"}
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
async def upload_video_background(video_url: str, sku: str, title: str, environment: str = "PRODUCTION"):
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
    from dotenv import load_dotenv
    
    load_dotenv()
    
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        print("[Error] Not authorized. Please run OAuth flow first.")
        exit(1)
    
    uploader = EbayVideoUploader(oauth)
    
    # Test creating a video resource
    print("\n=== Testing Video Resource Creation ===")
    try:
        video_id, location = uploader.create_video_resource(
            title="Test Video",
            description="Test video description",
            file_size=1000000  # 1MB dummy size
        )
        print(f"Video ID: {video_id}")
        print(f"Location: {location}")
        
        # Check status
        status = uploader.get_video_status(video_id)
        print(f"Status: {status}")
        
    except Exception as e:
        print(f"Error: {e}")
