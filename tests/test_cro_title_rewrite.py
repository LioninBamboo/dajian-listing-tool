"""CRO 标题重写通道测试 — 关键词增强、防抖、候选过滤、dry-run/apply 主流程."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.cro_title_rewrite import (
    build_enriched_title, recently_rewritten_skus, load_candidates, run,
    default_candidate_skus, _snapshot_fallback_skus,
    title_aspect_conflicts, render_email_html, _send_email,
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


def test_enrich_adds_supported_hot_keywords_only():
    title = "Cabinet Door Hinges 110 Degree Stainless Steel 10 Pack"
    aspects = {
        "Features": ["Soft Close"],
        "Material": ["Steel"],
    }
    hot_keywords = ["soft close", "IKEA Style", "waterproof"]
    res = build_enriched_title(title, aspects, hot_keywords=hot_keywords)
    assert res is not None
    assert "Soft Close" in res["new_title"]
    assert "Steel" in res["new_title"]
    assert "IKEA" not in res["new_title"]
    assert "waterproof" not in res["new_title"].lower()
    assert res["added_hot_keywords"] == ["Soft Close"]


def test_enrich_rejects_hot_keyword_without_aspect_support():
    title = "Cabinet Door Hinges 110 Degree Stainless Steel 10 Pack"
    aspects = {"Style": ["Modern"]}
    res = build_enriched_title(title, aspects, hot_keywords=["Soft Close"])
    assert res is not None
    assert "Soft Close" not in res["new_title"]
    assert res["added_hot_keywords"] == []


def test_enrich_skips_low_signal_context_aspects():
    title = "Potting Bench with Hutch"
    aspects = {"Room": ["Entryway"], "Finish": ["Matte"], "Indoor/Outdoor": ["Indoor"]}
    assert build_enriched_title(title, aspects) is None


def test_title_aspect_conflicts_flags_color_mismatch():
    conflicts = title_aspect_conflicts(
        "Black Velvet Sofa Modern Living Room Couch",
        {"Color": ["Blue"], "Material": ["Velvet"]},
    )
    assert conflicts == [{
        "aspect": "Color",
        "expected": "Blue",
        "found": "Black",
    }]


def test_run_skips_candidate_with_title_aspect_conflict(tmp_db, tmp_path):
    conn = sqlite3.connect(str(tmp_db))
    conn.execute(
        "UPDATE collected_products SET optimization=? WHERE sku='PUB1'",
        (json.dumps({
            "title": "Black Race Car Bed Kids Furniture",
            "aspects": {"Color": ["Blue"], "Material": ["Plywood"]},
        }),),
    )
    conn.commit()
    conn.close()

    rep = run(['PUB1'], apply_changes=False, limit=10,
              db_path=tmp_db, logs_dir=tmp_path / 'logs')

    row = next(r for r in rep['rows'] if r['sku'] == 'PUB1')
    assert row['status'] == 'skipped'
    assert row['reason'] == 'title conflicts with SKU aspects'
    assert row['conflicts'][0]['aspect'] == 'Color'


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
            'market_intel': {'top_keywords': ['Twin']},
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
    assert cands[0]['hot_keywords'] == ['Twin']


def test_snapshot_fallback_skus_only_uses_conservative_no_impression_promote(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("""
        CREATE TABLE cro_snapshots (
            snapshot_date TEXT NOT NULL,
            sku TEXT NOT NULL,
            impressions INTEGER DEFAULT 0,
            funnel_stage TEXT,
            cro_score INTEGER DEFAULT 0,
            top_action TEXT,
            actions_json TEXT,
            PRIMARY KEY (snapshot_date, sku)
        )""")
    rows = [
        ('2026-07-10', 'SAFE1', 0, 'no_impression', 20, 'promote',
         json.dumps([{'type': 'promote'}])),
        ('2026-07-10', 'DELIST1', 0, 'no_impression', 20, 'promote',
         json.dumps([{'type': 'promote'}, {'type': 'delist'}])),
        ('2026-07-10', 'LOWCTR1', 80, 'low_ctr', 18, 'title_refresh',
         json.dumps([{'type': 'title_refresh'}])),
        ('2026-07-09', 'OLD1', 0, 'no_impression', 20, 'promote',
         json.dumps([{'type': 'promote'}])),
    ]
    conn.executemany(
        "INSERT INTO cro_snapshots (snapshot_date, sku, impressions, funnel_stage, cro_score, top_action, actions_json) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    assert _snapshot_fallback_skus(limit=10, db_path=tmp_db) == ['SAFE1']


def test_default_candidate_skus_fills_from_snapshot_when_escalation_short(tmp_db, monkeypatch):
    import scripts.cro_title_rewrite as trw

    conn = sqlite3.connect(str(tmp_db))
    conn.execute("""
        CREATE TABLE cro_snapshots (
            snapshot_date TEXT NOT NULL,
            sku TEXT NOT NULL,
            impressions INTEGER DEFAULT 0,
            funnel_stage TEXT,
            cro_score INTEGER DEFAULT 0,
            top_action TEXT,
            actions_json TEXT,
            PRIMARY KEY (snapshot_date, sku)
        )""")
    conn.executemany(
        "INSERT INTO cro_snapshots (snapshot_date, sku, impressions, funnel_stage, cro_score, top_action, actions_json) VALUES (?,?,?,?,?,?,?)",
        [
            ('2026-07-10', 'ESC2', 0, 'no_impression', 20, 'promote',
             json.dumps([{'type': 'promote'}])),
            ('2026-07-10', 'FALL1', 0, 'no_impression', 20, 'promote',
             json.dumps([{'type': 'promote'}])),
            ('2026-07-10', 'FALL2', 0, 'no_impression', 21, 'promote',
             json.dumps([{'type': 'promote'}])),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(trw, '_escalation_skus', lambda days=7: ['ESC1', 'ESC2'])
    skus = default_candidate_skus(limit=4, escalation_days=7, db_path=tmp_db)
    assert skus == ['ESC1', 'ESC2', 'FALL1', 'FALL2']


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


def test_render_email_html_shows_summary_and_rows():
    rep = {
        'input_skus': 50,
        'cooldown_skipped': 2,
        'apply': True,
        'rows': [
            {
                'sku': 'PUB1',
                'status': 'done',
                'old_title': 'Old Title',
                'new_title': 'New Title',
                'added_keywords': ['Blue'],
                'added_hot_keywords': [],
            },
            {
                'sku': 'PUB2',
                'status': 'skipped',
                'reason': 'no safe keywords to add',
            },
        ],
    }
    html = render_email_html(rep)
    assert 'CRO 标题热词优化日报' in html
    assert '候选输入' in html
    assert 'PUB1' in html
    assert 'New Title' in html
    assert 'no safe keywords to add' in html


def test_send_email_uses_attachment_and_summary(monkeypatch, tmp_path):
    captured = {}

    def fake_send_email(subject, html, attachments=None):
        captured['subject'] = subject
        captured['html'] = html
        captured['attachments'] = attachments
        return True

    monkeypatch.setattr('src.utils.email_sender.send_email', fake_send_email)
    rep = {
        'input_skus': 12,
        'apply': False,
        'rows': [
            {'sku': 'PUB1', 'status': 'proposed', 'new_title': 'New', 'added_keywords': ['Blue']},
            {'sku': 'PUB2', 'status': 'failed', 'reason': 'inventory_put_500'},
        ],
    }
    report_path = tmp_path / 'cro_title_rewrite_report.json'
    report_path.write_text('{}', encoding='utf-8')

    _send_email(rep, Path(report_path))

    assert '候选12' in captured['subject']
    assert '建议1' in captured['subject']
    assert '失败1' in captured['subject']
    assert 'CRO 标题热词优化日报' in captured['html']
    assert captured['attachments'] == [str(report_path)]
