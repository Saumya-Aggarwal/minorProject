"""Single-shot product answers: retrieval + an optional one-line LLM intro.

This is contract C1, get_product_recommendations(). Since the conversational
assistant (bot/assistant.py) arrived, it is the fallback path: used when no LLM
key is set, when the LLM is rate-limited or down, or by anything that needs a
plain answer. It must always answer, so nothing here may raise.

Retrieval lives in bot/retrieval.py (shared with the assistant and the website).
"""

import os
import time
from typing import Any

from bot import retrieval


def _first_sentence(text: str, limit: int = 140) -> str:
    sentence = text.split(". ")[0].rstrip(".") + "."
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rsplit(" ", 1)[0] + "…"


def format_product_lines(products: list[dict[str, Any]]) -> str:
    """The numbered list, built by code so names and prices are always real.

    Contract C1 invariant: the order here is the order of the returned matches,
    so the customer's "2" selects exactly the second line.
    """
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    lines = []
    for index, product in enumerate(products, start=1):
        link = f"\n   {base_url}/product/{product['id']}" if base_url else ""
        lines.append(
            f"{index}. *{product['name']}* — Rs. {product['price']:,.0f}\n"
            f"   {_first_sentence(product['description'])}{link}"
        )
    return "\n".join(lines)


def product_caption(product: dict[str, Any], index: int | None = None) -> str:
    """Caption under a product photo on WhatsApp: number, name, price, one line.

    Built by code from the catalogue, like the text list, so the photo and the
    price beside it can never disagree.
    """
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    name = f"{index}. {product['name']}" if index else product["name"]
    price = f"Rs. {product['price']:,.0f}"
    mrp = float(product.get("mrp") or 0)
    if mrp > product["price"]:
        price += f"   ~Rs. {mrp:,.0f}~   {product.get('discount_percent', 0)}% off"
    lines = [f"*{name}*", price, _first_sentence(product["description"])]
    if base_url:
        lines.append(f"{base_url}/product/{product['id']}")
    return "\n".join(lines)


def _intro(query: str, products: list[dict[str, Any]], user_context: str | None) -> str:
    """One sentence introducing the list, or "" if no LLM is available.

    The model sees the products it is introducing — earlier it saw only the
    question, so it invited "tell me your preferences" above a finished list
    and called a single dupatta "a great selection".
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key or not products:
        return ""
    from openai import OpenAI

    base_url = os.getenv("LLM_BASE_URL") or ("https://api.groq.com/openai/v1" if os.getenv("LLM_API_KEY") else None)
    names = "; ".join(f"{p['name']} ({p['category']}, {', '.join(p['occasion'][:3])})" for p in products)
    options: dict[str, Any] = {
        "model": os.getenv("LLM_MODEL", os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")),
        "temperature": 0.4,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": (
                "You write the single opening line above a product list in a WhatsApp chat "
                "for an Indian ethnic wear boutique. One sentence, at most 22 words. No greeting, "
                "no question, no prices, no product names, no emojis. Describe what is being "
                "shown and why it fits the request. Never imply there are more items than listed."
            )},
            {"role": "user", "content": (
                f"Customer asked: {query}\n"
                f"Showing {len(products)} item(s): {names}\n"
                f"Customer context: {user_context or 'none'}"
            )},
        ],
    }
    if os.getenv("LLM_REASONING_EFFORT"):
        options["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT")
    client_args: dict[str, Any] = {"api_key": api_key, "timeout": 8.0, "max_retries": 0}
    if base_url:
        client_args["base_url"] = base_url
    try:
        started = time.perf_counter()
        response = OpenAI(**client_args).chat.completions.create(**options)
        print(f"[bot] intro in {(time.perf_counter() - started) * 1000:.0f} ms")
        text = (response.choices[0].message.content or "").strip().strip('"')
        # A question or a ramble means the model ignored the brief; drop it
        return "" if "?" in text or len(text) > 220 else text
    except Exception as exc:
        print(f"[bot] intro skipped: {exc!r}")
        return ""


def get_product_recommendations(
    query: str,
    top_k: int = 3,
    user_context: str | None = None,
    user_id: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Retrieve catalogue matches and turn them into a customer-facing reply.

    user_id (optional) applies that customer's "Not for me" choices.
    """
    try:
        result = retrieval.search(query, k=top_k, user_id=user_id)
    except Exception as exc:
        print(f"[bot] retrieval failed: {exc!r}")
        return ("Our catalogue search is having a moment. Please try again shortly.", [])

    products = result.products
    if not products:
        return (
            "I could not match that to anything in our collection. Tell me what it is for — "
            "the occasion, who will wear it, or a budget — and I will look again.",
            [],
        )

    # When a filter was relaxed the note says so; an LLM intro there once called
    # Rs. 8,999 sherwanis "each under Rs. 3000", so it is skipped
    intro = "" if result.relaxed else _intro(query, products, user_context)
    parts = [p for p in (result.note(), intro) if p]
    parts.append(format_product_lines(products))
    matches = [
        {"id": p["id"], "name": p["name"], "price": float(p["price"]), "description": p["description"]}
        for p in products
    ]
    return "\n\n".join(parts), matches
