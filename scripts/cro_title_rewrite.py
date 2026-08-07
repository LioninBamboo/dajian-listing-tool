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
  - 默认候选池 = promote 打满升级候选 + 最新 CRO 零曝光保守补池,
    仅扩量, 不放宽任何标题质检条件.

用法:
  python scripts/cro_title_rewrite.py                          # 升级候选 dry-run 预览
  python scripts/cro_title_rewrite.py --skus SKU1,SKU2         # 指定 SKU 预览
  python scripts/cro_title_rewrite.py --apply --yes --limit 5  # 真正改前 5 个
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
)
HOT_KEYWORD_SUPPORT_KEYS: tuple = TITLE_WORTHY_KEYS + ("Features",)

_MEANINGLESS = {"", "n/a", "na", "none", "null", "unknown", "does not apply",
                "no", "yes", "0", "0.0", "tbd", "other", "multicolor"}
_BLOCKED_HOT_KEYWORD_PHRASES = {
    "hot sale",
    "best gift",
    "free shipping",
    "fast shipping",
    "ikea style",
    "like ikea",
    "better than",
    "universal",
    "oem",
    "genuine",
    "original",
}
_COLOR_TERMS = {
    "black", "white", "blue", "red", "green", "gray", "grey", "brown",
    "beige", "cream", "ivory", "yellow", "pink", "purple", "orange",
    "gold", "silver", "navy", "natural", "walnut", "espresso",
}
_MATTRESS_SIZE_TERMS = {
    "twin", "full", "queen", "king", "california king",
}


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


def _phrase_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _title_case_keyword(text: str) -> str:
    words = _phrase_key(text).split()
    return " ".join(w.upper() if len(w) <= 3 and w.isalpha() else w.capitalize()
                    for w in words)


def _extract_hot_keywords(optimization: Dict[str, Any]) -> List[str]:
    """Return ordered market/search keyword candidates from known opt shapes."""
    opt = optimization or {}
    sources = [
        opt.get("hot_keywords"),
        opt.get("market_keywords"),
        opt.get("top_keywords"),
        (opt.get("market_intel") or {}).get("top_keywords")
        if isinstance(opt.get("market_intel"), dict) else None,
    ]
    out: List[str] = []
    seen = set()
    for raw in sources:
        if not raw:
            continue
        values = raw if isinstance(raw, list) else [raw]
        for item in values:
            val = item.get("keyword") if isinstance(item, dict) else item
            key = _phrase_key(val)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(str(val).strip())
    return out


def _supported_hot_keyword(keyword: str, aspects: Dict[str, Any]) -> str:
    """Allow a hot/search term only when SKU aspects already prove it."""
    key = _phrase_key(keyword)
    if not key or key in _MEANINGLESS:
        return ""
    if any(blocked in key for blocked in _BLOCKED_HOT_KEYWORD_PHRASES):
        return ""
    if len(key) > 35:
        return ""
    key_tokens = set(key.split())
    if not key_tokens:
        return ""
    for aspect_key in HOT_KEYWORD_SUPPORT_KEYS:
        raw = (aspects or {}).get(aspect_key)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            support = _phrase_key(value)
            if not support or support in _MEANINGLESS:
                continue
            support_tokens = set(support.split())
            if key == support or key_tokens.issubset(support_tokens):
                return _title_case_keyword(support)
    return ""


def _aspect_value_key(aspects: Dict[str, Any], key: str) -> str:
    return _phrase_key(_first_meaningful_value((aspects or {}).get(key)))


def _conflicting_known_term(title: str, expected: str,
                            known_terms: set[str]) -> str:
    if not expected:
        return ""
    expected_tokens = set(expected.split())
    for term in sorted(known_terms, key=len, reverse=True):
        if term == expected or set(term.split()).issubset(expected_tokens):
            continue
        if _contains_phrase(title, term):
            return _title_case_keyword(term)
    return ""


