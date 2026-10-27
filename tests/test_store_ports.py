"""Per-store FastAPI/Streamlit port derivation from .env + store_profile."""

from __future__ import annotations

import json
import textwrap

from src.utils.store_ports import (
    derive_streamlit_port,
    main as store_ports_main,
    resolve_store_ports,
)
from src.utils.store_profile import StoreProfile, load_store_profile, reset_store_profile_cache


def test_derive_streamlit_port_for_main_aqua_grovepop():
    assert derive_streamlit_port(8000) == 8501
    assert derive_streamlit_port(8001) == 8502
    assert derive_streamlit_port(8002) == 8503


def test_resolve_store_ports_from_profile_object(monkeypatch):
    monkeypatch.delenv("SERVER_PORT", raising=False)
    monkeypatch.delenv("STREAMLIT_PORT", raising=False)
    aqua = StoreProfile(brand_name="AquaRides", server_port=8001)
    ports = resolve_store_ports(aqua)
    assert ports["server_port"] == 8001
    assert ports["streamlit_port"] == 8502
    assert "AquaRides" in ports["fastapi_window_title"]
    assert "8001" in ports["fastapi_window_title"]
    assert "8502" in ports["streamlit_window_title"]


def test_resolve_store_ports_from_mapping(monkeypatch):
    monkeypatch.delenv("SERVER_PORT", raising=False)
    monkeypatch.delenv("STREAMLIT_PORT", raising=False)
    ports = resolve_store_ports({"brand_name": "GrovePop", "server_port": 8002})
    assert ports["server_port"] == 8002
    assert ports["streamlit_port"] == 8503
    assert ports["brand_name"] == "GrovePop"


def test_server_port_from_env_overrides_profile(monkeypatch):
    """Match server.py: SERVER_PORT from .env/process env first, then profile."""
    monkeypatch.setenv("SERVER_PORT", "8001")
    monkeypatch.delenv("STREAMLIT_PORT", raising=False)
    # Profile has no useful port (dataclass default 8000) — env must win.
    ports = resolve_store_ports(StoreProfile(brand_name="AquaRides", server_port=8000))
    assert ports["server_port"] == 8001
    assert ports["streamlit_port"] == 8502


def test_streamlit_port_from_env_overrides_derive(monkeypatch):
    monkeypatch.setenv("SERVER_PORT", "8002")
    monkeypatch.setenv("STREAMLIT_PORT", "8599")
    ports = resolve_store_ports(StoreProfile(brand_name="GrovePop", server_port=8002))
    assert ports["server_port"] == 8002
    assert ports["streamlit_port"] == 8599


def test_resolve_store_ports_missing_brand_raises(monkeypatch):
    monkeypatch.delenv("SERVER_PORT", raising=False)
    try:
        resolve_store_ports({"server_port": 8001, "brand_name": ""})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "brand" in str(exc).lower()


def test_store_profile_streamlit_port_property(tmp_path):
    yaml_file = tmp_path / "profile.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            store:
              brand_name: "AquaRides"
            server:
              port: 8001
            """
        ),
        encoding="utf-8",
    )
    profile = load_store_profile(yaml_file)
    assert profile.server_port == 8001
    assert profile.streamlit_port == 8502


def test_store_ports_cli_env_format(tmp_path, monkeypatch, capsys):
    local = tmp_path / "store_profile.local.yaml"
    local.write_text(
        textwrap.dedent(
            """
            store:
              brand_name: "GrovePop"
            server:
              port: 8002
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORE_PROFILE_PATH", str(local))
    monkeypatch.delenv("SERVER_PORT", raising=False)
    monkeypatch.delenv("STREAMLIT_PORT", raising=False)
    reset_store_profile_cache()
    try:
        assert store_ports_main(["--format", "env"]) == 0
    finally:
        reset_store_profile_cache()
    out = capsys.readouterr().out
    assert "SERVER_PORT=8002" in out
    assert "STREAMLIT_PORT=8503" in out
    assert "BRAND_NAME=GrovePop" in out


def test_store_ports_cli_json_format(tmp_path, monkeypatch, capsys):
    local = tmp_path / "store_profile.local.yaml"
    local.write_text(
        textwrap.dedent(
            """
            store:
              brand_name: "MainTest"
            server:
              port: 8000
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORE_PROFILE_PATH", str(local))
    monkeypatch.delenv("SERVER_PORT", raising=False)
    monkeypatch.delenv("STREAMLIT_PORT", raising=False)
    reset_store_profile_cache()
    try:
        assert store_ports_main(["--format", "json"]) == 0
    finally:
        reset_store_profile_cache()
    payload = json.loads(capsys.readouterr().out)
    assert payload["server_port"] == 8000
    assert payload["streamlit_port"] == 8501


def test_store_ports_cli_env_server_port_without_yaml_port(tmp_path, monkeypatch, capsys):
    """SERVER_PORT=8001 in env with profile default port => 8001."""
    local = tmp_path / "store_profile.local.yaml"
    local.write_text(
        textwrap.dedent(
            """
            store:
              brand_name: "AquaRides"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORE_PROFILE_PATH", str(local))
    monkeypatch.setenv("SERVER_PORT", "8001")
    monkeypatch.delenv("STREAMLIT_PORT", raising=False)
    reset_store_profile_cache()
    try:
        assert store_ports_main(["--format", "env"]) == 0
    finally:
        reset_store_profile_cache()
    out = capsys.readouterr().out
    assert "SERVER_PORT=8001" in out
    assert "STREAMLIT_PORT=8502" in out


def test_store_ports_cli_lookup_failure_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        "src.utils.store_ports.resolve_store_ports",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert store_ports_main(["--format", "env"]) == 1
    err = capsys.readouterr().err
    assert "lookup failed" in err.lower()
