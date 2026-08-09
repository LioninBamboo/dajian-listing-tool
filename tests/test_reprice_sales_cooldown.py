"""Sales cooldown guard for the blanket biweekly reprice (batch_smart_reprice).

A SKU that sold within the cooldown window has proven its current price
converts; the category-average-driven blanket reprice must not disturb it.
These tests pin the pure decision function and the best-effort sales fetch.
"""
from __future__ import annotations

import pytest

from scripts import batch_smart_reprice as reprice


# ── cooldown_decision (pure) ──────────────────────────────────────────────
def test_not_in_cooldown_always_allows():
    assert reprice.cooldown_decision(False, -5.0, "hold") == (True, "")
    assert reprice.cooldown_decision(False, +5.0, "no_downside") == (True, "")


def test_hold_mode_blocks_any_move():
    allow_down, reason_down = reprice.cooldown_decision(True, -5.0, "hold")
    allow_up, reason_up = reprice.cooldown_decision(True, +5.0, "hold")
    assert allow_down is False and reason_down == "recent_sale_hold"
    assert allow_up is False and reason_up == "recent_sale_hold"


def test_no_downside_blocks_drops_but_allows_raises():
    assert reprice.cooldown_decision(True, -0.01, "no_downside") == (
        False, "recent_sale_no_downside"
    )
    # A raise (capture margin on proven demand) is permitted.
    assert reprice.cooldown_decision(True, +5.0, "no_downside") == (True, "")
    # A flat move is not a drop, so it is allowed.
    assert reprice.cooldown_decision(True, 0.0, "no_downside") == (True, "")


def test_unknown_mode_falls_back_to_hold():
    assert reprice.cooldown_decision(True, +5.0, "banana") == (
        False, "recent_sale_hold"
    )


# ── fetch_recently_sold_skus (best-effort) ────────────────────────────────
class _FakeService:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def fetch_sales_data(self, days):
        self.calls.append(days)
        return self._payload


def test_fetch_collects_only_positive_qty_skus():
    svc = _FakeService({"by_sku": {
        "SOLD-1": {"qty": 2},
        "SOLD-2": {"qty": 1},
        "ZERO": {"qty": 0},
        "": {"qty": 5},          # blank SKU ignored
    }})
    skus, meta = reprice.fetch_recently_sold_skus(14, performance_service=svc)
    assert skus == {"SOLD-1", "SOLD-2"}
    assert svc.calls == [14]
    assert meta["available"] is True
    assert meta["sku_count"] == 2
    assert meta["days"] == 14


def test_fetch_disabled_when_days_zero_makes_no_api_call():
    svc = _FakeService({"by_sku": {"SOLD-1": {"qty": 1}}})
    skus, meta = reprice.fetch_recently_sold_skus(0, performance_service=svc)
    assert skus == set()
    assert svc.calls == []            # never touched the API
    assert meta["available"] is False
    assert meta["error"] == "disabled"


def test_fetch_fails_open_on_service_error():
    class _Boom:
        def fetch_sales_data(self, days):
            raise RuntimeError("api down")

    skus, meta = reprice.fetch_recently_sold_skus(14, performance_service=_Boom())
    # Degrade to legacy behaviour (reprice everything), never hold the whole run.
    assert skus == set()
    assert meta["available"] is False
    assert "api down" in str(meta["error"])
