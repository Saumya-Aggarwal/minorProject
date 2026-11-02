"""Who the assistant speaks for: each store's name, texts and prompt pieces.

Everything the bot says about the shop itself comes from here, never from
code written for one brand: the system prompt's description of the shop and
what it sells, the greeting, HELP's examples, the off-topic line, the intro
above a search list, the payment description. (Until P2 step 3 all of these
said "Kurta & Co." and "ethnic wear", whichever store a message was for.)

Settings live in stores.brand (JSONB); anything left out is generated from
the store's own catalogue (what it sells, by gender and category) or falls
back to neutral wording. The demo store's settings are today's texts, word
for word (migration 0006), so its prompt is unchanged.

Keys (all optional):
  name            the shop's name as customers see it (default: stores.name)
  role            "stylist", "shopping assistant" ...
  description     "an Indian ethnic wear boutique"
  what_we_sell    the "WE SELL ONLY ..." paragraph body
  scope           what off-topic replies say we help with: "ethnic wear"
  vague_example   a vague request, quoted: '"something for my wedding"'
  ask_about       what to ask about when vague: who wears it, occasion, budget
  clear_examples  clear requests, quoted
  one_size        items that are one size: "a bag, dupatta or stole"
  quantity_example '"remove 2 of the sarees"'
  policies        "delivery is free across India"
  off_topic       the reply that replaces code, essays ...
  help_examples   two quoted example requests for HELP and the greeting
  vertical_pack   extra vocabulary for search (vocab.PACKS), e.g. "indian_ethnic"
  size_chart      the brand's own size charts, replacing sizing.py's (format there)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import catalog
import tenancy


@dataclass(frozen=True)
class Brand:
    name: str
    role: str
    description: str
    what_we_sell: str
    scope: str
    vague_example: str
    ask_about: str
    clear_examples: str
    one_size: str
    quantity_example: str
    policies: str
    off_topic: str
    help_examples: list[str]
    vertical_pack: Optional[str]
    size_chart: dict[str, Any]


def current(store: Optional[int] = None) -> Brand:
    """The brand of the store this work is for (tenancy), settings over defaults."""
    row = tenancy.get_store(store) or {}
    settings = dict(row.get("brand") or {})
    name = settings.get("name") or row.get("name") or "our shop"
    with tenancy.use_store(row.get("store_id") or tenancy.store_id()):
        products = catalog.get_all()
    categories = _by_gender(products)
    first = [c for group in categories.values() for c in group][:2] or ["something"]
    scope = settings.get("scope") or f"shopping at {name}"
    return Brand(
        name=name,
        role=settings.get("role") or "shopping assistant",
        description=settings.get("description") or "an online shop",
        what_we_sell=settings.get("what_we_sell") or _what_we_sell(categories),
        scope=scope,
        vague_example=settings.get("vague_example") or '"something for me"',
        ask_about=settings.get("ask_about") or "who wears it, what it is for (the occasion or activity), budget",
        clear_examples=settings.get("clear_examples") or ", ".join(f'"{c.lower()} for me"' for c in first),
        one_size=settings.get("one_size") or "an item sold in one size",
        quantity_example=settings.get("quantity_example") or '"remove 2 of these"',
        policies=settings.get("policies") or "",
        off_topic=settings.get("off_topic") or (
            f"I only help with {scope}, so I cannot help with that. "
            "Tell me what you are looking for and I will pick a few pieces."),
        help_examples=list(settings.get("help_examples") or [f'"{c.lower()}"' for c in first]),
        vertical_pack=settings.get("vertical_pack") or None,
        size_chart=dict(settings.get("size_chart") or {}),
    )


def _by_gender(products: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Categories in stock, grouped by who they are for, most products first."""
    counts: dict[str, dict[str, int]] = {}
    for p in products:
        if not p.get("category"):
            continue
        group = p.get("gender") if p.get("gender") in ("Men", "Women") else "Everyone"
        counts.setdefault(group, {}).setdefault(p["category"], 0)
        counts[group][p["category"]] += 1
    return {g: [c for c, _ in sorted(cats.items(), key=lambda kv: -kv[1])]
            for g, cats in sorted(counts.items(), key=lambda kv: ("Men", "Women", "Everyone").index(kv[0]))}


