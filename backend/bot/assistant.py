"""The conversational shopping assistant: an LLM that uses tools.

Instead of treating every message as a search query, the model reads the
conversation, decides what the customer means, and calls tools: search the
catalogue, look up a product, read the customer's orders or cart, add to the
cart. It asks a question or two when a request is vague ("something for my
wedding" — whose outfit? which function? budget?) and searches when it is not.

What the model is NOT trusted with:
  * facts: product names, prices, sizes, order totals and payment links are
    written into the reply by code (send_reply attaches them), never by the model
  * other customers: every tool is bound to the sender's user_id on the server;
    no tool takes a user id, so no message can reach someone else's data
  * money: it can add to the cart, but paying is always the explicit CHECKOUT
    command, which answers with Razorpay's Pay Now button

Any failure — no key, timeout, rate limit, a malformed tool call — raises
AssistantUnavailable, and the webhook answers with the plain retrieval path
(bot/chat.py) instead. The customer always gets a reply.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import catalog
import repository as repo
from bot import retrieval
from selection import match_size, parse_bare_size

MAX_ROUNDS = 5            # model calls per customer message
TURN_BUDGET_S = 25.0      # stop and fall back rather than keep the customer waiting
MAX_PRODUCTS = 4
HISTORY_MESSAGES = 8      # remembered messages sent to the model each call
MAX_RATE_WAIT_S = 8.0     # wait this long at most for a rate limit to clear


class AssistantUnavailable(Exception):
    """The LLM could not produce an answer; use the fallback path."""


@dataclass
class AssistantReply:
    message: str                                   # the model's words, cleaned for WhatsApp
    products: list[dict[str, Any]] = field(default_factory=list)   # catalogue records, in order
    attach: Optional[str] = None                   # "orders" | "cart": rendered by the webhook
    tools_used: list[str] = field(default_factory=list)
    detail: bool = False                           # one product described: photo + the model's words
    # Lasting facts to store on the customer's profile: {"gender": ..., "note": ...}
    remember: dict[str, str] = field(default_factory=dict)


def enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))


# --- prompt ---------------------------------------------------------------------

SYSTEM_PROMPT = """You are the stylist for Kurta & Co., an Indian ethnic wear boutique, on WhatsApp.

WE SELL ONLY Indian ethnic wear, for every function (haldi, mehendi, sangeet, wedding, reception, festivals, office). Men: kurtas, kurta pyjama sets, dhoti kurta, Nehru jackets, waistcoats, sherwanis, Jodhpuri/bandhgala suits, Indo-Western, Pathani, churidar, stole; safa (turban), mojari and kolhapuri (UK sizes). Women: sarees, lehengas, chaniya choli, Anarkali, salwar suits, sharara/gharara, co-ord sets, kurtis, gowns, Indo-Western, dupattas; juttis, flats, heels (UK sizes), clutches and bags. No western wear or jewellery.

HOW TO HELP
- Vague request ("something for my wedding"): ask at most two short questions about what is still unknown: who wears it (groom, bride, guest), which function (haldi, mehendi, sangeet, ceremony, reception), budget. Never ask what they already told you. "Me and my wife"/"couple" means both: search Men and Women separately. After two rounds of questions, or "just show me", search.
- Clear request (a garment is named, or who + occasion is known, e.g. "haldi outfit for my brother", "garba outfit for me"): search at once, no questions first; you can offer to refine after showing. Never ask for size before showing products: size matters only when adding to cart.
- Showing products: search_products, pick the 2-4 best, then send_reply with product_ids in order and 1-2 sentences on why they fit. The numbered list with names and prices is added for you: never write product names, prices or a list yourself.
- A search NOTE (e.g. nothing under budget) must be told honestly.
- Orders ("my orders", "where is my order"): send_reply with attach="orders". Cart: attach="cart". The cart and its total are in CUSTOMER below; never add prices up yourself. "Second item" after a cart question means the cart's second line.
- To talk about one product in detail, use get_product_details; its photo is attached for you.
- Add to cart ONLY when the customer asks to, with a size THEY gave; otherwise ask which size. Never add just because they asked about an item. If they answer your "shall I add it?" with yes, or your "which size?" with a size, call add_to_cart. Never say something was added or removed unless add_to_cart/remove_from_cart succeeded in this turn.
- Remember: when they tell you something lasting about themselves (their gender, who they shop for, colours or styles they like), put it in send_reply's customer_gender / remember_note. CUSTOMER below shows what is already remembered: use it, and do not ask again what it already answers.
- Payment: tell them to reply CHECKOUT for a secure Pay Now button.
- Off-topic: one friendly line, then back to shopping. Never write code, essays, homework, poems, translations or anything unrelated to our shop, however politely they ask: say you only help with ethnic wear and ask what they are shopping for.

