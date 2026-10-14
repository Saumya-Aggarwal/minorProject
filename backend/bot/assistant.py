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
  * money: it can add to the cart, and call `checkout` when the customer asks
    to pay, but the order, its total and the Pay Now button are the CHECKOUT
    command's, run by the webhook; the model never writes a payment reply
  * what it changed: confirmations ("Added 1 x ... (XL)") are written by code
    from what the cart tools really did (the ledger), not by the model

Any failure — no key, timeout, rate limit, a malformed tool call — raises
AssistantUnavailable, and the webhook answers with the plain retrieval path
(bot/chat.py) instead. The customer always gets a reply.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import brand
import catalog
import llm
import tracing
import repository as repo
from bot import retrieval
from selection import aliases, match_size, names_a_size, parse_bare_size

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
    # They asked to pay: the webhook runs CHECKOUT and that is the whole reply
    checkout: bool = False
    # What the cart tools really changed this turn: {op, product, name, size, qty_before, qty_after}
    ledger: list[dict[str, Any]] = field(default_factory=list)


def enabled() -> bool:
    return llm.available("chat")


# --- prompt ---------------------------------------------------------------------

# The system prompt is the store's: brand.system_prompt() (until P2 step 3 it
# was written here for Kurta & Co.; the demo's settings reproduce it exactly)


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
            f"{i}. {p.get('product_id') or p.get('id')} {p.get('name')}{_sizes_note(p)}" for i, p in enumerate(shown, 1)))
    selected = context.get("selected_product")
    if selected:
        lines.append(f"- Item they picked: {selected.get('product_id') or selected.get('id')} {selected.get('name')}"
                     f"{_sizes_note(selected)}")
    return "\n".join(lines)


def _sizes_note(item: dict[str, Any]) -> str:
    """" (sizes in stock: 38, 40, 42)": the model asked "S, M, L, XL?" for a
    sherwani sold in 38-46 when it had to remember sizes from a tool result."""
    product = catalog.get_by_id(str(item.get("product_id") or item.get("id") or ""))
    sizes = _sizes_in_stock(product) if product else []
    if not sizes:
        return ""
    return " (one size)" if len(sizes) == 1 else f" (sizes in stock: {', '.join(sizes)})"


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
            {"product_id": {"type": "string"}, "size": _TEXT,
             "quantity": {**_NUMBER, "description": "How many, only if they said a number; default 1"}},
            ["product_id"]),
    _schema("remove_from_cart", "Remove a product from this customer's cart, when they ask to: all of it, "
            "or `quantity` of it.",
            {"product_id": {"type": "string"},
             "size": {**_TEXT, "description": "Which size's line, when the product is in the cart in several"},
             "quantity": {**_NUMBER, "description": "How many to remove; leave out to remove it all"}},
            ["product_id"]),
    _schema("checkout", "The customer wants to pay for their cart (pay, check out, place the order). Code "
            "creates the order and sends the Pay Now button, or asks for a missing size or address. "
            "Ends the turn: do not call send_reply after it.", {}, []),
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


def _size_was_said(size: str, customer_recent: str) -> bool:
    """Did the customer actually name this size? "medium" counts as M."""
    return any(re.search(rf"(?<!\w){re.escape(spelling)}(?!\w)", customer_recent, re.I)
               for spelling in aliases(size.strip()))


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a pair": 2, "both": 2,
                 "do": 2, "teen": 3, "char": 4, "paanch": 5, "ek": 1}


def _quantity(value: Any, customer_recent: str) -> tuple[int, str]:
    """(quantity, error). A quantity above 1 must be a number the customer said."""
    if value in (None, "", 0, "0"):
        return 1, ""
    try:
        quantity = int(float(str(value).strip()))
    except ValueError:
        return 0, f"ERROR: quantity {value!r} is not a number."
    if not 1 <= quantity <= 10:
        return 0, "ERROR: quantity must be between 1 and 10."
    said = {int(n) for n in re.findall(r"(?<![\d,])(\d{1,2})(?![\d,])", customer_recent)}
    said |= {n for word, n in _NUMBER_WORDS.items() if re.search(rf"\b{word}\b", customer_recent, re.I)}
    if quantity > 1 and quantity not in said:
        return 0, f"ERROR: the customer has not asked for {quantity}. Add 1, or ask how many."
    return quantity, ""


