"""CommerceConnector implementations, one per shop platform."""

from __future__ import annotations

from typing import Any

from connectors.base import CommerceConnector


def connector_for(store: dict[str, Any]) -> CommerceConnector:
    platform = store.get("platform")
    if platform == "feed":
        from connectors.feed import FeedConnector

        return FeedConnector(store)
    if platform == "shopify":
        from connectors.shopify_public import ShopifyPublicConnector

        return ShopifyPublicConnector(store)
    raise ValueError(f"no connector for platform {platform!r}")
