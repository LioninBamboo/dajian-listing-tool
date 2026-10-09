"""死链协调: 完整性闸 + READY 清 stale listing_id + 缺货豁免。"""
from __future__ import annotations

import sqlite3

import pytest

import src.services.listing_status_sync as lss


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # 避免重试 backoff 真的 sleep 拖慢测试
    monkeypatch.setattr(lss.time, "sleep", lambda *a, **k: None)


def _make_db(tmp_path, rows):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, status TEXT, listing_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO collected_products (sku, status, listing_id) VALUES (?,?,?)", rows
    )
    conn.commit()
    conn.close()
    return db


def _row(db, sku):
    conn = sqlite3.connect(str(db))
    r = conn.execute(
        "SELECT status, listing_id FROM collected_products WHERE sku=?", (sku,)
    ).fetchone()
    conn.close()
    return r


@pytest.fixture
def patched(tmp_path, monkeypatch):
    monkeypatch.setattr(lss, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(lss, "SYNC_CACHE_FILE", tmp_path / "logs" / "_cache.json")
    monkeypatch.setattr(lss, "_get_trading_client", lambda: object())
    return tmp_path


# ── sync_listing_status: 协调逻辑 ──

def test_ready_with_dead_listing_id_cleared_others_untouched(patched, monkeypatch):
    db = _make_db(patched, [
        ("DEAD", "READY", "111"),          # 不在 active_ids → 清 listing_id
        ("LIVE", "READY", "222"),          # 在 active_ids → 保留
        ("OOS", "READY", "333"),           # 缺货但 OOS 控制仍 active → 保留
        ("FRESH", "READY", None),          # 本就没 listing_id → 不动
        ("PUB_DEAD", "PUBLISHED", "999"),  # PUBLISHED 死链 → ENDED (原有行为)
    ])
    monkeypatch.setattr(
        lss, "fetch_all_active_listing_ids",
        lambda trading=None: ({"222", "333"}, 2, True),
    )

    res = lss.sync_listing_status(force_refresh=True)

    assert not res.get("skipped_incomplete")
    assert set(res["stale_ready_cleared"]) == {"DEAD"}
    assert res["stale_ended"] == ["PUB_DEAD"]
    assert _row(db, "DEAD") == ("READY", None)      # 清掉 listing_id, 状态仍 READY 可重发
    assert _row(db, "LIVE") == ("READY", "222")     # 不动
    assert _row(db, "OOS") == ("READY", "333")      # 缺货豁免
    assert _row(db, "FRESH") == ("READY", None)
    assert _row(db, "PUB_DEAD")[0] == "ENDED"


def test_incomplete_fetch_skips_all_reconciliation(patched, monkeypatch):
    db = _make_db(patched, [
        ("DEAD", "READY", "111"),
        ("PUB", "PUBLISHED", "999"),
    ])
    # fetch_complete=False → 残缺快照, 必须整批跳过, 不动任何数据
    monkeypatch.setattr(
        lss, "fetch_all_active_listing_ids",
        lambda trading=None: (set(), 50, False),
    )

    res = lss.sync_listing_status(force_refresh=True)

    assert res.get("skipped_incomplete") is True
    assert res["stale_ready_cleared"] == []
    assert res["stale_ended"] == []
    assert _row(db, "DEAD") == ("READY", "111")     # 未被误清
    assert _row(db, "PUB") == ("PUBLISHED", "999")  # 未被误判 ENDED


def test_fetched_fewer_than_api_total_skips(patched, monkeypatch):
    db = _make_db(patched, [("DEAD", "READY", "111")])
    # fetch_complete=True 但抓到数(1) < api_total(100) → 仍判残缺, 跳过
    monkeypatch.setattr(
        lss, "fetch_all_active_listing_ids",
        lambda trading=None: ({"222"}, 100, True),
    )

    res = lss.sync_listing_status(force_refresh=True)

    assert res.get("skipped_incomplete") is True
    assert _row(db, "DEAD") == ("READY", "111")


# ── fetch_all_active_listing_ids: 完整性标志 ──

class _FakeTrading:
    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def call(self, verb, payload):
        idx = self.calls
        self.calls += 1
        return self._pages[idx] if idx < len(self._pages) else None


def _active_xml(item_ids, total_entries, total_pages):
    items = "".join(f"<Item><ItemID>{i}</ItemID></Item>" for i in item_ids)
    return (
        '<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<ActiveList><PaginationResult>'
        f"<TotalNumberOfEntries>{total_entries}</TotalNumberOfEntries>"
        f"<TotalNumberOfPages>{total_pages}</TotalNumberOfPages>"
        "</PaginationResult>"
        f"<ItemArray>{items}</ItemArray>"
        "</ActiveList></GetMyeBaySellingResponse>"
    )


def test_fetch_complete_single_page():
    trading = _FakeTrading([_active_xml(["1", "2"], 2, 1)])
    ids, total, complete = lss.fetch_all_active_listing_ids(trading, max_pages=20)
    assert ids == {"1", "2"}
    assert total == 2
    assert complete is True


def test_fetch_incomplete_when_a_page_fails():
    # 首页声称有 2 页, 但第 2 页取不到 (None) → 残缺
    trading = _FakeTrading([_active_xml(["1"], 4, 2)])
    _ids, _total, complete = lss.fetch_all_active_listing_ids(trading, max_pages=20)
    assert complete is False


def test_fetch_incomplete_when_hits_max_pages():
    # 每页都说共 100 页, max_pages=3 → 撞上限, 残缺
    pages = [_active_xml([str(i)], 1000, 100) for i in range(5)]
    trading = _FakeTrading(pages)
    _ids, _total, complete = lss.fetch_all_active_listing_ids(trading, max_pages=3)
    assert complete is False