def _line(cart: dict[str, Any], product_id: str, size: str) -> Optional[dict[str, Any]]:
    return next((i for i in cart["items"] if i["product_id"] == product_id and (i["size"] or "") == (size or "")), None)


def tool_add_to_cart(user_id: int, product_id: str = "", size: str = "", quantity: Any = None, *,
                     customer_text: str = "", customer_recent: str = "",
                     ledger: Optional[list[dict[str, Any]]] = None, **_: Any) -> str:
    """customer_text / customer_recent / ledger are supplied by respond(), never
    the model: the current message must ask for it, the size and any quantity
    must be ones they typed (now or in their last few messages), and what really
    changed goes on the ledger, from which code writes the confirmation."""
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
    elif size and not _size_was_said(str(size), customer_recent):
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
    count, error = _quantity(quantity, customer_recent)
    if error:
        return error
    before = _line(repo.get_cart(user_id), product["id"], chosen)
    cart = repo.add_to_cart(user_id, product["id"], chosen, count)
    after = _line(cart, product["id"], chosen)
    if ledger is not None:
        ledger.append({"op": "add", "product": product["id"], "name": product["name"], "size": chosen,
                       "qty_before": (before or {}).get("quantity", 0), "qty_after": (after or {}).get("quantity", 0)})
    return (f"Added {count} x {product['name']} (size {chosen}). Cart now has {cart['count']} item(s), "
            f"total Rs {cart['total_inr']:.0f}. The confirmation is shown to them for you.")


_WANTS_TO_REMOVE = re.compile(
    r"\b(remove|delete|drop|take\s+(it|this|that)?\s*out|don'?t\s+want|do\s+not\s+want|cancel)\b", re.I)


def tool_remove_from_cart(user_id: int, product_id: str = "", size: str = "", quantity: Any = None, *,
                          customer_text: str = "", customer_recent: str = "",
                          ledger: Optional[list[dict[str, Any]]] = None, **_: Any) -> str:
    """All of a product, or `quantity` of one line. Live, "remove 2 aqua chanderi
    saree" from a line of 3 was answered "Removed 2" and emptied the line."""
    if not _WANTS_TO_REMOVE.search(customer_text):
        return "ERROR: the customer has not asked to remove anything."
    wanted = str(product_id).strip().upper()
    cart = repo.get_cart(user_id)
    lines = [i for i in cart["items"] if i["product_id"] == wanted]
    if not lines:
        return f"ERROR: {product_id!r} is not in the cart. Cart: " + (
            ", ".join(f"{i['product_id']} {i['name']}" for i in cart["items"]) or "empty")
    if size:
        chosen = match_size(str(size), [i["size"] for i in lines if i["size"]]) or ""
        lines = [i for i in lines if i["size"] == chosen]
        if not lines:
            return f"ERROR: {wanted} is not in the cart in size {size}."
    count = None
    if quantity not in (None, "", 0, "0"):
        count, error = _quantity(quantity, customer_recent)
        if error:
            return error
        if len(lines) > 1:
            return ("ERROR: it is in the cart in more than one size: "
                    + ", ".join(f"{i['size'] or 'no size'} x{i['quantity']}" for i in lines) + ". Ask which.")
    for line in lines:
        left = max(line["quantity"] - count, 0) if count else 0
        repo.update_cart_item(user_id, line["cart_item_id"], left)
        if ledger is not None:
            ledger.append({"op": "remove", "product": wanted, "name": line["name"], "size": line["size"],
                           "qty_before": line["quantity"], "qty_after": left})
    after = repo.get_cart(user_id)
    return (f"Removed. Cart now has {after['count']} item(s), total Rs {after['total_inr']:.0f}. "
            "The confirmation is shown to them for you.")


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
    """The chat role's primary model, then fallbacks (llm.py). On Groq each
    model has its own free-tier budget (8,000 tokens per minute each,
    measured), so a second and third model roughly triple how fast a
    conversation can go before anything waits."""
    return llm.config("chat").models


