"""Read access to the product catalog.

Products live in data/products.json, not Postgres: orders snapshot the name and
price at purchase time, so nothing needs a foreign key into the catalog.

Everything goes through this module so the backing store can become a database
table later without touching any caller.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "products.json"


@lru_cache(maxsize=1)
def _load() -> list[dict[str, Any]]:
    if not CATALOG_PATH.exists():
        print(f"[catalog] missing {CATALOG_PATH}")
        return []
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def get_all() -> list[dict[str, Any]]:
    return list(_load())


def get_by_id(product_id: str) -> Optional[dict[str, Any]]:
    return next((p for p in _load() if p["id"] == product_id), None)


def categories() -> list[str]:
    return sorted({p["category"] for p in _load()})
