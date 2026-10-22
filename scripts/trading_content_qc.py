#!/usr/bin/env python3
"""Continuous live-content QC for AquaRides Trading/Motors listings.

Read-only by default.  ``--fix`` regenerates the deterministic description and
repairs only CRITICAL/HIGH content issues.  Trading listings use
``ReviseFixedPriceItem``; an Inventory-created listing is detected through its
Inventory offer and repaired through ``PUT offer`` + ``publish`` instead.

Examples::

    python scripts/trading_content_qc.py
    python scripts/trading_content_qc.py --fix --email
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
MIN_SLEEP_SECONDS = 0.4
THIN_BODY_LIMIT = 800
ACTIONABLE_SEVERITIES = {"CRITICAL", "HIGH"}
KNOWN_INVENTORY_LISTING_IDS = frozenset({"188683585596"})

_CJK_RE = re.compile(
    r"[\u1100-\u11ff\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff"
    r"\u3100-\u312f\u3130-\u318f\u31a0-\u31bf\u31f0-\u31ff"
    r"\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"
)
_TAG_RE = re.compile(r"<[^>]+>")
_ASSEMBLY_RE = re.compile(r"assembly\s+(?:required|instructions?)", re.IGNORECASE)


@dataclass(frozen=True)
class PublishedListing:
    sku: str
    listing_id: str
    title: str
    source_description: str
    attributes: dict[str, Any]
    specs: dict[str, Any]
    aspects: dict[str, Any]


def _parse_mapping(raw: Any, field_name: str) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} is not a JSON object")
    return dict(value)


def load_published_rows(
    conn: sqlite3.Connection,
    *,
    skus: Sequence[str] | None = None,
    listing_ids: Sequence[str] | None = None,
) -> list[PublishedListing]:
    """Load only published rows with a non-empty listing id, without writes."""
    clauses = ["status = ?", "listing_id IS NOT NULL", "TRIM(listing_id) != ''"]
    params: list[Any] = ["PUBLISHED"]

    selected_skus = [str(value).strip() for value in (skus or []) if str(value).strip()]
    if selected_skus:
        clauses.append(f"sku IN ({','.join('?' for _ in selected_skus)})")
        params.extend(selected_skus)

    selected_ids = [str(value).strip() for value in (listing_ids or []) if str(value).strip()]
    if selected_ids:
        clauses.append(f"listing_id IN ({','.join('?' for _ in selected_ids)})")
        params.extend(selected_ids)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sku, listing_id, title, description, attributes, specs, optimization "
        "FROM collected_products WHERE " + " AND ".join(clauses) + " ORDER BY sku",
        params,
    ).fetchall()

    result: list[PublishedListing] = []
    for row in rows:
        optimization = _parse_mapping(row["optimization"], "optimization")
        result.append(
            PublishedListing(
                sku=str(row["sku"] or "").strip(),
                listing_id=str(row["listing_id"] or "").strip(),
                title=str(row["title"] or ""),
                source_description=str(row["description"] or ""),
                attributes=_parse_mapping(row["attributes"], "attributes"),
                specs=_parse_mapping(row["specs"], "specs"),
                aspects=_parse_mapping(optimization.get("aspects"), "optimization.aspects"),
            )
        )
    return result


def decode_live_description(payload: bytes | str) -> str:
    """Decode Trading response text and the two HTML/XML escaping layers."""
    if isinstance(payload, bytes):
        value = payload.decode("utf-8", "replace")
    else:
        value = str(payload or "")
    value = html.unescape(html.unescape(value))
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3]
    return value


def extract_trading_description(payload: bytes | str) -> str:
    """Extract and decode ``<Description>`` from a Trading GetItem response."""
    decoded = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    match = re.search(r"<Description(?:\s[^>]*)?>(.*?)</Description>", decoded, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return decode_live_description(match.group(1).strip())


def _plain_text(description: str) -> str:
    without_blocks = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        str(description or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", without_blocks))).strip()


def check_description(description: str) -> dict[str, Any]:
    """Return auditable issue rows for one live description."""
    value = str(description or "")
    plain = _plain_text(value)
    lowered = value.casefold()
    issues: list[dict[str, str]] = []

    def add(code: str, severity: str, detail: str) -> None:
        issues.append({"code": code, "severity": severity, "detail": detail})

    if "\ufffd" in value:
        add("mojibake", "CRITICAL", "live description contains U+FFFD replacement character")
    if _CJK_RE.search(plain):
        add("raw_cjk", "CRITICAL", "live description contains CJK characters")
    if "aquarides" not in lowered:
        add("missing_banner", "HIGH", "AQUARIDES banner marker is missing")
    if "key features" not in lowered:
        add("missing_key_features", "HIGH", "KEY FEATURES marker is missing")
    from src.services.auto_technical_prompt import looks_like_gold_template

    if len(plain) < THIN_BODY_LIMIT and not looks_like_gold_template(value):
        add("thin_description", "HIGH", f"tag-stripped body has {len(plain)} characters (< {THIN_BODY_LIMIT})")
    if _ASSEMBLY_RE.search(plain):
        add("assembly_copy", "MEDIUM", "Assembly Required/Instructions is not expected for auto listings")

    return {
        "length": len(value),
        "plain_length": len(plain),
        "issues": issues,
        "actionable": any(issue["severity"] in ACTIONABLE_SEVERITIES for issue in issues),
    }


def generate_description(listing: PublishedListing) -> str:
    """Generate the shared AquaRides gold description from stored source fields."""
    from src.services.auto_technical_prompt import build_auto_gold_description

    return build_auto_gold_description(
        title=listing.title,
        source_description=listing.source_description,
        attributes=listing.attributes,
        specs=listing.specs,
        aspects=listing.aspects,
    )


def _profile_guard() -> Any:
    from src.utils.store_profile import get_store_profile

    profile = get_store_profile()
    values = {
        "store_kind": getattr(profile, "store_kind", ""),
        "listing_channel": getattr(profile, "listing_channel", ""),
        "ebay_site_id": str(getattr(profile, "ebay_site_id", "")),
        "brand_name": str(getattr(profile, "brand_name", "")),
    }
    if (
        values["store_kind"] != "auto"
        or values["listing_channel"] != "trading"
        or values["ebay_site_id"] != "100"
        or values["brand_name"].casefold() != "aquarides"
    ):
        raise RuntimeError(f"refusing run outside AquaRides Trading profile: {values}")
    return profile


def _error_from_trading_response(payload: bytes | str) -> str:
    decoded = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    messages = re.findall(r"<LongMessage>(.*?)</LongMessage>", decoded, re.IGNORECASE | re.DOTALL)
    return "; ".join(_plain_text(decode_live_description(message)) for message in messages)[:500]


class EbayContentGateway:
    """Small API boundary used by the runner and replaceable by test fakes."""

    def __init__(self, client: Any, profile: Any):
        self.client = client
        self.profile = profile

    def get_live_description(self, listing_id: str) -> str:
        token = self.client.oauth.get_valid_token()
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{escape(str(listing_id))}</ItemID>"
            "<DetailLevel>ItemReturnDescription</DetailLevel>"
            "</GetItemRequest>"
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": str(self.profile.ebay_site_id),
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        response = self.client.session.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers,
            data=xml.encode("utf-8"),
            timeout=60,
        )
        body = response.content.decode("utf-8", "replace")
        ack_match = re.search(r"<Ack>(.*?)</Ack>", body, re.IGNORECASE | re.DOTALL)
        ack = (ack_match.group(1) if ack_match else "").strip()
        if ack not in {"Success", "Warning"}:
            raise RuntimeError(f"Trading GetItem failed (Ack={ack}): {_error_from_trading_response(body)}")
        description = extract_trading_description(body)
        if not description:
            raise RuntimeError("Trading GetItem returned no Description")
        return description

    def revise_description(self, listing_id: str, description: str, aspects: Mapping[str, Any] | None = None) -> dict[str, str]:
        return self.client.revise_fixed_price_item_motors(
            listing_id, description=description, aspects=aspects
        )

    def _inventory_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.client.oauth.get_valid_token()}",
            "Accept": "application/json",
        }

    def _strict_inventory_offers(self, sku: str) -> list[dict[str, Any]]:
        """Read Inventory offers without swallowing non-404 API failures."""
        session = getattr(self.client, "session", None)
        if session is None:
            return self.client.get_offers_by_sku(
                sku,
                marketplace_id=getattr(self.profile, "ebay_marketplace_id", "EBAY_US"),
            ) or []
        response = session.get(
            f"{self.client.base_url}/sell/inventory/v1/offer",
            headers=self._inventory_headers(),
            params={
                "sku": sku,
                "marketplace_id": getattr(self.profile, "ebay_marketplace_id", "EBAY_US"),
            },
            timeout=60,
        )
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise RuntimeError(f"Inventory offer lookup failed for SKU={sku}: HTTP {response.status_code}")
        payload = response.json()
        offers = payload.get("offers", []) if isinstance(payload, Mapping) else []
        return offers if isinstance(offers, list) else []

    def _strict_inventory_item(self, sku: str) -> dict[str, Any] | None:
        """Distinguish Inventory 404 from an unavailable Inventory API."""
        session = getattr(self.client, "session", None)
        if session is None:
            return self.client.get_inventory_item(sku)
        response = session.get(
            f"{self.client.base_url}/sell/inventory/v1/inventory_item/{sku}",
            headers=self._inventory_headers(),
            timeout=60,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"Inventory item lookup failed for SKU={sku}: HTTP {response.status_code}")
        payload = response.json()
        return payload if isinstance(payload, Mapping) else None

    def _find_inventory_offer(self, sku: str, listing_id: str) -> dict[str, Any] | None:
        offers = self._strict_inventory_offers(sku)
        for offer in offers or []:
            listing = offer.get("listing") or {}
            candidate_id = (
                listing.get("listingId")
                or listing.get("listing_id")
                or offer.get("listingId")
                or offer.get("listing_id")
            )
            if str(candidate_id or "").strip() == str(listing_id).strip():
                return offer
        return None

    def find_inventory_offer(self, sku: str, listing_id: str) -> dict[str, Any] | None:
        """Classify Inventory listings conservatively before a Trading write."""
        offer = self._find_inventory_offer(sku, listing_id)
        if offer:
            return offer
        if str(listing_id).strip() in KNOWN_INVENTORY_LISTING_IDS:
            raise RuntimeError(
                f"known Inventory listing {listing_id} has no matching offer for SKU={sku}"
            )
        # A present inventory item with no matching offer is not evidence that
        # the listing is Trading-managed.  Fail closed instead of risking a
        # cross-channel Revise call; the next run can retry the offer lookup.
        if self._strict_inventory_item(sku) is not None:
            raise RuntimeError(
                f"Inventory item exists but offer {listing_id} was not found for SKU={sku}"
            )
        return None

    def update_inventory_description(self, sku: str, listing_id: str, description: str) -> dict[str, str]:
        """Update and publish only the Inventory offer's listingDescription."""
        offer = self._find_inventory_offer(sku, listing_id)
        if not offer:
            raise RuntimeError(f"Inventory offer not found for SKU={sku}, ItemID={listing_id}")
        offer_id = str(offer.get("offerId") or "").strip()
        if not offer_id:
            raise RuntimeError(f"Inventory offer has no offerId for SKU={sku}, ItemID={listing_id}")

        current = self.client.get_offer(offer_id) or offer
        category_id = str(current.get("categoryId") or "").strip()
        if not category_id:
            raise RuntimeError(f"Inventory offer {offer_id} has no categoryId")

        from src.services.motors_trading import ascii_safe

        safe_description = ascii_safe(description)
        if not self.client.update_offer_category(
            offer_id,
            category_id,
            listing_description=safe_description,
        ):
            raise RuntimeError(f"Inventory offer update failed for offerId={offer_id}")

        verified = self.client.get_offer(offer_id) or {}
        actual = _plain_text(verified.get("listingDescription", ""))
        expected = _plain_text(safe_description)
        if actual != expected:
            raise RuntimeError(f"Inventory offer readback mismatch for offerId={offer_id}")

        # Inventory offer edits become the live listing only after publish.
        self.client.publish_offer(offer_id)
        verified = self.client.get_offer(offer_id) or {}
        actual = _plain_text(verified.get("listingDescription", ""))
        if actual != expected:
            raise RuntimeError(f"Inventory offer readback mismatch after publish for offerId={offer_id}")
        return {"channel": "inventory", "offer_id": offer_id, "ack": "Success"}


