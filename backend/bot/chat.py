import os
from typing import Any

import chromadb

from common import embeddings

COLLECTION_NAME = "products"


def _get_collection() -> Any:
    client = chromadb.HttpClient(
        host=os.getenv("CHROMA_HOST", "localhost"),
        port=int(os.getenv("CHROMA_PORT", "8001")),
    )
    return client.get_or_create_collection(name=COLLECTION_NAME)


def _format_matches(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return (
            "I could not find a close match in our catalog yet. "
            "Tell me the occasion, preferred fabric, or budget and I will search again."
        )

    lines = ["Here are a few options that may suit you:"]
    for index, product in enumerate(matches, start=1):
        lines.append(
            f"{index}. {product['name']} - Rs. {product['price']:.0f}\n"
            f"   {product['description']}"
        )
    lines.append("Reply with an item number or ask for more options.")
    return "\n".join(lines)


def _polish_response(query: str, matches: list[dict[str, Any]], fallback: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not matches:
        # No key: the formatted catalog list is the reply. Retrieval still works,
        # only the conversational phrasing is missing.
        return fallback

    from openai import OpenAI

    prompt = "\n".join(
        f"- {item['name']} (Rs. {item['price']:.0f}): {item['description']}"
        for item in matches
    )
    try:
        response = OpenAI(api_key=api_key).chat.completions.create(
            model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            temperature=0.4,
            max_tokens=250,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise WhatsApp shopping assistant for an ethnic wear store. "
                        "Recommend only the supplied products, include prices, and ask one short "
                        "follow-up question. Do not invent availability or discounts."
                    ),
                },
                {"role": "user", "content": f"Customer request: {query}\nProducts:\n{prompt}"},
            ],
        )
        content = response.choices[0].message.content
        return content.strip() if content else fallback
    except Exception as exc:
        print(f"[bot] OpenAI response failed, using catalog response: {exc!r}")
        return fallback


def get_product_recommendations(query: str, top_k: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """Retrieve catalog matches and turn them into a customer-facing response."""
    try:
        collection = _get_collection()
        embedding = embeddings.embed_one(query)
        result = collection.query(query_embeddings=[embedding], n_results=top_k)
    except Exception as exc:
        print(f"[bot] catalog lookup failed: {exc!r}")
        if "dimension" in str(exc).lower():
            # Vectors were written by a different model than the one querying
            print(
                f"[bot] embedding mismatch: querying with {embeddings.model_name()}. "
                "Re-run scripts/ingest_catalog.py after changing EMBEDDING_PROVIDER."
            )
        return (
            "Our catalog search is temporarily unavailable. Please try again in a moment.",
            [],
        )

    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    matches = []
    for document, metadata in zip(documents, metadatas):
        metadata = metadata or {}
        matches.append(
            {
                "id": str(metadata.get("id", "")),
                "name": str(metadata.get("name", "Item")),
                "price": float(metadata.get("price", 0)),
                "description": str(metadata.get("description", document)),
            }
        )

    fallback = _format_matches(matches)
    return _polish_response(query, matches, fallback), matches
