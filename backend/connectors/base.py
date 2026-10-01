"""What Munim needs from a brand's shop, whatever the shop runs on.

The core never assumes Shopify (plan.md, decision 7): it talks to a
CommerceConnector. P1 implements reading the catalogue; checkout, orders,
exchanges and webhooks arrive with P3 and P5 and raise until then.

A connector returns catalogue records in one shape (the shape of
data/products.json, see catalog_sync.normalise), optionally with an explicit
"variants" list; catalog_sync writes them to Postgres and embeds them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CommerceConnector(ABC):
    platform = ""

    def __init__(self, store: dict[str, Any]):
        self.store = store
        self.config: dict[str, Any] = store.get("connector") or {}

    @abstractmethod
    def fetch_catalog(self) -> list[dict[str, Any]]:
        """Every product the store sells now, as catalogue records."""

    def sync_catalog(self) -> dict[str, int]:
        """Fetch, write to Postgres, embed what changed. Returns counts."""
        import catalog_sync

        return catalog_sync.upsert(self.store["store_id"], self.fetch_catalog(),
                                   code_prefix=self.store.get("code_prefix") or "")

    # --- later phases ---------------------------------------------------------------

    def build_checkout(self, cart: dict[str, Any], buyer: dict[str, Any]) -> str:
        raise NotImplementedError("checkout through the brand's shop arrives in P3")

    def find_orders(self, phone: str) -> list[dict[str, Any]]:
        raise NotImplementedError("order lookup arrives in P5")

    def order_status(self, order_ref: str) -> dict[str, Any]:
        raise NotImplementedError("order status arrives in P5")

    def request_exchange(self, order_ref: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError("exchanges arrive in P5")

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError("shop webhooks arrive in P3")
