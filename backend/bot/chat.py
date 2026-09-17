import os
import re
from typing import Any, Optional

import chromadb

from common import embeddings

COLLECTION_NAME = "products"

# Embeddings alone leak across gender: "groom outfit" scored a women's kurta set
# above the sherwani, because the surrounding wedding vocabulary overwhelmed the
# one word that mattered. Metadata filtering is the reliable fix — the model is
# asked to rank within the right half of the catalogue rather than to infer it.
MENS_HINTS = {
    "men", "mens", "man", "male", "groom", "husband", "brother", "father",
    "dad", "papa", "boy", "boys", "gents", "sherwani", "jodhpuri", "pathani",
    "bandhgala", "dhoti", "churidar", "himself",
}
WOMENS_HINTS = {
    "women", "womens", "woman", "female", "bride", "wife", "sister", "mother",
    "mom", "mum", "girl", "girls", "ladies", "saree", "sari", "lehenga",
    "kurti", "anarkali", "sharara", "choli", "dupatta", "palazzo", "gown",
    "herself",
}


def _gender_filter(query: str) -> Optional[dict[str, str]]:
    """Narrow to one gender when the query names hints from exactly one set.

    Known limitation: only explicit words count, so "something for me and my
    wife" filters to Women — "wife" is a hint and "me" is not. Shopping for two
    people at once needs intent parsing rather than keywords; the fallback of
    showing one side is wrong but not confusing, and the customer can ask again.
    """
    words = set(re.findall(r"[a-z]+", query.lower()))
    mens = bool(words & MENS_HINTS)
    womens = bool(words & WOMENS_HINTS)

    if mens and not womens:
        return {"gender": "Men"}
    if womens and not mens:
        return {"gender": "Women"}
    return None


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
        where = _gender_filter(query)
        if where:
            print(f"[bot] filtering to {where['gender']}")
        result = collection.query(
            query_embeddings=[embedding], n_results=top_k, where=where
        )
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
