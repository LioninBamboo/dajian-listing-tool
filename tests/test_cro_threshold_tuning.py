"""S19 — suggest_threshold_adjustments + S20 — shadow_compare."""
from __future__ import annotations


# ─── S19 ───────────────────────────────────────────────────────────
def test_suggest_relaxes_when_improved_rate_low():
    from src.services.cro_thresholds import suggest_threshold_adjustments
    rep = {'rows': [
        {'category': 'FURN', 'action': 'price_drop', 'verdict': 'flat'},
        {'category': 'FURN', 'action': 'price_drop', 'verdict': 'worsened'},
        {'category': 'FURN', 'action': 'price_drop', 'verdict': 'improved'},
        {'category': 'FURN', 'action': 'price_drop', 'verdict': 'flat'},
        {'category': 'FURN', 'action': 'price_drop', 'verdict': 'flat'},
    ]}  # 1/5 = 20% improved
    cur = {'FURN': {'ctr': 0.020, 'cvr': 0.02, 'str': 0.0005}}
    sugg = suggest_threshold_adjustments(rep, cur)
    assert len(sugg) == 1
    s = sugg[0]
    assert s['direction'] == 'relax'
    assert s['metric'] == 'ctr'
    assert s['suggested'] < s['current']


def test_suggest_tightens_when_improved_rate_high():
    from src.services.cro_thresholds import suggest_threshold_adjustments
    rep = {'rows': [
        {'category': 'TOOL', 'action': 'fill_specifics', 'verdict': 'improved'},
        {'category': 'TOOL', 'action': 'fill_specifics', 'verdict': 'improved'},
        {'category': 'TOOL', 'action': 'fill_specifics', 'verdict': 'improved'},
        {'category': 'TOOL', 'action': 'fill_specifics', 'verdict': 'improved'},
        {'category': 'TOOL', 'action': 'fill_specifics', 'verdict': 'flat'},
        {'category': 'TOOL', 'action': 'fill_specifics', 'verdict': 'flat'},
    ]}  # 4/6 = 66.7% improved → tighten cvr
    cur = {'TOOL': {'ctr': 0.05, 'cvr': 0.06, 'str': 0.003}}
    sugg = suggest_threshold_adjustments(rep, cur)
    assert len(sugg) == 1
    assert sugg[0]['direction'] == 'tighten'
    assert sugg[0]['metric'] == 'cvr'
    assert sugg[0]['suggested'] > sugg[0]['current']


def test_suggest_skips_low_sample():
    from src.services.cro_thresholds import suggest_threshold_adjustments
    rep = {'rows': [
        {'category': 'X', 'action': 'price_drop', 'verdict': 'worsened'},
        {'category': 'X', 'action': 'price_drop', 'verdict': 'worsened'},
    ]}  # only 2 samples
    cur = {'X': {'ctr': 0.02, 'cvr': 0.02, 'str': 0.0005}}
    assert suggest_threshold_adjustments(rep, cur) == []


def test_suggest_skips_unlearned_category():
    from src.services.cro_thresholds import suggest_threshold_adjustments
    rep = {'rows': [
        {'category': 'NEWCAT', 'action': 'price_drop', 'verdict': 'worsened'}
    ] * 8}
    cur = {}  # NEWCAT 还没学到阈值
    assert suggest_threshold_adjustments(rep, cur) == []


# ─── S20 ───────────────────────────────────────────────────────────
def _make_products(n_low_ctr: int, n_healthy: int):
    products = []
    for i in range(n_low_ctr):
        products.append({
            'sku': f'L{i}', 'listing_id': str(i),
            'categoryId': 'FURN',
            'impressions': 1000, 'views': 10, 'transactions': 1, 'sold_qty': 1,
            'selling_price': 100, 'age_days': 30,
            'title': 't' * 70, 'images': [1, 2, 3],
        })  # ctr = 1.0% (低于 1.5% 默认 → low_ctr; 高于 0.5% → healthy)
    for i in range(n_healthy):
        products.append({
            'sku': f'H{i}', 'listing_id': str(100 + i),
            'categoryId': 'FURN',
            'impressions': 1000, 'views': 50, 'transactions': 5, 'sold_qty': 5,
            'selling_price': 100, 'age_days': 30,
            'title': 't' * 70, 'images': [1, 2, 3],
        })
    return products


def test_shadow_detects_p1_explosion():
    from scripts.cro_threshold_shadow import shadow_compare
    products = _make_products(n_low_ctr=30, n_healthy=5)
    # 旧阈值 ctr=0.005 → 所有 SKU 都 healthy → 0 P1
    old = {'FURN': {'ctr': 0.005, 'cvr': 0.005, 'str': 0.0001}}
    # 新阈值 ctr=0.015 → 30 个 low_ctr → 价格对齐时 image_refresh, 价格不齐时 price_drop
    # 但 _make_products 没传 market_data → price_pos='unknown' → image_refresh P2
    # 改用价格不健康场景: 把市场中位设为 70 (我们 100 → overpriced) → low_ctr + price_drop P1
    new = {'FURN': {'ctr': 0.015, 'cvr': 0.005, 'str': 0.0001}}
    market = {'FURN': {'median': 70.0}}
    rep = shadow_compare(products, market, old, new)
    assert rep['new']['p1_count'] > rep['old']['p1_count']
    # 应当触发 explosion (从 0 涨到 ≥ 20)
    assert rep['safe_to_promote'] is False
    assert rep['explosions']


def test_shadow_safe_when_no_explosion():
    from scripts.cro_threshold_shadow import shadow_compare
    products = _make_products(n_low_ctr=2, n_healthy=20)
    old = {'FURN': {'ctr': 0.005, 'cvr': 0.005, 'str': 0.0001}}
    new = {'FURN': {'ctr': 0.006, 'cvr': 0.005, 'str': 0.0001}}
    rep = shadow_compare(products, {'FURN': {'median': 70.0}}, old, new)
    assert rep['safe_to_promote'] is True
    assert rep['explosions'] == []
