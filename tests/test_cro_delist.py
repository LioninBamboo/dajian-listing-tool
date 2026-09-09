"""S25 — cro_delist magic-link 生成 + token 校验."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_token_round_trip():
    from scripts.cro_delist import make_token, verify_token
    secret = 'unit-test-secret'
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    tok = make_token('SKU1', expires, secret=secret)
    assert verify_token('SKU1', expires, tok, secret=secret) is True


def test_token_rejects_expired():
    from scripts.cro_delist import make_token, verify_token
    secret = 'unit-test-secret'
    expires = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    tok = make_token('SKU1', expires, secret=secret)
    assert verify_token('SKU1', expires, tok, secret=secret) is False


def test_token_rejects_wrong_sku():
    from scripts.cro_delist import make_token, verify_token
    secret = 'unit-test-secret'
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    tok = make_token('SKU1', expires, secret=secret)
    assert verify_token('SKU_OTHER', expires, tok, secret=secret) is False


def test_build_magic_links_writes_pending(tmp_path, monkeypatch):
    from scripts import cro_delist as cd
    monkeypatch.setattr(cd, 'load_pending', lambda action_type: [
        {'sku': 'A1', 'reason': 'no imp 60d', 'priority': 3},
        {'sku': 'A2', 'reason': 'dead', 'priority': 3},
    ])
    db = tmp_path / 'd.db'
    rep = cd.build_magic_links(base_url='http://x', db_path=db, limit=10)
    assert rep['pending_total'] == 2
    assert all(r['url'].startswith('http://x/api/cro/delist/confirm') for r in rep['rows'])

    # both SKUs are now in cro_delist_pending
    rec = cd.lookup_pending(db, 'A1')
    assert rec is not None
    token, expires = rec
    assert cd.verify_token('A1', expires, token) is True


def test_lookup_pending_skips_confirmed(tmp_path, monkeypatch):
    from scripts import cro_delist as cd
    monkeypatch.setattr(cd, 'load_pending', lambda action_type: [
        {'sku': 'X1', 'reason': 'r', 'priority': 3},
    ])
    db = tmp_path / 'd.db'
    cd.build_magic_links(base_url='http://x', db_path=db)
    cd.mark_confirmed(db, 'X1', 'ok')
    assert cd.lookup_pending(db, 'X1') is None


def test_build_magic_links_includes_lifecycle_final_delist_candidates(tmp_path, monkeypatch):
    from scripts import cro_delist as cd
    from src.services.cro_relist_lifecycle import ensure_schema

    monkeypatch.setattr(cd, 'load_pending', lambda action_type: [])
    db = tmp_path / 'd.db'
    ensure_schema(db)
    cd._ensure_schema(db)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cro_snapshots (
                snapshot_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                listing_id TEXT,
                transactions INTEGER DEFAULT 0,
                sold_qty INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES (?, 'final_delist', 'candidate', 'P3', ?, 1, ?, 'test')
            """,
            ('OLD-DEAD', 'old_dead_link age_days=73', '2026-05-13T00:00:00+00:00'),
        )
        conn.commit()

    rep = cd.build_magic_links(base_url='http://x', db_path=db, limit=10)

    assert rep['pending_total'] == 1
    assert rep['rows'][0]['sku'] == 'OLD-DEAD'
    assert rep['rows'][0]['source'] == 'lifecycle'
    assert cd.lookup_pending(db, 'OLD-DEAD') is not None


def test_build_magic_links_excludes_fallback_relist_candidates(tmp_path, monkeypatch):
    """A relist fallback is not evidence that a live listing is safe to end."""
    from scripts import cro_delist as cd
    from src.services.cro_relist_lifecycle import ensure_schema

    monkeypatch.setattr(cd, 'load_pending', lambda action_type: [])
    db = tmp_path / 'd.db'
    ensure_schema(db)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES ('RELIST-FALLBACK', 'revive_relist', 'fallback_delist_pending', 'P3',
                    'relist fallback', 1, '2026-05-13T00:00:00+00:00', 'test')
            """
        )
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES ('FINAL-DEAD', 'final_delist', 'candidate', 'P3',
                    'old_dead_link', 1, '2026-05-13T00:00:00+00:00', 'test')
            """
        )
        conn.commit()

    report = cd.build_magic_links(base_url='http://x', db_path=db, limit=10)

    assert [row['sku'] for row in report['rows']] == ['FINAL-DEAD']