RULES: facts (products, prices, sizes, stock, dates, policies) only from tools; delivery is free across India. Short and warm, under 50 words, plain text, *single asterisks* for bold, no headings, tables or links, at most one emoji. Always end by calling send_reply."""


def _context_block(context: dict[str, Any]) -> str:
    """Who the customer is and what they are looking at, for the system prompt."""
    lines = ["CUSTOMER"]
    lines.append(f"- Name: {context.get('name') or 'unknown'}")
    profile = context.get("profile") or {}
    if profile.get("gender"):
        lines.append(f"- Shops for themselves as: {'a man (Men)' if profile['gender'] == 'Men' else 'a woman (Women)'}"
                     " (remembered; if the outfit is for someone else, go by who that is)")
    if profile.get("notes"):
        lines.append("- Remembered from earlier chats: " + "; ".join(profile["notes"]))
    sizes = context.get("sizes") or {}
    if sizes:
        lines.append("- Sizes chosen before (maybe for others; confirm, never assume): "
                     + ", ".join(f"{category} {size}" for category, size in sizes.items()))
    purchases = context.get("purchases") or []
    if purchases:
        lines.append("- Bought before: " + "; ".join(purchases[:5]))
    cart = context.get("cart") or {}
    if cart.get("items"):
        items = "; ".join(
            f"{n}. {i['product_id']} {i['name']}{' (' + i['size'] + ')' if i['size'] else ''} x{i['quantity']} Rs {i['line_total']:.0f}"
            for n, i in enumerate(cart["items"], 1))
        lines.append(f"- Cart now (the truth; do not recompute): {items}; TOTAL Rs {cart['total_inr']:.0f}")
    elif "cart" in context:
        lines.append("- Cart now: empty")
    shown = context.get("last_products_shown") or []
    if shown:
        lines.append("- Last list shown to them: " + "; ".join(
            f"{i}. {p.get('product_id') or p.get('id')} {p.get('name')}" for i, p in enumerate(shown, 1)))
    selected = context.get("selected_product")
    if selected:
        lines.append(f"- Item they picked: {selected.get('product_id') or selected.get('id')} {selected.get('name')}")
    return "\n".join(lines)


# --- tools ----------------------------------------------------------------------

def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


# Optional fields accept null and no enums are used: Groq validates the model's
# arguments against this schema and rejects the whole call (HTTP 400) for
# `"attach": ""` or `"max_price": null`, which the model sends often. Allowed
# values are checked by our code instead, which ignores anything unexpected.
_TEXT = {"type": ["string", "null"]}
_NUMBER = {"type": ["number", "string", "null"]}
_LIST = {"type": ["array", "null"], "items": {"type": "string"}}

TOOLS = [
    _schema("search_products",
            "Search the catalogue by meaning. Returns product ids with name, category, price, colour, occasions and sizes in stock.",
            {
                "query": {"type": "string", "description": "What to look for, in words, e.g. 'light festive kurta for a daytime mehendi'"},
                "gender": {**_TEXT, "description": "Men, Women or Both"},
                "categories": {**_LIST, "description": "Garment types, e.g. ['Sherwani'] or ['Saree','Lehenga']"},
                "max_price": {**_NUMBER, "description": "Budget ceiling in rupees"},
                "min_price": _NUMBER,
            }, ["query"]),
    _schema("get_product_details", "Full details of one product: sizes in stock, fabric, care, delivery and returns.",
            {"product_id": {"type": "string"}}, ["product_id"]),
    _schema("get_my_orders", "This customer's recent orders with status, items and delivery date.", {}, []),
    _schema("get_my_cart", "What is in this customer's cart now.", {}, []),
    _schema("add_to_cart", "Add a product to this customer's cart in a size.",
            {"product_id": {"type": "string"}, "size": _TEXT}, ["product_id"]),
    _schema("remove_from_cart", "Remove a product from this customer's cart, when they ask to.",
            {"product_id": {"type": "string"}}, ["product_id"]),
    _schema("hide_product", "The customer does not like a product shown to them ('not the second one', "
            "'too flashy'): never show it to them again. Then offer alternatives.",
            {"product_id": {"type": "string"}, "reason": _TEXT}, ["product_id"]),
    _schema("send_reply",
            "Send your reply and end the turn. product_ids adds a numbered product list. attach is 'orders' or "
            "'cart' ONLY when the customer asked to see their orders or cart; otherwise leave it out.",
            {
                "message": {"type": "string"},
                "product_ids": _LIST,
                "attach": _TEXT,
                "customer_gender": {**_TEXT, "description": "Men or Women, ONLY if the customer said it about "
                                    "THEMSELVES (\"I'm a guy\", \"only male options\" when shopping for "
                                    "themselves). Never for someone else's outfit."},
                "remember_note": {**_TEXT, "description": "A lasting fact about the customer worth remembering "
                                  "next time, under 12 words, e.g. 'prefers pastel colours', 'shops for his "
                                  "brother'. Leave out if nothing new."},
            }, ["message"]),
]


def _sizes_in_stock(product: dict[str, Any]) -> list[str]:
    stock = product.get("stock") or {}
    return [s for s in product.get("sizes_available", []) if stock.get(s, 1) > 0]


def _product_line(p: dict[str, Any]) -> str:
    return (f"{p['id']} | {p['name']} | {p['gender']} {p['category']} | Rs {p['price']:.0f} | "
            f"{p['color']} | {', '.join(p['occasion'][:4])} | sizes: {', '.join(_sizes_in_stock(p))}")


def tool_search_products(user_id: int, query: str = "", gender: Any = None, categories: Any = None,
                         max_price: Any = None, min_price: Any = None, **_: Any) -> str:
    # Start from what the words say, then let the model's explicit choices win
    parsed = retrieval.parse_query(query)
    given = retrieval.normalise_filters(gender, categories, max_price, min_price)
    filters = retrieval.Filters(
        gender=given.gender if (given.gender or given.both_genders) else parsed.gender,
        both_genders=given.both_genders or (parsed.both_genders and not given.gender),
        categories=given.categories or parsed.categories,
        max_price=given.max_price or parsed.max_price,
        min_price=given.min_price or parsed.min_price,
        occasion=parsed.occasion,
    )
    result = retrieval.search(query, filters, k=6, user_id=user_id)
    if not result.products:
        return "No matches. Ask the customer for more detail, or search more broadly."
    lines = [_product_line(p) for p in result.products]
    if result.relaxed:
        lines.insert(0, f"NOTE: {result.note()}")
    return "\n".join(lines)


def tool_get_product_details(user_id: int, product_id: str = "", **_: Any) -> str:
    p = catalog.get_by_id(str(product_id).strip().upper())
    if p is None:
        return f"No product with id {product_id!r}."
    return "\n".join([
        _product_line(p),
        f"MRP Rs {p['mrp']:.0f} ({p['discount_percent']}% off) | rating {p['rating']} ({p['rating_count']})",
        f"Fabric: {p['fabric']} | fit: {p['fit']} | care: {p['care']}",
        "Highlights: " + "; ".join(p["highlights"][:3]),
        f"Delivery in {p['delivery_days']} days, free | returns within {p['return_days']} days",
    ])


def tool_get_my_orders(user_id: int, **_: Any) -> str:
    history = repo.get_order_history(user_id, 5)
    if not history:
        return "No orders yet."
    lines = []
    for summary in history:
        order = repo.get_order(summary["order_id"]) or {}
        status = {"captured": "paid", "failed": "cancelled"}.get(summary["status"], "awaiting payment")
        items = ", ".join(
            f"{i['product_name']}{' (' + i['size'] + ')' if i['size'] else ''} x{i['quantity']}"
            for i in summary["items"])
        arriving = f" | arriving by {order['deliver_by']:%d %b}" if status == "paid" and order.get("deliver_by") else ""
        lines.append(f"#{summary['order_id']} | {summary['created_at'][:10]} | {status} | "
                     f"Rs {summary['total_inr']:.0f} | {items}{arriving}")
    return "\n".join(lines)


def tool_get_my_cart(user_id: int, **_: Any) -> str:
    cart = repo.get_cart(user_id)
    if not cart["items"]:
        return "Cart is empty."
    lines = [f"{n}. {i['product_id']} {i['name']} | size {i['size'] or 'not chosen'} | "
             f"{i['quantity']} x Rs {i['price_inr']:.0f}" for n, i in enumerate(cart["items"], 1)]
    lines.append(f"Total Rs {cart['total_inr']:.0f}")
    return "\n".join(lines)


# Seen live: asked "tell me more about it", the model also added the item to the
# cart, in a size nobody chose. Changing the cart needs the customer to have asked.
_WANTS_TO_ADD = re.compile(
    r"\b(add|cart|bag|basket|buy|take|order|book|i'?ll\s+have|i\s+want|want\s+(it|this|that|one|the)|"
    r"get\s+(it|this|that|me|one)|put\s+(it|this|that))\b", re.I)


def tool_add_to_cart(user_id: int, product_id: str = "", size: str = "", *,
                     customer_text: str = "", customer_recent: str = "", **_: Any) -> str:
    """customer_text / customer_recent are supplied by respond(), never the model:
    the current message must ask for it, and the size must be one they typed
    (now or in their last few messages), not one the model picked."""
    if not _WANTS_TO_ADD.search(customer_text):
        return "ERROR: the customer has not asked to add anything. Do not add; answer their message."
    product = catalog.get_by_id(str(product_id).strip().upper())
    if product is None:
        return f"ERROR: no product with id {product_id!r}. Use an id from search_products."
    sizes = _sizes_in_stock(product)
    if not sizes:
        return "ERROR: out of stock."
    if len(sizes) == 1:
        size = sizes[0]          # Free Size: nothing to choose
    elif size and not re.search(rf"(?<!\w){re.escape(str(size).strip())}(?!\w)", customer_recent, re.I):
        return f"ERROR: the customer has not said size {size}. Ask them which size they want."
    chosen = ""
    if size:
        chosen = match_size(str(size), sizes) or ""
        if not chosen:
            return f"ERROR: size {size!r} is not available. In stock: {', '.join(sizes)}. Ask the customer."
    elif len(sizes) > 1:
        return f"ERROR: ask the customer for a size first. In stock: {', '.join(sizes)}."
    else:
        chosen = sizes[0]
    cart = repo.add_to_cart(user_id, product["id"], chosen)
    return (f"Added {product['name']} (size {chosen}). Cart now has {cart['count']} item(s), "
            f"total Rs {cart['total_inr']:.0f}. They can reply CHECKOUT to pay.")


_WANTS_TO_REMOVE = re.compile(
    r"\b(remove|delete|drop|take\s+(it|this|that)?\s*out|don'?t\s+want|do\s+not\s+want|cancel)\b", re.I)


def tool_remove_from_cart(user_id: int, product_id: str = "", *, customer_text: str = "",
                          customer_recent: str = "", **_: Any) -> str:
    if not _WANTS_TO_REMOVE.search(customer_text):
        return "ERROR: the customer has not asked to remove anything."
    wanted = str(product_id).strip().upper()
    cart = repo.get_cart(user_id)
    lines = [i for i in cart["items"] if i["product_id"] == wanted]
    if not lines:
        return f"ERROR: {product_id!r} is not in the cart. Cart: " + (
            ", ".join(f"{i['product_id']} {i['name']}" for i in cart["items"]) or "empty")
    for line in lines:
        repo.remove_from_cart(user_id, line["cart_item_id"])
    after = repo.get_cart(user_id)
    return (f"Removed {lines[0]['name']}. Cart now has {after['count']} item(s), "
            f"total Rs {after['total_inr']:.0f}.")


_DISLIKES = re.compile(
    r"\b(not\s+for\s+me|don'?t\s+(?:like|love|want|show)|do\s+not\s+(?:like|want|show)|dislike|hate|"
    r"not\s+(?:the|that|this)|no\s+to|hide|not\s+interested|too\s+\w+|ugly|boring)\b", re.I)


def tool_hide_product(user_id: int, product_id: str = "", reason: str = "", *, customer_text: str = "",
                      customer_recent: str = "", **_: Any) -> str:
    """The typed version of the "Not for me" button: personal, never global."""
    if not _DISLIKES.search(customer_text):
        return "ERROR: the customer has not said they dislike anything."
    product = catalog.get_by_id(str(product_id).strip().upper())
    if product is None:
        return f"ERROR: no product with id {product_id!r}."
    import training
    training.rate("customer", -1, customer_recent[-300:], product["id"], None, user_id,
                  f"Typed: {str(reason or customer_text)[:200]}")
    return f"Hidden {product['name']} from this customer's future results."


TOOL_FUNCTIONS: dict[str, Callable[..., str]] = {
    "hide_product": tool_hide_product,
    "remove_from_cart": tool_remove_from_cart,
    "search_products": tool_search_products,
    "get_product_details": tool_get_product_details,
    "get_my_orders": tool_get_my_orders,
    "get_my_cart": tool_get_my_cart,
    "add_to_cart": tool_add_to_cart,
}


# --- the model --------------------------------------------------------------------

def _models() -> list[str]:
    """Primary model, then fallbacks. On Groq each model has its own free-tier
    budget (8,000 tokens per minute each, measured), so a second and third
    model roughly triple how fast a conversation can go before anything waits."""
    primary = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
    default = "openai/gpt-oss-120b,qwen/qwen3.8-27b" if os.getenv("LLM_API_KEY") else ""
    fallbacks = [m.strip() for m in os.getenv("LLM_FALLBACK_MODEL", default).split(",") if m.strip()]
    return list(dict.fromkeys([primary, *fallbacks]))


def _complete(model: str, messages: list[dict[str, Any]]) -> Any:
    """One chat completion with tools. Returns the message object. Tests replace this."""
    from openai import OpenAI

    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or ("https://api.groq.com/openai/v1" if os.getenv("LLM_API_KEY") else None)
    client_args: dict[str, Any] = {"api_key": api_key, "timeout": 12.0, "max_retries": 0}
    if base_url:
        client_args["base_url"] = base_url
    options: dict[str, Any] = {
        "model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
        "temperature": 0.3, "max_tokens": 1500,
    }
    if os.getenv("LLM_REASONING_EFFORT"):
        options["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT")
    response = OpenAI(**client_args).chat.completions.create(**options)
    usage = getattr(response, "usage", None)
    if usage is not None:
        print(f"[assistant]   tokens: {usage.prompt_tokens} in, {usage.completion_tokens} out")
    return response.choices[0].message


def _clean(text: str) -> str:
    """Model output made safe for WhatsApp: its bold, no headings or links, bounded."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)   # reasoning leaks (qwen)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"【[^】]*】", "", text)                      # stray citation marks
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]+\)", r"\1", text)   # no model-made links
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= 900 else text[:897].rsplit(" ", 1)[0] + "…"


