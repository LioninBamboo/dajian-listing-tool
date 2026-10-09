#!/usr/bin/env python3
"""Write/read GIGA New Arrivals resumable checkpoints (P0-1).

Surfaces HARD_STOP_LOGGED_OUT clearly for the parent agent. Does not send
Slack/email — parent must ping Sergey from chat.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
HARD_STOP_LOGGED_OUT = "HARD_STOP_LOGGED_OUT"


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def default_paths(date: str | None = None) -> dict[str, Path]:
    day = date or _today()
    name = f"giga_new_arrivals_checkpoint_{day}.json"
    return {
        "box": Path("/workspace") / name,
        "zbook_cache": Path("cache") / name,
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_checkpoint(
    *,
    phase: str,
    status: str = "running",
    resume_from: str | None = None,
    login_ok: bool | None = None,
    login_detail: str | None = None,
    hard_stop: bool = False,
    hard_stop_reason: str | None = None,
    extra: dict[str, Any] | None = None,
    date: str | None = None,
    paths: list[Path] | None = None,
) -> dict[str, Any]:
    day = date or _today()
    now = datetime.now(TZ)
    targets = paths or [default_paths(day)["box"], default_paths(day)["zbook_cache"]]
    base: dict[str, Any] = {}
    for path in targets:
        if path.exists():
            base = load_checkpoint(path)
            break

    run_id = base.get("run_id") or f"giga-new-arrivals_{now.strftime('%Y%m%dT%H%M')}"
    payload = {
        **base,
        "job": "giga-new-arrivals-collect-publish",
        "run_id": run_id,
        "date": day,
        "asia_shanghai": now.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase,
        "status": status,
        "hard_stop": bool(hard_stop),
        "hard_stop_reason": hard_stop_reason,
        "updated_at": now.isoformat(),
    }
    if resume_from is not None:
        payload["resume_from"] = resume_from
    if login_ok is not None:
        payload["login_ok"] = bool(login_ok)
    if login_detail is not None:
        payload["login_detail"] = login_detail
    if extra:
        payload.update(extra)

    for path in targets:
        try:
            _atomic_write(path, payload)
        except OSError as exc:
            print(f"[WARN] checkpoint write failed for {path}: {exc}", file=sys.stderr)
    return payload


def mark_logged_out(*, login_detail: str, resume_from: str, date: str | None = None) -> dict[str, Any]:
    """Record HARD_STOP_LOGGED_OUT and return parent-facing ping text fields."""
    payload = write_checkpoint(
        phase="HARD_STOP",
        status="blocked_user",
        resume_from=resume_from,
        login_ok=False,
        login_detail=login_detail,
        hard_stop=True,
        hard_stop_reason=HARD_STOP_LOGGED_OUT,
        date=date,
    )
    paths = default_paths(date)
    payload["_parent_ping"] = (
        f"HARD_STOP_LOGGED_OUT: GIGA session lost ({login_detail}). "
        f"Checkpoint {paths['box']} (resume_from={resume_from}). "
        "Re-login on box, then reply 继续 — do not invent Slack sends."
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="Write/update today's checkpoint")
    w.add_argument("--phase", required=True)
    w.add_argument("--status", default="running")
    w.add_argument("--resume-from", default=None)
    w.add_argument("--login-ok", choices=["0", "1"], default=None)
    w.add_argument("--login-detail", default=None)
    w.add_argument("--date", default=None)

    s = sub.add_parser("hard-stop-logged-out", help="Mark HARD_STOP_LOGGED_OUT")
    s.add_argument("--login-detail", required=True)
    s.add_argument("--resume-from", required=True)
    s.add_argument("--date", default=None)

    r = sub.add_parser("read", help="Print checkpoint JSON")
    r.add_argument("--date", default=None)
    r.add_argument("--path", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "write":
        login_ok = None if args.login_ok is None else args.login_ok == "1"
        payload = write_checkpoint(
            phase=args.phase,
            status=args.status,
            resume_from=args.resume_from,
            login_ok=login_ok,
            login_detail=args.login_detail,
            date=args.date,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "hard-stop-logged-out":
        payload = mark_logged_out(
            login_detail=args.login_detail,
            resume_from=args.resume_from,
            date=args.date,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(payload["_parent_ping"], file=sys.stderr)
        return 0
    if args.cmd == "read":
        path = Path(args.path) if args.path else default_paths(args.date)["box"]
        if not path.exists():
            path = default_paths(args.date)["zbook_cache"]
        print(json.dumps(load_checkpoint(path), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
