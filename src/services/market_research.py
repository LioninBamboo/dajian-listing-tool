"""Market Research Service"""
import time
import os # Added import os
from duckduckgo_search import DDGS

class MarketResearch:
    """
    Use DuckDuckGo to perform market research for eBay listings.
    Fetch competitor titles and trending keywords.
    """
    
    def __init__(self, region: str = "wt-wt"):
        """
        Args:
            region: Search region (e.g., 'wt-wt' for global, 'us-en' for US)
        """
        self.region = region
        
    def search_market(self, keyword: str, limit: int = 5) -> list[str]:
        """
        Search for a keyword and return a list of result titles.
        Focuses on finding how competitors list similar items.
        
        Args:
            keyword: Product keyword (e.g. "Cordless Drill 20V")
            limit: Number of results to return
            
        Returns:
            List of titles/snippets found.
        """
        print(f"🔎 Searching market for: {keyword}")
        results = []
        try:
            # Configure Proxy: Use system proxy (TUN) or env var
            proxy_url = os.getenv("HTTP_PROXY") 
            
            with DDGS(proxy=proxy_url, timeout=30) as ddgs:
                # We search for "buy [keyword] ebay" to find eBay listings specifically
                # or just generic search to find how people describe it.
                # Let's try to be specific for e-commerce context.
                search_query = f"{keyword} site:ebay.com"
                
                # DDGS.text() yields results
                for r in ddgs.text(search_query, region=self.region, max_results=limit):
                    title = r.get("title", "")
                    if title:
                        # Clean up title (remove " | eBay" etc.)
                        title = title.replace(" | eBay", "").replace(" - eBay", "")
                        results.append(title)
                        
            if not results:
                print("  No direct eBay results, trying generic search...")
                # Fallback to generic search if site:ebay.com yields nothing
                with DDGS(proxy=proxy_url, timeout=30) as ddgs:
                    for r in ddgs.text(keyword, region=self.region, max_results=limit):
                         results.append(r.get("title", ""))
                         
        except Exception as e:
            # Squelch detailed network errors, just show simple message
            error_msg = str(e).split('params=')[0].strip()
            if "return None" in str(e):
                print(f"  ⚠️ Market Research skipped (Connection issue or no results)")
            else:
                print(f"  ⚠️ Market Research skipped: {error_msg}")
            return []
            
        return results

if __name__ == "__main__":
    # Simple test
    mr = MarketResearch()
    titles = mr.search_market("Heavy Duty Workbench")
    for t in titles:
        print(f"- {t}")
