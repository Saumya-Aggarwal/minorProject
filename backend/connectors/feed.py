"""A catalogue from a JSON file: the demo store's data/products.json.

Later this is where a Google Merchant feed or our REST ingest lands for
brands on custom sites (decision 7); the records are already in catalogue shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from connectors.base import CommerceConnector

ROOT = Path(__file__).resolve().parents[2]


class FeedConnector(CommerceConnector):
    platform = "feed"

    def fetch_catalog(self) -> list[dict[str, Any]]:
        path = ROOT / self.config.get("feed_path", "data/products.json")
        return json.loads(path.read_text(encoding="utf-8"))