# Attach orders or the cart only when the customer asked. The model sometimes
# sets attach="cart" on an unrelated question, and "Your cart is empty" under
# "Who is the outfit for?" reads like a glitch.
_ASKS_FOR = {
    "orders": re.compile(r"\b(orders?|bought|purchas\w*|deliver\w*|track\w*|ship\w*|paid|payment)\b", re.I),
    "cart": re.compile(r"\b(cart|bag|basket|added)\b", re.I),
}
# A numbered line in the model's own words duplicates the list code attaches
_LIST_LINE = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+.*$", re.MULTILINE)


def _without_listing(message: str, products: list[dict[str, Any]]) -> str:
    """Drop the model's own rendition of the list code is about to attach.

    Seen live despite the prompt: "Here are options for the ceremony: 1. Royal
    Blue Wedding Sherwani – classic. 2. Champagne Gold…", then the real list
    below it. Keep the words before the first item or product name.
    """
    message = _LIST_LINE.sub("", message)
    # "Here are options for the ceremony:" introduced the removed lines
    message = re.sub(r":[ \t]*$", ".", message, flags=re.MULTILINE)
    lowered = message.lower()
    cuts = [m.start() for m in [re.search(r"(?:^|\s)1[.)]\s", message)] if m]
    named = [lowered.find(p["name"].lower()) for p in products if p["name"].lower() in lowered]
    # One product named in a sentence is fine ("the royal blue one suits a day
    # ceremony"); several, or names with prices, is a listing
    if len(named) >= 2 or (named and re.search(r"(?:rs\.?|₹|inr)\s*\d", lowered)):
        cuts += named
    cut = min(cuts) if cuts else -1
    if cut > 0:
        message = message[:cut].rstrip(" :–—-") + ("." if not message[:cut].rstrip().endswith((".", "!")) else "")
    elif cut == 0:
        message = ""
    return message


