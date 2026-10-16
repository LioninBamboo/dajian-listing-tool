"""CRO 持久化 / 队列 / daily runner 联动测试."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.conversion_diagnoser import diagnose_batch
from src.services.cro_snapshot_store import (
    save_snapshots, load_snapshot, diff_snapshots, ensure_schema,
)
from src.services.cro_action_queue import (
    enqueue, enqueue_unique_pending, load_pending, mark_done,
    queue_stats, recent_terminal_keys,
)
from src.services.cro_daily_runner import run_cro_daily


def _prod(sku, imp=2000, views=10, tx=0, sold=0, price=120, cat='X'):
    return {
        'sku': sku, 'listing_id': f'L_{sku}',
        'impressions': imp, 'views': views,
        'transactions': tx, 'sold_qty': sold,
        'selling_price': price, 'age_days': 30,
        'title': 'x' * 70, 'images': ['u'],
        'categoryId': cat,
    }


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_cro.db"
    import src.services.cro_snapshot_store as s
    monkeypatch.setattr(s, 'DEFAULT_DB', db)
    return db


@pytest.fixture
def tmp_queue(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'DEFAULT_QUEUE', q)
    return q


def test_save_and_load_snapshot(tmp_db):
    diags = diagnose_batch([_prod('A', price=130)], market_data={'X': {'median': 100}})
    n = save_snapshots(diags, snapshot_date='2026-05-04')
    assert n == 1
    rows = load_snapshot('2026-05-04')
    assert rows[0]['sku'] == 'A'
    assert rows[0]['top_action'] in ('price_drop', 'image_refresh', 'title_refresh', 'fill_specifics', None)


def test_diff_snapshots_detects_improvement(tmp_db):
    bad = diagnose_batch([_prod('A', imp=2000, views=5, price=130)], market_data={'X': {'median': 100}})
    good = diagnose_batch([_prod('A', imp=2000, views=80, tx=5, sold=5, price=100)], market_data={'X': {'median': 100}})
    save_snapshots(bad, snapshot_date='2026-05-03')
    save_snapshots(good, snapshot_date='2026-05-04')
    delta = diff_snapshots('2026-05-04', '2026-05-03')
    assert 'A' in delta['improved']
    assert delta['today_total'] == 1
    assert delta['yesterday_total'] == 1


def test_enqueue_load_mark_done(tmp_queue):
    actions = [{'sku': 'A', 'action': 'price_drop', 'priority': 1, 'reason': 'r'}]
    assert enqueue(actions) == 1
    pend = load_pending(action_type='price_drop')
    assert len(pend) == 1 and pend[0]['sku'] == 'A'
    n = mark_done(['A'], action='price_drop')
    assert n == 1
    assert load_pending(action_type='price_drop') == []
    s = queue_stats()
    assert s['total'] == 1 and s['done'] == 1 and s['pending'] == 0


def test_enqueue_filter_by_action_type(tmp_queue):
    enqueue([
        {'sku': 'A', 'action': 'price_drop', 'priority': 1},
        {'sku': 'B', 'action': 'title_refresh', 'priority': 2},
    ])
    assert len(load_pending(action_type='price_drop')) == 1
    assert len(load_pending()) == 2


def test_enqueue_unique_pending_skips_duplicates_and_stats_by_action(tmp_queue):
    enqueue([{'sku': 'A', 'action': 'price_drop', 'priority': 1}])
    res = enqueue_unique_pending([
        {'sku': 'A', 'action': 'price_drop', 'priority': 1},
        {'sku': 'B', 'action': 'promote', 'priority': 1, 'cohort': 'treatment'},
    ])
    assert res == {'added': 1, 'skipped_duplicate': 1, 'input_count': 2}
    stats = queue_stats()
    assert stats['pending'] == 2
    assert stats['pending_executable'] == 2
    assert stats['pending_by_action'] == {'price_drop': 1, 'promote': 1}
    assert stats['pending_executable_by_action'] == {'price_drop': 1, 'promote': 1}


def test_queue_stats_breaks_done_down_by_action(tmp_queue):
    enqueue([
        {'sku': 'A', 'action': 'price_drop', 'priority': 1},
        {'sku': 'B', 'action': 'promote', 'priority': 1},
    ])
    mark_done(['A'], action='price_drop')
    stats = queue_stats()
    assert stats['done_by_action'] == {'price_drop': 1}
    assert stats['pending_by_action'] == {'promote': 1}


def test_queue_stats_separates_control_pending(tmp_queue):
    enqueue([
        {'sku': 'A', 'action': 'promote', 'priority': 1, 'cohort': 'treatment'},
        {'sku': 'B', 'action': 'promote', 'priority': 1, 'cohort': 'control'},
    ])
    stats = queue_stats()
    assert stats['pending'] == 2
    assert stats['pending_executable'] == 1
    assert stats['pending_control'] == 1
    assert stats['pending_executable_by_action'] == {'promote': 1}


def test_recent_terminal_keys_returns_recent_done_or_skipped(tmp_queue):
    enqueue([
        {'sku': 'A', 'action': 'promote', 'priority': 1},
        {'sku': 'B', 'action': 'price_drop', 'priority': 1},
    ])
    mark_done(['A'], action='promote', result='skipped')
    mark_done(['B'], action='price_drop', result='done')
    keys = recent_terminal_keys(hours=24)
    assert ('A', 'promote') in keys
    assert ('B', 'price_drop') in keys


def test_run_cro_daily_writes_report_and_queue(tmp_db, tmp_queue, tmp_path, monkeypatch):
    products = [
        _prod('A', imp=2000, views=5, price=130),   # low_ctr + overpriced → price_drop P1
        _prod('B', imp=0, views=0, price=100),       # no_imp
    ]
    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        report_dir=tmp_path / 'reports',
    )
    assert rep['snapshots_saved'] == 2
    assert rep['summary']['total'] == 2
    assert rep['p1_queued'] >= 1
    assert Path(rep['report_path']).exists()
    pend = load_pending(action_type='price_drop')
    assert any(p['sku'] == 'A' for p in pend)


def test_run_cro_daily_does_not_duplicate_existing_pending(tmp_db, tmp_queue, tmp_path):
    products = [_prod('A', imp=2000, views=5, price=130)]
    rep1 = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        report_dir=tmp_path / 'reports',
    )
    rep2 = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        report_dir=tmp_path / 'reports',
    )
    assert rep1['p1_queued'] == 1
    assert rep2['p1_queued'] == 0
    pend = load_pending(action_type='price_drop')
    assert len(pend) == 1


def test_run_cro_daily_skips_recently_handled_terminal_actions(tmp_db, tmp_queue, tmp_path):
    products = [_prod('A', imp=2000, views=5, price=130)]
    run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        report_dir=tmp_path / 'reports',
    )
    mark_done(['A'], action='price_drop', result='skipped')
    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        report_dir=tmp_path / 'reports',
    )
    assert rep['p1_queued'] == 0
    assert rep['p1_recently_handled_skipped'] == 1
    assert load_pending(action_type='price_drop') == []


def _write_terminal_row(queue_path, sku, action, days_ago):
    from datetime import datetime, timedelta, timezone
    done_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    queue_path.write_text(json.dumps({
        'sku': sku, 'action': action, 'status': 'skipped',
        'done_at': done_at, 'cohort': 'treatment',
    }) + '\n', encoding='utf-8')


def test_structural_skip_suppressed_beyond_24h(tmp_db, tmp_queue, tmp_path, monkeypatch):
    # fill_specifics 终态 skip 在 14d 冷却窗内不重复入队 (即使已超 24h), 消除空转
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'assign_cohort', lambda sku, action: 'treatment')
    _write_terminal_row(tmp_queue, 'SPEC', 'fill_specifics', days_ago=3)

    products = [_prod('SPEC', imp=2000, views=100, price=100)]
    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        enqueue_action_types=('fill_specifics',),
        enqueue_max_priority=2,
        report_dir=tmp_path / 'reports',
    )
    assert rep['p1_queued'] == 0
    assert rep['p1_recently_handled_skipped'] == 1
    assert load_pending(action_type='fill_specifics') == []


def test_actionable_skip_reenqueues_after_24h(tmp_db, tmp_queue, tmp_path, monkeypatch):
    # 对照: price_drop 非结构性动作, 终态 skip 超 24h 后仍可重新入队
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'assign_cohort', lambda sku, action: 'treatment')
    _write_terminal_row(tmp_queue, 'A', 'price_drop', days_ago=3)

    products = [_prod('A', imp=2000, views=5, price=130)]
    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        enqueue_action_types=('price_drop',),
        report_dir=tmp_path / 'reports',
    )
    assert rep['p1_queued'] == 1
    assert rep['p1_recently_handled_skipped'] == 0


def test_run_cro_diagnose_skips_when_traffic_degraded(monkeypatch):
    # 流量数据 api_ok=False (断网/Analytics 失败) → 跳过诊断, 不落污染快照
    import daily_tasks

    monkeypatch.setattr(
        daily_tasks, "logger",
        type("L", (), {"info": lambda *a, **k: None,
                       "warning": lambda *a, **k: None,
                       "error": lambda *a, **k: None})(),
    )

    class FakeCompetitionMonitor:
        @staticmethod
        def load_products_from_db():
            return [{'sku': 'SKU1', 'image_url': 'img'}]

        @staticmethod
        def load_performance_data(force_refresh=False):
            return {'traffic_meta': {'api_ok': False}}

        @staticmethod
        def merge_performance_into_products(products, perf):
            raise AssertionError('降级时不应进入 merge/诊断')

        @staticmethod
        def get_market_data_from_report(path):
            return {}

    monkeypatch.setitem(
        __import__('sys').modules,
        'src.web.pages.competition_monitor', FakeCompetitionMonitor)

    rep = daily_tasks.run_cro_diagnose(enqueue_p1=True)
    assert rep['status'] == 'degraded_skipped'
    assert rep['summary']['total'] == 0


def test_run_cro_daily_default_stays_p1_only(tmp_db, tmp_queue, tmp_path):
    # low_ctr + 价格对齐 + 标题充足 → image_refresh P2; 默认 max_priority=1 不入队
    products = [_prod('A', imp=2000, views=5, price=100)]
    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        enqueue_action_types=('image_refresh',),
        report_dir=tmp_path / 'reports',
    )
    assert rep['p1_queued'] == 0
    assert load_pending(action_type='image_refresh') == []


def test_run_cro_daily_max_priority_2_queues_image_and_specifics(tmp_db, tmp_queue, tmp_path, monkeypatch):
    # image_refresh 是 A/B 动作, 周哈希可能分 control 被 load_pending 隐藏; 钉死 cohort
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'assign_cohort', lambda sku, action: 'treatment')
    products = [
        # low_ctr + aligned + title 70 chars → image_refresh P2
        _prod('IMG', imp=2000, views=5, price=100),
        # healthy ctr (100/2000=5%) + cvr 0 → low_cvr + aligned → fill_specifics P2
        _prod('SPEC', imp=2000, views=100, price=100),
    ]
    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        enqueue_action_types=('image_refresh', 'fill_specifics'),
        enqueue_max_priority=2,
        report_dir=tmp_path / 'reports',
    )
    assert rep['p1_queued'] == 2
    assert rep['queued_by_action'] == {'image_refresh': 1, 'fill_specifics': 1}
    img = load_pending(action_type='image_refresh')
    spec = load_pending(action_type='fill_specifics')
    assert [p['sku'] for p in img] == ['IMG']
    assert [p['sku'] for p in spec] == ['SPEC']


def test_run_cro_daily_respects_per_action_activity_quotas(
        tmp_db, tmp_queue, tmp_path, monkeypatch):
    import src.services.cro_action_queue as q_mod
    monkeypatch.setattr(q_mod, 'assign_cohort', lambda sku, action: 'treatment')
    products = []
    for i in range(3):
        products.append(_prod(f'PRICE{i}', imp=2000, views=5, price=130))
        products.append(_prod(f'IMAGE{i}', imp=2000, views=5, price=100))
        products.append(_prod(f'SPEC{i}', imp=2000, views=100, price=100))
        products.append(_prod(f'PROMO{i}', imp=0, views=0, price=100))

    rep = run_cro_daily(
        products,
        market_data={'X': {'median': 100}},
        enqueue_action_types=('price_drop', 'image_refresh',
                              'fill_specifics', 'promote', 'send_offer'),
        enqueue_max_priority=2,
        action_quotas={
            'price_drop': 1,
            'image_refresh': 1,
            'fill_specifics': 2,
            'promote': 1,
            'send_offer': 1,
        },
        report_dir=tmp_path / 'reports',
    )

    assert rep['p1_queued'] == 6
    assert rep['queued_by_action'] == {
        'price_drop': 1,
        'image_refresh': 1,
        'fill_specifics': 2,
        'promote': 1,
        'send_offer': 1,
    }
    assert rep['action_quotas']['total'] == 6
    stats = queue_stats()
    assert stats['pending_by_action'] == rep['queued_by_action']


def test_daily_tasks_passes_50_slot_activity_quota(monkeypatch):
    import daily_tasks

    captured = {}

    monkeypatch.setattr(
        daily_tasks,
        "logger",
        type("L", (), {
            "info": lambda *a, **k: None,
            "warning": lambda *a, **k: None,
            "error": lambda *a, **k: None,
        })(),
    )

    def fake_run_cro_daily(products, **kwargs):
        captured.update(kwargs)
        return {
            'summary': {'total': len(products), 'avg_cro_score': 80,
                        'healthy_count': 1},
            'delta_vs_yesterday': {'improved': [], 'worsened': []},
            'p1_queued': 0,
            'queued_by_action': {},
        }

    class FakeCompetitionMonitor:
        @staticmethod
        def load_products_from_db():
            return [{'sku': 'SKU1', 'image_url': 'img'}]

        @staticmethod
        def load_performance_data(force_refresh=False):
            # api_ok=True: 通过 run_cro_diagnose 的流量数据质量守门
            return {'traffic_meta': {'api_ok': True}}

        @staticmethod
        def merge_performance_into_products(products, perf):
            return None

        @staticmethod
        def get_market_data_from_report(path):
            return {}

    monkeypatch.setitem(
        __import__('sys').modules,
        'src.web.pages.competition_monitor',
        FakeCompetitionMonitor,
    )
    import src.services.cro_daily_runner as runner
    monkeypatch.setattr(runner, 'run_cro_daily', fake_run_cro_daily)

    rep = daily_tasks.run_cro_diagnose(enqueue_p1=True)

    assert rep['summary']['total'] == 1
    assert captured['action_quotas'] == {
        'price_drop': 10,
        'image_refresh': 5,
        'fill_specifics': 15,
        'promote': 10,
        'send_offer': 10,
    }
    assert sum(captured['action_quotas'].values()) == 50


def test_run_cro_daily_never_queues_p3_or_p4(tmp_db, tmp_queue, tmp_path):
    # underpriced + 有销售 → 反向提价 price_drop P4; 即使 max_priority 放宽也不得入队
    p4 = _prod('P4', imp=2000, views=100, tx=3, sold=3, price=70)
    # 60+ 天零展示零销售 → delist P3
    p3 = _prod('P3', imp=0, views=0, price=100)
    p3['age_days'] = 90
    rep = run_cro_daily(
        [p4, p3],
        market_data={'X': {'median': 100}},
        enqueue_action_types=('price_drop', 'delist'),
        enqueue_max_priority=9,
        report_dir=tmp_path / 'reports',
    )
    assert load_pending(action_type='delist') == []
    assert all(a['priority'] <= 2 for a in load_pending())
    assert rep['queued_by_action'].get('delist') is None


def test_diff_handles_missing_yesterday(tmp_db):
    diags = diagnose_batch([_prod('A')], market_data={'X': {'median': 100}})
    save_snapshots(diags, snapshot_date='2026-05-04')
    delta = diff_snapshots('2026-05-04', '2026-05-03')
    assert delta['yesterday_total'] == 0
    assert delta['today_total'] == 1
