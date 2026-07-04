"""S36 \u2014 \u5229\u6da6\u611f\u77e5\u51fa\u4ef7.

\u9ed8\u8ba4 promote MAX_BID_PCT=25% \u662f\u9759\u6001, \u4f46\u9ad8\u6bdb\u5229 SKU \u53ef\u4ee5\u91c7\u53d6\u66f4\u6fc0\u8fdb\u51fa\u4ef7,
\u5fae\u5229 SKU \u5fc5\u987b\u4e25\u63a7. \u672c\u6a21\u5757\u63d0\u4f9b\u4e00\u4e2a\u7ebf\u6027\u6620\u5c04:
  margin <= 0.05  \u2192 5%   (\u5b9e\u8d28\u4e0d\u52a0\u4ef7)
  margin >= 0.40  \u2192 40%
  \u4e2d\u95f4\u7ebf\u6027\u63d2\u503c.

\u590d\u7528 src.web.pages.competition_monitor.calc_net_margin (\u5b58\u5728\u4e14\u5df2\u4e0a\u7ebf\u4e0d\u53d8).

\u4f7f\u7528:
  cap = bid_cap_for_margin(margin)
  bid_cap_for_sku(sku, db_path)  \u8bfb products \u8868 ourPrice/\u603b\u6210\u672c \u7b97\u51fa cap

\u6c7d\u7535: \u672c\u6a21\u5757\u4e0d\u4fee\u6539 cro_promote.py, \u8c03\u7528\u8005\u53ef\u9009\u62e9\u4f20\u5165 dynamic_cap.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'

MIN_MARGIN_FOR_PROMOTE = 0.05  # \u4f4e\u4e8e\u6b64\u5b8c\u5168\u4e0d\u52a0\u4ef7
HIGH_MARGIN_THRESHOLD = 0.40   # \u5230\u8fbe\u6b64\u4e0a\u9650\u53d6\u6700\u5927
HARD_FLOOR_PCT = 5.0           # \u5fae\u5229\u573a\u666f\u4e0b\u9650
HARD_CEILING_PCT = 40.0        # \u9ad8\u6bdb\u5229\u4e0a\u9650 (\u4ecd\u7559 eBay \u5929\u82b1\u677f)


def bid_cap_for_margin(margin: float) -> float:
    """margin \u662f\u51c0\u5229\u6da6\u7387 (0.0 \u2192 1.0). \u8fd4\u56de\u63a8\u8350\u7684 max bid %."""
    if margin is None or margin <= MIN_MARGIN_FOR_PROMOTE:
        return HARD_FLOOR_PCT
    if margin >= HIGH_MARGIN_THRESHOLD:
        return HARD_CEILING_PCT
    span = HIGH_MARGIN_THRESHOLD - MIN_MARGIN_FOR_PROMOTE
    pct_span = HARD_CEILING_PCT - HARD_FLOOR_PCT
    ratio = (margin - MIN_MARGIN_FOR_PROMOTE) / span
    return round(HARD_FLOOR_PCT + ratio * pct_span, 2)


def _load_sku_economics(db_path: Path, sku: str) -> Optional[dict]:
    """委托共享加载器 (products 表优先, 回退 collected_products).

    修复前此处只查 products 表 — 生产库没有这张表, OperationalError 被
    静默吞掉, bid cap 恒退化为 HARD_FLOOR_PCT=5%: S36 利润感知出价在
    生产从未生效, 也是大量 SKU 卡在 'already at cap (5%)' 的根因.
    """
    from src.services.cro_sku_economics import load_sku_economics
    return load_sku_economics(sku, db_path=db_path)


def bid_cap_for_sku(sku: str, db_path: Optional[Path] = None,
                    margin_calc=None) -> float:
    """\u8fd4\u56de\u8be5 SKU \u52a8\u6001 max bid %. \u627e\u4e0d\u5230\u6570\u636e \u2192 HARD_FLOOR_PCT."""
    db = Path(db_path) if db_path else DEFAULT_DB
    eco = _load_sku_economics(db, sku)
    if not eco or eco['price'] <= 0 or eco['cost'] <= 0:
        return HARD_FLOOR_PCT
    if margin_calc is None:
        try:
            from src.web.pages.competition_monitor import calc_net_margin
            margin_calc = calc_net_margin
        except Exception:
            return HARD_FLOOR_PCT
    margin = float(margin_calc(eco['price'], eco['cost']) or 0.0)
    return bid_cap_for_margin(margin)