# A shop assistant that writes code is a party trick, and a demo risk: asked
# "can u write a python code for 2+2" it answered with a snippet.
_LOOKS_LIKE_CODE = re.compile(
    r"```"                                              # a fenced code block
    r"|\b(?:print|printf|console\.log|System\.out\.println)\s*\("
    r"|\bdef\s+\w+\s*\("
    r"|^\s*(?:import\s+\w+|from\s+\w+\s+import\b)"      # not "we import our silks"
    r"|\bpublic\s+static\s+void\b|^\s*#include\b|\bSELECT\b[^.]*\bFROM\b"
    r"|</[a-z]+>|<\s*(?:html|body|div|script)\b", re.I | re.M)
OFF_TOPIC = ("I only help with our ethnic wear, so I cannot help with that. "
             "Tell me what you are shopping for and I will pick a few pieces.")


def _finish(args: dict[str, Any], customer_text: str) -> tuple[Optional[AssistantReply], str]:
    """Validate send_reply. Returns (reply, "") or (None, error for the model)."""
    raw_ids = args.get("product_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = re.findall(r"EW\d{3}", raw_ids.upper())
    ids = [str(i).strip().upper() for i in raw_ids if str(i).strip()]
    products = [p for p in (catalog.get_by_id(i) for i in dict.fromkeys(ids)) if p is not None]
    products = [p for p in products if _sizes_in_stock(p)][:MAX_PRODUCTS]
    if ids and not products:
        return None, (f"ERROR: none of {ids} are products we stock. Only use ids returned by "
                      "search_products, then call send_reply again.")
    message = str(args.get("message") or "")
    if products:
        message = _without_listing(message, products)
    message = _clean(message)
    attach = str(args.get("attach") or "").strip().lower()
    attach = attach if attach in _ASKS_FOR and _ASKS_FOR[attach].search(customer_text) else None
    if not message and not products and not attach:
        return None, "ERROR: the message is empty. Call send_reply with a message."
    remember = {key: str(args[field_name]).strip() for key, field_name in
                (("gender", "customer_gender"), ("note", "remember_note")) if args.get(field_name)}
    return AssistantReply(message=message, products=products, attach=attach, remember=remember), ""


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        value = raw
    else:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict):
        return {}
    # Drop empty values and junk keys ({"": ""} has been seen) before calling tools
    return {k: v for k, v in value.items() if k and v not in (None, "", [])}


