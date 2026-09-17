"""Embed the product catalog into ChromaDB.

Re-run after editing data/products.json or changing EMBEDDING_PROVIDER.
Upsert by product id, so re-running is safe and updates in place.
"""

import json
import os
import sys
from pathlib import Path

import chromadb
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "backend" / ".env")

from common import embeddings  # noqa: E402


def build_document(product: dict) -> str:
    """The text that gets embedded, and therefore what can be found.

    Occasion, fabric and colour vocabulary carry the most weight: customers ask
    by event ("something for a sangeet") rather than by SKU. Price and stock are
    left out on purpose — they are filters, not meaning, and bare numbers add
    noise to the vector without helping any query.
    """
    return ". ".join(
        [
            product["title"],
            f"Category: {product['gender']} {product['category']}, {product['subcategory']}",
            f"Fabric: {product['fabric']}. Colour: {product['color']}. Pattern: {product['pattern']}",
            f"Fit: {product['fit']}. Sleeve: {product['sleeve']}. Neckline: {product['neck']}",
            f"Suitable for: {', '.join(product['occasion'])}",
            product["description"],
            " ".join(product["highlights"]),
        ]
    )


def build_metadata(product: dict) -> dict:
    """Chroma metadata must be flat scalars — no lists, no nested dicts."""
    return {
        # These keys are read by backend/bot/chat.py; renaming breaks retrieval
        "id": product["id"],
        "name": product["name"],
        "category": product["category"],
        "fabric": product["fabric"],
        "price": product["price"],
        "description": product["description"],
        # Extra catalogue detail, for filtering and for the storefront
        "title": product["title"],
        "brand": product["brand"],
        "gender": product["gender"],
        "color": product["color"],
        "mrp": product["mrp"],
        "discount_percent": product["discount_percent"],
        "rating": product["rating"],
        "rating_count": product["rating_count"],
        "in_stock": product["in_stock"],
        "image_url": product["image_url"],
        "occasion": ", ".join(product["occasion"]),
        "sizes": ", ".join(product["sizes_available"]),
    }


def main() -> None:
    products = json.loads((ROOT / "data" / "products.json").read_text(encoding="utf-8"))
    print(f"Embedding with {embeddings.provider()} / {embeddings.model_name()}")

    chroma_client = chromadb.HttpClient(
        host=os.getenv("CHROMA_HOST", "localhost"),
        port=int(os.getenv("CHROMA_PORT", "8001")),
    )
    collection = chroma_client.get_or_create_collection(name="products")

    documents = [build_document(product) for product in products]
    vectors = embeddings.embed(documents)

    collection.upsert(
        ids=[product["id"] for product in products],
        documents=documents,
        embeddings=vectors,
        metadatas=[build_metadata(product) for product in products],
    )

    print(
        f"Ingested {len(products)} products into ChromaDB collection 'products' "
        f"({len(vectors[0])} dimensions). Collection now holds {collection.count()}."
    )


if __name__ == "__main__":
    main()
