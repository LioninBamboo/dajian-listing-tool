"""S25 — CRO delist 候选清死链 (人工 magic-link 确认).

策略:
  1. 拉队列里 status=pending 且 action=delist 的 SKU (P3 死链候选)
  2. 写入 cro_delist_pending 表 (token + expires_at)
  3. 邮件给运营人, 含 magic-link → server /api/cro/delist/confirm?sku=&token=
  4. 不直接调 eBay; 必须人工点链接确认才下架

magic-link 校验:
  HMAC(secret, sku + expires_iso) == token
  - secret: 环境变量 CRO_DELIST_SECRET, fallback 项目本地 logs/_cro_delist.secret (自动生成)
  - 链接 7 天有效

调度: 每周一 09:00.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_action_queue import load_pending  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'
SECRET_FILE = PROJECT_ROOT / 'logs' / '_cro_delist.secret'
TTL_DAYS = 7

logger = logging.getLogger(__name__)


def _get_secret() -> str:
    s = os.environ.get('CRO_DELIST_SECRET')
    if s:
        return s
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding='utf-8').strip()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    new = secrets.token_hex(32)
    SECRET_FILE.write_text(new, encoding='utf-8')
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return new


def make_token(sku: str, expires_iso: str, secret: Optional[str] = None) -> str:
    sec = (secret or _get_secret()).encode('utf-8')
    msg = f"{sku}|{expires_iso}".encode('utf-8')
    return hmac.new(sec, msg, hashlib.sha256).hexdigest()


def verify_token(sku: str, expires_iso: str, token: str,
                 secret: Optional[str] = None) -> bool:
    expected = make_token(sku, expires_iso, secret=secret)
    if not hmac.compare_digest(expected, token):
        return False
    try:
        exp = datetime.fromisoformat(expires_iso)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)
    except Exception:
        return False


def _ensure_schema(db: Path) -> None:
    with sqlite3.connect(str(db)) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS cro_delist_pending (
                sku TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                confirmed_result TEXT
            )
        """)
        c.commit()