def _salvage(exc: Exception) -> Any:
    """Rebuild a tool call Groq rejected on a technicality, or None.

    Groq checks tool calls before returning them and answers 400
    "tool_use_failed" with the model's attempt in failed_generation. Seen live:
    "send_reply<|channel|>commentary" (a chat-template token leaking into the
    name) and null or "" in optional fields. The intent is clear, so repair it.
    """
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else None
    generation = (error or {}).get("failed_generation") if isinstance(error, dict) else None
    if not generation:
        return None
    try:
        attempt = json.loads(generation)
    except (TypeError, ValueError):
        return None
    attempts = attempt if isinstance(attempt, list) else [attempt]
    tool_calls = []
    for n, item in enumerate(attempts):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).split("<|")[0].strip()
        if name not in TOOL_FUNCTIONS and name != "send_reply":
            continue
        arguments = item.get("arguments", item.get("parameters", {}))
        tool_calls.append(_Call(id=f"salvaged_{n}", name=name, arguments=json.dumps(_arguments(arguments))))
    if not tool_calls:
        return None
    print(f"[assistant] repaired a rejected tool call: {[c.function.name for c in tool_calls]}")
    return _Message(content="", tool_calls=tool_calls)


@dataclass
class _Function:
    name: str
    arguments: str


class _Call:
    def __init__(self, id: str, name: str, arguments: str):
        self.id, self.function = id, _Function(name, arguments)


@dataclass
class _Message:
    content: str
    tool_calls: list[Any]


def _ask_model(models: list[str], messages: list[dict[str, Any]], round_number: int) -> Any:
    """One model message. Retries once, repairs rejected tool calls, then tries
    the fallback model; raises AssistantUnavailable when nothing works.

    `models` is mutated: a model that fails for capacity reasons is skipped for
    the rest of this customer message.
    """
    last_error: Optional[Exception] = None
    retried = False
    rate_limited: dict[str, float] = {}      # model -> seconds Groq asked us to wait
    waited = False
    while models:
        model = models[0]
        try:
            started = time.perf_counter()
            message = _complete(model, messages)
            print(f"[assistant] {model} round {round_number}: {(time.perf_counter() - started) * 1000:.0f} ms")
            return message
        except Exception as exc:
            last_error = exc
            text = str(exc)
            print(f"[assistant] {model} failed: {text[:200]}")
            if "401" in text or "invalid_api_key" in text:
                raise AssistantUnavailable("LLM key rejected") from exc
            repaired = _salvage(exc)
            if repaired is not None:
                return repaired
            if "tool_use_failed" in text and not retried:
                retried = True           # a one-off bad generation: same model, once more
                continue
            wait = re.search(r"try again in ([\d.]+)s", text)
            if "429" in text and wait:
                rate_limited[model] = float(wait.group(1))
            models.pop(0)                # rate limit, outage, or repeated bad output
            retried = False
            if not models and rate_limited and not waited:
                # Every model is over its per-minute budget. Groq says when the
                # soonest frees up; a few seconds' wait beats a worse answer.
                model, seconds = min(rate_limited.items(), key=lambda kv: kv[1])
                if seconds <= MAX_RATE_WAIT_S:
                    print(f"[assistant] all models rate-limited; waiting {seconds:.1f}s for {model}")
                    time.sleep(seconds + 0.3)
                    models.append(model)
                    waited = True
    raise AssistantUnavailable(f"no model answered: {last_error!r}")