def _complete(model: str, messages: list[dict[str, Any]]) -> Any:
    """One chat completion with tools. Returns the message object. Tests replace this."""
    response = llm.complete("chat", messages, model=model, timeout=12.0, tools=TOOLS,
                            tool_choice="auto", temperature=0.3, max_tokens=1500)
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
    "cart": re.compile(r"\b(cart|bag|basket|added|items?\s+(?:do\s+)?i\s+have|how\s+many\s+items)\b", re.I),
}
# A numbered line in the model's own words duplicates the list code attaches
_LIST_LINE = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+.*$", re.MULTILINE)


def _without_listing(message: str, products: list[dict[str, Any]], changed_cart: bool = False) -> str:
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
    # ceremony"); several, or names with prices, is a listing. "Added the Mint
    # Box Clutch to your cart. Total Rs 1,299." is a confirmation, not a listing:
    # cutting it left the customer reading "Added the."
    if len(named) >= 2 or (named and not changed_cart and re.search(r"(?:rs\.?|₹|inr)\s*\d", lowered)):
        cuts += named
    cut = min(cuts) if cuts else -1
    if cut > 0:
        message = message[:cut].rstrip(" :–—-") + ("." if not message[:cut].rstrip().endswith((".", "!")) else "")
    elif cut == 0:
        message = ""
    # What is left of a cut sentence must still be a sentence
    if cut > 0 and (len(message.split()) < 3 or re.search(r"\b(?:the|a|an|of|in|to|and|our|your)\W*$", message, re.I)):
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
# What replaces such a reply: the store's own line (brand.Brand.off_topic)


def _finish(args: dict[str, Any], customer_text: str,
            changed_cart: bool = False) -> tuple[Optional[AssistantReply], str]:
    """Validate send_reply. Returns (reply, "") or (None, error for the model)."""
    raw_ids = args.get("product_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = _CODE_LIKE.findall(raw_ids.upper())
    ids = [str(i).strip().upper() for i in raw_ids if str(i).strip()]
    products = [p for p in (catalog.get_by_id(i) for i in dict.fromkeys(ids)) if p is not None]
    products = [p for p in products if _sizes_in_stock(p)][:MAX_PRODUCTS]
    if ids and not products:
        tracing.guard("unknown_products", ids=ids)
        return None, (f"ERROR: none of {ids} are products we stock. Only use ids returned by "
                      "search_products, then call send_reply again.")
    message = str(args.get("message") or "")
    if products:
        written = message
        message = _without_listing(message, products, changed_cart)
        if message != written:
            tracing.guard("listing_cut", before=written, after=message)
    message = _clean(message)
    attach = str(args.get("attach") or "").strip().lower()
    attach = attach if attach in _ASKS_FOR and _ASKS_FOR[attach].search(customer_text) else None
    if not message and not products and not attach:
        tracing.guard("empty_message")
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
        attempt = _plain_text_reply(generation)
        if attempt is None:
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


# Seen in the 14 Oct eval (twice in 63 cases): send_reply whose "arguments" is
# the message itself as bare text, not a JSON object:
#   {"name": "send_reply<|channel|>final", "arguments": Your cart now has 1 item ... ADD another item."}
_BARE_ARGUMENTS = re.compile(r'^\s*\{\s*"name"\s*:\s*"(?P<name>[^"]+)"\s*,\s*"arguments"\s*:\s*(?P<text>.*?)"?\s*\}\s*$',
                             re.DOTALL)


def _plain_text_reply(generation: str) -> Optional[dict[str, Any]]:
    """A send_reply whose arguments are plain words: the words are the message."""
    match = _BARE_ARGUMENTS.match(generation or "")
    if not match or match.group("name").split("<|")[0].strip() != "send_reply":
        return None
    text = match.group("text").strip().strip('"')
    try:
        # The model's own escapes (\n,  ) are JSON string escapes
        text = json.loads('"' + text.replace('\\"', '"').replace('"', '\\"') + '"')
    except ValueError:
        text = text.replace("\\n", "\n")
    return {"name": "send_reply", "arguments": {"message": text}} if text.strip() else None


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
                # The customer gets this model's answer although its call "failed"
                tracing.guard("repaired_tool_call", model=model,
                              tools=[c.function.name for c in repaired.tool_calls])
                return repaired
            if "tool_use_failed" in text and not retried:
                retried = True           # a one-off bad generation: same model, once more
                tracing.guard("tool_use_retry", model=model)
                continue
            wait = re.search(r"try again in ([\d.]+)s", text)
            if "429" in text and wait:
                rate_limited[model] = float(wait.group(1))
            models.pop(0)                # rate limit, outage, or repeated bad output
            tracing.guard("model_dropped", model=model, error=text[:200])
            retried = False
            if not models and rate_limited and not waited:
                # Every model is over its per-minute budget. Groq says when the
                # soonest frees up; a few seconds' wait beats a worse answer.
                model, seconds = min(rate_limited.items(), key=lambda kv: kv[1])
                if seconds <= MAX_RATE_WAIT_S:
                    print(f"[assistant] all models rate-limited; waiting {seconds:.1f}s for {model}")
                    tracing.guard("rate_wait", model=model, seconds=seconds)
                    time.sleep(seconds + 0.3)
                    models.append(model)
                    waited = True
    raise AssistantUnavailable(f"no model answered: {last_error!r}")


# --- checking what the model wrote -------------------------------------------------

_AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand)?", re.IGNORECASE)
_NUMBER_IN_TEXT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand)?", re.IGNORECASE)
# Any store's codes look like this (EW006, FW012); only the current store's are removed
_CODE_LIKE = re.compile(r"\b[A-Z]{1,4}\d{3,5}\b", re.IGNORECASE)


