"""Website search and recommendations, on the same vector index as the bot.

The WhatsApp assistant and the website run on one retrieval engine: the
product vectors in Postgres (pgvector, written by catalog_sync.py), queried
with the same embedding model (common/embeddings.py). A customer typing
"something for my sister's mehendi" into the search box gets the same kind of
meaning-based match as one typing it into WhatsApp.

Search itself is bot/retrieval.py, shared with the chat; this module only
returns catalogue records for pages (search results, similar, recommended).

If vector search fails, search falls back to keyword matching over the
catalogue, so the search box never errors during a demo.
"""

from typing import Any, Optional

import catalog
from bot import retrieval


def _records(ids: list[str]) -> list[dict[str, Any]]:
    """Catalogue records for product codes, in order, skipping any that vanished."""
    found = (catalog.get_by_id(product_id) for product_id in ids)
    return [record for record in found if record is not None]


def search_products(query: str, top_k: int = 24) -> list[dict[str, Any]]:
    """Catalogue records ranked by meaning, best first.

    The same engine as the WhatsApp bot (bot/retrieval.py): "saree under 3000"
    on the website filters exactly as it does in chat, and falls back to
    keyword ranking by itself when vector search fails.
    """
    query = query.strip()
    if not query:
        return []
    return retrieval.search(query, k=top_k).products


def similar_products(product_id: str, k: int = 4) -> list[dict[str, Any]]:
    """Nearest neighbours of one product's own vector, same gender, not itself."""
    product = catalog.get_by_id(product_id)
    if product is None:
        return []
    try:
        vector = catalog.stored_vector(product_id)
        if vector is None:
            raise RuntimeError(f"{product_id} has no vector for the current model")
        ranked = catalog.ranked_by_vector(vector, exclude={product_id},
                                          gender=product["gender"] or None, limit=k)
        ids = [code for code, _ in ranked]
    except Exception as exc:
        print(f"[search] similar products unavailable, using category: {exc!r}")
        same = catalog.filter_products(catalog.get_all(), gender=product["gender"],
                                       category=product["category"])
        ids = [p["id"] for p in catalog.sort_products(same, "popular") if p["id"] != product_id][:k]
    return _records(ids)


def recommended_for(purchased_ids: list[str], k: int = 4) -> list[dict[str, Any]]:
    """Pieces close to what the customer already bought, excluding those.

    Takes neighbours of the three most recent purchases in turn, so one big
    order does not crowd out the others. purchased_ids comes from
    repository.get_purchased_product_ids (contract C2), most recent first.
    """
    owned = set(purchased_ids)
    picks: list[str] = []
    neighbour_lists = [
        [p["id"] for p in similar_products(pid, k=k + len(owned))]
        for pid in purchased_ids[:3]
    ]
    for rank in range(max((len(n) for n in neighbour_lists), default=0)):
        for neighbours in neighbour_lists:
            if rank < len(neighbours):
                pid = neighbours[rank]
                if pid not in owned and pid not in picks:
                    picks.append(pid)
    return _records(picks[:k])


def available() -> Optional[bool]:
    """True if the current store has products with vectors; diagnostics only."""
    try:
        return bool(catalog.ranked_by_vector(catalog.stored_vector(catalog.get_all()[0]["id"]), limit=1))
    except Exception:
        return False
