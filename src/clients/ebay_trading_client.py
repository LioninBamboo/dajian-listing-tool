import requests
from .ebay_client import EbayClient

class EbayTradingClient:
    """
    eBay Trading API Client (XML-based)
    Used for legacy operations like creating listings with inline policies.
    """
    
    ENDPOINTS = {
        "sandbox": "https://api.sandbox.ebay.com/ws/api.dll",
        "production": "https://api.ebay.com/ws/api.dll"
    }
    
    def __init__(self, ebay_client: EbayClient):
        self.ebay_client = ebay_client
        self.env = ebay_client.env
        self.endpoint = self.ENDPOINTS[self.env]
        
    def _build_headers(self, call_name: str) -> dict:
        """Build headers for Trading API"""
        token = self.ebay_client.get_oauth_token()
        
        return {
            "X-EBAY-API-SESSION-CERTIFICATE": f"{self.ebay_client.app_id};{self.ebay_client.dev_id};{self.ebay_client.cert_id}",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": "0",  # EBAY_US
            "X-EBAY-API-IAF-TOKEN": token, # OAuth token
            "Content-Type": "text/xml"
        }

    def call(self, call_name: str, payload_xml_body: str) -> str:
        """
        Make a Trading API call
        
        Args:
            call_name: e.g., 'AddFixedPriceItem'
            payload_xml_body: The XML body INSIDE the Request tag. 
                              (e.g. <Item>...</Item>)
                              
        Returns:
            Response XML string
        """
        
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">
    <ErrorLanguage>en_US</ErrorLanguage>
    <WarningLevel>High</WarningLevel>
    {payload_xml_body}
</{call_name}Request>"""

        headers = self._build_headers(call_name)
        
        response = requests.post(
            self.endpoint,
            headers=headers,
            data=xml_request.encode('utf-8'),
            timeout=30,
            verify=False  # 禁用 SSL 验证以应对代理
        )
        
        # Simple error check (requests level)
        response.raise_for_status()
        
        return response.text

    def get_item(self, item_id: str) -> str:
        """
        Get Item Details
        """
        xml_payload = f"""
            <ItemID>{item_id}</ItemID>
            <DetailLevel>ReturnAll</DetailLevel>
        """
        return self.call("GetItem", xml_payload)

    def revise_item(self, item_id: str, xml_body: str) -> str:
        """
        Revise an existing fixed price item.
        
        Args:
            item_id: The eBay Item ID to revise.
            xml_body: The XML content INSIDE the <Item> tag. 
                      e.g. <Quantity>5</Quantity><StartPrice>19.99</StartPrice>
        """
        payload = f"""
            <Item>
                <ItemID>{item_id}</ItemID>
                {xml_body}
            </Item>
        """
        return self.call("ReviseFixedPriceItem", payload)

    def end_item(self, item_id: str, reason: str = "NotAvailable") -> str:
        """
        End a fixed price item.
        
        Args:
            item_id: eBay Item ID
            reason: EndingReasonCodeType (NotAvailable, Incorrect, OtherListingError, etc.)
        """
        payload = f"""
            <ItemID>{item_id}</ItemID>
            <EndingReason>{reason}</EndingReason>
        """
        return self.call("EndFixedPriceItem", payload)

    def get_active_listings(self, page: int = 1, limit: int = 20) -> str:
        """
        Get all active listings using GetMyeBaySelling.
        Returns XML response string.
        """
        payload = f"""
            <ActiveList>
                <Sort>TimeLeft</Sort>
                <Pagination>
                    <EntriesPerPage>{limit}</EntriesPerPage>
                    <PageNumber>{page}</PageNumber>
                </Pagination>
            </ActiveList>
            <DetailLevel>ReturnAll</DetailLevel>
        """
        return self.call("GetMyeBaySelling", payload)
