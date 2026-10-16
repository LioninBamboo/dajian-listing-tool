from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    conn = sqlite3.connect(ROOT / "ebay_collection.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT optimization FROM collected_products WHERE sku='W1117S00388'"
    ).fetchone()
    if not row:
        return

    optimization = json.loads(row["optimization"])
    (ROOT / "debug_desc.html").write_text(
        optimization.get("description", ""),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
