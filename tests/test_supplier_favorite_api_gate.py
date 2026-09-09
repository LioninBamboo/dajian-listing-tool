from __future__ import annotations

import json
from pathlib import Path

import scripts.supplier_favorite_api_gate as gate


def test_load_skus_from_file_and_csv(tmp_path: Path):
    p = tmp_path / "skus.txt"
    p.write_text("A1\n# comment\nB2\nA1\n", encoding="utf-8")

    class Args:
        skus = "C3,B2"
        sku_list = p

    assert gate.load_skus(Args) == ["C3", "B2", "A1"]


def test_check_sku_marks_readable(monkeypatch):
    class FakeClient:
        def get_stock_info(self, sku):
            assert sku == "SKU1"
            return {"quantity": 12, "in_stock": True, "price": 10, "shipping_fee": 2}

    row = gate.check_sku("SKU1", FakeClient())
    assert row["api_readable"] is True
    assert row["quantity"] == 12


def test_check_sku_marks_blocked(monkeypatch):
    class FakeClient:
        def get_stock_info(self, sku):
            raise RuntimeError("Region restriction")

    monkeypatch.setattr(
        gate,
        "fetch_live_supplier_quantity",
        lambda sku, dajian_client=None: None,
    )
    row = gate.check_sku("SKU2", FakeClient())
    assert row["api_readable"] is False
    assert "Region" in (row["error"] or "")
