"""Derive per-store service ports from the store profile.

Each Windows clone has its own ``config/store_profile.local.yaml`` with
``server.port``. Streamlit is not stored in the profile; it is derived so
Main/Aqua/GrovePop never collide when started side by side:

    FastAPI   = server_port          (8000 / 8001 / 8002)
    Streamlit = 8501 + (server_port - 8000)   (8501 / 8502 / 8503)
"""

from __future__ import annotations

from typing import Any, Mapping

from src.utils.store_profile import StoreProfile, get_store_profile

DEFAULT_SERVER_PORT = 8000
DEFAULT_STREAMLIT_BASE = 8501


def derive_streamlit_port(server_port: int) -> int:
    """Map a FastAPI port onto the matching Streamlit port for this instance."""
    port = int(server_port)
    return DEFAULT_STREAMLIT_BASE + (port - DEFAULT_SERVER_PORT)


def resolve_store_ports(profile: StoreProfile | Mapping[str, Any] | None = None) -> dict[str, int | str]:
    """Return the ports and window-title tags for one store checkout."""
    if profile is None:
        profile = get_store_profile()
    if isinstance(profile, Mapping):
        server_port = int(profile.get("server_port") or DEFAULT_SERVER_PORT)
        brand = str(profile.get("brand_name") or "Store").strip() or "Store"
    else:
        server_port = int(getattr(profile, "server_port", DEFAULT_SERVER_PORT) or DEFAULT_SERVER_PORT)
        brand = str(getattr(profile, "brand_name", None) or "Store").strip() or "Store"

    streamlit_port = derive_streamlit_port(server_port)
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
    """CLI for Windows bat helpers: print KEY=VALUE lines."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Resolve this checkout's service ports")
    parser.add_argument(
        "--format",
        choices=("env", "json"),
        default="env",
        help="env: KEY=VALUE lines for `for /f`; json: one JSON object",
    )
    args = parser.parse_args(argv)
    ports = resolve_store_ports()
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
