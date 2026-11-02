"""Lossless codec for collected_products.logs (JSON list, possibly with a text tail)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

ParseStatus = Literal["ok", "empty", "trailing_text", "non_list", "garbage"]


@dataclass
class ParsedLogs:
    entries: list[Any]
    status: ParseStatus
    trailing: str = ""
    error: str | None = None

    def entries_with_trailing(self) -> list[Any]:
        """Return stored entries plus any preserved tail, never dropping history."""
        if self.status == "garbage":
            return [self.trailing]
        if self.trailing:
            return list(self.entries) + [self.trailing]
        return list(self.entries)


def _empty(error: str | None = None) -> ParsedLogs:
    return ParsedLogs(entries=[], status="empty", trailing="", error=error)


def _non_list(value: Any, error: str | None = None) -> ParsedLogs:
    if value is None:
        return ParsedLogs(entries=[], status="non_list", trailing="", error=error)
    return ParsedLogs(entries=[value], status="non_list", trailing="", error=error)


def parse_logs(raw: Any) -> ParsedLogs:
    """Parse a logs cell. Never raises; never drops a recoverable tail."""
    if raw is None:
        return _empty()
    if isinstance(raw, list):
        return ParsedLogs(entries=list(raw), status="ok")
    if isinstance(raw, (dict, int, float, bool)):
        return _non_list(raw)
    if not isinstance(raw, str):
        text = str(raw)
        return ParsedLogs(entries=[], status="garbage", trailing=text, error="unsupported_type")
    if not raw.strip():
        return _empty()

    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return _raw_decode_string(raw, error=str(exc))

    if isinstance(loaded, list):
        return ParsedLogs(entries=loaded, status="ok")
    if loaded is None:
        return _non_list(None, error="null")
    return _non_list(loaded)


def _raw_decode_string(raw: str, error: str | None = None) -> ParsedLogs:
    try:
        loaded, end = json.JSONDecoder().raw_decode(raw.lstrip())
    except (TypeError, ValueError) as exc:
        return ParsedLogs(entries=[], status="garbage", trailing=raw, error=str(exc))

    if not isinstance(loaded, list):
        return ParsedLogs(entries=[], status="garbage", trailing=raw, error=error or "raw_decode_non_list")

    stripped = raw.lstrip()
    trailing = stripped[end:].strip("\n")
    return ParsedLogs(
        entries=loaded,
        status="trailing_text",
        trailing=trailing,
        error=error,
    )


def dump_logs(entries: list[Any]) -> str:
    """Serialize a logs list to a JSON array string."""
    if not isinstance(entries, list):
        raise TypeError(f"dump_logs expects a list, got {type(entries).__name__}")
    return json.dumps(entries, ensure_ascii=False)


def append_log(raw: Any, entry: Any) -> str:
    """Append one entry and always emit valid JSON, keeping any prior tail."""
    return dump_logs(parse_logs(raw).entries_with_trailing() + [entry])
