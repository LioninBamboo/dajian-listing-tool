import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from src.services.pricing_engine import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "grove_apply_freight_safe_floor", ROOT / "scripts" / "apply_freight_safe_floor.py"
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)
load_floor_candidates = mod.load_floor_candidates


def test_load_floor_candidates_uses_shipping_column(tmp_path: Path):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            shipping REAL,
            listing_id TEXT,
            cost_breakdown TEXT,
            specs TEXT,
            attributes TEXT
        )
        """
    )
    blind = PricingEngine.calculate_dajian_cost(90.0, 0.0)
    conn.execute(
        """
        INSERT INTO collected_products
        (sku, title, status, shipping, listing_id, cost_breakdown, specs, attributes)
        VALUES (?, ?, 'PUBLISHED', ?, '1', ?, '{}', '{}')
        """,
        ("W2500P479541", "Hanging chair", 36.58, json.dumps(blind)),
    )
    conn.commit()
    conn.close()

    rows = load_floor_candidates(db, sku="W2500P479541")
    assert len(rows) == 1
    assert rows[0]["landed"] == PricingEngine.resolve_landed_cost(
        blind, shipping_fallback=36.58, specs={}, attributes={}
    )
    assert rows[0]["abs_floor"] > 170
    assert rows[0]["safe_floor"] > rows[0]["abs_floor"]
