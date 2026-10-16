"""
Publish gate: do not call eBay with a retired, non-leaf, or unconfirmed category.

Invalid-category responses (errorId 25005 / "invalid category ID") are not
retried three times. One suggestion remap, then the human gate.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from src.services.ebay_category_matcher import (
    EbayCategoryMatcher,
    _is_api_category_plausible,
    canonicalize_category,
    is_category_plausible_for_text,
)

logger = logging.getLogger(__name__)

HUMAN_GATE_ERROR = "category_invalid_needs_human"
MAX_OTHER_ATTEMPTS = 3


class PublishResult:
    def __init__(self, status: str, errors: Optional[List[str]] = None, **extra):
        self.status = status
        self.errors = list(errors or [])
        self.extra = extra

    def __repr__(self) -> str:
        return f"PublishResult({self.status!r}, errors={self.errors!r})"


def is_invalid_category_error(message: str = "", error_id: Optional[int] = None) -> bool:
    if error_id == 25005:
        return True
    return "invalid category id" in (message or "").lower()


def invalid_category_from_exception(exc: BaseException) -> bool:
    error_ids = []
    text = str(exc or "")
    response = getattr(exc, "response", None)
    if response is not None:
        text += " " + (getattr(response, "text", "") or "")
        try:
            payload = response.json()
        except Exception:
            payload = {}
        for err in payload.get("errors") or []:
            text += " " + str(err.get("message") or "")
            try:
                error_ids.append(int(err.get("errorId")))
            except (TypeError, ValueError):
                continue
    if 25005 in error_ids:
        return True
    return is_invalid_category_error(text)


class EbayPublisher:
    def __init__(self, client, validator, matcher: Optional[EbayCategoryMatcher] = None, tree_id: str = "0"):
        self.client = client
        self.validator = validator
        self.matcher = matcher or EbayCategoryMatcher()
        self.tree_id = str(tree_id)

    def publish(
        self,
        product: Dict[str, Any],
        existing_offer_id: Optional[str] = None,
        publish_offer: bool = True,
    ) -> PublishResult:
        """
        Both offer paths validate before any inventory/offer write.

        existing_offer_id set → update_offer_category path.
        otherwise → create inventory + create offer path.
        """
        product.setdefault("optimization", {})
        product.setdefault("logs", [])
        self._get_category_and_aspects(product)

        if not self._ensure_valid_leaf(product, reason="pre_publish"):
            return self._human_gate(product)

        category_id = str(product["optimization"].get("categoryId") or "")
        if existing_offer_id:
            self.update_offer_category(
                existing_offer_id,
                product["sku"],
                product["price"],
                category_id,
            )
            offer_id = existing_offer_id
        else:
            self._create_inventory_item(product)
            offer = self._create_offer(product, category_id)
            offer_id = (offer or {}).get("offerId")
            if not offer_id:
                return PublishResult("ERROR", ["offer_not_created"])
            if (offer or {}).get("status") == "EXISTING":
                self.update_offer_category(offer_id, product["sku"], product["price"], category_id)

        if not publish_offer:
            return PublishResult("SUCCESS", offer_id=offer_id, category_id=category_id)

        listing = self._publish_offer_with_policy(offer_id, product)
        if isinstance(listing, PublishResult):
            return listing
        if not listing or not listing.get("listingId"):
            return PublishResult("ERROR", ["publish_failed"], offer_id=offer_id, category_id=category_id)
        return PublishResult(
            "SUCCESS",
            listing_id=listing.get("listingId"),
            offer_id=offer_id,
            category_id=category_id,
        )

    def _get_category_and_aspects(self, product: Dict[str, Any]):
        """Read stored aspects. Never invent item specifics."""
        opt = product.get("optimization") or {}
        aspects = opt.get("aspects")
        if aspects is None:
            aspects = product.get("aspects") or {}
        return opt.get("categoryId"), aspects

    def _create_inventory_item(self, product: Dict[str, Any]):
        _category_id, aspects = self._get_category_and_aspects(product)
        opt = product.get("optimization") or {}
        return self.client.create_or_replace_inventory_item(
            sku=product["sku"],
            product={
                "title": opt.get("title") or product.get("title") or "",
                "description": opt.get("description") or product.get("description") or "",
                "image_urls": product.get("image_urls") or [],
                "price": product.get("price"),
                "quantity": product.get("quantity") or 1,
                "condition": product.get("condition") or "NEW",
                "aspects": aspects,
            },
        )

    def _create_offer(self, product: Dict[str, Any], category_id: str):
        return self.client.create_offer(
            sku=product["sku"],
            price=product["price"],
            category_id=category_id,
        )

    def update_offer_category(self, offer_id: str, sku: str, price, category_id: str):
        return self.client.update_offer_category(offer_id, sku, price, category_id)

    def _ensure_valid_leaf(self, product: Dict[str, Any], reason: str) -> bool:
        opt = product["optimization"]
        title = opt.get("title") or product.get("title") or ""
        current = str(opt.get("categoryId") or "")
        canonical = canonicalize_category(current, title) if current else None
        if canonical and canonical.get("reason") not in (None, "unchanged") and canonical.get("categoryId"):
            self._write_category(
                product,
                canonical["categoryId"],
                canonical.get("categoryName") or "",
                canonical.get("reason") or reason,
                old_id=current,
            )
            current = str(product["optimization"].get("categoryId") or "")

        if current:
            check = self.validator.validate(current, self.tree_id)
            if check.get("valid") and check.get("leaf"):
                if check.get("name") and not product["optimization"].get("categoryName"):
                    product["optimization"]["categoryName"] = check["name"]
                return True

        return self._remap_from_suggestions(product, reason=reason if not current else "invalid_or_non_leaf")

    def _remap_from_suggestions(self, product: Dict[str, Any], reason: str) -> bool:
        opt = product["optimization"]
        title = opt.get("title") or product.get("title") or ""
        old_id = str(opt.get("categoryId") or "")
        suggestions = self._suggestions(title)
        for suggestion in suggestions:
            cid = str(suggestion.get("categoryId") or "")
            name = suggestion.get("categoryName") or ""
            if not cid:
                continue
            if not (
                is_category_plausible_for_text(cid, name, title)
                and _is_api_category_plausible(cid, name, title)
            ):
                continue
            check = self.validator.validate(cid, self.tree_id)
            if check.get("valid") and check.get("leaf"):
                resolved_name = check.get("name") or name
                self._write_category(product, cid, resolved_name, reason, old_id=old_id)
                logger.info("[CAT_REMAP] %s→%s (%s)", old_id or "(none)", cid, reason)
                return True
        product["_category_suggestions"] = suggestions[:3]
        return False

    def _suggestions(self, title: str) -> List[dict]:
        getter = getattr(self.client, "get_category_suggestions", None)
        if getter is None:
            return []
        raw = getter(title) or []
        normalized = []
        for item in raw:
            if "category" in item and isinstance(item["category"], dict):
                cat = item["category"]
                normalized.append(
                    {
                        "categoryId": str(cat.get("categoryId") or ""),
                        "categoryName": cat.get("categoryName") or "",
                    }
                )
            else:
                normalized.append(
                    {
                        "categoryId": str(item.get("categoryId") or ""),
                        "categoryName": item.get("categoryName") or "",
                    }
                )
        return normalized

    def _write_category(self, product, category_id: str, category_name: str, reason: str, old_id: str):
        opt = product["optimization"]
        opt["categoryId"] = str(category_id)
        if category_name:
            opt["categoryName"] = category_name
        line = f"[CAT_REMAP] {old_id or '(none)'}→{category_id} ({reason})"
        logs = list(product.get("logs") or [])
        logs.append(line)
        product["logs"] = logs
        logger.info(line)

    def _human_gate(self, product: Dict[str, Any]) -> PublishResult:
        opt = product.get("optimization") or {}
        old_id = opt.get("categoryId") or "(none)"
        suggestions = product.get("_category_suggestions") or []
        top = []
        for item in suggestions[:3]:
            top.append(f"{item.get('categoryId')}:{item.get('categoryName')}")
        shown = ", ".join(top) if top else "(no suggestions)"
        line = f"category_invalid_needs_human old={old_id} suggestions={shown}"
        logs = list(product.get("logs") or [])
        logs.append(line)
        product["logs"] = logs
        product["status"] = "READY"
        logger.warning(line)
        return PublishResult("ERROR", [HUMAN_GATE_ERROR])

    def _publish_offer_with_policy(self, offer_id: str, product: Dict[str, Any]):
        """
        Other eBay errors may retry up to 3 times.
        Invalid category: one remap, then the human gate. Never a 3x loop.
        """
        remapped_after_error = False
        other_attempts = 0
        while True:
            try:
                return self.client.publish_offer(offer_id)
            except Exception as exc:
                if invalid_category_from_exception(exc):
                    if remapped_after_error:
                        return self._human_gate(product)
                    remapped_after_error = True
                    if not self._remap_from_suggestions(product, reason="ebay_invalid_category"):
                        return self._human_gate(product)
                    new_id = str(product["optimization"].get("categoryId") or "")
                    self.update_offer_category(offer_id, product["sku"], product["price"], new_id)
                    continue
                other_attempts += 1
                if other_attempts >= MAX_OTHER_ATTEMPTS:
                    raise
