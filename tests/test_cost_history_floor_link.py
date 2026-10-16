"""P17 — cost_history × floor 联动 tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.services.cost_history import (
    record_cost_snapshot, detect_anomalies, render_alert_html,
)


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'ch.db'


def test_enrich_attaches_new_floor(db):
    today = '2026-04-02'
    yest = '2026-03-26'
    record_cost_snapshot([{'sku': 'X', 'total_cost': 20.0}], db_path=db, date=yest)
    record_cost_snapshot([{'sku': 'X', 'total_cost': 25.0}], db_path=db, date=today)
    out = detect_anomalies(db_path=db, today=today, enrich_with_floor=True,
                            live_price_fn=lambda s: 30.0)
    assert len(out) == 1
    a = out[0]
    assert 'new_floor' in a
    assert a['live_price'] == 30.0
    assert isinstance(a['underwater'], bool)


def test_underwater_flag_when_live_price_below_new_floor(db):
    today = '2026-04-03'
    yest = '2026-03-27'
    record_cost_snapshot([{'sku': 'Y', 'total_cost': 20.0}], db_path=db, date=yest)
    # 大幅涨价 → 新死线高
    record_cost_snapshot([{'sku': 'Y', 'total_cost': 60.0}], db_path=db, date=today)
    out = detect_anomalies(db_path=db, today=today, enrich_with_floor=True,
                            live_price_fn=lambda s: 30.0)
    assert out[0]['underwater'] is True
    assert out[0]['drop_to_safe'] > 0


def test_render_html_includes_floor_columns_when_enriched(db):
    today = '2026-04-04'
    yest = '2026-03-28'
    record_cost_snapshot([{'sku': 'Z', 'total_cost': 10.0}], db_path=db, date=yest)
    record_cost_snapshot([{'sku': 'Z', 'total_cost': 30.0}], db_path=db, date=today)
    out = detect_anomalies(db_path=db, today=today, enrich_with_floor=True,
                            live_price_fn=lambda s: 5.0)
    html = render_alert_html(out)
    assert '新死线' in html
    assert '跌入死线' in html


def test_render_html_no_floor_columns_when_not_enriched(db):
    today = '2026-04-05'
    yest = '2026-03-29'
    record_cost_snapshot([{'sku': 'Q', 'total_cost': 10.0}], db_path=db, date=yest)
    record_cost_snapshot([{'sku': 'Q', 'total_cost': 20.0}], db_path=db, date=today)
    out = detect_anomalies(db_path=db, today=today, enrich_with_floor=False)
    html = render_alert_html(out)
    assert '新死线' not in html
    assert 'Q' in html
