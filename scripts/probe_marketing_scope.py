#!/usr/bin/env python3
"""Probe / re-auth sell.marketing for the ambient store instance.

Refresh tokens cannot grow scopes. Substores that were authorized before
Promoted Listings eligibility will 403 until the seller re-consents and the
new token is imported into THAT instance's ebay_tokens.db.

    python scripts/probe_marketing_scope.py              # read-only probe
    python scripts/probe_marketing_scope.py --auth-url   # print consent URL
    python scripts/probe_marketing_scope.py --import ebay_token.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.utils.store_profile import get_store_profile  # noqa: E402


def classify_marketing_response(status_code: int, body: str) -> str:
    text = (body or "").lower()
    if status_code in (200, 201):
        return "ok"
    if "invalid_scope" in text:
        return "invalid_scope"
    if status_code == 403 or "1100" in text or "insufficient permission" in text:
        return "missing_scope"
    return "error"


def _save_token_file(path: Path) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "access_token" not in data:
        raise SystemExit(f"invalid token file: {path} (missing access_token)")
    from src.services.ebay_auth import EbayOAuthService
    EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))._save_token(data)


def import_token(path: Path, allow_main: bool = False) -> None:
    brand = str(getattr(get_store_profile(), "brand_name", "") or "")
    if brand == "AquaVerve" and not allow_main:
        raise SystemExit(
            "Refusing to import a token into the AquaVerve main store. "
            "Run this from GrovePop_Listing_Tool or AutoParts_Listing_Tool. "
            "Pass --allow-main only if you really intend to replace the main token."
        )
    _save_token_file(path)
    print(f"imported token into {brand} ({ROOT / 'ebay_tokens.db'})")


# User-consent scopes for ads re-auth. Do NOT include
# commerce.taxonomy.readonly — that scope is not enabled on this RuName's
# user-token flow and would make the whole consent fail with invalid_scope.
REAUTH_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
)


def auth_url() -> str:
    from src.services.ebay_auth import EbayOAuthService
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    original = list(oauth.scopes)
    try:
        oauth.scopes = list(REAUTH_SCOPES)
        return oauth.get_authorization_url(state="marketing_reauth")
    finally:
        oauth.scopes = original


def probe() -> dict:
    from src.services.ebay_auth import EbayOAuthService
    import requests

    profile = get_store_profile()
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    response = requests.get(
        f"{oauth.api_base}/sell/marketing/v1/ad_campaign",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params={"campaign_status": "RUNNING", "limit": "1"},
        timeout=30,
        verify=False,
    )
    body = response.text or ""
    verdict = classify_marketing_response(response.status_code, body)
    return {
        "store": getattr(profile, "brand_name", ""),
        "status": response.status_code,
        "verdict": verdict,
        "detail": body[:180].replace("\n", " "),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe or re-auth sell.marketing")
    parser.add_argument("--auth-url", action="store_true")
    parser.add_argument("--import", dest="import_path", default="")
    parser.add_argument("--allow-main", action="store_true")
    args = parser.parse_args()

    if args.auth_url:
        print(auth_url())
        return 0
    if args.import_path:
        import_token(Path(args.import_path), allow_main=args.allow_main)
        result = probe()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["verdict"] == "ok" else 2

    result = probe()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["verdict"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