# --- checking what the model wrote -------------------------------------------------

_AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand)?", re.IGNORECASE)
_NUMBER_IN_TEXT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand)?", re.IGNORECASE)
_PRODUCT_CODE = re.compile(r"\bEW\d{3}\b", re.IGNORECASE)


def _value(number: str, unit: Optional[str]) -> int:
    value = float(number.replace(",", ""))
    return round(value * 1000 if unit else value)


def _unverified_amounts(message: str, evidence: list[str]) -> list[str]:
    """Rupee amounts in the message that appear nowhere we can vouch for.

    Allowed: any catalogue price or MRP, and any number in the customer's own
    words, the context we gave the model (the real cart total) or a tool result
    from this turn. "Your current total is Rs 17,498" — a sum the model worked
    out itself, and wrongly — fails, and it is told to try again.
    """
    known = {round(p["price"]) for p in catalog.get_all()} | {round(p["mrp"]) for p in catalog.get_all()}
    for text in evidence:
        known |= {_value(n, u) for n, u in _NUMBER_IN_TEXT.findall(text or "")}
    wrong = []
    for number, unit in _AMOUNT.findall(message):
        if _value(number, unit) not in known:
            wrong.append(f"Rs {number}{unit or ''}")
    return list(dict.fromkeys(wrong))


def _drop_sentences_with(message: str, amounts: list[str]) -> str:
    """Last resort after one retry: remove the sentences carrying bad amounts."""
    numbers = [a.split(" ", 1)[1] for a in amounts]
    sentences = re.split(r"(?<=[.!?])\s+", message)
    kept = [s for s in sentences if not any(n in s for n in numbers)]
    return " ".join(kept).strip() or "Reply CART to see your cart and its total."


def _attach_mentioned(reply: AssistantReply, changed_cart: bool = False) -> AssistantReply:
    """Products the model described in its own words get the real photo and list.

    Seen live: asked for anniversary gifts, it wrote "EW020 Emerald Kanjivaram
    Silk Saree – Rs 9499 ..." as plain text: no photo, and "2" selected nothing.
    Two or more products named -> the numbered list (with photos) is attached
    and the model's version removed. One product -> its photo goes with the
    description (detail).
    """
    if reply.products or reply.attach or changed_cart:
        # "Added the Kanjivaram saree to your cart" is a confirmation, not a pitch
        reply.message = _PRODUCT_CODE.sub("", reply.message).replace("  ", " ").strip()
        return reply
    lowered = reply.message.lower()
    found: list[tuple[int, dict[str, Any]]] = []
    for product in catalog.get_all():
        spots = [m.start() for m in re.finditer(rf"\b{product['id']}\b", reply.message, re.IGNORECASE)]
        if product["name"].lower() in lowered:
            spots.append(lowered.find(product["name"].lower()))
        if spots:
            found.append((min(spots), product))
    found.sort(key=lambda pair: pair[0])
    products = [p for _, p in found if _sizes_in_stock(p)][:MAX_PRODUCTS]
    if len(products) >= 2:
        reply.products = products
        reply.message = _without_listing(_PRODUCT_CODE.sub("", reply.message), products)
        reply.message = re.sub(r"[ \t]{2,}", " ", reply.message).strip()
    elif len(products) == 1:
        reply.products, reply.detail = products, True
        reply.message = re.sub(r"[ \t]{2,}", " ", _PRODUCT_CODE.sub("", reply.message)).strip(" –—-\n")
    return reply


def _owner_guidance(text: str) -> str:
    """What the store owner taught for questions like this one (training.py).

    Replies the owner rated good become examples to imitate; notes on replies
    rated bad become rules. Only the closest two of each, to spare tokens.
    """
    try:
        import training
        taught = training.guidance(text)
    except Exception:
        return ""
    lines = []
    if taught["examples"]:
        lines.append("\n\nREPLIES THE OWNER APPROVED FOR SIMILAR QUESTIONS (match this style):")
        lines += [f"---\n{example}" for example in taught["examples"]]
    if taught["notes"]:
        lines.append("\nOWNER'S NOTES FOR SIMILAR QUESTIONS (follow these):")
        lines += [f"- {note}" for note in taught["notes"]]
    return "\n".join(lines)


# --- conversation rules enforced in code ----------------------------------------------
# Each of these was seen live on 22 Sep:
#   bot "Would you like me to add it (XL)?"  customer "yes pls add"  -> refused, asked again
#   customer "46" to "which size?"           -> bot "is now in your cart" (nothing was added)
#   "something for a night wedding"          -> a bridal lehenga, without asking who it is for

_AFFIRM = re.compile(r"^\s*(?:yes|yeah|yea|yep|yup|ya|haan|han|ha|sure|ok+|okay|ohk|okk|pls|please|go\s+ahead|"
                     r"do\s+it|confirm|correct|right|perfect|great)\b", re.I)