def _build_gateway() -> EbayContentGateway:
    profile = _profile_guard()
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_auth import EbayOAuthService

    oauth = EbayOAuthService(environment="PRODUCTION")
    return EbayContentGateway(RealEbayClient(oauth, policy_manager=None), profile)


def _is_actionable(issues: Sequence[Mapping[str, Any]]) -> bool:
    return any(str(issue.get("severity", "")).upper() in ACTIONABLE_SEVERITIES for issue in issues)


def run_qc(
    listings: Sequence[PublishedListing],
    *,
    gateway: Any,
    fix: bool = False,
    sleep_seconds: float = MIN_SLEEP_SECONDS,
) -> dict[str, Any]:
    """Read, classify, optionally repair, and read back each selected listing."""
    if sleep_seconds < MIN_SLEEP_SECONDS:
        raise ValueError(f"sleep_seconds must be >= {MIN_SLEEP_SECONDS}")

    report_items: list[dict[str, Any]] = []
    transport_issues: list[dict[str, str]] = []
    fixed_count = 0

    for index, listing in enumerate(listings, 1):
        item: dict[str, Any] = {
            "sku": listing.sku,
            "listing_id": listing.listing_id,
            "title": listing.title,
            "status": "checked",
            "fixes_applied": [],
        }
        try:
            live_description = gateway.get_live_description(listing.listing_id)
            check = check_description(live_description)
            item.update(
                {
                    "live_length": check["length"],
                    "plain_length": check["plain_length"],
                    "issues": check["issues"],
                }
            )
        except Exception as exc:  # transport/API failure is not a content finding
            message = re.sub(r"\s+", " ", str(exc)).strip()[:500]
            item.update({"status": "transport_failed", "issues": [], "error": message})
            transport_issues.append(
                {"sku": listing.sku, "listing_id": listing.listing_id, "error": message}
            )
            report_items.append(item)
            print(f"[{index}/{len(listings)}] TRANSPORT_FAILED {listing.listing_id}: {message}")
            time.sleep(sleep_seconds)
            continue

        if fix and _is_actionable(item["issues"]):
            try:
                regenerated = generate_description(listing)
                generated_check = check_description(regenerated)
                if generated_check["actionable"]:
                    raise RuntimeError(
                        "regenerated description remains actionable: "
                        + "; ".join(issue["code"] for issue in generated_check["issues"])
                    )

                inventory_offer = None
                if hasattr(gateway, "find_inventory_offer"):
                    inventory_offer = gateway.find_inventory_offer(listing.sku, listing.listing_id)
                elif hasattr(gateway, "_find_inventory_offer"):
                    inventory_offer = gateway._find_inventory_offer(listing.sku, listing.listing_id)
                if inventory_offer:
                    result = gateway.update_inventory_description(
                        listing.sku,
                        listing.listing_id,
                        regenerated,
                    )
                else:
                    result = gateway.revise_description(
                        listing.listing_id, regenerated, aspects=listing.aspects
                    )
                item["fixes_applied"] = [issue["code"] for issue in item["issues"] if issue["severity"] in ACTIONABLE_SEVERITIES]
                item["fix_result"] = result or {}
                time.sleep(sleep_seconds)

                verified_description = gateway.get_live_description(listing.listing_id)
                verified = check_description(verified_description)
                item["post_check"] = verified
                if verified["actionable"]:
                    item["status"] = "verify_failed"
                else:
                    item["status"] = "fixed"
                    fixed_count += 1
            except Exception as exc:
                item["status"] = "fix_failed"
                item["error"] = re.sub(r"\s+", " ", str(exc)).strip()[:500]
            finally:
                time.sleep(sleep_seconds)
        else:
            item["post_check"] = check_description(live_description)

        report_items.append(item)
        status = item["status"].upper()
        print(f"[{index}/{len(listings)}] {status} {listing.listing_id} issues={len(item['issues'])}")

    severity_counts: dict[str, int] = {}
    for item in report_items:
        for issue in item.get("issues", []):
            severity = str(issue.get("severity", "UNKNOWN")).upper()
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

    unresolved_actionable = sum(
        1
        for item in report_items
        if _is_actionable(item.get("post_check", {}).get("issues", item.get("issues", [])))
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "fix" if fix else "check",
        "total_published": len(listings),
        "total_checked": sum(item.get("status") != "transport_failed" for item in report_items),
        "fixed_count": fixed_count,
        "unresolved_actionable_count": unresolved_actionable,
        "severity_counts": severity_counts,
        "transport_issues": transport_issues,
        "items": report_items,
    }


