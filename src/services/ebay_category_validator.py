"""
Taxonomy leaf check for eBay category IDs.

validate() never treats a network failure as a publishable category.
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(days=7)
TREE_MARKETPLACES = {
    "0": "EBAY_US",
    "100": "EBAY_MOTORS_US",
}


def default_cache_db_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "ebay_collection.db")


class CategoryValidator:
    def __init__(
        self,
        session=None,
        token_provider: Optional[Callable[[], str]] = None,
        db_path: Optional[str] = None,
        api_base: str = "https://api.ebay.com",
        clock: Optional[Callable[[], datetime]] = None,
        write_cache: bool = True,
    ):
        self.session = session
        self.token_provider = token_provider
        self.db_path = db_path or default_cache_db_path()
        self.api_base = api_base.rstrip("/")
        self._clock = clock or datetime.utcnow
        self.write_cache = write_cache
        self._app_token: Optional[str] = None
        self._app_token_expiry: Optional[datetime] = None
        if write_cache:
            self._ensure_schema()

    def validate(self, category_id, tree_id="0") -> dict:
        """
        Returns {valid, leaf, name, reason}.

        reason is one of: leaf, non_leaf, not_in_tree, unknown.
        unknown is never valid.
        """
        cid = str(category_id or "").strip()
        tid = str(tree_id or "0").strip()
        if not cid:
            return {"valid": False, "leaf": False, "name": "", "reason": "unknown"}

        tree_version = self._tree_version(tid)
        if self.write_cache and tree_version:
            self._invalidate_if_version_changed(tid, tree_version)
            cached = self._read_cache(tid, cid, tree_version)
            if cached is not None:
                return cached

        try:
            result = self._fetch(cid, tid)
        except Exception as exc:
            logger.warning("[CAT_VALIDATE] network/parse failure for %s tree %s: %s", cid, tid, exc)
            return {"valid": False, "leaf": False, "name": "", "reason": "unknown"}

        if self.write_cache and result["reason"] != "unknown":
            self._write_cache(tid, cid, result, tree_version)
        return result

    def _fetch(self, category_id: str, tree_id: str) -> dict:
        session = self._session()
        token = self._token()
        url = (
            f"{self.api_base}/commerce/taxonomy/v1/category_tree/"
            f"{tree_id}/get_category_subtree"
        )
        response = session.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"category_id": category_id},
            timeout=20,
        )
        status = getattr(response, "status_code", None)
        if status == 200:
            return _result_from_subtree(response.json())
        if status == 400 and _error_id(response) == 62005:
            return {"valid": False, "leaf": False, "name": "", "reason": "not_in_tree"}
        raise RuntimeError(f"taxonomy subtree HTTP {status}")

    def _tree_version(self, tree_id: str) -> Optional[str]:
        marketplace = TREE_MARKETPLACES.get(str(tree_id), "EBAY_US")
        try:
            session = self._session()
            token = self._token()
            response = session.get(
                f"{self.api_base}/commerce/taxonomy/v1/get_default_category_tree_id",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"marketplace_id": marketplace},
                timeout=20,
            )
            if getattr(response, "status_code", None) != 200:
                return None
            version = response.json().get("categoryTreeVersion")
            return str(version) if version else None
        except Exception as exc:
            logger.info("[CAT_VALIDATE] tree version unavailable for %s: %s", tree_id, exc)
            return None

    def _token(self) -> str:
        if self.token_provider:
            return self.token_provider()
        return self._client_credentials_token()

    def _client_credentials_token(self) -> str:
        now = self._clock()
        if self._app_token and self._app_token_expiry and now < self._app_token_expiry:
            return self._app_token
        app_id = os.getenv("EBAY_APP_ID")
        cert_id = os.getenv("EBAY_CERT_ID")
        if not app_id or not cert_id:
            raise RuntimeError("EBAY_APP_ID/EBAY_CERT_ID missing for taxonomy app token")
        basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
        session = self._session()
        response = session.post(
            f"{self.api_base}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=20,
        )
        if getattr(response, "status_code", None) != 200:
            raise RuntimeError(f"app token HTTP {getattr(response, 'status_code', None)}")
        body = response.json()
        self._app_token = body["access_token"]
        self._app_token_expiry = now + timedelta(seconds=int(body.get("expires_in") or 3600) - 60)
        return self._app_token

    def _session(self):
        if self.session is not None:
            return self.session
        import requests
        return requests.Session()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS category_validity_cache (
                    tree_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    valid INTEGER NOT NULL,
                    leaf INTEGER NOT NULL,
                    name TEXT,
                    tree_version TEXT,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY (tree_id, category_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS category_tree_meta (
                    tree_id TEXT PRIMARY KEY,
                    tree_version TEXT,
                    checked_at TEXT NOT NULL
                )
                """
            )

    def _invalidate_if_version_changed(self, tree_id: str, tree_version: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tree_version FROM category_tree_meta WHERE tree_id = ?",
                (tree_id,),
            ).fetchone()
            if row and row["tree_version"] and row["tree_version"] != tree_version:
                conn.execute("DELETE FROM category_validity_cache WHERE tree_id = ?", (tree_id,))
                logger.info(
                    "[CAT_VALIDATE] invalidated cache for tree %s (%s → %s)",
                    tree_id,
                    row["tree_version"],
                    tree_version,
                )
            conn.execute(
                """
                INSERT INTO category_tree_meta (tree_id, tree_version, checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tree_id) DO UPDATE SET
                    tree_version = excluded.tree_version,
                    checked_at = excluded.checked_at
                """,
                (tree_id, tree_version, self._clock().isoformat()),
            )

    def _read_cache(self, tree_id: str, category_id: str, tree_version: Optional[str]) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT valid, leaf, name, tree_version, checked_at
                FROM category_validity_cache
                WHERE tree_id = ? AND category_id = ?
                """,
                (tree_id, category_id),
            ).fetchone()
        if row is None:
            return None
        if tree_version and row["tree_version"] and row["tree_version"] != tree_version:
            return None
        checked = datetime.fromisoformat(row["checked_at"])
        if self._clock() - checked > CACHE_TTL:
            return None
        valid = bool(row["valid"])
        leaf = bool(row["leaf"])
        if valid and leaf:
            reason = "leaf"
        elif row["name"]:
            reason = "non_leaf"
        else:
            reason = "not_in_tree"
        return {"valid": valid, "leaf": leaf, "name": row["name"] or "", "reason": reason}

    def _write_cache(self, tree_id: str, category_id: str, result: dict, tree_version: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO category_validity_cache
                    (tree_id, category_id, valid, leaf, name, tree_version, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tree_id, category_id) DO UPDATE SET
                    valid = excluded.valid,
                    leaf = excluded.leaf,
                    name = excluded.name,
                    tree_version = excluded.tree_version,
                    checked_at = excluded.checked_at
                """,
                (
                    tree_id,
                    category_id,
                    1 if result["valid"] else 0,
                    1 if result["leaf"] else 0,
                    result.get("name") or "",
                    tree_version,
                    self._clock().isoformat(),
                ),
            )


def _error_id(response) -> Optional[int]:
    try:
        errors = response.json().get("errors") or []
    except Exception:
        return None
    if not errors:
        return None
    try:
        return int(errors[0].get("errorId"))
    except (TypeError, ValueError):
        return None


def _result_from_subtree(payload: dict) -> dict:
    node = payload.get("categorySubtreeNode") or {}
    category = node.get("category") or {}
    name = category.get("categoryName") or ""
    is_leaf = bool(node.get("leafCategoryTreeNode"))
    if is_leaf:
        return {"valid": True, "leaf": True, "name": name, "reason": "leaf"}
    return {"valid": False, "leaf": False, "name": name, "reason": "non_leaf"}