_OFFERED_TO_ADD = re.compile(r"\b(add|cart|bag|which\s+size|what\s+size|size\s+would|sizes?\b)", re.I)
_CLAIMS_CART_CHANGE = re.compile(
    r"\b(added|i'?ve\s+added|i\s+have\s+added|(?:is|are)\s+now\s+in\s+your\s+(?:cart|bag)|"
    r"in\s+your\s+(?:cart|bag)\s+now|removed|taken\s+(?:it\s+)?out|"
    # A promise is as good as a claim to the customer: live, "Sure, I'll add the
    # Silver Paisley Mojari in UK 11 to your cart" was followed by an empty cart
    r"i'?ll\s+add|i\s+will\s+add|let\s+me\s+add|going\s+to\s+add|adding\s+(?:it|this|that|the))\b", re.I)
_OTHER_PERSON = re.compile(
    r"\b(brother|sister|wife|husband|mother|mom|mum|father|dad|papa|son|daughter|friend|cousin|uncle|aunt|"
    r"bhai|didi|bhabhi|jiju|fiance|fiancee|him|her|his|their|nephew|niece|grand\w*|boss|colleague|"
    r"girlfriend|boyfriend|parents?|family)\b", re.I)
_GENDER_WORDS = {"Men": r"\b(male|man|men|mens|guy|boy|gents?|groom)\b",
                 "Women": r"\b(female|woman|women|womens|girl|lady|ladies|bride)\b"}
_SAYS_OWN_GENDER = re.compile(r"\bi\s*(?:'?m|am)\s+(?:a\s+)?(male|man|guy|boy|groom|female|woman|girl|lady|bride)\b", re.I)
_SHOPPING = re.compile(r"\b(buy|wear|outfits?|dress(?:es)?|suggest|recommend|options|ideas?|looking\s+for|"
                       r"shopping|something\s+for|anything\s+for)\b", re.I)


def _last_bot_message(history: list[dict[str, Any]]) -> str:
    return next((str(m["content"]) for m in reversed(history) if m.get("role") == "assistant"), "")


def _consent(text: str, history: list[dict[str, Any]]) -> str:
    """The customer's words as the add_to_cart gate should read them.

    "yes" or "XL" alone does not ask to add anything, but it does when it
    answers our own "shall I add it?" / "which size?"."""
    if (_AFFIRM.match(text) or parse_bare_size(text)) and _OFFERED_TO_ADD.search(_last_bot_message(history)):
        return f"{text} (add to cart)"
    return text


def _gender(value: str) -> Optional[str]:
    lowered = value.strip().lower()
    return "Men" if lowered in ("men", "man", "male", "m") else "Women" if lowered in ("women", "woman", "female", "w") else None


def _facts_to_keep(reply: AssistantReply, text: str, customer_recent: str) -> dict[str, str]:
    """What may go on the profile. The customer's gender only when their own
    words state it and no one else's outfit is being discussed: "only male
    options" while shopping for a brother is about the brother."""
    keep: dict[str, str] = {}
    own = _SAYS_OWN_GENDER.search(text)
    if own:
        keep["gender"] = "Men" if re.match(_GENDER_WORDS["Men"], own.group(1), re.I) else "Women"
    else:
        claimed = _gender(reply.remember.get("gender", ""))
        if claimed and re.search(_GENDER_WORDS[claimed], customer_recent, re.I) \
                and not _OTHER_PERSON.search(customer_recent):
            keep["gender"] = claimed
    note = " ".join(reply.remember.get("note", "").split())
    if 3 <= len(note) <= 120:
        keep["note"] = note
    return keep


def _who_hint(text: str, customer_recent: str, parsed: retrieval.Filters, profile: dict[str, Any]) -> str:
    """Say plainly what is known about who the outfit is for, so the model asks
    only when it must (and searches the right gender when it need not)."""
    recent = retrieval.parse_query(customer_recent)
    if recent.gender or recent.both_genders:
        return ""
    own = profile.get("gender")
    someone_else = bool(_OTHER_PERSON.search(text))
    if own and not someone_else:
        who = "a man: search Men" if own == "Men" else "a woman: search Women"
        return f"\n- They shop for themselves as {who} unless they say it is for someone else."
    if not parsed.categories and (_SHOPPING.search(text) or parsed.occasion):
        budget = "" if (recent.max_price or recent.min_price) else " and their budget"
        return ("\n- WHO WILL WEAR IT IS NOT KNOWN (not in their words, not remembered). Do not search yet: ask, "
                f"in one short friendly message, who it is for (you or someone else? man or woman?){budget}. "
                "Only skip this if they said 'just show me'.")
    return ""