def _actionable_items(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = []
    for item in report.get("items", []) or []:
        issues = [
            issue
            for issue in item.get("issues", []) or []
            if str(issue.get("severity", "")).upper() in ACTIONABLE_SEVERITIES
        ]
        if issues:
            trimmed = dict(item)
            trimmed["issues"] = issues
            items.append(trimmed)
    return items


def unresolved_severity_counts(report: Mapping[str, Any]) -> dict[str, int]:
    """Count only CRITICAL/HIGH findings that remain after an optional fix."""
    counts = {"CRITICAL": 0, "HIGH": 0}
    for item in report.get("items", []) or []:
        if item.get("status") == "fixed":
            continue
        issues = item.get("post_check", {}).get("issues") or item.get("issues", []) or []
        for issue in issues:
            severity = str(issue.get("severity", "")).upper()
            if severity in counts:
                counts[severity] += 1
    return counts


def render_email_html(report: Mapping[str, Any]) -> str:
    """Render Chinese summary; MEDIUM/LOW findings never enter the body."""
    actionable = _actionable_items(report)
    unresolved = int(report.get("unresolved_actionable_count", len(actionable)))
    severity = unresolved_severity_counts(report)
    critical = severity["CRITICAL"]
    high = severity["HIGH"]
    color = "#c0392b" if unresolved else "#27ae60"
    headline = f"需处理 {unresolved} 条：CRITICAL {critical} / HIGH {high}"
    rows = []
    for item in actionable:
        issue_text = "<br>".join(
            f"<b>{html.escape(str(issue.get('severity', '')))}</b> "
            f"{html.escape(str(issue.get('detail', '')))}"
            for issue in item.get("issues", [])
        )
        if item.get("status") == "fixed":
            issue_text += "<br><span style='color:#27ae60'>已自动修复并回读确认</span>"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('sku', '')))}</td>"
            f"<td>{html.escape(str(item.get('listing_id', '')))}</td>"
            f"<td>{html.escape(str(item.get('title', ''))[:100])}</td>"
            f"<td>{issue_text}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='4'>无 CRITICAL/HIGH 内容问题</td></tr>")

    transport = report.get("transport_issues", []) or []
    transport_html = ""
    if transport:
        transport_html = (
            f"<p>另有 {len(transport)} 条 live 读取失败，详见附件报告。</p>"
        )
    return (
        "<h2>AquaRides Trading 内容质检</h2>"
        f"<div style='background:{color};color:#fff;padding:12px 16px;border-radius:6px;font-weight:700'>{headline}</div>"
        f"<p>检查 {int(report.get('total_published', 0))} 条，已自动修复 {int(report.get('fixed_count', 0))} 条。</p>"
        f"{transport_html}"
        "<table style='border-collapse:collapse;width:100%'>"
        "<thead><tr><th align='left'>SKU</th><th align='left'>ItemID</th>"
        "<th align='left'>标题</th><th align='left'>问题</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def write_report(report: Mapping[str, Any], path: Path | None = None) -> Path:
    path = path or LOG_DIR / (
        "trading_content_qc_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="repair CRITICAL/HIGH issues")
    parser.add_argument("--email", action="store_true", help="send the Chinese summary email")
    parser.add_argument("--sku", action="append", default=[], help="limit to SKU; repeatable")
    parser.add_argument("--listing-id", action="append", default=[], help="limit to ItemID; repeatable")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=MIN_SLEEP_SECONDS, dest="sleep_seconds")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.sleep_seconds < MIN_SLEEP_SECONDS:
        raise SystemExit(f"--sleep must be >= {MIN_SLEEP_SECONDS}")
    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")

    _profile_guard()
    conn = sqlite3.connect(str(args.db))
    try:
        listings = load_published_rows(conn, skus=args.sku, listing_ids=args.listing_id)
    finally:
        conn.close()
    if args.limit:
        listings = listings[: args.limit]

    print(f"AquaRides Trading QC: {len(listings)} listings (mode={'FIX' if args.fix else 'CHECK'})")
    gateway = _build_gateway()
    report = run_qc(
        listings,
        gateway=gateway,
        fix=args.fix,
        sleep_seconds=args.sleep_seconds,
    )
    report_path = write_report(report, args.report)
    if args.email:
        from src.utils.email_sender import send_email
        from src.utils.email_policy import should_smtp

        unresolved = int(report.get("unresolved_actionable_count", 0))
        severity = unresolved_severity_counts(report)
        subject = (
            "AquaRides Trading 内容质检 - "
            f"需处理 {unresolved} 条（CRITICAL {severity.get('CRITICAL', 0)} / HIGH {severity.get('HIGH', 0)}）"
        )
        has_issues = bool(unresolved) or bool(report.get("transport_issues"))
        sent = send_email(
            subject,
            render_email_html(report),
            attachments=[str(report_path)],
            send_smtp=should_smtp(has_issues=has_issues),
        )
        print(f"email={'sent' if sent else 'saved locally'}; smtp={'on' if has_issues else 'silenced'}")
    print(f"report={report_path}")
    print(
        "done: "
        + json.dumps(
            {
                "checked": report["total_checked"],
                "fixed": report["fixed_count"],
                "unresolved_actionable": report["unresolved_actionable_count"],
                "transport_failures": len(report["transport_issues"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if report["transport_issues"] or report["unresolved_actionable_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
