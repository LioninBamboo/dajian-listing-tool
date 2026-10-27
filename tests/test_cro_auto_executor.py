from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_queue(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.jsonl"
    import src.services.cro_action_queue as queue_mod
    monkeypatch.setattr(queue_mod, 'DEFAULT_QUEUE', queue_path)
    return queue_path


def test_safe_auto_actions_excludes_promote_by_default():
    from src.services.cro_auto_executor import SAFE_AUTO_ACTIONS

    assert 'promote' not in SAFE_AUTO_ACTIONS
    assert set(SAFE_AUTO_ACTIONS) == {
        'price_drop', 'image_refresh', 'fill_specifics',
    }


def test_auto_enqueue_and_execute_only_handles_safe_actions(tmp_queue, monkeypatch):
    import src.services.cro_auto_executor as auto_mod
    monkeypatch.setattr(auto_mod, 'queue_stats', lambda: {'pending': 0})

    calls = []

    def fake_executor(*, apply_changes, limit, send_email):
        calls.append((apply_changes, limit, send_email))
        return {
            'pending_total': 1,
            'done': ['A'],
            'failed': [],
            'skipped': [],
            'marked_done': 1,
        }

    rep = auto_mod.auto_enqueue_and_execute(
        [
            {'sku': 'A', 'action': 'price_drop', 'priority': 1},
            {'sku': 'B', 'action': 'delist', 'priority': 1},
            {'sku': 'C', 'action': 'promote', 'priority': 1},
            {'sku': 'D', 'action': 'price_drop', 'priority': 2},
        ],
        action_types=('price_drop', 'delist', 'promote'),
        executor_map={'price_drop': fake_executor},
        send_email=False,
    )
    assert rep['selected_count'] == 1
    assert rep['enqueue']['added'] == 1
    assert rep['execution']['unsupported'] == ['delist', 'promote']
    assert calls == [(True, 200, False)]


def test_auto_enqueue_and_execute_skips_recently_handled(tmp_queue, monkeypatch):
    import src.services.cro_auto_executor as auto_mod
    monkeypatch.setattr(auto_mod, 'queue_stats', lambda: {'pending': 0})
    monkeypatch.setattr(
        auto_mod, 'recent_terminal_keys', lambda hours=24: {('A', 'price_drop')}
    )

    calls = []

    def fake_executor(*, apply_changes, limit, send_email):
        calls.append((apply_changes, limit, send_email))
        return {'pending_total': 0, 'done': [], 'failed': [], 'skipped': []}

    rep = auto_mod.auto_enqueue_and_execute(
        [{'sku': 'A', 'action': 'price_drop', 'priority': 1}],
        action_types=('price_drop',),
        executor_map={'price_drop': fake_executor},
    )
    assert rep['selected_count'] == 0
    assert rep['enqueue']['added'] == 0
    assert rep['enqueue']['skipped_recently_handled'] == 1
    assert calls == [(True, 200, False)]


def test_queued_promote_flag_off_does_not_call_ad_api(tmp_queue, monkeypatch):
    """CRO queue auto-execute must not hit the ad API when promote is queued."""
    import src.services.cro_auto_executor as auto_mod
    from src.services.cro_action_queue import enqueue, load_pending

    enqueue(
        [{
            'sku': 'AD-1',
            'action': 'promote',
            'priority': 1,
            'cohort': 'treatment',
        }],
        queue_path=tmp_queue,
        source='test',
    )
    assert load_pending(action_type='promote', queue_path=tmp_queue)

    monkeypatch.setattr(auto_mod, 'queue_stats', lambda: {'pending': 1})
    from src.utils.store_profile import StoreProfile
    monkeypatch.setattr(
        'src.utils.store_profile.get_store_profile',
        lambda: StoreProfile(brand_name='AquaVerve', scheduler_profile='full'),
    )

    def boom():
        raise AssertionError('promote runner must not load')

    monkeypatch.setattr(auto_mod, '_load_promote_runner', boom)
    monkeypatch.setattr(
        auto_mod,
        'SAFE_AUTO_ACTIONS',
        ('price_drop', 'image_refresh', 'fill_specifics', 'promote'),
    )

    ad_calls = []

    def fake_promote(*, apply_changes, limit, send_email):
        ad_calls.append(('create_ad_safe', apply_changes, limit))
        return {'pending_total': 1, 'done': ['AD-1'], 'failed': [], 'skipped': []}

    def fake_price(*, apply_changes, limit, send_email):
        return {'results': [], 'total_products': 0}

    rep = auto_mod.execute_pending_actions(
        action_types=('promote', 'price_drop'),
        executor_map={'promote': fake_promote, 'price_drop': fake_price},
        send_email=False,
    )

    assert ad_calls == []
    assert rep['blocked_by_flag'] == ['promote']
    runner = auto_mod._default_executor('promote')
    skipped = runner(apply_changes=True, limit=10, send_email=False)
    assert skipped['ad_api_called'] is False
    assert skipped['done'] == []


def test_aqua_queued_promote_still_runs_without_cro_promote_key(tmp_queue, monkeypatch):
    """AquaRides keeps the pre-PR promote path when the key is absent."""
    import src.services.cro_auto_executor as auto_mod
    from src.utils.store_profile import StoreProfile

    aqua = StoreProfile(
        brand_name='AquaRides',
        scheduler_profile='ops',
        scheduler_enable_ads=True,
    )
    assert aqua.scheduler_enable_cro_promote is False
    assert aqua.scheduled_ad_create_blocked is False
    monkeypatch.setattr(auto_mod, 'queue_stats', lambda: {'pending': 1})
    monkeypatch.setattr('src.utils.store_profile.get_store_profile', lambda: aqua)
    monkeypatch.setattr(
        auto_mod,
        'SAFE_AUTO_ACTIONS',
        ('price_drop', 'image_refresh', 'fill_specifics', 'promote'),
    )
    ad_calls = []

    def fake_promote(*, apply_changes, limit, send_email):
        ad_calls.append(apply_changes)
        return {'pending_total': 1, 'done': ['AD-1'], 'failed': [], 'skipped': []}

    rep = auto_mod.execute_pending_actions(
        action_types=('promote',),
        executor_map={'promote': fake_promote},
        send_email=False,
    )
    assert ad_calls == [True]
    assert rep['blocked_by_flag'] == []


def test_write_auto_execution_report(tmp_path):
    from src.services.cro_auto_executor import write_auto_execution_report
    path = write_auto_execution_report({'ok': True}, report_dir=Path(tmp_path))
    assert path.exists()
    assert path.read_text(encoding='utf-8').strip().startswith('{')