def respond(user_id: int, text: str, context: dict[str, Any]) -> AssistantReply:
    """Answer one customer message. Synchronous (network I/O): call via a thread."""
    if not enabled():
        raise AssistantUnavailable("no LLM key configured")

    # Recent memory only, trimmed: every message is resent on every model call,
    # and the free Groq tier allows 8,000 tokens a minute per model
    history = [{"role": m["role"], "content": str(m["content"])[:350]}
               for m in context.get("history", [])[-HISTORY_MESSAGES:]
               if m.get("role") in ("user", "assistant") and m.get("content")]
    customer_recent = " ".join([m["content"] for m in history if m["role"] == "user"][-3:] + [text])
    # The model still asked "who will wear the jacket?" for "jacket to wear over
    # my kurta" despite the prompt; stating what the words already settle helps
    parsed = retrieval.parse_query(text)
    hint = ""
    if parsed.categories:
        hint = (f"\n- This message already names what they want ({', '.join(parsed.categories)}"
                f"{', for ' + parsed.gender if parsed.gender else ''}): search now, do not ask first.")
    # Only this message and the one it may be answering: an old "royal blue
    # sherwani" three turns back says nothing about who today's outfit is for
    last_said = [m["content"] for m in history if m["role"] == "user"][-1:]
    hint += _who_hint(text, " ".join(last_said + [text]), parsed, context.get("profile") or {})
    consent_text = _consent(text, history)
    context_text = _context_block(context) + hint + _owner_guidance(text)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context_text},
        *history,
        {"role": "user", "content": text},
    ]
    models = _models()
    started = time.perf_counter()
    used: list[str] = []
    nudged = False
    fact_checked = False
    claim_checked = False
    succeeded: set[str] = set()         # tools that really did something this turn
    # Every rupee amount the model may state must come from somewhere real
    evidence = [context_text, customer_recent]

    def checked(reply: AssistantReply) -> tuple[Optional[AssistantReply], str]:
        """Final gate on what the model wrote: amounts verified, products attached."""
        nonlocal fact_checked, claim_checked
        if _LOOKS_LIKE_CODE.search(reply.message):
            # "can u write a python code for 2+2" got a working snippet, live
            reply.message, reply.products, reply.attach = OFF_TOPIC, [], None
        if _CLAIMS_CART_CHANGE.search(reply.message) and not {"add_to_cart", "remove_from_cart"} & succeeded:
            if not claim_checked:
                claim_checked = True
                return None, ("ERROR: nothing was added to or removed from the cart this turn. If they asked "
                              "and gave a size, call add_to_cart now; otherwise tell them honestly what is "
                              "still needed. Then send_reply again.")
            kept = [s for s in re.split(r"(?<=[.!?])\s+", reply.message) if not _CLAIMS_CART_CHANGE.search(s)]
            reply.message = " ".join(kept).strip() or (
                "I have not added it yet: reply ADD with your size (for example ADD M) and I will.")
        unverified = _unverified_amounts(reply.message, evidence)
        if unverified and not fact_checked:
            fact_checked = True
            return None, (f"ERROR: {', '.join(unverified)} does not match our records. Never add up or "
                          "estimate prices; for cart totals use attach='cart'. Send your reply again.")
        if unverified:
            reply.message = _drop_sentences_with(reply.message, unverified)
        reply.tools_used = used
        reply.remember = _facts_to_keep(reply, text, customer_recent)
        return _attach_mentioned(reply, changed_cart=bool({"add_to_cart", "remove_from_cart"} & set(used))), ""

    for round_number in range(1, MAX_ROUNDS + 1):
        if time.perf_counter() - started > TURN_BUDGET_S:
            raise AssistantUnavailable("turn took too long")

        message = _ask_model(models, messages, round_number)
        calls = list(getattr(message, "tool_calls", None) or [])
        content = getattr(message, "content", "") or ""

        if not calls:
            if "search_products" in used and not nudged:
                # It searched, then wrote the products out itself instead of
                # attaching them: "2" would then select nothing. Ask once more.
                nudged = True
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": (
                    "(system) Call send_reply now: product_ids = the ids you want to show, in order, and a "
                    "1-2 sentence message without product names, prices or a list.")})
                continue
            cleaned = _clean(content)
            if not cleaned:
                raise AssistantUnavailable("empty answer")
            reply, error = checked(AssistantReply(message=cleaned))
            if reply is not None:
                return reply
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "(system) " + error})
            continue

        messages.append({
            "role": "assistant", "content": content,
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                           for c in calls],
        })
        # Information tools first, so a send_reply in the same batch comes last
        calls.sort(key=lambda c: c.function.name == "send_reply")
        for call in calls:
            name, args = call.function.name.split("<|")[0].strip(), _arguments(call.function.arguments)
            used.append(name)
            if name == "send_reply":
                reply, error = _finish(args, text)
                if reply is not None:
                    reply, error = checked(reply)
                    if reply is not None:
                        return reply
                result = error
            elif name in TOOL_FUNCTIONS:
                try:
                    # user_id and the customer's words are ours, never the model's
                    for key in ("user_id", "customer_text", "customer_recent"):
                        args.pop(key, None)
                    if name in ("add_to_cart", "remove_from_cart", "hide_product"):
                        args.update(customer_text=consent_text, customer_recent=customer_recent)
                    result = TOOL_FUNCTIONS[name](user_id, **args)
                except Exception as exc:
                    print(f"[assistant] tool {name} failed: {exc!r}")
                    result = "ERROR: that lookup failed. Apologise briefly and suggest trying again."
            else:
                result = f"ERROR: unknown tool {name!r}."
            print(f"[assistant]   {name}({json.dumps(args, ensure_ascii=False)[:120]}) -> {result[:80]!r}")
            if not result.startswith("ERROR"):
                # Our own "Rs 17,498 does not match" must not make 17,498 look verified
                evidence.append(result)
                succeeded.add(name)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    raise AssistantUnavailable("no reply after the maximum number of rounds")



def memory_text(reply: AssistantReply) -> str:
    """How this reply is remembered for the next turn: words plus what was shown."""
    parts = [reply.message] if reply.message else []
    if reply.products:
        parts.append("[Showed: " + "; ".join(
            f"{n}. {p['id']} {p['name']} Rs {p['price']:.0f}" for n, p in enumerate(reply.products, 1)) + "]")
    if reply.attach:
        parts.append(f"[Showed their {reply.attach}]")
    return "\n".join(parts)
