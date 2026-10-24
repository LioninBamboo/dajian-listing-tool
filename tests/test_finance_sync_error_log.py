"""Finance sync pythonw error-log hunk."""

from __future__ import annotations

from pathlib import Path

import scripts.finance_sync_orders as finance_sync


def test_sync_exception_appends_error_log(monkeypatch, tmp_path):
    monkeypatch.setattr(finance_sync, "PROJECT_ROOT", tmp_path)

    def _boom(**_kwargs):
        raise RuntimeError("ebay down")

    monkeypatch.setattr(
        "src.services.finance_orders.sync_orders_from_ebay",
        _boom,
    )
    monkeypatch.setattr(
        "src.services.finance_orders.query_finance_summary",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "src.services.finance_orders.recompute_stored_pnls",
        lambda *_args, **_kwargs: {},
    )

    monkeypatch.setattr(
        "sys.argv",
        ["finance_sync_orders.py", "--days", "1", "--db", str(tmp_path / "x.db")],
    )
    rc = finance_sync.main()
    assert rc == 1
    log_path = tmp_path / "logs" / "finance_sync_error.log"
    text = log_path.read_text(encoding="utf-8")
    assert "ERROR: ebay down" in text


def test_sync_oserror_on_log_write_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(finance_sync, "PROJECT_ROOT", tmp_path)

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    real_open = Path.open

    def _open(self, *args, **kwargs):
        if self.name == "finance_sync_error.log":
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(
        "src.services.finance_orders.sync_orders_from_ebay",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("ebay down")),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["finance_sync_orders.py", "--days", "1", "--db", str(tmp_path / "x.db")],
    )
    rc = finance_sync.main()
    assert rc == 1
