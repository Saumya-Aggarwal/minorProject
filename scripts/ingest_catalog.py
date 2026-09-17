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


def main() -> None:
    products = json.loads((ROOT / "data" / "products.json").read_text(encoding="utf-8"))
    print(f"Embedding with {embeddings.provider()} / {embeddings.model_name()}")
    chroma_client = chromadb.HttpClient(
        host=os.getenv("CHROMA_HOST", "localhost"),
        port=int(os.getenv("CHROMA_PORT", "8001")),
    )
    collection = chroma_client.get_or_create_collection(name="products")

    documents = []
    for product in products:
        documents.append(
            " | ".join(
                [
                    product["name"],
                    f"Category: {product['category']}",
                    f"Fabric: {product['fabric']}",
                    f"Price: Rs. {product['price']}",
                    f"Sizes: {', '.join(product['sizes_available'])}",
                    product["description"],
                ]
            )
        )

    vectors = embeddings.embed(documents)
    collection.upsert(
        ids=[product["id"] for product in products],
        documents=documents,
        embeddings=vectors,
        metadatas=[
            {
                "id": product["id"],
                "name": product["name"],
                "category": product["category"],
                "fabric": product["fabric"],
                "price": product["price"],
                "description": product["description"],
            }
            for product in products
        ],
    )
    print(
        f"Ingested {len(products)} products into ChromaDB collection 'products' "
        f"({len(vectors[0])} dimensions)."
    )


if __name__ == "__main__":
    main()
