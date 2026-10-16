from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from src.clients.dajian_client import DaJianClient


def main() -> None:
    client = DaJianClient(
        os.getenv("DAJIAN_API_KEY"),
        os.getenv("DAJIAN_API_SECRET"),
    )
    data = {
        "W1143P494948": client.get_product_detail_by_sku("W1143P494948"),
        "W1117S00388": client.get_product_detail_by_sku("W1117S00388"),
    }
    (ROOT / "debug_api.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
