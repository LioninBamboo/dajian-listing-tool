"""Write-surface test: only the section-9 targets may be written."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import requests

from src.services import sku_field_edit as sfe
from tests.test_sku_field_edit import (
    FakeEbay,
    ROWS,
    inventory_item,
    make_ctx,
    parse,
    run,
)


ALLOWED_TABLES = {"collected_products"}
ALLOWED_COLUMNS = {"optimization", "updated_at", "logs"}
FORBIDDEN_SQL = ("INSERT ", "DELETE ", "DROP ", "ALTER ")


def _is_allowed_report_or_lock(path: Path, tmp_path: Path) -> bool:
    text = str(path)
    if "sku_field_edit" in text and text.endswith(".json"):
        return True
    if text.endswith("sku_field_edit.lock"):
        return True
    try:
        path.resolve().relative_to((tmp_path / "backups").resolve())
        return True
    except Exception:
        pass
    try:
        path.resolve().relative_to(tmp_path.resolve())
        return "report" in path.name or path.suffix == ".json"
    except Exception:
        return False


def test_write_surface_dry_run_apply_and_live(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    writes = {"sql": [], "put": [], "post": [], "open": []}
    real_connect = sqlite3.connect
    real_put = requests.Session.put
    real_post = requests.Session.post
    real_open = open

    class LoggingConnection:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def execute(self, sql, *params):
            writes["sql"].append(str(sql))
            return self._conn.execute(sql, *params)

        def backup(self, target, *args, **kwargs):
            real_target = getattr(target, "_conn", target)
            return self._conn.backup(real_target, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            if name == "_conn":
                object.__setattr__(self, name, value)
            else:
                setattr(self._conn, name, value)

    def wrapped_connect(*args, **kwargs):
        return LoggingConnection(real_connect(*args, **kwargs))

    def wrapped_put(self, url, *args, **kwargs):
        writes["put"].append(str(url))
        return real_put(self, url, *args, **kwargs)

    def wrapped_post(self, url, *args, **kwargs):
        writes["post"].append(str(url))
        return real_post(self, url, *args, **kwargs)

    def wrapped_open(file, mode="r", *args, **kwargs):
        if "b" not in str(mode) and any(flag in str(mode) for flag in ("w", "a", "x")):
            writes["open"].append((str(file), str(mode)))
        return real_open(file, mode, *args, **kwargs)

    ebay = FakeEbay({ROWS[2]["sku"]: inventory_item(ROWS[2])})
    ctx = make_ctx(tmp_path, ebay=ebay)
    with patch.object(sqlite3, "connect", wrapped_connect), patch.object(
        requests.Session, "put", wrapped_put
    ), patch.object(requests.Session, "post", wrapped_post), patch("builtins.open", wrapped_open):
        code, out, report = run(
            [
                "--store",
                "main",
                "--sku",
                "W465P341531",
                "--strip-claim",
                "plywood",
                "--location",
                "features",
                "--report",
                str(tmp_path / "dry.json"),
            ],
            ctx,
        )
        assert code == 0
        assert not any(sql.strip().upper().startswith("UPDATE") for sql in writes["sql"])
        assert writes["put"] == []
        assert writes["post"] == []
        for path, _mode in writes["open"]:
            assert _is_allowed_report_or_lock(Path(path), tmp_path), path
            assert not path.endswith(".env")
            assert "store_profile" not in path
            assert not path.endswith(".bat")

        writes["sql"].clear()
        writes["open"].clear()
        ctx = make_ctx(tmp_path / "apply", ebay=ebay)
        code, out, report = run(
            [
                "--store",
                "main",
                "--sku",
                "W465P341531",
                "--strip-claim",
                "plywood",
                "--location",
                "features",
                "--apply",
                "--note",
                "write surface apply",
                "--report",
                str(tmp_path / "apply.json"),
            ],
            ctx,
        )
        assert code == 0
        updates = [sql for sql in writes["sql"] if sql.strip().upper().startswith("UPDATE")]
        assert updates
        for sql in updates:
            assert "collected_products" in sql
            assert "status" not in sql.lower() or "status" not in sql.split("SET")[1].split("WHERE")[0].lower()
        assert writes["put"] == []
        assert writes["post"] == []

        writes["sql"].clear()
        writes["put"].clear()
        writes["open"].clear()
        ctx = make_ctx(tmp_path / "live", ebay=ebay)
        code, out, report = run(
            [
                "--store",
                "main",
                "--sku",
                "W465P341531",
                "--strip-claim",
                "plywood",
                "--location",
                "features",
                "--apply",
                "--push-live",
                "--note",
                "write surface live",
                "--report",
                str(tmp_path / "live.json"),
            ],
            ctx,
        )
        assert code == 0
        assert writes["put"] == []
        assert writes["post"] == []
        assert ebay.puts
        for item in ebay.puts:
            url = item["url"]
            assert url.startswith("https://api.ebay.com/sell/inventory/v1/")
            assert "/publish" not in url
            assert "create_or_replace" not in url
        _ = report
        _ = sfe
        _ = parse
