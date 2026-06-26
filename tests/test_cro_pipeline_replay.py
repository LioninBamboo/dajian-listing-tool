"""S30 \u2014 CRO pipeline \u5386\u53f2\u56de\u653e\u56de\u5f52.

\u7528\u786c\u7f16\u7801\u7684\u786e\u5b9a\u6027\u4ea7\u54c1\u5217\u8868\u8dd1\u4e00\u8f6e diagnose_batch + summarize + top_actions,
\u65ad\u8a00\u5173\u952e\u8f93\u51fa\u4e0d\u6f02\u79fb. \u4efb\u4f55\u8c03\u6574\u8bca\u65ad\u9608\u503c/\u89c4\u5219\u7684\u6539\u52a8\u90fd\u4f1a\u88ab\u8fd9\u4e2a\u6d4b\u8bd5\u6293\u4f4f.
\u8c03\u6574\u540e\u9700\u4eba\u5de5\u786e\u8ba4\u5e76\u540c\u6b65\u66f4\u65b0\u671f\u671b\u503c.
"""
from __future__ import annotations

from src.services.conversion_diagnoser import (
    diagnose_batch, summarize, top_actions,
)


def _fixture_products():
    """\u5341\u4e2a\u4ea7\u54c1, \u8986\u76d6\u56db\u4e2a\u6f0f\u6597\u9636\u6bb5 + healthy.

    snapshot_id field tracks scenario. \u6240\u6709\u6570\u503c\u51b3\u5b9a\u8d77\u6e90, \u4e0e\u5f53\u524d\u65e5\u671f\u65e0\u5173.
    """
    return [
        # no_imp \u00d7 2
        {'sku': 'NI-1', 'listing_id': '1001', 'categoryId': '11700',
         'impressions': 0, 'views': 0, 'transactions': 0, 'sold_qty': 0,
         'ourPrice': 25.0},
        {'sku': 'NI-2', 'listing_id': '1002', 'categoryId': '11700',
         'impressions': 0, 'views': 0, 'transactions': 0, 'sold_qty': 0,
         'ourPrice': 50.0},
        # low_ctr \u00d7 3 (\u9ad8\u66dd\u5149\u4f4ects)
        {'sku': 'LC-1', 'listing_id': '2001', 'categoryId': '11700',
         'impressions': 5000, 'views': 30, 'transactions': 1, 'sold_qty': 1,
         'ourPrice': 35.0},
        {'sku': 'LC-2', 'listing_id': '2002', 'categoryId': '11700',
         'impressions': 4000, 'views': 20, 'transactions': 0, 'sold_qty': 0,
         'ourPrice': 45.0},
        {'sku': 'LC-3', 'listing_id': '2003', 'categoryId': '20081',
         'impressions': 8000, 'views': 50, 'transactions': 1, 'sold_qty': 2,
         'ourPrice': 60.0},
        # low_cvr \u00d7 2 (CTR \u5065\u5eb7, CVR \u4f4e)
        {'sku': 'CV-1', 'listing_id': '3001', 'categoryId': '11700',
         'impressions': 2000, 'views': 200, 'transactions': 1, 'sold_qty': 1,
         'ourPrice': 80.0},
        {'sku': 'CV-2', 'listing_id': '3002', 'categoryId': '11700',
         'impressions': 1500, 'views': 180, 'transactions': 0, 'sold_qty': 0,
         'ourPrice': 90.0},
        # healthy \u00d7 3
        {'sku': 'HL-1', 'listing_id': '4001', 'categoryId': '11700',
         'impressions': 1000, 'views': 50, 'transactions': 5, 'sold_qty': 6,
         'ourPrice': 30.0},
        {'sku': 'HL-2', 'listing_id': '4002', 'categoryId': '11700',
         'impressions': 800, 'views': 40, 'transactions': 4, 'sold_qty': 5,
         'ourPrice': 40.0},
        {'sku': 'HL-3', 'listing_id': '4003', 'categoryId': '20081',
         'impressions': 1200, 'views': 60, 'transactions': 6, 'sold_qty': 7,
         'ourPrice': 55.0},
    ]


# \u671f\u671b\u503c \u2014\u2014 \u4ee3\u7801\u53d8\u52a8\u540e\u624b\u5de5\u91cd\u65b0\u8bc4\u4f30\u4e26\u66f4\u65b0
EXPECTED_TOTAL = 10
EXPECTED_FUNNEL_BUCKETS = {'no_impression', 'low_ctr', 'low_cvr', 'healthy'}
EXPECTED_NO_IMP = 2
EXPECTED_HEALTHY_MIN = 3
EXPECTED_URGENT_MIN = 2  # \u4f4e\u5206\u8d77\u7801 no_imp + low_ctr
EXPECTED_ACTION_TYPES = {'price_drop', 'image_refresh', 'fill_specifics', 'promote'}


def test_replay_fixture_summary_stable():
    diagnoses = diagnose_batch(_fixture_products())
    s = summarize(diagnoses)
    assert s['total'] == EXPECTED_TOTAL, s
    stages = set(s['by_funnel_stage'].keys())
    assert EXPECTED_FUNNEL_BUCKETS.issubset(stages | {'low_str'}), stages
    assert s['by_funnel_stage'].get('no_impression', 0) == EXPECTED_NO_IMP
    assert s['by_funnel_stage'].get('healthy', 0) >= EXPECTED_HEALTHY_MIN
    assert s['urgent_count'] >= EXPECTED_URGENT_MIN
    assert 0 <= s['avg_cro_score'] <= 100


def test_replay_fixture_action_types_subset():
    diagnoses = diagnose_batch(_fixture_products())
    s = summarize(diagnoses)
    produced = set(s['by_action_type'].keys())
    # \u4ea7\u51fa\u52a8\u4f5c\u5e94\u4e3a\u9884\u671f\u96c6\u5408\u7684\u5b50\u96c6 (\u5141\u8bb8\u672a\u542f\u7528\u67d0\u4e2a\u7c7b\u578b)
    unexpected = produced - EXPECTED_ACTION_TYPES
    assert not unexpected, f"unexpected action types: {unexpected}"


def test_replay_fixture_top_actions_priority_sorted():
    diagnoses = diagnose_batch(_fixture_products())
    rows = top_actions(diagnoses, limit=20)
    assert rows, 'expected at least one P1 candidate'
    priorities = [r['priority'] for r in rows]
    assert priorities == sorted(priorities), priorities
    # \u540c\u4f18\u5148\u7ea7\u5185, \u9ad8\u66dd\u5149\u5728\u524d
    p1 = [r for r in rows if r['priority'] == 1]
    if len(p1) >= 2:
        imps = [r['impressions'] for r in p1]
        assert imps == sorted(imps, reverse=True), imps


def test_replay_fixture_no_imp_have_no_promote_action():
    """no_imp SKU \u4e0d\u5e94\u4ea7\u751f promote \u52a8\u4f5c (\u96f6\u66dd\u5149\u52a0\u51fa\u4ef7\u4e0d\u5408\u7406)."""
    diagnoses = diagnose_batch(_fixture_products())
    for d in diagnoses:
        if d.funnel_stage == 'no_impression':
            action_types = {a.type for a in d.actions}
            assert 'promote' not in action_types, (d.sku, action_types)
