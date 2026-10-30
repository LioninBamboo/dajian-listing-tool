"""Derive per-store service ports from .env + store profile.

Resolution order matches ``server.py`` (``SERVER_PORT`` from this checkout's
``.env`` / process env first, then ``store_profile.server_port``). Streamlit
follows the same pattern via ``STREAMLIT_PORT``, else:

    Streamlit = 8501 + (server_port - 8000)   (8501 / 8502 / 8503)

Windows bat helpers must treat a non-zero exit or empty output as fatal —
never fall back to 8000/8501.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

from src.utils.store_profile import StoreProfile, get_store_profile

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SERVER_PORT = 8000
DEFAULT_STREAMLIT_BASE = 8501


def _load_checkout_env() -> None:
    """Load this checkout's ``.env`` without overriding already-set process env."""
    env_path = _PROJECT_ROOT / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:
        # dotenv optional at import time; CLI still works from process env + profile.
        pass


def derive_streamlit_port(server_port: int) -> int:
    """Map a FastAPI port onto the matching Streamlit port for this instance."""
    port = int(server_port)
    return DEFAULT_STREAMLIT_BASE + (port - DEFAULT_SERVER_PORT)


def _profile_server_port(profile: StoreProfile | Mapping[str, Any]) -> int:
    if isinstance(profile, Mapping):
        raw = profile.get("server_port")
    else:
        raw = getattr(profile, "server_port", None)
    if raw is None or raw == "":
        raise ValueError("store profile missing server.port / server_port")
    port = int(raw)
    if port <= 0 or port > 65535:
        raise ValueError(f"invalid server_port: {port}")
    return port


def _profile_brand(profile: StoreProfile | Mapping[str, Any]) -> str:
    if isinstance(profile, Mapping):
        brand = str(profile.get("brand_name") or "").strip()
    else:
        brand = str(getattr(profile, "brand_name", None) or "").strip()
    if not brand:
        raise ValueError("store profile missing brand_name")
    return brand


def resolve_store_ports(profile: StoreProfile | Mapping[str, Any] | None = None) -> dict[str, int | str]:
    """Return the ports and window-title tags for one store checkout.

    Raises ``ValueError`` when ports/brand cannot be resolved — callers/CLI
    must not invent 8000/8501 defaults on failure.
    """
    _load_checkout_env()
    if profile is None:
        profile = get_store_profile()

    brand = _profile_brand(profile)
    env_server = (os.getenv("SERVER_PORT") or "").strip()
    if env_server:
        server_port = int(env_server)
    else:
        server_port = _profile_server_port(profile)
    if server_port <= 0 or server_port > 65535:
        raise ValueError(f"invalid SERVER_PORT / server_port: {server_port}")

    env_streamlit = (os.getenv("STREAMLIT_PORT") or "").strip()
    if env_streamlit:
        streamlit_port = int(env_streamlit)
    else:
        streamlit_port = derive_streamlit_port(server_port)
    if streamlit_port <= 0 or streamlit_port > 65535:
        raise ValueError(f"invalid STREAMLIT_PORT / streamlit_port: {streamlit_port}")

    # Window titles include brand + port so stop.bat never kills a sibling store.
    safe_brand = "".join(ch if ch.isalnum() or ch in {"-", "_", " "} else "" for ch in brand).strip()
    safe_brand = safe_brand or "Store"
    return {
        "server_port": server_port,
        "streamlit_port": streamlit_port,
        "brand_name": brand,
        "fastapi_window_title": f"FastAPI Server - {safe_brand} ({server_port})",
        "streamlit_window_title": f"Streamlit UI - {safe_brand} ({streamlit_port})",
    }


def main(argv: list[str] | None = None) -> int:
    """CLI for Windows bat helpers: print KEY=VALUE lines. Non-zero on failure."""
    import argparse

    parser = argparse.ArgumentParser(description="Resolve this checkout's service ports")
    parser.add_argument(
        "--format",
        choices=("env", "json"),
        default="env",
        help="env: KEY=VALUE lines for `for /f`; json: one JSON object",
    )
    args = parser.parse_args(argv)
    try:
        ports = resolve_store_ports()
        if not ports.get("server_port") or not ports.get("streamlit_port"):
            raise ValueError("store_ports resolved empty ports")
        if not str(ports.get("brand_name") or "").strip():
            raise ValueError("store_ports resolved empty brand_name")
    except Exception as exc:
        sys.stderr.write(f"store_ports lookup failed: {exc}\n")
        return 1

    if args.format == "json":
        import json

        sys.stdout.write(json.dumps(ports, ensure_ascii=False) + "\n")
    else:
        for key, value in ports.items():
            # Bat `for /f` splits on spaces; encode spaces as +.
            text = str(value).replace(" ", "+")
            sys.stdout.write(f"{key.upper()}={text}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
