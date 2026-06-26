"""
eBay Policy Manager

Manages and caches eBay business policies (Fulfillment, Return, Payment)
"""

import sqlite3
import requests
from typing import List, Dict, Optional
from datetime import UTC, datetime
from src.services.ebay_auth import EbayOAuthService


class EbayPolicyManager:
    """Manage eBay business policies"""
    
    def __init__(self, oauth_service: EbayOAuthService):
        """
        Initialize Policy Manager
        
        Args:
            oauth_service: EbayOAuthService instance for API authentication
        """
        self.oauth = oauth_service
        self.base_url = oauth_service.api_base
        self._init_policy_db()
    
    def _init_policy_db(self):
        """Initialize SQLite database for policy caching"""
        conn = sqlite3.connect("ebay_collection.db")
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ebay_policies (
                id INTEGER PRIMARY KEY,
                policy_type TEXT NOT NULL,
                policy_id TEXT NOT NULL UNIQUE,
                policy_name TEXT NOT NULL,
                is_default BOOLEAN DEFAULT 0,
                policy_data TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def fetch_and_cache_all_policies(self):
        """Fetch all policies from eBay and cache them"""
        print("📋 Fetching eBay policies...")
        
        # Fetch each policy type
        fulfillment_policies = self._fetch_policies("fulfillment")
        return_policies = self._fetch_policies("return")
        payment_policies = self._fetch_policies("payment")
        
        # Cache to database
        self._cache_policies("fulfillment", fulfillment_policies)
        self._cache_policies("return", return_policies)
        self._cache_policies("payment", payment_policies)
        
        print(f"✅ Cached {len(fulfillment_policies)} fulfillment, {len(return_policies)} return, {len(payment_policies)} payment policies")
    
    def _fetch_policies(self, policy_type: str) -> List[Dict]:
        """
        Fetch policies from eBay Account API
        
        Args:
            policy_type: "fulfillment", "return", or "payment"
            
        Returns:
            List of policy dicts
        """
        token = self.oauth.get_valid_token()
        
        endpoint_map = {
            "fulfillment": "/sell/account/v1/fulfillment_policy",
            "return": "/sell/account/v1/return_policy",
            "payment": "/sell/account/v1/payment_policy"
        }
        
        url = f"{self.base_url}{endpoint_map[policy_type]}"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            # Extract policies from response
            policy_key = f"{policy_type}Policies"
            return data.get(policy_key, [])
            
        except requests.exceptions.HTTPError as e:
            print(f"⚠️ Failed to fetch {policy_type} policies: {e}")
            return []
    
    def _cache_policies(self, policy_type: str, policies: List[Dict]):
        """Cache policies to database"""
        conn = sqlite3.connect("ebay_collection.db")
        cursor = conn.cursor()
        
        for policy in policies:
            policy_id = policy.get(f"{policy_type}PolicyId") or policy.get("policyId")
            policy_name = policy.get("name", "Unnamed Policy")
            
            # Check if this is marked as default
            is_default = policy.get("marketplaceId") == "EBAY_US"  # Simplified logic
            
            cursor.execute("""
                INSERT OR REPLACE INTO ebay_policies 
                (policy_type, policy_id, policy_name, is_default, policy_data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                policy_type,
                policy_id,
                policy_name,
                is_default,
                str(policy),  # Store full policy data as string
                datetime.now(UTC).replace(tzinfo=None).isoformat()
            ))
        
        conn.commit()
        conn.close()
    
    def get_default_fulfillment_policy_id(self) -> Optional[str]:
        """Get default fulfillment policy ID"""
        return self._get_default_policy_id("fulfillment")
    
    def get_default_return_policy_id(self) -> Optional[str]:
        """Get default return policy ID"""
        return self._get_default_policy_id("return")
    
    def get_default_payment_policy_id(self) -> Optional[str]:
        """Get default payment policy ID"""
        return self._get_default_policy_id("payment")
    
    def _get_default_policy_id(self, policy_type: str) -> Optional[str]:
        """Get default policy ID for a given type"""
        conn = sqlite3.connect("ebay_collection.db")
        cursor = conn.cursor()
        
        # Try to get default policy
        cursor.execute("""
            SELECT policy_id FROM ebay_policies
            WHERE policy_type = ? AND is_default = 1
            ORDER BY updated_at DESC
            LIMIT 1
        """, (policy_type,))
        
        row = cursor.fetchone()
        
        # If no default, get the first one
        if not row:
            cursor.execute("""
                SELECT policy_id FROM ebay_policies
                WHERE policy_type = ?
                ORDER BY updated_at DESC
                LIMIT 1
            """, (policy_type,))
            row = cursor.fetchone()
        
        conn.close()
        
        return row[0] if row else None
    
    def get_all_policies(self, policy_type: Optional[str] = None) -> List[Dict]:
        """
        Get all cached policies
        
        Args:
            policy_type: Optional filter by type ("fulfillment", "return", "payment")
            
        Returns:
            List of policy dicts
        """
        conn = sqlite3.connect("ebay_collection.db")
        cursor = conn.cursor()
        
        if policy_type:
            cursor.execute("""
                SELECT policy_type, policy_id, policy_name, is_default
                FROM ebay_policies
                WHERE policy_type = ?
                ORDER BY is_default DESC, policy_name
            """, (policy_type,))
        else:
            cursor.execute("""
                SELECT policy_type, policy_id, policy_name, is_default
                FROM ebay_policies
                ORDER BY policy_type, is_default DESC, policy_name
            """)
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "type": row[0],
                "id": row[1],
                "name": row[2],
                "isDefault": bool(row[3])
            }
            for row in rows
        ]
    
    def set_default_policies(self, fulfillment_id: str, return_id: str, payment_id: str):
        """
        Set default policies
        
        Args:
            fulfillment_id: Fulfillment policy ID
            return_id: Return policy ID
            payment_id: Payment policy ID
        """
        conn = sqlite3.connect("ebay_collection.db")
        cursor = conn.cursor()
        
        # Clear all defaults first
        cursor.execute("UPDATE ebay_policies SET is_default = 0")
        
        # Set new defaults
        for policy_id in [fulfillment_id, return_id, payment_id]:
            cursor.execute("""
                UPDATE ebay_policies
                SET is_default = 1
                WHERE policy_id = ?
            """, (policy_id,))
        
        conn.commit()
        conn.close()
        
        print(f"✅ Default policies updated")


if __name__ == "__main__":
    # Test Policy Manager
    from src.services.ebay_auth import EbayOAuthService
    
    oauth = EbayOAuthService()
    
    if not oauth.is_authorized():
        print("❌ Not authorized. Please run OAuth flow first.")
        exit(1)
    
    manager = EbayPolicyManager(oauth)
    
    # Fetch and cache policies
    manager.fetch_and_cache_all_policies()
    
    # Display default policies
    print("\n📋 Default Policies:")
    print(f"Fulfillment: {manager.get_default_fulfillment_policy_id()}")
    print(f"Return: {manager.get_default_return_policy_id()}")
    print(f"Payment: {manager.get_default_payment_policy_id()}")
    
    # Display all policies
    print("\n📋 All Policies:")
    for policy in manager.get_all_policies():
        default_mark = " (DEFAULT)" if policy["isDefault"] else ""
        print(f"  [{policy['type']}] {policy['name']} (ID: {policy['id']}){default_mark}")
