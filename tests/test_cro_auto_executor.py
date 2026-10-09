from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_queue(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.jsonl"
    import src.services.cro_action_queue as queue_mod
    monkeypatch.setattr(queue_mod, 'DEFAULT_QUEUE', queue_path)
    return queue_path


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
            {'sku': 'A', 'action': 'promote', 'priority': 1},
            {'sku': 'B', 'action': 'delist', 'priority': 1},
            {'sku': 'C', 'action': 'title_refresh', 'priority': 1},
            {'sku': 'D', 'action': 'price_drop', 'priority': 2},
        ],
        action_types=('promote', 'delist', 'title_refresh'),
        executor_map={'promote': fake_executor},
        send_email=False,
    )
    assert rep['selected_count'] == 1
    assert rep['enqueue']['added'] == 1
    assert rep['execution']['unsupported'] == ['delist', 'title_refresh']
    assert calls == [(True, 200, False)]


def test_auto_enqueue_and_execute_skips_recently_handled(tmp_queue, monkeypatch):
    import src.services.cro_auto_executor as auto_mod
    monkeypatch.setattr(auto_mod, 'queue_stats', lambda: {'pending': 0})
    monkeypatch.setattr(auto_mod, 'recent_terminal_keys', lambda hours=24: {('A', 'promote')})

    calls = []

    def fake_executor(*, apply_changes, limit, send_email):
        calls.append((apply_changes, limit, send_email))
        return {'pending_total': 0, 'done': [], 'failed': [], 'skipped': []}

    rep = auto_mod.auto_enqueue_and_execute(
        [{'sku': 'A', 'action': 'promote', 'priority': 1}],
        action_types=('promote',),
        executor_map={'promote': fake_executor},
    )
    assert rep['selected_count'] == 0
    assert rep['enqueue']['added'] == 0
    assert rep['enqueue']['skipped_recently_handled'] == 1
    assert calls == [(True, 200, False)]


def test_write_auto_execution_report(tmp_path):
    from src.services.cro_auto_executor import write_auto_execution_report
    path = write_auto_execution_report({'ok': True}, report_dir=Path(tmp_path))
    assert path.exists()
    assert path.read_text(encoding='utf-8').strip().startswith('{')