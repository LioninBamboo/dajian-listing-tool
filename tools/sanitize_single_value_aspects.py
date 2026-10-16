import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.utils.publish_autofix import sanitize_single_value_aspects

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "ebay_collection.db"


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

def main():
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT id, sku, optimization FROM collected_products WHERE optimization IS NOT NULL AND optimization != ''"
    ).fetchall()

    changed = []
    for pid, sku, optimization in rows:
        try:
            data = json.loads(optimization) if isinstance(optimization, str) else optimization
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        aspects = data.get("aspects")
        if not isinstance(aspects, dict):
            continue

        before = json.dumps(aspects, ensure_ascii=False, sort_keys=True)
        sanitize_single_value_aspects(aspects)
        after = json.dumps(aspects, ensure_ascii=False, sort_keys=True)
        touched = before != after

        if touched:
            data["aspects"] = aspects
            cur.execute(
                "UPDATE collected_products SET optimization=?, updated_at=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), _utcnow_naive().isoformat(), pid),
            )
            changed.append(sku)

    conn.commit()
    conn.close()

    print(f"Sanitized SKUs: {len(changed)}")
    if changed:
        print("Examples:", ", ".join(changed[:20]))


if __name__ == "__main__":
    main()