def test_load_confirmation_rows_returns_unconfirmed_lifecycle_links(tmp_path, monkeypatch):
    from scripts import cro_delist as cd
    from src.services.cro_relist_lifecycle import ensure_schema

    monkeypatch.setattr(cd, 'load_pending', lambda action_type: [])
    db = tmp_path / 'd.db'
    ensure_schema(db)
    cd._ensure_schema(db)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES (?, 'final_delist', 'candidate', 'P3', ?, 1, ?, 'test')
            """,
            ('UI-DEAD', 'old_dead_link age_days=90', '2026-05-13T00:00:00+00:00'),
        )
        conn.commit()
    cd.build_magic_links(base_url='http://x', db_path=db, limit=10)

    rows = cd.load_confirmation_rows(base_url='http://x', db_path=db, limit=10)

    assert len(rows) == 1
    assert rows[0]['sku'] == 'UI-DEAD'
    assert rows[0]['priority'] == '3'
    assert rows[0]['status'] == 'candidate'
    assert rows[0]['url'].startswith('http://x/api/cro/delist/confirm')


def test_mark_confirmed_updates_lifecycle_status(tmp_path):
    from scripts import cro_delist as cd
    from src.services.cro_relist_lifecycle import ensure_schema

    db = tmp_path / 'd.db'
    ensure_schema(db)
    cd._ensure_schema(db)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES ('DONE-DEAD', 'final_delist', 'candidate', 'P3',
                    'old_dead_link', 1, '2026-05-13T00:00:00+00:00', 'test')
            """
        )
        conn.execute(
            """
            INSERT INTO cro_delist_pending
            (sku, token, expires_at, created_at)
            VALUES ('DONE-DEAD', 'tok', '2099-01-01T00:00:00+00:00',
                    '2026-05-13T00:00:00+00:00')
            """
        )
        conn.commit()

    cd.mark_confirmed(db, 'DONE-DEAD', 'ok')

    with __import__('sqlite3').connect(str(db)) as conn:
        status = conn.execute(
            "SELECT status FROM cro_listing_lifecycle_actions WHERE sku='DONE-DEAD'"
        ).fetchone()[0]
    assert status == 'final_delisted'


def test_execute_pending_delist_marks_success_once(tmp_path, monkeypatch):
    from scripts import cro_delist as cd
    from src.services.cro_relist_lifecycle import ensure_schema

    class FakeEbayClient:
        def __init__(self):
            self.calls = []

        def delist_sku(self, sku):
            self.calls.append(sku)
            return {'success': True, 'sku': sku}

    db = tmp_path / 'd.db'
    ensure_schema(db)
    cd._ensure_schema(db)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    token = cd.make_token('BATCH-1', expires)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES ('BATCH-1', 'final_delist', 'candidate', 'P3',
                    'old_dead_link', 1, '2026-05-13T00:00:00+00:00', 'test')
            """
        )
        conn.execute(
            """
            INSERT INTO cro_delist_pending
            (sku, token, expires_at, created_at)
            VALUES ('BATCH-1', ?, ?, '2026-05-13T00:00:00+00:00')
            """,
            (token, expires),
        )
        conn.commit()

    fake = FakeEbayClient()
    res = cd.execute_pending_delist(
        db,
        'BATCH-1',
        ebay_client=fake,
        mark_queue=False,
        source_checker=lambda sku: {'verified': True, 'sku_available': False},
    )

    assert res['ok'] is True
    assert fake.calls == ['BATCH-1']
    assert cd.lookup_pending(db, 'BATCH-1') is None
    with __import__('sqlite3').connect(str(db)) as conn:
        lifecycle_status, confirmed_result = conn.execute(
            """
            SELECT l.status, p.confirmed_result
            FROM cro_listing_lifecycle_actions l
            JOIN cro_delist_pending p ON p.sku = l.sku
            WHERE l.sku='BATCH-1'
            """
        ).fetchone()
    assert lifecycle_status == 'final_delisted'
    assert confirmed_result == 'ok'


def test_execute_pending_delist_rejects_expired_pending(tmp_path):
    from scripts import cro_delist as cd

    db = tmp_path / 'd.db'
    cd._ensure_schema(db)
    expires = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    token = cd.make_token('EXPIRED-1', expires)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_delist_pending
            (sku, token, expires_at, created_at)
            VALUES ('EXPIRED-1', ?, ?, '2026-05-13T00:00:00+00:00')
            """,
            (token, expires),
        )
        conn.commit()

    res = cd.execute_pending_delist(db, 'EXPIRED-1', mark_queue=False)

    assert res['ok'] is False
    assert res['status'] == 'invalid_or_expired_token'
    assert cd.lookup_pending(db, 'EXPIRED-1') is not None