def title_aspect_conflicts(title: str, aspects: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return high-confidence title/SKU aspect mismatches before any live write."""
    base = str(title or "")
    conflicts: List[Dict[str, str]] = []
    color = _aspect_value_key(aspects, "Color")
    found_color = _conflicting_known_term(base, color, _COLOR_TERMS)
    if found_color:
        conflicts.append({
            "aspect": "Color",
            "expected": _title_case_keyword(color),
            "found": found_color,
        })
    mattress_size = _aspect_value_key(aspects, "Compatible Mattress Size")
    found_size = _conflicting_known_term(base, mattress_size, _MATTRESS_SIZE_TERMS)
    if found_size:
        conflicts.append({
            "aspect": "Compatible Mattress Size",
            "expected": _title_case_keyword(mattress_size),
            "found": found_size,
        })
    return conflicts


def build_enriched_title(title: str, aspects: Dict[str, Any],
                         hot_keywords: Optional[List[str]] = None,
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
    added_hot: List[str] = []
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
    for raw_keyword in hot_keywords or []:
        value = _supported_hot_keyword(raw_keyword, aspects or {})
        if not value:
            continue
        candidate_title = " ".join(parts)
        if _contains_phrase(candidate_title, value):
            continue
        if current_len + 1 + len(value) > max_length:
            continue
        parts.append(value)
        added.append(value)
        added_hot.append(value)
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
    surviving_hot = [kw for kw in added_hot if _contains_phrase(safe_title, kw)]
    return {
        "new_title": safe_title,
        "added_keywords": surviving,
        "added_hot_keywords": surviving_hot,
    }


def title_fact_guard(cand: Dict[str, Any], new_title: str) -> Tuple[bool, List[Dict[str, Any]]]:
    """FactSheet backstop before any live title write.

    ``build_enriched_title`` only appends the SKU's own aspect values, so the
    enriched title is grounded by construction — but this runs the shared
    semantic FactSheet as an explicit gate so a scheduled LIVE apply can never
    push an un-sourced claim. Returns (passed, blocking_violations). QC being
    unavailable (no QWEN key / no conn) does NOT block — it degrades to the
    existing aspect-grounded behavior rather than silently failing every SKU.
    """
    try:
        from src.utils.listing_fact_sheet import check_fact_sheet_violations
    except Exception:
        return True, []
    opt = cand.get("optimization") or {}
    aspects = cand.get("aspects") or {}
    try:
        conn = sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"), timeout=30)
    except Exception:
        return True, []
    try:
        res = check_fact_sheet_violations(
            conn,
            source_title=str(cand.get("title") or ""),
            source_description=str(opt.get("description") or ""),
            source_attributes=aspects,
            source_specs={},
            candidate_title=new_title,
            candidate_description=new_title,   # fact-check the title as the copy
            candidate_aspects=aspects,
        )
    except Exception:
        return True, []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if res.get("status") != "violations":
        return True, []
    blocking = [v for v in res.get("violations", [])
                if str(v.get("severity")) in ("CRITICAL", "HIGH")]
    return (not blocking), blocking


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
            "hot_keywords": _extract_hot_keywords(opt),
            "optimization": opt,
        })
    return out


def _escalation_skus(days: int = 7) -> List[str]:
    from src.services.cro_promote_escalation import escalation_candidates
    return [c["sku"] for c in escalation_candidates(days=days)]


def _snapshot_fallback_skus(limit: int,
                            db_path: Optional[Path] = None) -> List[str]:
    """保守补池: 最新 CRO 快照里仍零曝光、主动作=promote、且未出现 delist 信号."""
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists() or limit <= 0:
        return []
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            "SELECT MAX(snapshot_date) FROM cro_snapshots"
        ).fetchone()
        snapshot_date = latest[0] if latest else None
        if not snapshot_date:
            return []
        rows = conn.execute(
            """
            SELECT sku
            FROM cro_snapshots
            WHERE snapshot_date = ?
              AND funnel_stage = 'no_impression'
              AND top_action = 'promote'
              AND COALESCE(impressions, 0) = 0
              AND (
                    actions_json IS NULL
                    OR actions_json NOT LIKE '%"type": "delist"%'
                  )
            ORDER BY cro_score ASC, sku ASC
            LIMIT ?
            """,
            (snapshot_date, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["sku"]) for row in rows if row["sku"]]


def default_candidate_skus(limit: int,
                           escalation_days: int = 7,
                           db_path: Optional[Path] = None) -> List[str]:
    """默认候选池: 升级候选优先, 不足时用 CRO 零曝光保守补池补足."""
    merged: List[str] = []
    seen = set()
    for sku in _escalation_skus(days=escalation_days):
        if sku and sku not in seen:
            merged.append(sku)
            seen.add(sku)
            if len(merged) >= limit:
                return merged
    remaining = limit - len(merged)
    if remaining <= 0:
        return merged
    for sku in _snapshot_fallback_skus(limit=remaining * 3, db_path=db_path):
        if sku and sku not in seen:
            merged.append(sku)
            seen.add(sku)
            if len(merged) >= limit:
                break
    return merged


def _status_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"proposed": 0, "done": 0, "failed": 0, "skipped": 0}
    for row in rows or []:
        status = str(row.get("status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
    return counts


def render_email_html(rep: Dict[str, Any]) -> str:
    counts = _status_counts(rep.get("rows") or [])
    changed_rows = [
        row for row in (rep.get("rows") or [])
        if row.get("status") in {"proposed", "done"} and row.get("new_title")
    ][:20]
    blocked_rows = [
        row for row in (rep.get("rows") or [])
        if row.get("status") in {"skipped", "failed"}
    ][:20]

    def _fmt(text: Any) -> str:
        return html.escape(str(text or ""))

    def _join_keywords(row: Dict[str, Any]) -> str:
        kws = list(row.get("added_keywords") or [])
        hot = set(row.get("added_hot_keywords") or [])
        parts = []
        for kw in kws:
            label = f"{kw} (hot)" if kw in hot else kw
            parts.append(_fmt(label))
        return "<br>".join(parts) if parts else "—"

    changed_html = "".join(
        f"""
        <tr>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('sku'))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('status'))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('old_title'))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('new_title'))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_join_keywords(row)}</td>
        </tr>
        """
        for row in changed_rows
    ) or """
        <tr><td colspan="5" style="padding:10px;border:1px solid #e5e7eb;color:#6b7280;">本次无可展示的改写结果</td></tr>
    """

    blocked_html = "".join(
        f"""
        <tr>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('sku'))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('status'))}</td>
          <td style="padding:8px;border:1px solid #e5e7eb;vertical-align:top;">{_fmt(row.get('reason'))}</td>
        </tr>
        """
        for row in blocked_rows
    ) or """
        <tr><td colspan="3" style="padding:10px;border:1px solid #e5e7eb;color:#6b7280;">本次无失败/拦截项</td></tr>
    """

    return f"""
    <div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#111827;line-height:1.5;">
      <h2 style="margin:0 0 12px 0;">✏️ CRO 标题热词优化日报</h2>
      <p style="margin:0 0 16px 0;color:#4b5563;">
        本邮件展示今日标题重写任务的候选池规模、质检拦截情况，以及实际改写明细。
      </p>
      <table style="border-collapse:collapse;width:100%;max-width:860px;margin-bottom:18px;">
        <tr>
          <td style="padding:12px;border:1px solid #e5e7eb;background:#f9fafb;"><b>候选输入</b><br>{int(rep.get('input_skus') or 0)}</td>
          <td style="padding:12px;border:1px solid #e5e7eb;background:#f9fafb;"><b>冷却跳过</b><br>{int(rep.get('cooldown_skipped') or 0)}</td>
          <td style="padding:12px;border:1px solid #e5e7eb;background:#ecfdf5;"><b>成功/建议</b><br>{counts['done'] + counts['proposed']}</td>
          <td style="padding:12px;border:1px solid #e5e7eb;background:#fef2f2;"><b>失败/拦截</b><br>{counts['failed'] + counts['skipped']}</td>
        </tr>
      </table>

      <p style="margin:0 0 10px 0;">
        <b>执行模式：</b>{'正式改写' if rep.get('apply') else '预演预览'}
        &nbsp;·&nbsp;
        <b>建议/成功：</b>{counts['proposed']} / {counts['done']}
        &nbsp;·&nbsp;
        <b>失败：</b>{counts['failed']}
        &nbsp;·&nbsp;
        <b>质检拦截：</b>{counts['skipped']}
      </p>

      <h3 style="margin:18px 0 8px 0;">改写明细（前 20 条）</h3>
      <table style="border-collapse:collapse;width:100%;max-width:1100px;">
        <tr style="background:#f3f4f6;">
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">SKU</th>
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">状态</th>
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">原标题</th>
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">新标题</th>
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">新增词</th>
        </tr>
        {changed_html}
      </table>

      <h3 style="margin:18px 0 8px 0;">失败 / 拦截（前 20 条）</h3>
      <table style="border-collapse:collapse;width:100%;max-width:860px;">
        <tr style="background:#f3f4f6;">
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">SKU</th>
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">状态</th>
          <th style="padding:8px;border:1px solid #e5e7eb;text-align:left;">原因</th>
        </tr>
        {blocked_html}
      </table>

      <p style="margin:18px 0 0 0;color:#6b7280;font-size:12px;">
        标题仅允许追加 SKU 自身 aspects 与可证实热词；冲突属性、30 天内已改写 SKU、无安全增词 SKU 均会被自动拦截。
      </p>
    </div>
    """


def _send_email(rep: Dict[str, Any], report_path: Path) -> bool:
    from src.utils.email_sender import send_email

    counts = _status_counts(rep.get("rows") or [])
    subject = (
        f"✏️ CRO 标题优化 - 候选{int(rep.get('input_skus') or 0)} "
        f"成功{counts['done']} 建议{counts['proposed']} "
        f"失败{counts['failed']} 拦截{counts['skipped']}"
    )
    return bool(send_email(subject, render_email_html(rep), attachments=[str(report_path)]))


# ── live 写路径 (--apply) ────────────────────────────────────

def _build_oauth():
    from dotenv import load_dotenv
    from src.services.ebay_auth import EbayOAuthService
    load_dotenv(PROJECT_ROOT / ".env")
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    if not oauth.is_authorized():
        raise RuntimeError("eBay OAuth is not authorized")
    return oauth


_TRADING_CLIENT = None


def _get_trading_client():
    """懒构建 Trading API client (模块级缓存, 单次运行最多 27 个 SKU)."""
    global _TRADING_CLIENT
    if _TRADING_CLIENT is None:
        from src.clients.ebay_client import EbayClient
        from src.clients.ebay_trading_client import EbayTradingClient
        environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper()
        ebay = EbayClient(
            os.getenv("EBAY_APP_ID"),
            os.getenv("EBAY_CERT_ID"),
            os.getenv("EBAY_DEV_ID"),
            env="production" if environment == "PRODUCTION" else "sandbox",
        )
        _TRADING_CLIENT = EbayTradingClient(ebay)
    return _TRADING_CLIENT


# ReviseItem 返回这两个错误码 = 该 listing 由 Inventory API 管理 → 走回退
_TRADING_USE_INVENTORY_CODES = {"21919474", "21919456"}


def revise_title_live(oauth, sku: str, listing_id: str,
                      new_title: str) -> Dict[str, Any]:
    """双通道标题改写, 与 remove_supplier_brand_from_published_titles.py 同构:

    1. Trading API ReviseItem — 老 listing (Trading 创建) 的唯一可用通道;
       Inventory API 对它们的 GET 直接 500 (2026-07-03 实测 11/16).
    2. 错误码提示由 Inventory API 管理时, 回退 inventory 路径:
       live snapshot 为基只改 title → PUT → republish offer → 二次验证.
    """
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import escape as xml_escape

    safe_title, _ = normalize_listing_title_for_ebay(
        new_title, source_title=new_title)

    try:
        trading = _get_trading_client()
        xml_payload = (
            f"<Item><ItemID>{xml_escape(str(listing_id))}</ItemID>"
            f"<Title>{xml_escape(safe_title)}</Title></Item>"
        )
        response = trading.call("ReviseItem", xml_payload)
        root = ET.fromstring(response)
        ns = {"ebay": "urn:ebay:apis:eBLBaseComponents"}
        ack = root.find(".//ebay:Ack", ns)
        if ack is not None and ack.text in {"Success", "Warning"}:
            return {"ok": True, "reason": "trading_api", "title": safe_title}
        use_inventory = False
        for err in root.findall(".//ebay:Errors", ns):
            code = err.find("ebay:ErrorCode", ns)
            if code is None:
                continue
            if code.text in _TRADING_USE_INVENTORY_CODES:
                use_inventory = True
            elif code.text == "291":
                return {"ok": False, "reason": "listing_ended"}
        if not use_inventory:
            logger.info("ReviseItem non-fatal for %s, trying inventory path", sku)
    except Exception as e:
        logger.info("Trading path unavailable for %s (%s), trying inventory", sku, e)

    return _revise_title_inventory(oauth, sku, listing_id, safe_title)


def _revise_title_inventory(oauth, sku: str, listing_id: str,
                            safe_title: str) -> Dict[str, Any]:
    """Inventory API 回退: live snapshot 为基只改 title + 写后验证."""
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
        conflicts = title_aspect_conflicts(cand["title"], cand["aspects"])
        if conflicts:
            rep["rows"].append({
                "sku": sku,
                "status": "skipped",
                "reason": "title conflicts with SKU aspects",
                "conflicts": conflicts,
            })
            rep["skipped"].append(sku)
            continue
        proposal = build_enriched_title(
            cand["title"], cand["aspects"], cand.get("hot_keywords") or [])
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
            "added_hot_keywords": proposal.get("added_hot_keywords", []),
        }
        # QC backstop: never write a title the semantic FactSheet flags. Runs on
        # dry-run too, so the report shows exactly which SKUs would be blocked.
        guard_ok, guard_violations = title_fact_guard(cand, proposal["new_title"])
        if not guard_ok:
            row["status"] = "skipped"
            row["reason"] = "title failed FactSheet guard"
            row["fact_violations"] = guard_violations
            rep["skipped"].append(sku)
            rep["rows"].append(row)
            continue
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


def main() -> int:
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
    p.add_argument("--email", action="store_true",
                   help="Send email summary after run")
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
        skus = default_candidate_skus(
            limit=args.limit,
            escalation_days=args.escalation_days,
        )

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

    if args.email:
        try:
            if _send_email(rep, out_path):
                print("Email sent")
            else:
                print("Email delivery failed; local report was saved")
                return 5
        except Exception as e:
            print(f"Email failed: {e}")
            return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
