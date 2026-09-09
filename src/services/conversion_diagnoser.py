"""转化率诊断器 — Conversion Diagnoser.

主线: **竞争监控 → 提高转化率**.

输入: 已发布 SKU 的性能 (impressions/views/transactions/sold_qty)
      + 同品类市场水位 (market_median, total_listings)
      + (可选) 我们的售价 / 标题 / 图片数

输出: 每个 SKU 的:
  - funnel_stage: 'no_impression' | 'low_ctr' | 'low_cvr' | 'healthy' | 'insufficient_data'
  - bottleneck: 主要漏斗损失点
  - actions: 优先级排序的可执行动作列表 (每条带类型 + 原因 + 预期影响)
  - cro_score: 0-100, 100 = 健康, 越低越急

CRO 阈值参考 (家具品类经验值):
  - HEALTHY_CTR = 0.015   (1.5%)
  - HEALTHY_CVR = 0.02    (2% views → sales)
  - HEALTHY_STR = 0.0005  (0.05% imp → sales)

不依赖 eBay API; 纯计算 + 规则引擎.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ─── 健康基线 ─────────────────────────────────────────────
MIN_IMPRESSIONS_FOR_DIAGNOSIS = 50  # 噪声下限
HEALTHY_CTR = 0.015
HEALTHY_CVR = 0.02
HEALTHY_STR = 0.0005

# 价格相对市场中位价的告警分位
PRICE_OVERPRICED = 1.15
PRICE_PREMIUM = 1.05


@dataclass
class CroAction:
    type: str          # 'price_drop' | 'title_refresh' | 'image_refresh' | 'fill_specifics' | 'promote' | 'delist'
    priority: int      # 1 (最高) - 5 (最低)
    reason: str
    expected_lift: str  # 短描述, e.g. "+0.5% CTR"
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CroDiagnosis:
    sku: str
    listing_id: str
    impressions: int
    views: int
    transactions: int
    sold_qty: int
    ctr: float
    cvr: float
    str_pct: float
    funnel_stage: str   # 见模块说明
    bottleneck: str     # 中文短句
    cro_score: int      # 0-100
    actions: List[CroAction] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['actions'] = [a.to_dict() for a in self.actions]
        return d


def _calc_ctr(views: int, impressions: int) -> float:
    return round(views / impressions, 4) if impressions > 0 else 0.0


def _calc_cvr(transactions: int, views: int) -> float:
    return round(transactions / views, 4) if views > 0 else 0.0


def _calc_str(transactions: int, impressions: int) -> float:
    return round(transactions / impressions, 5) if impressions > 0 else 0.0


def _classify_funnel(impressions: int, ctr: float, cvr: float, str_pct: float,
                     ctr_thr: float = HEALTHY_CTR,
                     cvr_thr: float = HEALTHY_CVR) -> str:
    if impressions < MIN_IMPRESSIONS_FOR_DIAGNOSIS:
        return 'insufficient_data' if impressions > 0 else 'no_impression'
    if ctr < ctr_thr:
        return 'low_ctr'
    if cvr < cvr_thr:
        return 'low_cvr'
    return 'healthy'


def _price_position(price: float, market_median: float) -> str:
    if not (price and market_median):
        return 'unknown'
    ratio = price / market_median
    if ratio >= PRICE_OVERPRICED:
        return 'overpriced'   # ≥ 115% 中位
    if ratio >= PRICE_PREMIUM:
        return 'premium'      # 105-115%
    if ratio <= 0.85:
        return 'underpriced'  # ≤ 85% — 可能错失利润
    return 'aligned'


def _cro_score(funnel: str, ctr: float, cvr: float, str_pct: float, has_sales: bool,
               ctr_thr: float = HEALTHY_CTR, cvr_thr: float = HEALTHY_CVR,
               str_thr: float = HEALTHY_STR) -> int:
    if funnel == 'insufficient_data':
        return 50
    if funnel == 'no_impression':
        return 20
    score = 0
    score += min(40, (ctr / ctr_thr) * 40) if ctr_thr else 0
    score += min(30, (cvr / cvr_thr) * 30) if cvr_thr else 0
    score += min(20, (str_pct / str_thr) * 20) if str_thr else 0
    if has_sales:
        score += 10
    return max(0, min(100, int(round(score))))


def _build_actions(
    *, funnel: str, price_pos: str, ctr: float, cvr: float,
    impressions: int, views: int, sold_qty: int,
    title_len: int, image_count: int, age_days: int,
    has_sales: bool, market_median: float, price: float,
) -> List[CroAction]:
    actions: List[CroAction] = []

    # 规则 1: 零展示 + 上架 > 14 天 → 推广
    if funnel == 'no_impression' and age_days >= 14 and not has_sales:
        actions.append(CroAction(
            type='promote', priority=1,
            reason=f'上架 {age_days} 天仍零展示, eBay 自然流量未触达',
            expected_lift='+impressions',
            detail={'suggested_bid_pct': 5.0},
        ))

    # 规则 2: 低 CTR + 价格偏高 → 降价是最强杠杆
    if funnel == 'low_ctr' and price_pos in ('overpriced', 'premium'):
        target = round(market_median * 1.0, 2)
        actions.append(CroAction(
            type='price_drop', priority=1,
            reason=f'CTR {ctr*100:.2f}% < 健康 {HEALTHY_CTR*100:.1f}%; '
                   f'售价 ${price:.0f} 高于市场中位 ${market_median:.0f} ({price_pos})',
            expected_lift=f'+CTR (跟齐市场中位 ${target})',
            detail={'current_price': price, 'suggested_price': target,
                    'market_median': market_median, 'drop_pct': round((1 - target / price) * 100, 1)},
        ))

    # 规则 3: 低 CTR + 价格 OK → 标题/主图问题
    if funnel == 'low_ctr' and price_pos in ('aligned', 'underpriced', 'unknown'):
        if title_len and title_len < 60:
            actions.append(CroAction(
                type='title_refresh', priority=2,
                reason=f'CTR {ctr*100:.2f}% 偏低, 但价格已对齐市场; 标题仅 {title_len} 字符 (建议 70-80 充满 keywords)',
                expected_lift='+0.3-0.8% CTR',
                detail={'current_len': title_len, 'target_len': 80},
            ))
        else:
            actions.append(CroAction(
                type='image_refresh', priority=2,
                reason=f'CTR {ctr*100:.2f}% 偏低, 价格已对齐, 标题已充足; 主图缩略图可能不吸引点击',
                expected_lift='+0.2-0.5% CTR',
                detail={'image_count': image_count},
            ))

    # 规则 4: 低 CVR (有点击但不下单) → specifics / 价格 / 运费
    if funnel == 'low_cvr':
        if price_pos in ('overpriced', 'premium'):
            target = round(market_median * 0.98, 2)
            actions.append(CroAction(
                type='price_drop', priority=1,
                reason=f'CVR {cvr*100:.2f}% < 健康 {HEALTHY_CVR*100:.1f}%; 浏览到下单流失大, 价格高于中位是首要因素',
                expected_lift=f'+CVR (降至 ${target})',
                detail={'current_price': price, 'suggested_price': target},
            ))
        else:
            actions.append(CroAction(
                type='fill_specifics', priority=2,
                reason=f'CVR {cvr*100:.2f}% < 健康 {HEALTHY_CVR*100:.1f}%; 价格 OK, 多半因 item specifics 或描述不全劝退',
                expected_lift='+0.5-1.5% CVR',
                detail={},
            ))
            # 有点击不下单 → 给 watchers/加购买家发限时 offer 直接刺激下单.
            # 执行器 (cro_send_offer) 有保本地板价守门: offer 价不低于
            # PricingEngine 费率推导的最低净利率价, 无让利空间则 skip.
            actions.append(CroAction(
                type='send_offer', priority=2,
                reason=f'CVR {cvr*100:.2f}% 低但有点击; 向已表达兴趣的买家发保本限时 offer',
                expected_lift='+CVR (interested buyers)',
                detail={'suggested_discount_pct': 5.0},
            ))

    # 规则 5: 长期零销售 + 健康 funnel → 强力推广 + 价格复核
    if not has_sales and age_days >= 30 and impressions >= MIN_IMPRESSIONS_FOR_DIAGNOSIS * 4:
        actions.append(CroAction(
            type='promote', priority=2,
            reason=f'30+ 天零销售但已有 {impressions:,} 展示, 自然漏斗未转化; 推广拉一把 + 复核价格',
            expected_lift='+sales',
            detail={'suggested_bid_pct': 7.0},
        ))

    # 规则 6: 死链候选 — 60+ 天零展示零销售 → 下架
    if not has_sales and impressions == 0 and age_days >= 60:
        actions.append(CroAction(
            type='delist', priority=3,
            reason=f'{age_days} 天零展示零销售, 占用账号 listing 配额',
            expected_lift='清账号位',
            detail={},
        ))

    # 规则 7: 极低价 → 提示提价 (CRO 健康但利润流失)
    if price_pos == 'underpriced' and has_sales:
        target = round(market_median * 0.95, 2)
        actions.append(CroAction(
            type='price_drop', priority=4,  # 反向: 提价
            reason=f'价 ${price:.0f} 显著低于市场中位 ${market_median:.0f}; 有销售证明需求, 可逐步提价',
            expected_lift='+margin (CRO 不变)',
            detail={'current_price': price, 'suggested_price': target,
                    'is_increase': True},
        ))

    actions.sort(key=lambda a: a.priority)
    return actions


def _bottleneck_text(funnel: str, ctr: float, cvr: float) -> str:
    return {
        'no_impression': 'eBay 未给曝光 (SEO 或推广问题)',
        'insufficient_data': '数据不足 (展示 < 50)',
        'low_ctr': f'曝光→点击漏 (CTR {ctr*100:.2f}%)',
        'low_cvr': f'点击→下单漏 (CVR {cvr*100:.2f}%)',
        'healthy': '漏斗健康',
    }.get(funnel, '未知')


def diagnose_sku(product: Dict[str, Any], market_median: float = 0,
                 thresholds: Optional[Dict[str, float]] = None,
                 ) -> CroDiagnosis:
    """对单个 SKU 出诊断 + 建议动作.

    product 必备字段: sku, listing_id, impressions, views, transactions, sold_qty,
                     selling_price, age_days, title (str), images (list 或 int)
    thresholds: optional {'ctr':..., 'cvr':..., 'str':...} 按品类阈值 (S16).
    """
    th = thresholds or {}
    ctr_thr = float(th.get('ctr', HEALTHY_CTR))
    cvr_thr = float(th.get('cvr', HEALTHY_CVR))
    str_thr = float(th.get('str', HEALTHY_STR))
    impressions = int(product.get('impressions', 0) or 0)
    views = int(product.get('views', 0) or 0)
    transactions = int(product.get('transactions', 0) or 0)
    sold_qty = int(product.get('sold_qty', 0) or 0)
    price = float(product.get('selling_price', 0) or 0)
    age_days = int(product.get('age_days', 0) or 0)
    title = product.get('title', '') or ''
    images = product.get('images', []) or []
    image_count = len(images) if isinstance(images, list) else int(images or 0)

    ctr = _calc_ctr(views, impressions)
    # CVR: 用 transactions/views; 若 transactions 缺失但 sold_qty 有, 退而求其次
    effective_tx = transactions or sold_qty
    cvr = _calc_cvr(effective_tx, views)
    str_pct = _calc_str(effective_tx, impressions)
    funnel = _classify_funnel(impressions, ctr, cvr, str_pct, ctr_thr, cvr_thr)
    has_sales = (transactions > 0) or (sold_qty > 0)
    price_pos = _price_position(price, market_median)
    score = _cro_score(funnel, ctr, cvr, str_pct, has_sales, ctr_thr, cvr_thr, str_thr)

    actions = _build_actions(
        funnel=funnel, price_pos=price_pos, ctr=ctr, cvr=cvr,
        impressions=impressions, views=views, sold_qty=sold_qty,
        title_len=len(title), image_count=image_count, age_days=age_days,
        has_sales=has_sales, market_median=market_median, price=price,
    )

    return CroDiagnosis(
        sku=product.get('sku', ''),
        listing_id=str(product.get('listing_id', '') or ''),
        impressions=impressions, views=views,
        transactions=transactions, sold_qty=sold_qty,
        ctr=ctr, cvr=cvr, str_pct=str_pct,
        funnel_stage=funnel,
        bottleneck=_bottleneck_text(funnel, ctr, cvr),
        cro_score=score, actions=actions,
    )


def diagnose_batch(products: List[Dict[str, Any]],
                    market_data: Optional[Dict[str, Dict]] = None,
                    thresholds_by_category: Optional[Dict[str, Dict[str, float]]] = None,
                    ) -> List[CroDiagnosis]:
    """批量诊断. market_data: {category_id: {median: float, ...}}.

    thresholds_by_category: {category_id: {'ctr':..., 'cvr':..., 'str':...}} 可选 (S16).
    """
    market_data = market_data or {}
    thresholds_by_category = thresholds_by_category or {}
    out = []
    for p in products:
        cat = p.get('categoryId', '?')
        median = float((market_data.get(cat) or {}).get('median', 0) or 0)
        thr = thresholds_by_category.get(cat) or thresholds_by_category.get(str(cat)) or None
        out.append(diagnose_sku(p, market_median=median, thresholds=thr))
    return out


def summarize(diagnoses: List[CroDiagnosis]) -> Dict[str, Any]:
    """返回顶层汇总 — 用于仪表盘 KPI."""
    if not diagnoses:
        return {'total': 0}
    by_stage: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for d in diagnoses:
        by_stage[d.funnel_stage] = by_stage.get(d.funnel_stage, 0) + 1
        for a in d.actions:
            by_action[a.type] = by_action.get(a.type, 0) + 1
    avg_score = sum(d.cro_score for d in diagnoses) / len(diagnoses)
    return {
        'total': len(diagnoses),
        'avg_cro_score': round(avg_score, 1),
        'by_funnel_stage': by_stage,
        'by_action_type': by_action,
        'urgent_count': sum(1 for d in diagnoses if d.cro_score < 30),
        'healthy_count': sum(1 for d in diagnoses if d.funnel_stage == 'healthy'),
    }


def top_actions(diagnoses: List[CroDiagnosis], limit: int = 50,
                action_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """按 (priority, -impressions) 排序 — 优先做高曝光 SKU 的 P1 动作."""
    rows: List[Dict[str, Any]] = []
    for d in diagnoses:
        for a in d.actions:
            if action_type and a.type != action_type:
                continue
            rows.append({
                'sku': d.sku, 'listing_id': d.listing_id,
                'priority': a.priority, 'action': a.type,
                'reason': a.reason, 'expected_lift': a.expected_lift,
                'cro_score': d.cro_score, 'funnel_stage': d.funnel_stage,
                'impressions': d.impressions, 'ctr': d.ctr, 'cvr': d.cvr,
                'detail': a.detail,
            })
    rows.sort(key=lambda r: (r['priority'], -r['impressions']))
    return rows[:limit]
