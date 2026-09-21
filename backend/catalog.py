"""Read access to the product catalog.

Products live in data/products.json, not Postgres: orders snapshot the name and
price at purchase time, so nothing needs a foreign key into the catalog.

Everything goes through this module so the backing store can become a database
table later without touching any caller.
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import chromadb

from common import embeddings

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


def filter_products(
    category: str | None = None,
    gender: str | None = None,
    max_price: float | None = None,
) -> list[dict[str, Any]]:
    """Apply exact catalogue filters without involving vector search."""
    products = _load()
    if category:
        products = [product for product in products if product.get("category") == category]
    if gender:
        products = [product for product in products if product.get("gender") == gender]
    if max_price is not None:
        products = [product for product in products if float(product.get("price", 0)) <= max_price]
    return list(products)


def _get_collection() -> Any:
    client = chromadb.HttpClient(
        host=os.getenv("CHROMA_HOST", "localhost"),
        port=int(os.getenv("CHROMA_PORT", "8001")),
    )
    return client.get_or_create_collection(name="products")


def _top_rated(exclude_ids: set[str], k: int, gender: str | None) -> list[dict[str, Any]]:
    products = [product for product in _load() if product["id"] not in exclude_ids]
    if gender:
        products = [product for product in products if product.get("gender") == gender]
    return sorted(
        products,
        key=lambda product: (float(product.get("rating", 0)), int(product.get("rating_count", 0))),
        reverse=True,
    )[:k]


def get_similar_products(
    product_ids: list[str],
    exclude_ids: list[str],
    k: int = 4,
    gender: str | None = None,
) -> list[dict[str, Any]]:
    """Return full catalog records similar to the supplied products.

    Recommendations use the same Chroma collection as chat search. If there is
    no purchase history or vector search is unavailable, popular catalog items
    provide a deterministic fallback for the storefront.
    """
    excluded = set(exclude_ids) | set(product_ids)
    source_products = [product for product in _load() if product["id"] in product_ids]
    if not source_products:
        return _top_rated(excluded, k, gender)

    query = ". ".join(
        " ".join(
            [
                product.get("title", product.get("name", "")),
                f"Category: {product.get('gender', '')} {product.get('category', '')}",
                f"Fabric: {product.get('fabric', '')}",
                f"Suitable for: {', '.join(product.get('occasion', []))}",
                product.get("description", ""),
            ]
        )
        for product in source_products
    )

    try:
        where = {"gender": {"$eq": gender}} if gender else None
        result = _get_collection().query(
            query_embeddings=[embeddings.embed_one(query)],
            n_results=min(max(k + len(excluded), k), 20),
            where=where,
        )
        metadata = [item or {} for item in result.get("metadatas", [[]])[0]]
        recommendations = []
        for item in metadata:
            product_id = str(item.get("id", ""))
            if product_id in excluded:
                continue
            product = get_by_id(product_id)
            if product is not None:
                recommendations.append(product)
            if len(recommendations) == k:
                break
        return recommendations or _top_rated(excluded, k, gender)
    except Exception as exc:
        print(f"[catalog] recommendation lookup failed, using top-rated items: {exc!r}")
        return _top_rated(excluded, k, gender)