def _save_pending(db: Path, sku: str, token: str, expires_iso: str) -> None:
    with sqlite3.connect(str(db)) as c:
        c.execute(
            "INSERT OR REPLACE INTO cro_delist_pending "
            "(sku, token, expires_at, created_at, confirmed_at, confirmed_result) "
            "VALUES (?,?,?,?,NULL,NULL)",
            (sku, token, expires_iso, datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def lookup_pending(db: Path, sku: str) -> Optional[Tuple[str, str]]:
    """返回 (token, expires_iso) 或 None."""
    with sqlite3.connect(str(db)) as c:
        row = c.execute(
            "SELECT token, expires_at FROM cro_delist_pending WHERE sku = ? "
            "AND confirmed_at IS NULL",
            (sku,),
        ).fetchone()
        return tuple(row) if row else None


def _latest_snapshot_sales_state(db: Path, sku: str) -> dict[str, Any]:
    try:
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            exists = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'cro_snapshots'"
            ).fetchone()
            if not exists:
                return {'has_sales': False, 'snapshot': None}
            row = c.execute(
                """
                SELECT snapshot_date, transactions, sold_qty
                FROM cro_snapshots
                WHERE sku = ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (sku,),
            ).fetchone()
            if not row:
                return {'has_sales': False, 'snapshot': None}
            snapshot = dict(row)
            transactions = int(snapshot.get('transactions') or 0)
            sold_qty = int(snapshot.get('sold_qty') or 0)
            return {
                'has_sales': transactions > 0 or sold_qty > 0,
                'snapshot': snapshot,
            }
    except sqlite3.OperationalError:
        return {'has_sales': False, 'snapshot': None}


def _mark_lifecycle_confirmation(db: Path, sku: str, result: str) -> None:
    try:
        with sqlite3.connect(str(db)) as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'cro_listing_lifecycle_actions'"
            ).fetchone()
            if not exists:
                return
            if result == 'ok':
                status = 'final_delisted'
                error = None
            elif str(result).startswith('skipped:'):
                status = 'skipped'
                error = result.split(':', 1)[1]
            else:
                status = 'failed'
                error = result
            c.execute(
                """
                UPDATE cro_listing_lifecycle_actions
                SET status = ?,
                    finished_at = CASE WHEN ? = 'final_delisted' THEN ? ELSE finished_at END,
                    error = ?,
                    updated_at = ?
                WHERE sku = ?
                  AND (
                    (action_type = 'final_delist' AND status = 'candidate')
                    OR status = 'fallback_delist_pending'
                  )
                """,
                (
                    status,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    error,
                    datetime.now(timezone.utc).isoformat(),
                    sku,
                ),
            )
            c.commit()
    except sqlite3.OperationalError:
        return


def mark_confirmed(db: Path, sku: str, result: str) -> None:
    with sqlite3.connect(str(db)) as c:
        c.execute(
            "UPDATE cro_delist_pending SET confirmed_at = ?, "
            "confirmed_result = ? WHERE sku = ?",
            (datetime.now(timezone.utc).isoformat(), result, sku),
        )
        c.commit()
    _mark_lifecycle_confirmation(db, sku, result)


def _default_ebay_client():
    from dotenv import load_dotenv
    from src.clients.real_ebay_client import create_real_ebay_client

    load_dotenv(PROJECT_ROOT / ".env")
    return create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))


def _mark_queue_done(sku: str) -> None:
    try:
        from src.services.cro_action_queue import mark_done
        mark_done([sku], action='delist')
    except Exception:
        pass


def execute_pending_delist(
    db: Path,
    sku: str,
    token: Optional[str] = None,
    ebay_client: Any = None,
    ebay_client_factory: Optional[Callable[[], Any]] = None,
    mark_queue: bool = True,
) -> dict:
    """Execute one human-confirmed pending delist.

    If ``token`` is supplied, it must match the pending magic-link token. Batch
    UI calls omit the token but still require a valid unexpired pending row.
    """
    db = Path(db)
    sku = str(sku or '').strip()
    if not sku:
        return {'sku': sku, 'ok': False, 'status': 'invalid_sku'}
    _ensure_schema(db)
    rec = lookup_pending(db, sku)
    if not rec:
        return {
            'sku': sku,
            'ok': False,
            'status': 'not_pending',
            'error': f'no pending delist for SKU {sku}',
        }

    db_token, expires_iso = rec
    check_token = token if token is not None else db_token
    if not verify_token(sku, expires_iso, check_token):
        return {
            'sku': sku,
            'ok': False,
            'status': 'invalid_or_expired_token',
            'error': 'invalid or expired token',
        }
    if token is not None and not hmac.compare_digest(db_token, token):
        return {
            'sku': sku,
            'ok': False,
            'status': 'token_mismatch',
            'error': 'token mismatch',
        }

    sales_state = _latest_snapshot_sales_state(db, sku)
    if sales_state.get('has_sales'):
        mark_confirmed(db, sku, 'skipped:sales_present_on_recheck')
        snapshot = sales_state.get('snapshot') or {}
        return {
            'sku': sku,
            'ok': False,
            'status': 'sales_present_on_recheck',
            'error': (
                f"latest CRO snapshot already has sales "
                f"(date={snapshot.get('snapshot_date')}, "
                f"transactions={snapshot.get('transactions')}, "
                f"sold_qty={snapshot.get('sold_qty')})"
            ),
        }

    try:
        client = ebay_client or (
            ebay_client_factory() if ebay_client_factory else _default_ebay_client()
        )
        result = client.delist_sku(sku)
        ok = bool(result.get('success'))
    except Exception as exc:
        mark_confirmed(db, sku, f'error: {exc}')
        return {
            'sku': sku,
            'ok': False,
            'status': 'error',
            'error': str(exc),
        }

    mark_confirmed(db, sku, 'ok' if ok else f"failed: {result.get('error', '')}")
    if ok and mark_queue:
        _mark_queue_done(sku)
    return {
        'sku': sku,
        'ok': ok,
        'status': 'ok' if ok else 'failed',
        'result': result,
        'error': '' if ok else result.get('error', ''),
    }


def batch_confirm_pending_delists(
    skus: list[str],
    db_path: Optional[Path] = None,
    ebay_client: Any = None,
    ebay_client_factory: Optional[Callable[[], Any]] = None,
    mark_queue: bool = True,
    max_count: int = 200,
) -> dict:
    db = Path(db_path) if db_path else DEFAULT_DB
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in skus or []:
        sku = str(raw or '').strip()
        if not sku or sku in seen:
            continue
        seen.add(sku)
        normalized.append(sku)
    if len(normalized) > max_count:
        raise ValueError(f'batch delist limit exceeded: {len(normalized)} > {max_count}')

    client = ebay_client
    if client is None and normalized:
        client = ebay_client_factory() if ebay_client_factory else _default_ebay_client()

    results = [
        execute_pending_delist(
            db,
            sku,
            ebay_client=client,
            mark_queue=mark_queue,
        )
        for sku in normalized
    ]
    ok_count = sum(1 for row in results if row.get('ok'))
    skipped_count = sum(
        1 for row in results
        if row.get('status') in {'not_pending', 'invalid_sku', 'invalid_or_expired_token', 'token_mismatch'}
    )
    return {
        'requested': len(normalized),
        'ok': ok_count,
        'failed': len(results) - ok_count - skipped_count,
        'skipped': skipped_count,
        'results': results,
    }


def _priority_value(priority) -> str:
    text = str(priority or '').strip()
    return text[1:] if text.upper().startswith('P') else text


def _load_lifecycle_delist_candidates(db: Path, limit: int,
                                      exclude_skus: set[str] | None = None) -> list[dict]:
    if limit <= 0:
        return []
    exclude_skus = exclude_skus or set()
    try:
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            exists = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'cro_listing_lifecycle_actions'"
            ).fetchone()
            if not exists:
                return []
            rows = c.execute(
                """
                SELECT id, sku, reason, priority, status, action_type
                FROM cro_listing_lifecycle_actions
                WHERE (
                    action_type = 'final_delist'
                    AND status = 'candidate'
                ) OR status = 'fallback_delist_pending'
                ORDER BY
                    CASE priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2
                                  WHEN 'P3' THEN 3 ELSE 9 END,
                    id
                LIMIT ?
                """,
                (limit + len(exclude_skus),),
            ).fetchall()
    except sqlite3.OperationalError:
        return []

    out = []
    for row in rows:
        sku = str(row['sku'] or '').strip()
        if not sku or sku in exclude_skus:
            continue
        out.append({
            'sku': sku,
            'reason': row['reason'] or row['status'] or '',
            'priority': _priority_value(row['priority']),
            'source': 'lifecycle',
            'lifecycle_action_id': row['id'],
        })
        if len(out) >= limit:
            break
    return out


def build_magic_links(base_url: str, db_path: Optional[Path] = None,
                      limit: int = 50) -> dict:
    db = Path(db_path) if db_path else DEFAULT_DB
    _ensure_schema(db)
    queue_pending = [
        {**action, 'source': action.get('source') or 'queue'}
        for action in load_pending(action_type='delist')
    ][:limit]
    seen_skus = {str(action.get('sku') or '').strip() for action in queue_pending}
    lifecycle_pending = _load_lifecycle_delist_candidates(
        db,
        max(0, limit - len(queue_pending)),
        exclude_skus=seen_skus,
    )
    pending = queue_pending + lifecycle_pending
    rows = []
    expires = (datetime.now(timezone.utc) + timedelta(days=TTL_DAYS))
    expires_iso = expires.isoformat()
    for action in pending:
        sku = action.get('sku')
        if not sku:
            continue
        token = make_token(sku, expires_iso)
        _save_pending(db, sku, token, expires_iso)
        url = (f"{base_url.rstrip('/')}/api/cro/delist/confirm"
               f"?sku={sku}&token={token}")
        rows.append({
            'sku': sku,
            'reason': action.get('reason') or '',
            'priority': action.get('priority'),
            'source': action.get('source') or 'queue',
            'url': url,
        })
    return {
        'pending_total': len(pending),
        'expires_at': expires_iso,
        'rows': rows,
    }


def load_confirmation_rows(base_url: str, db_path: Optional[Path] = None,
                           limit: int = 200) -> list[dict]:
    db = Path(db_path) if db_path else DEFAULT_DB
    _ensure_schema(db)
    try:
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            lifecycle_exists = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'cro_listing_lifecycle_actions'"
            ).fetchone()
            if lifecycle_exists:
                query = """
                    SELECT p.sku, p.token, p.expires_at, p.created_at,
                           l.id AS lifecycle_action_id, l.reason, l.priority,
                           l.status, l.action_type
                    FROM cro_delist_pending p
                    LEFT JOIN cro_listing_lifecycle_actions l
                      ON l.id = (
                        SELECT id
                        FROM cro_listing_lifecycle_actions
                        WHERE sku = p.sku
                        ORDER BY id DESC
                        LIMIT 1
                      )
                    WHERE p.confirmed_at IS NULL
                    ORDER BY p.created_at, p.sku
                    LIMIT ?
                """
            else:
                query = """
                    SELECT p.sku, p.token, p.expires_at, p.created_at,
                           NULL AS lifecycle_action_id, '' AS reason,
                           NULL AS priority, NULL AS status, NULL AS action_type
                    FROM cro_delist_pending p
                    WHERE p.confirmed_at IS NULL
                    ORDER BY p.created_at, p.sku
                    LIMIT ?
                """
            rows = c.execute(query, (max(0, int(limit)),)).fetchall()
    except sqlite3.OperationalError:
        return []

    out = []
    for row in rows:
        sku = str(row['sku'] or '').strip()
        token = str(row['token'] or '').strip()
        expires_at = str(row['expires_at'] or '').strip()
        if not sku or not token or not verify_token(sku, expires_at, token):
            continue
        out.append({
            'sku': sku,
            'reason': row['reason'] or '',
            'priority': _priority_value(row['priority']),
            'status': row['status'] or '',
            'action_type': row['action_type'] or '',
            'source': 'lifecycle' if row['lifecycle_action_id'] else 'queue',
            'lifecycle_action_id': row['lifecycle_action_id'],
            'expires_at': expires_at,
            'url': (
                f"{base_url.rstrip('/')}/api/cro/delist/confirm"
                f"?sku={sku}&token={token}"
            ),
        })
    return out


def render_email_html(rep: dict) -> str:
    parts = ['<h2>🗑️ CRO 死链下架候选 (人工确认)</h2>']
    parts.append(f"<p>共 {rep['pending_total']} 个 SKU; "
                 f"链接 {rep['expires_at']} 过期. 点击即下架.</p>")
    parts.append('<table border=1 cellspacing=0 cellpadding=4>'
                 '<tr><th>SKU</th><th>原因</th><th>优先级</th>'
                 '<th>操作</th></tr>')
    for r in rep['rows']:
        parts.append(
            f"<tr><td>{r['sku']}</td>"
            f"<td>{r.get('reason', '')}</td>"
            f"<td>P{r.get('priority', '?')}</td>"
            f"<td><a href='{r['url']}'>确认下架</a></td></tr>"
        )
    parts.append('</table>')
    return ''.join(parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', default='http://localhost:8000',
                   help='magic-link 前缀 (默认本地)')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--email', action='store_true')
    p.add_argument('--out')
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    rep = build_magic_links(base_url=args.base_url, limit=args.limit)
    print(json.dumps({'pending_total': rep['pending_total'],
                      'expires_at': rep['expires_at']}, indent=2))

    out_path = (Path(args.out) if args.out
                else PROJECT_ROOT / 'logs'
                / f"cro_delist_{datetime.now():%Y%m%d_%H%M%S}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding='utf-8')

    if args.email and rep['pending_total']:
        try:
            from src.utils.email_sender import send_email
            send_email(
                f"🗑️ CRO 待下架候选 {rep['pending_total']} 个 (需人工确认)",
                render_email_html(rep),
            )
        except Exception as e:
            logger.warning(f'email failed: {e}')


if __name__ == '__main__':
    main()
