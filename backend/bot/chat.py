import os
import re
import time
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

CATEGORY_HINTS = {
    "kurta": "Kurta",
    "kurtas": "Kurta",
    "saree": "Saree",
    "sari": "Saree",
    "sarees": "Saree",
    "dupatta": "Dupatta",
    "dupattas": "Dupatta",
    "jacket": "Jacket",
    "jackets": "Jacket",
    "nehru": "Jacket",
    "bandhgala": "Jacket",
    "sherwani": "Sherwani",
    "sherwanis": "Sherwani",
    "lehenga": "Lehenga",
    "lehengas": "Lehenga",
    "gown": "Gown",
    "gowns": "Gown",
}


def _gender_filter(query: str) -> Optional[dict[str, str]]:
    """Narrow to one gender only when the query names exactly one side."""
    words = set(re.findall(r"[a-z]+", query.lower()))
    mens = bool(words & MENS_HINTS)
    womens = bool(words & WOMENS_HINTS)

    if mens and not womens:
        return {"gender": "Men"}
    if womens and not mens:
        return {"gender": "Women"}
    return None


def _category_filter(query: str) -> Optional[str]:
    words = re.findall(r"[a-z]+", query.lower())
    for word in words:
        if word in CATEGORY_HINTS:
            return CATEGORY_HINTS[word]
    return None


def _budget_limit(query: str) -> Optional[float]:
    match = re.search(
        r"\b(?:under|below|less than|up to|upto|within|budget(?: of)?)\s*"
        r"(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*([km])?\b",
        query.lower(),
    )
    if not match:
        return None

    amount = float(match.group(1).replace(",", ""))
    suffix = match.group(2)
    if suffix == "k":
        amount *= 1000
    elif suffix == "m":
        amount *= 1_000_000
    return amount


def _build_where(query: str) -> Optional[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    gender = _gender_filter(query)
    category = _category_filter(query)
    budget = _budget_limit(query)

    if gender:
        filters.append({"gender": {"$eq": gender["gender"]}})
    if category:
        filters.append({"category": {"$eq": category}})
    if budget is not None:
        filters.append({"price": {"$lte": budget}})

    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return {"$and": filters}


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

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    lines = ["Here are a few options that may suit you:"]
    for index, product in enumerate(matches, start=1):
        product_url = f"\n   {public_base_url}/product/{product['id']}" if public_base_url else ""
        lines.append(
            f"{index}. {product['name']} - Rs. {product['price']:.0f}\n"
            f"   {product['description']}{product_url}"
        )
    return "\n".join(lines)


def _polish_response(
    query: str,
    matches: list[dict[str, Any]],
    fallback: str,
    user_context: str | None = None,
) -> str:
    llm_api_key = os.getenv("LLM_API_KEY")
    api_key = llm_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key or not matches:
        return fallback

    from openai import OpenAI

    context = user_context or "No purchase history is available."
    base_url = os.getenv("LLM_BASE_URL")
    if llm_api_key and not base_url:
        base_url = "https://api.groq.com/openai/v1"
    client_options = {"api_key": api_key, "timeout": 8.0, "max_retries": 1}
    if base_url:
        client_options["base_url"] = base_url

    try:
        started_at = time.perf_counter()
        completion_options = {
            "model": os.getenv("LLM_MODEL", os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")),
            "temperature": 0.4,
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a concise WhatsApp shopping assistant for an ethnic wear store. "
                        "Write only a friendly introduction in one or two sentences. "
                        "Do not number products, list products, include prices, or invent "
                        "availability. Do not ask a follow-up question."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Customer request: {query}\n"
                        f"Customer context: {context}\n"
                        "The application will append the product list separately."
                    ),
                },
            ],
        }
        reasoning_effort = os.getenv("LLM_REASONING_EFFORT")
        if reasoning_effort:
            completion_options["reasoning_effort"] = reasoning_effort
        response = OpenAI(**client_options).chat.completions.create(**completion_options)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        print(f"[bot] LLM response time: {elapsed_ms:.0f} ms")
        content = response.choices[0].message.content
        intro = content.strip() if content else ""
        return f"{intro}\n\n{fallback}" if intro else fallback
    except Exception as exc:
        print(f"[bot] LLM response failed, using catalog response: {exc!r}")
        return fallback


def get_product_recommendations(
    query: str,
    top_k: int = 3,
    user_context: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Retrieve catalog matches and turn them into a customer-facing response."""
    try:
        collection = _get_collection()
        embedding = embeddings.embed_one(query)
        where = _build_where(query)
        if where:
            print(f"[bot] applying filters: {where}")
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
    return _polish_response(query, matches, fallback, user_context), matches
