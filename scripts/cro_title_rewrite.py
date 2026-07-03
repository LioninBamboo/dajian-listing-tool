"""CRO 标题重写通道 — 零曝光 + 广告打满 SKU 的运营手动 SEO 手段.

背景 (ADR-002): 定时标题优化因幻觉与截断事故被移除. 本脚本是它的
受控替代品, 只面向"广告杠杆已打满仍零曝光"的升级候选
(cro_promote_escalation), 这类 listing 没有搜索排名积累可损失,
标题重写风险最低、收益最高.

安全设计:
  - 确定性关键词增强: 新标题 = 原标题 + 该 SKU 自身 optimization.aspects
    里的真实值 (白名单键), 不调用 AI, 不可能引入 unsupported claims.
  - 所有标题写入走 title_sanitizer.normalize_listing_title_for_ebay
    (ADR-002 要求, 禁止裸 [:80]).
  - 默认 dry-run, 只出 old→new 预览报告; --apply 必须搭配 --yes.
  - 写入以 live inventory snapshot 为基 (GET → 只改 title → PUT →
    republish offer → 二次 GET 验证).
  - 30 天重写防抖: 近期已重写过的 SKU 直接跳过, 防标题反复抖动.
  - 不进 scheduler. 这是运营手动通道.

用法:
  python scripts/cro_title_rewrite.py                          # 升级候选 dry-run 预览
  python scripts/cro_title_rewrite.py --skus SKU1,SKU2         # 指定 SKU 预览
  python scripts/cro_title_rewrite.py --apply --yes --limit 5  # 真正改前 5 个
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.title_sanitizer import normalize_listing_title_for_ebay  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"
DEFAULT_LOGS_DIR = PROJECT_ROOT / "logs"
MAX_TITLE_LEN = 80
REWRITE_COOLDOWN_DAYS = 30
_LOG_NAME_RE = re.compile(r"cro_title_rewrite_(\d{8})_\d{6}\.json$")

# 只允许这些 aspect 键进标题 — 高搜索价值且值为可读关键词.
# 顺序即优先级. 尺寸/重量/编码类键 (Item Length, MPN, UPC...) 一律排除.
TITLE_WORTHY_KEYS: tuple = (
    "Type",
    "Style",
    "Material",
    "Frame Material",
    "Upholstery Fabric",
    "Color",
    "Compatible Mattress Size",
    "Size",
    "Number of Seats",
    "Number of Pieces",
    "Room",
    "Shape",
    "Indoor/Outdoor",
    "Finish",
    "Pattern",
)

_MEANINGLESS = {"", "n/a", "na", "none", "null", "unknown", "does not apply",
                "no", "yes", "0", "0.0", "tbd", "other", "multicolor"}


def _first_meaningful_value(raw: Any) -> str:
    values = raw if isinstance(raw, list) else [raw]
    for v in values:
        s = str(v or "").strip()
        if s and s.lower() not in _MEANINGLESS:
            return s
    return ""


def _contains_phrase(title: str, phrase: str) -> bool:
    """词边界匹配, 避免 'Blue' 误匹配 'Blueprint'."""
    return bool(re.search(
        r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])",
        title, flags=re.IGNORECASE))


def build_enriched_title(title: str, aspects: Dict[str, Any],
                         max_length: int = MAX_TITLE_LEN,
                         ) -> Optional[Dict[str, Any]]:
    """返回 {'new_title', 'added_keywords'} 或 None (无可安全添加的关键词).

    只追加该 SKU 自身 aspects 白名单键里的真实值; 不改写原标题内容.
    """
    base = re.sub(r"\s+", " ", str(title or "")).strip()
    if not base:
        return None
    parts = [base]
    added: List[str] = []
    current_len = len(base)
    for key in TITLE_WORTHY_KEYS:
        value = _first_meaningful_value((aspects or {}).get(key))
        if not value:
            continue
        candidate_title = " ".join(parts)
        if _contains_phrase(candidate_title, value):
            continue
        if current_len + 1 + len(value) > max_length:
            continue
        parts.append(value)
        added.append(value)
        current_len += 1 + len(value)
    if not added:
        return None
    enriched = " ".join(parts)
    # source_title 留空: enriched 本身就是完整意图, 不存在"被截断自更长原文",
    # 传自身会让防截断启发式把追加的关键词误判成残片 (如 Blueprint→Blue)
    safe_title, _ = normalize_listing_title_for_ebay(
        enriched, source_title="", max_length=max_length)
    # 归一化可能截掉刚加的词; 只保留真的进入了最终标题的关键词
    surviving = [kw for kw in added if _contains_phrase(safe_title, kw)]
    if not surviving or safe_title == base:
        return None
    return {"new_title": safe_title, "added_keywords": surviving}


def recently_rewritten_skus(days: int = REWRITE_COOLDOWN_DAYS,
                            logs_dir: Optional[Path] = None) -> set:
    """近 N 天已重写过标题的 SKU — 防抖, 不重复折腾同一 listing."""
    d = Path(logs_dir) if logs_dir else DEFAULT_LOGS_DIR
    if not d.is_dir():
        return set()
    cutoff = datetime.now().date() - timedelta(days=days)
    out: set = set()
    for p in d.glob("cro_title_rewrite_*.json"):
        m = _LOG_NAME_RE.search(p.name)
        if not m:
            continue
        try:
            if datetime.strptime(m.group(1), "%Y%m%d").date() < cutoff:
                continue
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        for row in rep.get("rows", []) or []:
            if isinstance(row, dict) and row.get("status") == "done" and row.get("sku"):
                out.add(str(row["sku"]))
    return out


def load_candidates(skus: List[str],
                    db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """从 collected_products 取 PUBLISHED + 有 listing_id 的候选行."""
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists() or not skus:
        return []
    out: List[Dict[str, Any]] = []
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in skus)
        rows = conn.execute(
            f"""SELECT sku, title, optimization, listing_id, status
                FROM collected_products
                WHERE sku IN ({placeholders})
                  AND status = 'PUBLISHED'
                  AND listing_id IS NOT NULL AND listing_id != ''""",
            list(skus),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        opt: Dict[str, Any] = {}
        if row["optimization"]:
            try:
                opt = json.loads(row["optimization"])
            except (TypeError, json.JSONDecodeError):
                opt = {}
        out.append({
            "sku": row["sku"],
            "listing_id": str(row["listing_id"]),
            "title": (opt.get("title") or row["title"] or "").strip(),
            "aspects": opt.get("aspects") or {},
            "optimization": opt,
        })
    return out


def _escalation_skus(days: int = 7) -> List[str]:
    from src.services.cro_promote_escalation import escalation_candidates
    return [c["sku"] for c in escalation_candidates(days=days)]


# ── live 写路径 (--apply) ────────────────────────────────────

def _build_oauth():
    from dotenv import load_dotenv
    from src.services.ebay_auth import EbayOAuthService
    load_dotenv(PROJECT_ROOT / ".env")
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    if not oauth.is_authorized():
        raise RuntimeError("eBay OAuth is not authorized")
    return oauth


def revise_title_live(oauth, sku: str, listing_id: str,
                      new_title: str) -> Dict[str, Any]:
    """以 live inventory snapshot 为基只改 title, republish offer, 二次验证.

    与 scripts/remove_supplier_brand_from_published_titles.py 的
    inventory-API 通道同构 (ADR-002 认可的写路径).
    """
    import requests
    token = oauth.get_valid_token()
    base_url = oauth.api_base
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }
    inv_url = f"{base_url}/sell/inventory/v1/inventory_item/{sku}"
    inv_resp = requests.get(inv_url, headers=headers, timeout=40)
    if inv_resp.status_code != 200:
        return {"ok": False, "reason": f"inventory_get_{inv_resp.status_code}"}
    inv_data = inv_resp.json()
    inv_data.pop("sku", None)
    inv_data.pop("locale", None)
    pws = inv_data.get("packageWeightAndSize", {}) or {}
    weight = (pws.get("weight") or {}).get("value")
    try:
        if weight is not None and float(weight) <= 0:
            inv_data.pop("packageWeightAndSize", None)
    except (TypeError, ValueError):
        inv_data.pop("packageWeightAndSize", None)
    availability = inv_data.get("availability", {}) or {}
    (availability.get("shipToLocationAvailability", {}) or {}).pop(
        "allocationByFormat", None)

    safe_title, _ = normalize_listing_title_for_ebay(
        new_title, source_title=new_title)
    inv_data.setdefault("product", {})["title"] = safe_title

    put_resp = requests.put(inv_url, headers=headers, json=inv_data, timeout=60)
    if put_resp.status_code not in (200, 204):
        return {"ok": False, "reason": f"inventory_put_{put_resp.status_code}"}

    offers_resp = requests.get(
        f"{base_url}/sell/inventory/v1/offer", headers=headers,
        params={"sku": sku}, timeout=40)
    if offers_resp.status_code != 200:
        return {"ok": False, "reason": f"offer_get_{offers_resp.status_code}"}
    offers = offers_resp.json().get("offers", []) or []
    offer = next(
        (o for o in offers
         if str((o.get("listing") or {}).get("listingId") or "") == str(listing_id)),
        offers[0] if offers else None)
    if not offer or not offer.get("offerId"):
        return {"ok": False, "reason": "offer_not_found"}
    pub_resp = requests.post(
        f"{base_url}/sell/inventory/v1/offer/{offer['offerId']}/publish",
        headers=headers, timeout=60)
    if pub_resp.status_code != 200:
        return {"ok": False, "reason": f"offer_publish_{pub_resp.status_code}"}

    verify = requests.get(inv_url, headers=headers, timeout=40)
    live_title = ""
    if verify.status_code == 200:
        live_title = ((verify.json().get("product") or {}).get("title") or "")
    if live_title != safe_title:
        return {"ok": False, "reason": "verification mismatch",
                "live_title": live_title}
    return {"ok": True, "reason": "inventory_api", "title": safe_title}


def _update_local_db(sku: str, new_title: str, optimization: Dict[str, Any],
                     db_path: Optional[Path] = None) -> None:
    db = Path(db_path) if db_path else DEFAULT_DB
    opt = dict(optimization or {})
    opt["title"] = new_title
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT logs FROM collected_products WHERE sku = ?", (sku,)
        ).fetchone()
        logs: List[str] = []
        if row and row[0]:
            try:
                logs = json.loads(row[0]) if isinstance(row[0], str) else []
            except (TypeError, json.JSONDecodeError):
                logs = []
        logs.append(f"CRO title rewrite at {datetime.now().isoformat()}")
        conn.execute(
            """UPDATE collected_products
               SET title = ?, optimization = ?, logs = ?, updated_at = ?
               WHERE sku = ?""",
            (new_title, json.dumps(opt, ensure_ascii=False),
             json.dumps(logs, ensure_ascii=False),
             datetime.now().isoformat(), sku),
        )
        conn.commit()
    finally:
        conn.close()


# ── 主流程 ───────────────────────────────────────────────────

def run(skus: List[str], apply_changes: bool, limit: int,
        db_path: Optional[Path] = None,
        logs_dir: Optional[Path] = None,
        cooldown_days: int = REWRITE_COOLDOWN_DAYS,
        oauth=None) -> Dict[str, Any]:
    rewritten_recently = recently_rewritten_skus(
        days=cooldown_days, logs_dir=logs_dir)
    rep: Dict[str, Any] = {
        "started_at": datetime.now().isoformat(),
        "apply": apply_changes,
        "input_skus": len(skus),
        "rows": [], "done": [], "failed": [], "skipped": [], "proposed": [],
    }
    fresh = [s for s in skus if s not in rewritten_recently]
    rep["cooldown_skipped"] = len(skus) - len(fresh)
    candidates = load_candidates(fresh, db_path=db_path)[:limit]
    found = {c["sku"] for c in candidates}
    for s in fresh[:limit]:
        if s not in found:
            rep["rows"].append({"sku": s, "status": "skipped",
                                "reason": "not PUBLISHED or no listing_id"})
            rep["skipped"].append(s)

    if apply_changes and candidates and oauth is None:
        oauth = _build_oauth()

    for cand in candidates:
        sku = cand["sku"]
        proposal = build_enriched_title(cand["title"], cand["aspects"])
        if not proposal:
            rep["rows"].append({"sku": sku, "status": "skipped",
                                "reason": "no safe keywords to add"})
            rep["skipped"].append(sku)
            continue
        row = {
            "sku": sku,
            "listing_id": cand["listing_id"],
            "old_title": cand["title"],
            "new_title": proposal["new_title"],
            "added_keywords": proposal["added_keywords"],
        }
        if not apply_changes:
            row["status"] = "proposed"
            rep["proposed"].append(sku)
        else:
            try:
                res = revise_title_live(
                    oauth, sku, cand["listing_id"], proposal["new_title"])
            except Exception as e:
                res = {"ok": False, "reason": f"unhandled: {e}"}
            if res.get("ok"):
                row["status"] = "done"
                rep["done"].append(sku)
                _update_local_db(sku, proposal["new_title"],
                                 cand["optimization"], db_path=db_path)
            else:
                row["status"] = "failed"
                row["reason"] = res.get("reason")
                rep["failed"].append(sku)
            time.sleep(1.0)
        rep["rows"].append(row)

    rep["finished_at"] = datetime.now().isoformat()
    return rep


def main():
    p = argparse.ArgumentParser(
        description="CRO title rewrite channel (operator-only, dry-run default)")
    p.add_argument("--skus", help="Comma-separated SKUs (overrides escalation source)")
    p.add_argument("--sku-file", help="File with one SKU per line")
    p.add_argument("--escalation-days", type=int, default=7,
                   help="Escalation lookback window (default 7)")
    p.add_argument("--apply", action="store_true",
                   help="Actually revise live titles (requires --yes)")
    p.add_argument("--yes", action="store_true",
                   help="Confirm you reviewed the dry-run preview")
    p.add_argument("--limit", type=int, default=10,
                   help="Max SKUs per run (default 10)")
    p.add_argument("--out", help="Write JSON report to path")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.apply and not args.yes:
        p.error("--apply requires --yes (run a dry-run preview first)")

    if args.skus:
        skus = [s.strip() for s in args.skus.split(",") if s.strip()]
    elif args.sku_file:
        skus = [line.strip() for line in
                Path(args.sku_file).read_text(encoding="utf-8").splitlines()
                if line.strip()]
    else:
        skus = _escalation_skus(days=args.escalation_days)

    rep = run(skus, apply_changes=args.apply, limit=args.limit)
    print(json.dumps({
        "input_skus": rep["input_skus"],
        "cooldown_skipped": rep["cooldown_skipped"],
        "proposed": len(rep["proposed"]),
        "done": len(rep["done"]),
        "failed": len(rep["failed"]),
        "skipped": len(rep["skipped"]),
        "apply": rep["apply"],
    }, indent=2))
    for row in rep["rows"]:
        if row.get("new_title"):
            print(f"  {row['sku']} [{row['status']}]")
            print(f"    old: {row['old_title']}")
            print(f"    new: {row['new_title']}  (+{', '.join(row['added_keywords'])})")

    out_path = (Path(args.out) if args.out
                else DEFAULT_LOGS_DIR
                / f"cro_title_rewrite_{datetime.now():%Y%m%d_%H%M%S}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"Report → {out_path}")


if __name__ == "__main__":
    main()