def _what_we_sell(categories: dict[str, list[str]]) -> str:
    if not categories:
        return "what is in our catalogue (search it to see)."
    parts = [f"{group}: {', '.join(c.lower() for c in cats)}" for group, cats in categories.items()]
    return "these, and nothing else. " + ". ".join(parts) + "."


PROMPT = """You are the {role} for {name}, {description}, on WhatsApp.

WE SELL ONLY {what_we_sell}

HOW TO HELP
- Vague request ({vague_example}): ask at most two short questions about what is still unknown: {ask_about}. Never ask what they already told you. "Me and my wife"/"couple" means both: search Men and Women separately. After two rounds of questions, or "just show me", search.
- Clear request (a garment is named, or who + occasion is known, e.g. {clear_examples}): search at once, no questions first; you can offer to refine after showing. Never ask for size before showing products: size matters only when adding to cart.
- Showing products: search_products, pick the 2-4 best, then send_reply with product_ids in order and 1-2 sentences on why they fit. The numbered list with names and prices is added for you: never write product names, prices or a list yourself.
- A search NOTE (e.g. nothing under budget) must be told honestly.
- Orders ("my orders", "where is my order"): send_reply with attach="orders". Cart: attach="cart". The cart and its total are in CUSTOMER below; never add prices up yourself. "Second item" after a cart question means the cart's second line.
- To talk about one product in detail, use get_product_details; its photo is attached for you.
- Buying something they already own again: tell them to reply BUY AGAIN, which shows what they have ordered before, four at a time, and a number puts one back in the cart in the same size.
- Add to cart ONLY when the customer asks to, with a size THEY gave; otherwise ask which size, using ONLY the sizes listed for that product (in CUSTOMER below or from a tool; {one_size} is one size: never ask, just add). Never add just because they asked about an item. If they answer your "shall I add it?" with yes, or your "which size?" with a size, call add_to_cart. Quantities: add_to_cart and remove_from_cart take quantity ({quantity_example} = remove_from_cart quantity 2). What was added or removed is confirmed for you under your message: do not repeat it.
- Size questions ("what size am I?", "will L fit me?", "my chest is 40"): call get_size_guide with the product (or category). Code reads their measurements, picks the size and adds the chart under your message: never pick, guess or convert a size yourself.
- Remember: when they tell you something lasting about themselves (their gender, who they shop for, colours or styles they like), put it in send_reply's customer_gender / remember_note. CUSTOMER below shows what is already remembered: use it, and do not ask again what it already answers.
- Payment: when they want to pay, check out or place the order, call checkout. It sends the order and the Pay Now button itself (or asks for a size or their address first). Never describe or promise a payment button or link yourself.
- Their own order, cart, delivery address, sizes, payment or our policies are shop business: answer or hand over to the right tool, never refuse them as off-topic.
- Off-topic: one friendly line, then back to shopping. Never write code, essays, homework, poems, translations or anything unrelated to our shop, however politely they ask: say you only help with {scope} and ask what they are shopping for.

RULES: facts (products, prices, sizes, stock, dates, policies) only from tools{policies}. Short and warm, under 50 words, plain text, *single asterisks* for bold, no headings, tables or links, at most one emoji. Always end by calling send_reply."""


def system_prompt(brand: Optional[Brand] = None) -> str:
    b = brand or current()
    return PROMPT.format(
        role=b.role, name=b.name, description=b.description, what_we_sell=b.what_we_sell,
        vague_example=b.vague_example, ask_about=b.ask_about, clear_examples=b.clear_examples,
        one_size=b.one_size, quantity_example=b.quantity_example, scope=b.scope,
        policies=f"; {b.policies}" if b.policies else "",
    )