def test_execute_pending_delist_skips_when_latest_snapshot_has_sales(tmp_path):
    from scripts import cro_delist as cd
    from src.services.cro_relist_lifecycle import ensure_schema

    class FakeEbayClient:
        def __init__(self):
            self.calls = []

        def delist_sku(self, sku):
            self.calls.append(sku)
            return {'success': True, 'sku': sku}

    db = tmp_path / 'd.db'
    ensure_schema(db)
    cd._ensure_schema(db)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    token = cd.make_token('SOLD-1', expires)
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cro_snapshots (
                snapshot_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                listing_id TEXT,
                transactions INTEGER DEFAULT 0,
                sold_qty INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES ('SOLD-1', 'final_delist', 'candidate', 'P3',
                    'old_dead_link', 1, '2026-05-13T00:00:00+00:00', 'test')
            """
        )
        conn.execute(
            """
            INSERT INTO cro_delist_pending
            (sku, token, expires_at, created_at)
            VALUES ('SOLD-1', ?, ?, '2026-05-13T00:00:00+00:00')
            """,
            (token, expires),
        )
        conn.execute(
            """
            INSERT INTO cro_snapshots
            (snapshot_date, sku, listing_id, transactions, sold_qty)
            VALUES ('2026-05-14', 'SOLD-1', 'L-1', 1, 1)
            """
        )
        conn.commit()

    fake = FakeEbayClient()
    res = cd.execute_pending_delist(db, 'SOLD-1', ebay_client=fake, mark_queue=False)

    assert res['ok'] is False
    assert res['status'] == 'sales_present_on_recheck'
    assert fake.calls == []
    assert cd.lookup_pending(db, 'SOLD-1') is None
    with __import__('sqlite3').connect(str(db)) as conn:
        lifecycle_status, confirmed_result = conn.execute(
            """
            SELECT l.status, p.confirmed_result
            FROM cro_listing_lifecycle_actions l
            JOIN cro_delist_pending p ON p.sku = l.sku
            WHERE l.sku='SOLD-1'
            """
        ).fetchone()
    assert lifecycle_status == 'skipped'
    assert confirmed_result == 'skipped:sales_present_on_recheck'


def test_execute_pending_delist_skips_when_giga_source_is_still_available(tmp_path):
    from scripts import cro_delist as cd

    class FakeEbayClient:
        def __init__(self):
            self.calls = []

        def delist_sku(self, sku):
            self.calls.append(sku)
            return {'success': True}

    db = tmp_path / 'd.db'
    cd._ensure_schema(db)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """INSERT INTO cro_delist_pending (sku, token, expires_at, created_at)
               VALUES ('SOURCE-LIVE', ?, ?, ?)""",
            (cd.make_token('SOURCE-LIVE', expires), expires, expires),
        )
        conn.commit()

    fake = FakeEbayClient()
    result = cd.execute_pending_delist(
        db,
        'SOURCE-LIVE',
        ebay_client=fake,
        mark_queue=False,
        source_checker=lambda sku: {'verified': True, 'sku_available': True},
    )

    assert result['status'] == 'source_still_available'
    assert fake.calls == []
    assert cd.lookup_pending(db, 'SOURCE-LIVE') is None


def test_execute_pending_delist_fails_closed_when_giga_recheck_is_unavailable(tmp_path):
    from scripts import cro_delist as cd

    class FakeEbayClient:
        def delist_sku(self, sku):
            raise AssertionError('eBay must not be called when source recheck failed')

    db = tmp_path / 'd.db'
    cd._ensure_schema(db)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with __import__('sqlite3').connect(str(db)) as conn:
        conn.execute(
            """INSERT INTO cro_delist_pending (sku, token, expires_at, created_at)
               VALUES ('SOURCE-UNKNOWN', ?, ?, ?)""",
            (cd.make_token('SOURCE-UNKNOWN', expires), expires, expires),
        )
        conn.commit()

    result = cd.execute_pending_delist(
        db,
        'SOURCE-UNKNOWN',
        ebay_client=FakeEbayClient(),
        mark_queue=False,
        source_checker=lambda sku: {'verified': False, 'reason': 'upstream timeout'},
    )

    assert result['status'] == 'source_recheck_unavailable'
    assert cd.lookup_pending(db, 'SOURCE-UNKNOWN') is not None


def test_default_giga_recheck_fails_closed_when_availability_field_is_missing(monkeypatch):
    from scripts import cro_delist as cd
    from src.clients import dajian_client

    class FakeDajian:
        def __init__(self, *args, **kwargs):
            pass

        def get_product_detail_by_sku(self, sku):
            return {'sku': sku, 'productName': 'Ambiguous source response'}

    monkeypatch.setenv('DAJIAN_API_KEY', 'test-key')
    monkeypatch.setenv('DAJIAN_API_SECRET', 'test-secret')
    monkeypatch.setattr(dajian_client, 'DaJianClient', FakeDajian)
    # Keep the real .env out of os.environ: the checker's load_dotenv would
    # otherwise leak keys (e.g. ENABLE_SCHEDULED_TITLE_REWRITE_APPLY) into
    # later tests in the same process.
    monkeypatch.setattr('dotenv.load_dotenv', lambda *args, **kwargs: None)

    state = cd._default_source_checker('AMBIGUOUS-1')

    assert state['verified'] is False
    assert 'skuAvailable' in state['reason']


def test_default_ebay_client_uses_configured_environment(monkeypatch):
    # Patch BEFORE importing: real_ebay_client -> ebay_auth runs a module-level
    # load_dotenv() on first import, which would leak real .env keys (e.g.
    # ENABLE_SCHEDULED_TITLE_REWRITE_APPLY) into later tests in this process.
    monkeypatch.setattr('dotenv.load_dotenv', lambda *args, **kwargs: None)

    from scripts import cro_delist as cd
    from src.clients import real_ebay_client

    seen = {}

    def fake_create_real_ebay_client(environment):
        seen['environment'] = environment
        return object()

    monkeypatch.setenv('EBAY_ENVIRONMENT', 'PRODUCTION')
    monkeypatch.setattr(
        real_ebay_client,
        'create_real_ebay_client',
        fake_create_real_ebay_client,
    )

    cd._default_ebay_client()

    assert seen['environment'] == 'PRODUCTION'


def test_batch_confirm_pending_delists_dedupes_and_counts(tmp_path):
    from scripts import cro_delist as cd

    class FakeEbayClient:
        def __init__(self):
            self.calls = []

        def delist_sku(self, sku):
            self.calls.append(sku)
            return {'success': sku != 'FAIL-1', 'error': 'blocked'}

    db = tmp_path / 'd.db'
    cd._ensure_schema(db)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with __import__('sqlite3').connect(str(db)) as conn:
        for sku in ['OK-1', 'FAIL-1']:
            conn.execute(
                """
                INSERT INTO cro_delist_pending
                (sku, token, expires_at, created_at)
                VALUES (?, ?, ?, '2026-05-13T00:00:00+00:00')
                """,
                (sku, cd.make_token(sku, expires), expires),
            )
        conn.commit()

    fake = FakeEbayClient()
    rep = cd.batch_confirm_pending_delists(
        ['OK-1', 'OK-1', 'MISSING-1', 'FAIL-1'],
        db_path=db,
        ebay_client=fake,
        mark_queue=False,
        source_checker=lambda sku: {'verified': True, 'sku_available': False},
    )

    assert fake.calls == ['OK-1', 'FAIL-1']
    assert rep['requested'] == 3
    assert rep['ok'] == 1
    assert rep['failed'] == 1
    assert rep['skipped'] == 1
    assert [r['status'] for r in rep['results']] == [
        'ok', 'not_pending', 'failed'
    ]
