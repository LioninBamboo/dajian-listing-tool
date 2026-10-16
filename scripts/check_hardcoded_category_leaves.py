"""
Read-only Taxonomy check for hardcoded category IDs.

GET get_category_subtree only. Does not write the cache or the product DB.
A leaf is HTTP 200 with leafCategoryTreeNode=true. Anything else is a WARN.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.services.ebay_category_validator import CategoryValidator


def main() -> int:
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    api_base = "https://api.sandbox.ebay.com" if environment == "SANDBOX" else "https://api.ebay.com"
    validator = CategoryValidator(api_base=api_base, write_cache=False)
    failed = 0
    for category_id, name, tree_id in EbayCategoryMatcher.hardcoded_categories():
        result = validator.validate(category_id, tree_id)
        ok = result.get("valid") and result.get("leaf")
        flag = "OK" if ok else "WARN"
        if not ok:
            failed += 1
        print(f"{flag} tree={tree_id} {category_id} {name} reason={result.get('reason')} api_name={result.get('name')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
