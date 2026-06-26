"""S43 \u2014 \u4ef7\u683c\u53cd\u54fa tests."""
from __future__ import annotations

import json
from pathlib import Path

from src.services.cro_pricing_feedback import (
    DEFAULT_MARGIN, ELASTIC_MARGIN, INELASTIC_MARGIN, learn_elasticity,
    recommended_target_margin,
)


def _write(qp: Path, rows):
    qp.parent.mkdir(parents=True, exist_ok=True)
    with qp.open('w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_elastic_category_when_lift_strong(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    rows = [
        {'category': 'BBQ', 'action': 'price_drop', 'status': 'done',
         'detail': {'sold_lift_pct': 0.30}}
        for _ in range(6)
    ]
    _write(qp, rows)
    learned = learn_elasticity(qp)
    assert learned['BBQ']['verdict'] == 'elastic'
    assert recommended_target_margin('BBQ', learned) == ELASTIC_MARGIN


def test_inelastic_when_lift_flat(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    rows = [
        {'category': 'Tools', 'action': 'price_drop', 'status': 'done',
         'detail': {'sold_lift_pct': 0.01}}
        for _ in range(6)
    ]
    _write(qp, rows)
    learned = learn_elasticity(qp)
    assert learned['Tools']['verdict'] == 'inelastic'
    assert recommended_target_margin('Tools', learned) == INELASTIC_MARGIN


def test_unknown_category_returns_default(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    qp.write_text('', encoding='utf-8')
    assert recommended_target_margin('NEW', queue_path=qp) == DEFAULT_MARGIN


def test_low_sample_returns_unknown(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    rows = [
        {'category': 'X', 'action': 'price_drop', 'status': 'done',
         'detail': {'sold_lift_pct': 0.40}}
    ]
    _write(qp, rows)
    learned = learn_elasticity(qp)
    assert learned['X']['verdict'] == 'unknown'


def test_rollback_counted_as_inelastic_signal(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    rows = [
        {'category': 'PromoBad', 'action': 'promote', 'status': 'done',
         'detail': {'rolled_back': True}}
        for _ in range(5)
    ]
    _write(qp, rows)
    learned = learn_elasticity(qp)
    assert learned['PromoBad']['verdict'] == 'inelastic'


def test_missing_queue_file_yields_empty(tmp_path: Path):
    learned = learn_elasticity(tmp_path / 'never.jsonl')
    assert learned == {}
