"""send_offer 执行器 + 保本定价 + 经济数据双源测试."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.services.cro_offer_pricing import (
    compute_offer, floor_price, MIN_NET_MARGIN, MAX_DISCOUNT_PCT,
)
from src.services.cro_sku_economics import load_sku_economics
from src.web.pages.competition_monitor import calc_net_margin


# ── 保本定价 ─────────────────────────────────────────────────

def test_floor_price_guarantees_min_margin():
    for cost in (50.0, 224.74, 1000.0):
        floor = floor_price(cost)
        # 地板价上的净利率必须 ≥ MIN_NET_MARGIN (同一费率模型交叉验证)
        assert calc_net_margin(floor, cost) >= MIN_NET_MARGIN - 1e-6
        # 地板价往下 2% 就应跌破最低净利率 (地板确实是下界附近)
        assert calc_net_margin(floor * 0.98, cost) < MIN_NET_MARGIN


def test_compute_offer_normal_discount():
    # 高毛利: 现价远高于地板 → 5% 折扣原样通过
    res = compute_offer(price=400.0, cost=200.0, discount_pct=5.0)
    assert res['safe'] is True
    assert res['offer_price'] == pytest.approx(380.0)
    assert res['discount_pct'] == pytest.approx(5.0)
    assert res['offer_price'] >= res['floor']


def test_compute_offer_clamps_to_floor():
    cost = 200.0
    floor = floor_price(cost)
    price = round(floor * 1.04, 2)  # 现价只比地板高 4%
    res = compute_offer(price=price, cost=cost, discount_pct=10.0)
    assert res['safe'] is True
    assert res['offer_price'] == pytest.approx(floor)      # 被抬回地板
    assert res['discount_pct'] < 10.0
    assert calc_net_margin(res['offer_price'], cost) >= MIN_NET_MARGIN - 1e-6


def test_compute_offer_no_room_is_unsafe():
    cost = 200.0
    floor = floor_price(cost)
    res = compute_offer(price=round(floor * 0.99, 2), cost=cost)
    assert res['safe'] is False
    assert 'no discount room' in res['reason']


def test_compute_offer_discount_capped():
    res = compute_offer(price=1000.0, cost=100.0, discount_pct=50.0)
    assert res['safe'] is True
    assert res['discount_pct'] == pytest.approx(MAX_DISCOUNT_PCT)
    assert res['offer_price'] == pytest.approx(900.0)


def test_compute_offer_missing_economics_unsafe():
    assert compute_offer(price=0, cost=100)['safe'] is False
    assert compute_offer(price=100, cost=0)['safe'] is False


def test_compute_offer_tiny_discount_not_worth_sending():
    cost = 200.0
    floor = floor_price(cost)
    price = round(floor * 1.01, 2)  # 只有 ~1% 让利空间
    res = compute_offer(price=price, cost=cost, discount_pct=5.0)
    assert res['safe'] is False
    assert 'too small' in res['reason']


# ── 经济数据双源 ─────────────────────────────────────────────

@pytest.fixture
def eco_db(tmp_path):
    db = tmp_path / 'eco.db'
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE collected_products "
                  "(sku TEXT PRIMARY KEY, suggested_price REAL, "
                  "cost_breakdown TEXT, listing_id TEXT, status TEXT)")
        c.execute("INSERT INTO collected_products VALUES (?,?,?,?,?)",
                  ('PROD1', 353.1,
                   json.dumps({'total_dajian_cost': 224.74}), 'L1', 'PUBLISHED'))
        c.execute("INSERT INTO collected_products VALUES (?,?,?,?,?)",
                  ('NOCOST', 100.0, None, 'L2', 'PUBLISHED'))
        c.commit()
    return db


def test_economics_from_collected_products(eco_db):
    eco = load_sku_economics('PROD1', db_path=eco_db)
    assert eco == {'price': 353.1, 'cost': 224.74}
    assert load_sku_economics('NOCOST', db_path=eco_db) is None
    assert load_sku_economics('GHOST', db_path=eco_db) is None


def test_economics_products_table_precedence(eco_db):
    with sqlite3.connect(str(eco_db)) as c:
        c.execute("CREATE TABLE products "
                  "(sku TEXT PRIMARY KEY, ourPrice REAL, total_cost REAL)")
        c.execute("INSERT INTO products VALUES ('PROD1', 399.0, 250.0)")
        c.commit()
    eco = load_sku_economics('PROD1', db_path=eco_db)
    assert eco == {'price': 399.0, 'cost': 250.0}


# ── 执行器 ───────────────────────────────────────────────────

@pytest.fixture
def tmp_queue(tmp_path, monkeypatch):
    q = tmp_path / 'queue.jsonl'
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'DEFAULT_QUEUE', q)
    monkeypatch.setattr(q_mod, 'assign_cohort', lambda sku, action: 'treatment')
    return q


def _enqueue_offer(sku, listing_id='L1'):
    from src.services.cro_action_queue import enqueue
    enqueue([{'sku': sku, 'listing_id': listing_id, 'action': 'send_offer',
              'priority': 2, 'detail': {'suggested_discount_pct': 5.0}}])


def test_run_dry_run_computes_offer_without_network(tmp_queue, eco_db, tmp_path):
    import scripts.cro_send_offer as so
    _enqueue_offer('PROD1')
    rep = so.run(apply_changes=False, limit=10,
                 db_path=eco_db, logs_dir=tmp_path / 'logs')
    row = rep['rows'][0]
    assert row['status'] == 'dry_run'
    assert row['offer_price'] == pytest.approx(353.1 * 0.95, abs=0.01)
    assert rep['done'] == []


def test_run_apply_sends_to_eligible_and_marks_done(tmp_queue, eco_db, tmp_path, monkeypatch):
    import scripts.cro_send_offer as so
    from src.services.cro_action_queue import load_pending
    _enqueue_offer('PROD1', listing_id='L1')
    sent = {}
    monkeypatch.setattr(so, 'fetch_eligible_listing_ids', lambda oauth: {'L1'})
    monkeypatch.setattr(so, 'send_offer_live',
                        lambda oauth, lid, price, message=so.OFFER_MESSAGE:
                        sent.update({'lid': lid, 'price': price}) or {'ok': True, 'reason': 'sent'})
    monkeypatch.setattr(so.time, 'sleep', lambda s: None)
    rep = so.run(apply_changes=True, limit=10, db_path=eco_db,
                 logs_dir=tmp_path / 'logs', oauth=object())
    assert rep['done'] == ['PROD1']
    assert sent['lid'] == 'L1'
    assert sent['price'] >= so.compute_offer(353.1, 224.74)['floor']
    assert load_pending(action_type='send_offer') == []   # marked done


def test_run_apply_skips_not_eligible_non_terminal(tmp_queue, eco_db, tmp_path, monkeypatch):
    import scripts.cro_send_offer as so
    from src.services.cro_action_queue import load_pending
    _enqueue_offer('PROD1', listing_id='L1')
    monkeypatch.setattr(so, 'fetch_eligible_listing_ids', lambda oauth: set())
    rep = so.run(apply_changes=True, limit=10, db_path=eco_db,
                 logs_dir=tmp_path / 'logs', oauth=object())
    assert rep['skipped'] == ['PROD1']
    # 非终态: 仍 pending, 等 interested buyers 出现
    assert len(load_pending(action_type='send_offer')) == 1


def test_run_apply_guard_blocks_unprofitable_terminal(tmp_queue, eco_db, tmp_path, monkeypatch):
    import scripts.cro_send_offer as so
    from src.services.cro_action_queue import load_pending
    # NOCOST 无成本数据 → 保本闸终态 skip
    _enqueue_offer('NOCOST', listing_id='L2')
    monkeypatch.setattr(so, 'fetch_eligible_listing_ids', lambda oauth: {'L2'})
    monkeypatch.setattr(so, 'send_offer_live',
                        lambda *a, **k: pytest.fail('must not send without economics'))
    rep = so.run(apply_changes=True, limit=10, db_path=eco_db,
                 logs_dir=tmp_path / 'logs', oauth=object())
    assert rep['skipped'] == ['NOCOST']
    assert rep['rows'][0]['terminal'] is True
    assert load_pending(action_type='send_offer') == []   # marked skipped


def test_run_respects_offer_cooldown(tmp_queue, eco_db, tmp_path, monkeypatch):
    import scripts.cro_send_offer as so
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / f"cro_send_offer_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
        json.dumps({'rows': [{'sku': 'PROD1', 'status': 'done'}]}),
        encoding='utf-8')
    _enqueue_offer('PROD1')
    rep = so.run(apply_changes=False, limit=10, db_path=eco_db, logs_dir=logs)
    assert rep['rows'][0]['reason'].startswith('offer cooldown')


def test_diagnoser_low_cvr_aligned_recommends_send_offer():
    from src.services.conversion_diagnoser import diagnose_sku
    d = diagnose_sku({
        'sku': 'A', 'listing_id': '1', 'impressions': 2000, 'views': 100,
        'transactions': 0, 'sold_qty': 0, 'selling_price': 100,
        'age_days': 20, 'title': 'x' * 70, 'images': ['u'],
    }, market_median=100)
    assert d.funnel_stage == 'low_cvr'
    offers = [a for a in d.actions if a.type == 'send_offer']
    assert len(offers) == 1
    assert offers[0].priority == 2
    assert offers[0].detail['suggested_discount_pct'] == 5.0
