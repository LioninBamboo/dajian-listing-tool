import os
import sys
from dotenv import load_dotenv
import random

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.clients.ebay_client import EbayClient
from src.clients.ebay_trading_client import EbayTradingClient

def create_legacy_listing():
    load_dotenv()
    
    app_id = os.getenv("EBAY_APP_ID")
    cert_id = os.getenv("EBAY_CERT_ID")
    dev_id = os.getenv("EBAY_DEV_ID")
    
    # Init base client for token
    base_client = EbayClient(app_id=app_id, cert_id=cert_id, dev_id=dev_id)
    
    # Init Trading API client
    trading_client = EbayTradingClient(base_client)
    
    # Generate a random SKU to avoid duplicates during testing
    sku = f"TEST-LEGACY-{random.randint(1000, 9999)}"
    
    print(f"Creating Listing for SKU: {sku}...")
    
    # Payload with INLINE policies (No IDs needed)
    xml_body = f"""
    <Item>
        <Title>Test Listing with Inline Policies {sku}</Title>
        <Description>This is a test listing created via eBay Trading API with inline policies.</Description>
        <PrimaryCategory>
            <CategoryID>20349</CategoryID> <!-- Building Materials & Supplies -->
        </PrimaryCategory>
        <StartPrice currencyID="USD">19.99</StartPrice>
        <ConditionID>1000</ConditionID> <!-- New -->
        <Country>US</Country>
        <Currency>USD</Currency>
        <DispatchTimeMax>3</DispatchTimeMax>
        <ListingDuration>GTC</ListingDuration>
        <ListingType>FixedPriceItem</ListingType>
        <PaymentMethods>PayPal</PaymentMethods>
        <PayPalEmailAddress>sandbox_test@paypal.com</PayPalEmailAddress>
        <PictureDetails>
            <PictureURL>https://i.ebayimg.com/images/g/H0YAAOSw~dVh3x~e/s-l1600.jpg</PictureURL>
        </PictureDetails>
        <ItemSpecifics>
            <NameValueList>
                <Name>Brand</Name>
                <Value>Unbranded</Value>
            </NameValueList>
            <NameValueList>
                <Name>Type</Name>
                <Value>Tool</Value>
            </NameValueList>
        </ItemSpecifics>
        <Location>San Jose</Location>
        <Quantity>10</Quantity>
        <ReturnPolicy>
            <ReturnsAcceptedOption>ReturnsAccepted</ReturnsAcceptedOption>
            <ReturnsWithinOption>Days_30</ReturnsWithinOption>
            <ShippingCostPaidByOption>Buyer</ShippingCostPaidByOption>
        </ReturnPolicy>
        <ShippingDetails>
            <ShippingServiceOptions>
                <ShippingServicePriority>1</ShippingServicePriority>
                <ShippingService>USPSPriority</ShippingService>
                <ShippingServiceCost currencyID="USD">0.0</ShippingServiceCost>
            </ShippingServiceOptions>
            <ShippingType>Flat</ShippingType>
        </ShippingDetails>
        <Site>US</Site>
        <SKU>{sku}</SKU>
    </Item>
    """
    
    try:
        response = trading_client.call("AddFixedPriceItem", xml_body)
        
        with open("last_response.xml", "w", encoding="utf-8") as f:
            f.write(response)
            
        print("\n✅ Response Received (Saved to last_response.xml):")
        print(response)

        
        # Check for Success
        if "<Ack>Success</Ack>" in response or "<Ack>Warning</Ack>" in response:
             print("\n🎉 Listing Created Successfully!")
        else:
             print("\n❌ Listing Creation Failed.")
             
    except Exception as e:
        print(f"\n❌ Script Error: {e}")

if __name__ == "__main__":
    create_legacy_listing()
