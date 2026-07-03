"""CRO 标题重写通道测试 — 关键词增强、防抖、候选过滤、dry-run/apply 主流程."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from scripts.cro_title_rewrite import (
    build_enriched_title, recently_rewritten_skus, load_candidates, run,
)


# ── build_enriched_title ─────────────────────────────────────

def test_enrich_adds_missing_whitelisted_keywords():
    title = "Race Car Bed Kids Furniture 250 LBS Capacity"
    aspects = {
        "Color": ["Blue"],
        "Material": ["Plywood"],
        "Compatible Mattress Size": ["Twin"],
        "MPN": ["Does Not Apply"],       # 非白名单键, 不得进标题
        "Item Length": ["98.7 in"],      # 尺寸键, 不得进标题
    }
    res = build_enriched_title(title, aspects)
    assert res is not None
    assert "Blue" in res["added_keywords"]
    assert "Plywood" in res["added_keywords"]
    assert "Twin" in res["added_keywords"]
    assert "98.7 in" not in res["new_title"]
    assert "Does Not Apply" not in res["new_title"]
    assert len(res["new_title"]) <= 80
    assert res["new_title"].startswith(title)


def test_enrich_skips_keywords_already_in_title():
    title = "Blue Velvet Sofa Modern Living Room Couch"
    aspects = {"Color": ["Blue"], "Material": ["Velvet"], "Room": ["Living Room"]}
    assert build_enriched_title(title, aspects) is None


def test_enrich_word_boundary_no_substring_false_positive():
    # 标题含 Blueprint, 但 Color=Blue 仍应被视为缺失
    title = "Blueprint Pattern Area Rug Soft"
    res = build_enriched_title(title, {"Color": ["Blue"]})
    assert res is not None
    assert res["added_keywords"] == ["Blue"]


def test_enrich_respects_80_char_budget():
    title = "X" * 78
    res = build_enriched_title(title, {"Color": ["Emerald Green"]})
    assert res is None  # 加不进去就不硬塞


def test_enrich_skips_meaningless_values():
    title = "Wooden Bookshelf 5 Tier"
    aspects = {"Color": ["Does Not Apply"], "Style": ["N/A"], "Finish": [""]}
    assert build_enriched_title(title, aspects) is None


def test_enrich_empty_title_returns_none():
    assert build_enriched_title("", {"Color": ["Blue"]}) is None


# ── 防抖 (recently_rewritten_skus) ───────────────────────────

def _write_rewrite_log(logs_dir, dt, rows):
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"cro_title_rewrite_{dt:%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({'rows': rows}), encoding='utf-8')
    return path


def test_recently_rewritten_only_counts_done_and_recent(tmp_path):
    logs = tmp_path / 'logs'
    now = datetime.now()
    _write_rewrite_log(logs, now, [
        {'sku': 'DONE1', 'status': 'done'},
        {'sku': 'PROP1', 'status': 'proposed'},
        {'sku': 'FAIL1', 'status': 'failed'},
    ])
    _write_rewrite_log(logs, now - timedelta(days=60), [
        {'sku': 'OLD1', 'status': 'done'},
    ])
    assert recently_rewritten_skus(days=30, logs_dir=logs) == {'DONE1'}


def test_recently_rewritten_missing_dir_empty(tmp_path):
    assert recently_rewritten_skus(days=30, logs_dir=tmp_path / 'nope') == set()


# ── 候选加载 ─────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / 'test.db'
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE collected_products (
        sku TEXT PRIMARY KEY, title TEXT, optimization TEXT,
        listing_id TEXT, status TEXT, logs TEXT, updated_at TEXT)""")
    rows = [
        ('PUB1', 'Race Car Bed Kids', json.dumps({
            'title': 'Race Car Bed Kids Furniture',
            'aspects': {'Color': ['Blue'], 'Material': ['Plywood']},
        }), 'L1', 'PUBLISHED'),
        ('PEND1', 'Pending item', None, '', 'PENDING'),
        ('NOLIST', 'Published no listing', None, '', 'PUBLISHED'),
    ]
    for sku, title, opt, lid, status in rows:
        conn.execute(
            "INSERT INTO collected_products (sku,title,optimization,listing_id,status) VALUES (?,?,?,?,?)",
            (sku, title, opt, lid, status))
    conn.commit()
    conn.close()
    return db


