import json
import os
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to ingest the catalog")

    products = json.loads((ROOT / "data" / "products.json").read_text(encoding="utf-8"))
    openai_client = OpenAI(api_key=api_key)
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

    embedding_response = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=documents,
    )
    collection.upsert(
        ids=[product["id"] for product in products],
        documents=documents,
        embeddings=[item.embedding for item in embedding_response.data],
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
    print(f"Ingested {len(products)} products into ChromaDB collection 'products'.")


if __name__ == "__main__":
    main()