def _strip_codes(message: str) -> str:
    """The model's own product codes, out of the customer's sight."""
    return _CODE_LIKE.sub(lambda m: "" if catalog.get_by_id(m.group(0).upper()) else m.group(0), message)


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


def _drop_sentences(message: str, bad: Callable[[str], bool]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", message)
    return " ".join(s for s in sentences if s.strip() and not bad(s)).strip()


_PAY_BUTTON_CLAIM = re.compile(
    r"pay\s*now\s+(?:button|link)|payment\s+(?:button|link)|tap\s+(?:the\s+|on\s+)?pay\s*now"
    r"|here'?s\s+(?:your|the)\s+(?:secure\s+)?(?:pay|payment|checkout)\b", re.I)
_SAYS_REPLY_CHECKOUT = re.compile(r"\b(?:reply|type|send|say)\s+\*?checkout\b", re.I)
_PAY_WORDS = re.compile(r"\b(?:pay|payment|check\s*out|checkout|place\s+(?:my\s+|the\s+)?order)\b", re.I)


def _wants_to_pay(text: str) -> bool:
    """Their words ask to pay ("chedckout" too). Gates the checkout tool."""
    from selection import _fix_command_typos
    return bool(_PAY_WORDS.search(_fix_command_typos(text.lower())))


# Sizes as checked facts, like rupee amounts. Live: "Which size? (Sizes: S, M, L,
# XL)" for a one-size clutch, "S, M, L, XL, XXL" for a sherwani sold in 38-46
_SIZE_SENTENCE = re.compile(r"\bsizes?\b|\bin\s+(?:a\s+)?(?:small|medium|large)\b", re.I)
_LETTER_SIZES = re.compile(r"\b(?:XXXL|XXL|XL|XS|S|M|L)\b")            # capitals only: not the s of "it's"
_OTHER_SIZES = re.compile(r"\b(?:UK\s?\d{1,2}|\d{2}|small|medium|large)\b", re.I)
_SIZE_WORD_KEYS = {"small": "S", "medium": "M", "large": "L"}


def _size_key(token: str) -> str:
    token = re.sub(r"\s+", " ", token.strip()).upper()
    return _SIZE_WORD_KEYS.get(token.lower(), "UK " + token[2:].strip() if token.startswith("UK") else token)


def _sizes_named(sentence: str) -> set[str]:
    """Sizes a sentence states, if it is a sentence about size."""
    if not _SIZE_SENTENCE.search(sentence):
        return set()
    tokens = _LETTER_SIZES.findall(sentence) + _OTHER_SIZES.findall(sentence)
    return {_size_key(t) for t in tokens if not (t.isdigit() and not 26 <= int(t) <= 50)}


def _invented_sizes(reply: AssistantReply, context: dict[str, Any], customer_recent: str) -> Optional[dict[str, Any]]:
    """Sizes the reply offers that none of the products it can be about come in."""
    named = set().union(*[_sizes_named(s) for s in re.split(r"(?<=[.!?])\s+|\n+", reply.message)])
    if not named:
        return None
    # The most specific thing the reply can be about: a product it names, else the
    # ones it attaches, else the item picked, else the list on screen. (A union of
    # all of them let "Sizes: S, M, L, XL" for the picked clutch pass, because an
    # older list on screen had kurtas in those sizes.)
    lowered = reply.message.lower()
    selected = context.get("selected_product") or {}
    tiers = [
        [p for p in catalog.get_all() if p["name"].lower() in lowered],
        list(reply.products),
        [catalog.get_by_id(str(selected.get("product_id") or selected.get("id") or ""))],
        [catalog.get_by_id(str(p.get("product_id") or p.get("id") or ""))
         for p in context.get("last_products_shown") or []],
    ]
    focus = next(([p for p in tier if p] for tier in tiers if any(tier)), [])
    if not focus:
        return None
    real = {_size_key(s) for p in focus for s in _sizes_in_stock(p)} | {"FREE SIZE"}
    said = {_size_key(t) for t in re.findall(r"\b(?:xxxl|xxl|xl|xs|s|m|l|uk\s?\d{1,2}|\d{2}|small|medium|large)\b",
                                             customer_recent, re.I)}
    invented = sorted(named - real - said)
    if not invented:
        return None
    return {"sizes": invented, "names": " / ".join(p["name"] for p in focus[:3]),
            "real": "; ".join(f"{p['name']}: {', '.join(_sizes_in_stock(p))}" for p in focus[:3])}


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
        reply.message = _strip_codes(reply.message).replace("  ", " ").strip()
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
    if products:
        tracing.guard("products_attached", ids=[p["id"] for p in products])
    if len(products) >= 2:
        reply.products = products
        reply.message = _without_listing(_strip_codes(reply.message), products)
        reply.message = re.sub(r"[ \t]{2,}", " ", reply.message).strip()
    elif len(products) == 1:
        reply.products, reply.detail = products, True
        reply.message = re.sub(r"[ \t]{2,}", " ", _strip_codes(reply.message)).strip(" –—-\n")
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
# "a wedding of my friend", "my sister's reception": the other person is whose
# function it is, not who wears the outfit. Live, "i would like to buy something
# to wear on a wedding of my friend" was answered "is your friend a man or a
# woman?" — the friend is getting married; the customer is the one dressing.
_FUNCTIONS = (r"wedding|shaadi|shadi|marriage|reception|sangeet|mehendi|mehndi|haldi|engagement|roka|"
              r"anniversary|birthday|party|function|ceremony|puja|pooja|griha\s+pravesh")
_THEIR_FUNCTION = re.compile(
    rf"\b(?:{_FUNCTIONS})\b\s+(?:of|for)\s+(?:my|our|a|his|her|their)\s+\w+"
    # "my best friend's reception" needs the words between "my" and the owner
    rf"|\b(?:my|our|his|her|their|a)\s+(?:\w+\s+){{0,2}}\w+(?:'s|s'|s)\s+(?:{_FUNCTIONS})\b", re.I)
# "I would like to buy something to wear": the customer is the one wearing it
_WEARS_IT_THEMSELVES = re.compile(
    r"\bfor\s+(?:me|myself|us|ourselves)\b|\bto\s+wear\b|\bon\s+me\b"
    r"|\b(?:i|we)\s+(?:can|could|should|shall|will|would|'?ll|'?d|can'?t)?\s*(?:wear|am\s+wearing)\b"
    r"|\b(?:should|can|could|shall|will|do|what\s+do)\s+(?:i|we)\s+wear\b", re.I)


def _wearer(text: str) -> str:
    """"someone_else", "themselves" or "unknown", reading the whole sentence."""
    # Whose function it is says nothing about who dresses for it
    about_people = _THEIR_FUNCTION.sub(" ", text)
    if _OTHER_PERSON.search(about_people):
        return "someone_else"
    return "themselves" if _WEARS_IT_THEMSELVES.search(text) else "unknown"


def _last_bot_message(history: list[dict[str, Any]]) -> str:
    return next((str(m["content"]) for m in reversed(history) if m.get("role") == "assistant"), "")


def _consent(text: str, history: list[dict[str, Any]]) -> str:
    """The customer's words as the add_to_cart gate should read them.

    "yes" or "XL" alone does not ask to add anything, but it does when it
    answers our own "shall I add it?" / "which size?"."""
    if _OFFERED_TO_ADD.search(_last_bot_message(history)) and (
            _AFFIRM.match(text) or parse_bare_size(text) or names_a_size(text)):
        # "i think medium would look good on her" answers "which size?"
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
                and _wearer(customer_recent) != "someone_else":
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
    wearer = _wearer(text)
    if own and wearer != "someone_else":
        who = "a man: search Men" if own == "Men" else "a woman: search Women"
        return f"\n- They shop for themselves as {who} unless they say it is for someone else."
    if not parsed.categories and (_SHOPPING.search(text) or parsed.occasion):
        budget = "" if (recent.max_price or recent.min_price) else " and their budget"
        if wearer == "themselves":
            # Their words settle the wearer; only menswear or womenswear is open
            return ("\n- THEY are the one who will wear it, in their own words: never ask who it is for. "
                    "What is still open is whether to search menswear or womenswear, so ask them that "
                    f"in your own plain words{budget}, in one short message.")
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
        {"role": "system", "content": brand.system_prompt() + "\n\n" + context_text},
        *history,
        {"role": "user", "content": text},
    ]
    models = _models()
    started = time.perf_counter()
    used: list[str] = []
    nudged = False
    fact_checked = False
    claim_checked = False
    pay_checked = False
    size_checked = False
    succeeded: set[str] = set()         # tools that really did something this turn
    ledger: list[dict[str, Any]] = []   # what add_to_cart / remove_from_cart really changed
    # Every rupee amount the model may state must come from somewhere real
    evidence = [context_text, customer_recent]

    def checked(reply: AssistantReply) -> tuple[Optional[AssistantReply], str]:
        """Final gate on what the model wrote: amounts verified, products attached."""
        nonlocal fact_checked, claim_checked, pay_checked, size_checked
        if _LOOKS_LIKE_CODE.search(reply.message):
            # "can u write a python code for 2+2" got a working snippet, live
            tracing.guard("off_topic", message=reply.message)
            reply.message, reply.products, reply.attach = brand.current().off_topic, [], None
        if _CLAIMS_CART_CHANGE.search(reply.message) and not {"add_to_cart", "remove_from_cart"} & succeeded:
            if not claim_checked:
                claim_checked = True
                tracing.guard("claim_retry", message=reply.message)
                return None, ("ERROR: nothing was added to or removed from the cart this turn. If they asked "
                              "and gave a size, call add_to_cart now; otherwise tell them honestly what is "
                              "still needed. Then send_reply again.")
            tracing.guard("claim_cut", message=reply.message)
            kept = [s for s in re.split(r"(?<=[.!?])\s+", reply.message) if not _CLAIMS_CART_CHANGE.search(s)]
            reply.message = " ".join(kept).strip() or (
                "I have not added it yet: reply ADD with your size (for example ADD M) and I will.")
        unverified = _unverified_amounts(reply.message, evidence)
        if unverified and not fact_checked:
            fact_checked = True
            tracing.guard("amount_retry", amounts=unverified)
            return None, (f"ERROR: {', '.join(unverified)} does not match our records. Never add up or "
                          "estimate prices; for cart totals use attach='cart'. Send your reply again.")
        if unverified:
            tracing.guard("amount_cut", amounts=unverified)
            reply.message = _drop_sentences_with(reply.message, unverified)

        # Only checkout sends a payment button. The 14 Oct eval: "i wanna pay" got
        # "Here's your secure Pay Now button" (none sent), or "reply CHECKOUT"
        pay_talk = _PAY_BUTTON_CLAIM.search(reply.message) or (
            _wants_to_pay(text) and _SAYS_REPLY_CHECKOUT.search(reply.message))
        if pay_talk and not pay_checked:
            pay_checked = True
            tracing.guard("pay_retry", message=reply.message)
            return None, ("ERROR: you cannot send a payment button or link, and they should not have to type "
                          "CHECKOUT. If they want to pay, call checkout now; otherwise do not mention payment.")
        if pay_talk:
            tracing.guard("pay_cut", message=reply.message)
            reply.message = _drop_sentences(reply.message, lambda s: bool(
                _PAY_BUTTON_CLAIM.search(s) or _SAYS_REPLY_CHECKOUT.search(s))) or "Reply CHECKOUT to pay."

        invented = _invented_sizes(reply, context, customer_recent)
        if invented and not size_checked:
            size_checked = True
            tracing.guard("size_retry", sizes=invented["sizes"])
            return None, (f"ERROR: {', '.join(invented['sizes'])} {'is' if len(invented['sizes']) == 1 else 'are'} "
                          f"not a size of {invented['names']}. In stock: {invented['real']}. Use only those.")
        if invented:
            tracing.guard("size_cut", sizes=invented["sizes"])
            reply.message = _drop_sentences(reply.message, lambda s: bool(_sizes_named(s) & set(invented["sizes"])))

        if ledger:
            # The confirmation is written by code from the ledger (webhook); the
            # model's own "Added the ..." would say it twice, or cut, "Added the."
            reply.ledger = ledger
            reply.message = _drop_sentences(reply.message, lambda s: bool(_CLAIMS_CART_CHANGE.search(s)))
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
                tracing.guard("nudge_send_reply")
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
            tool_started = time.perf_counter()
            if name == "send_reply" and str(call.id).startswith("salvaged_") and not args.get("product_ids") \
                    and "search_products" in used and _LIST_LINE.search(str(args.get("message") or "")) \
                    and not nudged:
                # A repaired reply that types out the products it found: no photos,
                # and "1" next would select nothing (14 Oct eval). Ask for the ids.
                nudged = True
                tracing.guard("nudge_send_reply", repaired=True)
                result = ("ERROR: do not write the products out. Call send_reply with product_ids = the ids "
                          "you want to show, in order, and a 1-2 sentence message.")
            elif name == "send_reply":
                reply, error = _finish(args, text, bool({"add_to_cart", "remove_from_cart"} & succeeded))
                if reply is not None:
                    reply, error = checked(reply)
                    if reply is not None:
                        tracing.tool(name, args, "sent", (time.perf_counter() - tool_started) * 1000)
                        return reply
                result = error
            elif name == "checkout":
                if _wants_to_pay(consent_text):
                    tracing.tool(name, {}, "started", (time.perf_counter() - tool_started) * 1000)
                    return AssistantReply(message="", checkout=True, tools_used=used, ledger=ledger)
                result = "ERROR: the customer has not asked to pay. Answer their message instead."
            elif name in TOOL_FUNCTIONS:
                try:
                    # user_id, the customer's words and the ledger are ours, never the model's
                    for key in ("user_id", "customer_text", "customer_recent", "ledger"):
                        args.pop(key, None)
                    if name in ("add_to_cart", "remove_from_cart", "hide_product"):
                        args.update(customer_text=consent_text, customer_recent=customer_recent)
                    if name in ("add_to_cart", "remove_from_cart"):
                        args["ledger"] = ledger
                    result = TOOL_FUNCTIONS[name](user_id, **args)
                    args.pop("ledger", None)
                except Exception as exc:
                    print(f"[assistant] tool {name} failed: {exc!r}")
                    result = "ERROR: that lookup failed. Apologise briefly and suggest trying again."
            else:
                result = f"ERROR: unknown tool {name!r}."
            print(f"[assistant]   {name}({json.dumps(args, ensure_ascii=False)[:120]}) -> {result[:80]!r}")
            tracing.tool(name, args, result, (time.perf_counter() - tool_started) * 1000)
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