def test_load_candidates_filters_published_with_listing(tmp_db):
    cands = load_candidates(['PUB1', 'PEND1', 'NOLIST', 'GHOST'], db_path=tmp_db)
    assert [c['sku'] for c in cands] == ['PUB1']
    assert cands[0]['listing_id'] == 'L1'
    assert cands[0]['title'] == 'Race Car Bed Kids Furniture'  # optimization 优先
    assert cands[0]['aspects'] == {'Color': ['Blue'], 'Material': ['Plywood']}


# ── run() 主流程 ─────────────────────────────────────────────

def test_run_dry_run_proposes_without_network(tmp_db, tmp_path):
    rep = run(['PUB1', 'PEND1'], apply_changes=False, limit=10,
              db_path=tmp_db, logs_dir=tmp_path / 'logs')
    assert rep['proposed'] == ['PUB1']
    assert rep['done'] == [] and rep['failed'] == []
    prop = next(r for r in rep['rows'] if r['sku'] == 'PUB1')
    assert prop['status'] == 'proposed'
    assert 'Blue' in prop['added_keywords']
    skipped = next(r for r in rep['rows'] if r['sku'] == 'PEND1')
    assert skipped['status'] == 'skipped'


def test_run_respects_rewrite_cooldown(tmp_db, tmp_path):
    logs = tmp_path / 'logs'
    _write_rewrite_log(logs, datetime.now(), [{'sku': 'PUB1', 'status': 'done'}])
    rep = run(['PUB1'], apply_changes=False, limit=10,
              db_path=tmp_db, logs_dir=logs)
    assert rep['cooldown_skipped'] == 1
    assert rep['proposed'] == []


def test_run_apply_uses_injected_oauth_and_updates_db(tmp_db, tmp_path, monkeypatch):
    import scripts.cro_title_rewrite as trw
    calls = {}

    def fake_revise(oauth, sku, listing_id, new_title):
        calls['args'] = (sku, listing_id, new_title)
        return {'ok': True, 'reason': 'inventory_api', 'title': new_title}

    monkeypatch.setattr(trw, 'revise_title_live', fake_revise)
    monkeypatch.setattr(trw.time, 'sleep', lambda s: None)
    rep = run(['PUB1'], apply_changes=True, limit=10,
              db_path=tmp_db, logs_dir=tmp_path / 'logs', oauth=object())
    assert rep['done'] == ['PUB1']
    sku, lid, new_title = calls['args']
    assert (sku, lid) == ('PUB1', 'L1')
    assert 'Blue' in new_title and len(new_title) <= 80
    # 本地 DB 已同步
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute(
        "SELECT title, optimization FROM collected_products WHERE sku='PUB1'"
    ).fetchone()
    conn.close()
    assert row[0] == new_title
    assert json.loads(row[1])['title'] == new_title


def test_run_apply_failure_does_not_touch_db(tmp_db, tmp_path, monkeypatch):
    import scripts.cro_title_rewrite as trw
    monkeypatch.setattr(trw, 'revise_title_live',
                        lambda *a, **k: {'ok': False, 'reason': 'inventory_put_500'})
    monkeypatch.setattr(trw.time, 'sleep', lambda s: None)
    rep = run(['PUB1'], apply_changes=True, limit=10,
              db_path=tmp_db, logs_dir=tmp_path / 'logs', oauth=object())
    assert rep['failed'] == ['PUB1']
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute(
        "SELECT title FROM collected_products WHERE sku='PUB1'").fetchone()
    conn.close()
    assert row[0] == 'Race Car Bed Kids'  # 原值未动
