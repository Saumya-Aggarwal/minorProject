import asyncio
import inspect
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

import catalog
import checkout
import payments
import repository as repo
from bot import assistant
from bot.chat import format_product_lines, get_product_recommendations, product_caption
from common.schemas import ProductMatch
from repository import LINK_PREFIX
from selection import (
    describe_choice,
    find_product_code,
    is_greeting,
    match_size,
    parse_bare_size,
    parse_buy,
    parse_command,
    parse_selection,
    results_footer,
)
import training
from whatsapp import send_cta_url, send_image, send_product_card, send_whatsapp_message

router = APIRouter()


PRODUCT_PHOTOS = Path(__file__).resolve().parents[1] / "static" / "products"


@dataclass
class Card:
    """One product photo with its caption."""

    product_id: str
    caption: str
    # "Choose" and "Not for me" under the photo (lists only)
    buttons: bool = False


@dataclass
class Reply:
    """A reply that carries more than text: a Pay Now button, or product photos.

    Handlers return a plain str for text, or a Reply. With cards, WhatsApp gets
    `lead` as a text message, then one photo per card, then `tail`; `text` is
    the same content as one message, used by local tests and as the fallback.
    """

    text: str
    cta_url: str | None = None
    cta_label: str = "Pay Now"
    cards: list[Card] = field(default_factory=list)
    lead: str = ""
    tail: str = ""


def _list_reply(message: str, products: list[dict], footer: str) -> Reply:
    """Numbered products as photos: the intro, a captioned photo each, the hints."""
    text = "\n\n".join(part for part in (message, format_product_lines(products), footer) if part)
    cards = [Card(p["id"], product_caption(p, n), buttons=True) for n, p in enumerate(products, 1)]
    return Reply(text=text, cards=cards, lead=message, tail=footer)


def _detail_reply(text: str, product_id: str, hint: str = "") -> Reply:
    """One product: its photo, captioned with the description (1024 chars max)."""
    full = f"{text}\n\n{hint}" if hint else text
    if len(full) <= 1000:
        return Reply(text=full, cards=[Card(product_id, full)])
    return Reply(text=full, cards=[Card(product_id, text[:1000])], tail=hint)


PAYMENT_UNAVAILABLE = (
    "I could not create the payment link just now, so nothing has been charged "
    "and your cart is unchanged. Please try again in a minute."
)


@router.get("/webhook")
async def verify_webhook(request: Request):
    """Meta calls this once when you click "Verify and save" in the dashboard."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == os.environ["WHATSAPP_VERIFY_TOKEN"]:
        print("[webhook] verification succeeded")
        # Must echo the challenge back as plain text, not JSON
        return Response(content=challenge, media_type="text/plain")

    print(f"[webhook] verification failed: mode={mode!r}")
    raise HTTPException(status_code=403, detail="Verification failed")


def _recommend(query: str, user_context: str | None, user_id: int | None = None):
    """Call the RAG contract, passing user_context only if it accepts one.

    The shared contract is get_product_recommendations(query, top_k). Adding
    user_context is agreed but not yet implemented on the RAG side, so this
    checks the signature instead of assuming — the bot keeps working either way.
    """
    parameters = inspect.signature(get_product_recommendations).parameters
    extra = {"user_id": user_id} if "user_id" in parameters else {}
    if user_context and "user_context" in parameters:
        return get_product_recommendations(query, user_context=user_context, **extra)
    return get_product_recommendations(query, **extra)


async def _safe_send(to: str, reply: "Reply") -> None:
    """Send without letting a failure escape into the webhook response.

    A failed send must not 500: Meta would treat the delivery as failed and
    redeliver the same inbound message, which fails identically. One lost reply
    beats an infinite retry loop.

    A Pay Now button falls back to plain text with the link in it, so a rejected
    interactive message still leaves the customer a way to pay.
    """
    try:
        if reply.cards:
            if reply.lead:
                await send_whatsapp_message(to, reply.lead)
            for card in reply.cards:
                photo = PRODUCT_PHOTOS / f"{card.product_id}.jpg"
                try:
                    if card.buttons:
                        try:
                            await send_product_card(to, photo, card.caption, [
                                (f"pick:{card.product_id}", "Choose"),
                                (f"hide:{card.product_id}", "Not for me"),
                            ])
                            continue
                        except Exception as exc:
                            print(f"[webhook] button card for {card.product_id} refused, plain photo: {exc!r}")
                    await send_image(to, photo, card.caption)
                except Exception as exc:
                    # No photo (or upload refused): the caption alone still reads fine
                    print(f"[webhook] photo for {card.product_id} not sent, sending text: {exc!r}")
                    await send_whatsapp_message(to, card.caption)
            if reply.tail:
                await send_whatsapp_message(to, reply.tail)
            return
        if reply.cta_url:
            try:
                await send_cta_url(to, reply.text, reply.cta_label, reply.cta_url)
                return
            except Exception as exc:
                print(f"[webhook] Pay Now button rejected, sending the link as text: {exc!r}")
                await send_whatsapp_message(to, f"{reply.text}\n\n{reply.cta_url}")
                return
        await send_whatsapp_message(to, reply.text)
    except Exception as exc:
        print(f"[webhook] reply to {to} not delivered: {exc!r}")


def _profile_names(value: dict) -> dict[str, str]:
    """Map wa_id -> profile name from the contacts block, when Meta sends one."""
    names = {}
    for contact in value.get("contacts", []):
        wa_id = contact.get("wa_id")
        name = (contact.get("profile") or {}).get("name")
        if wa_id and name:
            names[wa_id] = name
    return names


async def _handle_link_code(sender: str, text: str) -> str:
    """Bind this number to the website account that minted the code.

    A browser session can never reach us, so this one-time code is the only
    proof of who the sender is. After it succeeds, the number itself is enough.
    """
    token = text[len(LINK_PREFIX):].strip()
    try:
        user = await asyncio.to_thread(repo.consume_link_token, token, sender)
    except Exception as exc:
        print(f"[webhook] link failed: {exc!r}")
        return "Something went wrong linking your account. Please try again from the website."

    if user is None:
        return (
            "That link code is invalid or has expired. "
            "Open your account page on our website and tap Connect WhatsApp for a fresh one."
        )

    name = user.get("display_name") or "there"
    history = await asyncio.to_thread(repo.get_purchased_items, user["user_id"], 3)

    if history:
        recent = ", ".join(item["product_name"] for item in history)
        return (
            f"Welcome back, {name}! Your account is connected. "
            f"I can see your recent orders: {recent}. What are you shopping for today?"
        )
    return (
        f"Welcome back, {name}! Your account is connected. "
        "Tell me what you are looking for and I will suggest something from our collection."
    )


def _sizes_for(product_id: str) -> list[str]:
    product = catalog.get_by_id(product_id)
    return (product or {}).get("sizes_available") or []


def _product_id(product: dict) -> str:
    return product.get("product_id") or product.get("id") or ""


async def _handle_follow_up(
    user_id: int, text: str, previous: dict | None
) -> "str | Reply | None":
    """Handle cart commands, "2", and BUY. None means "treat it as a search".

    Cart commands come first and need no previous turn: "CART" works even as the
    very first message. Selection and BUY refer back to the last list shown.
    """
    command = parse_command(text)
    if command is not None:
        name, args = command
        if name == "add" and not (previous or {}).get("selected_product"):
            shown = (previous or {}).get("last_products_shown") or []
            if len(shown) == 1:
                # "add it in 42" right after a single suggestion: "it" is that one
                await asyncio.to_thread(repo.record_selection, user_id, shown[0])
                previous = {**previous, "selected_product": shown[0]}
            elif assistant.enabled():
                # "add the blue one in 42": the assistant can resolve which one
                return None
        try:
            return await _handle_cart_command(user_id, name, args, previous)
        except Exception as exc:
            print(f"[webhook] cart command {name!r} failed: {exc!r}")
            return "Sorry, I could not reach your cart just now. Please try again in a moment."

    code = find_product_code(text)
    product = catalog.get_by_id(code) if code else None
    if product is not None:
        # Arrived from a product page on the website: show it and select it, so
        # "ADD 42" or "BUY 42" works as the very next message
        chosen = ProductMatch.from_raw(product).model_dump()
        print(f"[webhook] product code {code} from the website")
        await asyncio.to_thread(repo.record_selection, user_id, chosen)
        return _detail_reply(describe_choice(chosen, 0, product.get("sizes_available") or []), product["id"])

    if not previous:
        return None

    shown = previous.get("last_products_shown") or []
    index = parse_selection(text, len(shown))
    if index is not None:
        product = shown[index]
        print(f"[webhook] selection: item {index + 1} ({product.get('name')})")
        await asyncio.to_thread(repo.record_selection, user_id, product)
        return _detail_reply(describe_choice(product, index, _sizes_for(_product_id(product))),
                             _product_id(product))

    bare_size = parse_bare_size(text)
    if bare_size:
        reply = await _bare_size(user_id, bare_size, previous)
        if reply is not None:
            return reply

    buy_size = parse_buy(text)
    if buy_size is not None:
        chosen = previous.get("selected_product")
        if not chosen:
            return (
                "Tell me which item you would like first — reply with its number, "
                "or describe what you are looking for."
            )
        return await _place_order(user_id, chosen, buy_size)

    return None


async def _bare_size(user_id: int, text: str, previous: dict) -> str | None:
    """A message that is only a size ("xxl", "42", "8") answers "which size?".

    It sizes the cart line still waiting for one (the picked item's first), or
    else adds the picked item in that size. Live, "add this one to cart" then
    "xxl" was refused twice because "xxl" alone did not ask to add anything.
    """
    chosen = previous.get("selected_product")
    chosen_id = _product_id(chosen) if chosen else None
    cart = await asyncio.to_thread(repo.get_cart, user_id)
    missing = _missing_sizes(cart)
    waiting = next((line for line in missing if line[1]["product_id"] == chosen_id), None) or (
        missing[-1] if missing else None)
    if waiting is not None:
        position, item = waiting
        if match_size(text, item["sizes_available"]) or item["product_id"] == chosen_id:
            return await _handle_cart_command(user_id, "size", {"position": position, "size": text}, previous)
    if chosen:
        return await _handle_cart_command(user_id, "add", {"size": text}, previous)
    return None


HELP_TEXT = "\n".join(
    [
        "Here is how I can help:",
        "• Describe what you want — \"sherwani for my wedding\", \"saree for office\"",
        "• Reply with a number to pick an item from my list",
        "• ADD M — add it to your cart in size M (or just ADD)",
        "• BUY M — order only that item",
        "• CART — see your cart · REMOVE 2 · SIZE 1 L",
        "• CHECKOUT — pay for everything in your cart",
        "• ORDERS — track your orders",
    ]
)


def _greeting(user: dict | None, history: list[dict]) -> str:
    """Welcome message; linked customers are recognised by name and history."""
    name = (user or {}).get("display_name")
    opening = f"Hi {name}!" if name else "Hi!"
    lines = [f"{opening} I am the Kurta & Co. shopping assistant."]
    if history:
        lines.append(f"Last time you picked up the {history[0]['product_name']}.")
    lines.append(
        "Tell me what you are shopping for — for example \"sherwani for my wedding\" "
        "or \"saree for office\" — and I will suggest pieces from our collection."
    )
    lines.append("Reply HELP any time to see everything I can do.")
    return "\n".join(lines)


async def _format_orders(user_id: int) -> str:
    """Recent orders with status; unpaid ones carry their payment link again."""
    history = await asyncio.to_thread(repo.get_order_history, user_id, 3)
    if not history:
        return "You have no orders yet. Tell me what you are looking for."

    lines = ["Your recent orders:"]
    for summary in history:
        order = await asyncio.to_thread(repo.get_order, summary["order_id"])
        items = order["items"]
        what = items[0]["product_name"] if len(items) == 1 else f"{len(items)} items"
        line = f"#{order['order_id']} · {what} · Rs. {order['total_inr']:,.0f} · "
        if order["status"] == "captured":
            line += "paid"
            if order["deliver_by"]:
                line += f", arriving by {order['deliver_by']:%d %b}"
        elif order["status"] == "failed":
            line += "cancelled"
        else:
            line += "awaiting payment"
            if order["payment_link_url"]:
                line += f" — pay here: {order['payment_link_url']}"
        lines.append(line)

    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base_url:
        lines.append(f"\nFull details on the website: {base_url}/account")
    return "\n".join(lines)


def _format_cart(cart: dict) -> str:
    if not cart["items"]:
        return "Your cart is empty. Tell me what you are looking for."

    lines = ["Your cart:"]
    for position, item in enumerate(cart["items"], start=1):
        if not item["available"]:
            lines.append(f"{position}. {item['name']} · no longer available")
            continue
        size = item["size"] or "size not chosen"
        lines.append(
            f"{position}. {item['name']} · {size} · "
            f"{item['quantity']} × Rs. {item['price_inr']:,.0f}"
        )
    noun = "item" if cart["count"] == 1 else "items"
    lines.append(f"Total: Rs. {cart['total_inr']:,.0f} ({cart['count']} {noun})")
    lines.append("")
    lines.append(_cart_hints(cart))
    return "\n".join(lines)


def _cart_hints(cart: dict) -> str:
    """Command hints using this cart's own positions and sizes, not placeholders."""
    missing = _missing_sizes(cart)
    if missing:
        position, item = missing[0]
        sizes = item["sizes_available"]
        return f"Reply with the size you want ({', '.join(sizes)})"
    last = len(cart["items"])
    return f"REMOVE {last} to drop an item · CHECKOUT to pay"


def _missing_sizes(cart: dict) -> list[tuple[int, dict]]:
    """Lines that still need a size before they can be ordered."""
    return [
        (position, item)
        for position, item in enumerate(cart["items"], start=1)
        if item["available"] and not item["size"] and len(item["sizes_available"]) > 1
    ]


def _ask_for_sizes(missing: list[tuple[int, dict]]) -> str:
    lines = ["Choose a size before checkout:"]
    for position, item in missing:
        lines.append(f"{position}. {item['name']} — {', '.join(item['sizes_available'])}")
    first_position, first_item = missing[0]
    example = first_item["sizes_available"][len(first_item["sizes_available"]) // 2]
    lines.append(f"Reply SIZE {first_position} {example}, for example.")
    return "\n".join(lines)


async def _handle_cart_command(
    user_id: int, name: str, args: dict, previous: dict | None
) -> str:
    if name == "help":
        return HELP_TEXT

    if name == "orders":
        return await _format_orders(user_id)

    if name == "cart":
        return _format_cart(await asyncio.to_thread(repo.get_cart, user_id))

    if name == "clear":
        await asyncio.to_thread(repo.clear_cart, user_id)
        return "Your cart is empty now. Tell me what you are looking for."

    if name == "add":
        chosen = (previous or {}).get("selected_product")
        if not chosen:
            return (
                "Pick an item first — reply with its number from the list, "
                "or tell me what you are looking for."
            )
        product_id = _product_id(chosen)
        sizes = _sizes_for(product_id)
        size = ""
        if args["size"]:
            size = match_size(args["size"], sizes) or ""
            if not size:
                return f"{chosen.get('name', 'That item')} comes in {', '.join(sizes)}. Reply ADD M, for example."

        cart = await asyncio.to_thread(repo.add_to_cart, user_id, product_id, size)
        added = next(
            (i for i in reversed(cart["items"]) if i["product_id"] == product_id and i["size"] == size),
            None,
        )
        shown_size = (added or {}).get("size") or size
        note = f" (size {shown_size})" if shown_size else " (size not chosen yet)"
        noun = "item" if cart["count"] == 1 else "items"
        hint = (
            _cart_hints(cart) + ", or keep browsing."
            if _missing_sizes(cart)
            else "Reply CHECKOUT to pay, CART to review, or keep browsing."
        )
        return (
            f"Added {chosen.get('name', 'it')}{note} to your cart.\n"
            f"Cart: {cart['count']} {noun}, Rs. {cart['total_inr']:,.0f}.\n{hint}"
        )

    cart = await asyncio.to_thread(repo.get_cart, user_id)

    if name in ("remove", "size"):
        position = args["position"]
        if not 1 <= position <= len(cart["items"]):
            return f"There is no item {position} in your cart. Reply CART to see it."
        item = cart["items"][position - 1]

        if name == "remove":
            await asyncio.to_thread(repo.remove_from_cart, user_id, item["cart_item_id"])
            remaining = await asyncio.to_thread(repo.get_cart, user_id)
            return f"Removed {item['name']}.\n\n" + _format_cart(remaining)

        size = match_size(args["size"], item["sizes_available"])
        if not size:
            return f"{item['name']} comes in {', '.join(item['sizes_available'])}."
        await asyncio.to_thread(repo.update_cart_item_size, user_id, item["cart_item_id"], size)
        updated = await asyncio.to_thread(repo.get_cart, user_id)
        return f"{item['name']} set to size {size}.\n\n" + _format_cart(updated)

    if name == "checkout":
        if not any(item["available"] for item in cart["items"]):
            return "Your cart is empty. Tell me what you are looking for, then reply ADD."
        missing = _missing_sizes(cart)
        if missing:
            return _ask_for_sizes(missing)

        try:
            placed = await checkout.checkout_cart(user_id, "bot")
        except payments.PaymentError as exc:
            print(f"[webhook] payment link failed: {exc}")
            return PAYMENT_UNAVAILABLE
        if placed is None:
            return "Your cart is empty. Tell me what you are looking for, then reply ADD."
        print(f"[webhook] order {placed['order_id']} ready to pay for user {user_id}")
        noun = "item" if placed["item_count"] == 1 else "items"
        summary = f"{placed['item_count']} {noun}, total Rs. {placed['total_inr']:,.0f}"
        headline = (
            f"Order #{placed['order_id']} is still waiting for payment — {summary}."
            if placed["reused"]
            else f"Order #{placed['order_id']} created — {summary}."
        )
        return Reply(
            text=f"{headline}\nTap Pay Now to pay securely by UPI or card.",
            cta_url=placed["url"],
        )

    return "Sorry, I did not understand that. Reply CART to see your cart."


async def _place_order(user_id: int, product: dict, size_token: str = "") -> "str | Reply":
    """Single-item BUY: create the order and send a Pay Now button for it.

    Refuses to create an order without a size for products that have several —
    an unsized order for a kurta is not something a shop can fulfil.
    """
    name = product.get("name", "your item")
    price = product.get("price_inr", product.get("price", 0))
    product_id = _product_id(product)
    sizes = _sizes_for(product_id)

    size = ""
    if size_token:
        size = match_size(size_token, sizes) or ""
        if not size:
            return f"{name} comes in {', '.join(sizes)}. Reply BUY M, for example."
    elif len(sizes) > 1:
        example = sizes[len(sizes) // 2]
        return f"Which size? {name} comes in {', '.join(sizes)}. Reply BUY {example}, for example."

    try:
        placed = await checkout.checkout_single(user_id, product_id, size, 1, "bot")
    except payments.PaymentError as exc:
        print(f"[webhook] payment link failed: {exc}")
        return PAYMENT_UNAVAILABLE
    except Exception as exc:
        print(f"[webhook] order creation failed: {exc!r}")
        return "Sorry, I could not place that order just now. Please try again in a moment."

    print(f"[webhook] order {placed['order_id']} ready to pay for user {user_id}")
    size_note = f" (size {size})" if size else ""
    return Reply(
        text=(
            f"Order #{placed['order_id']} created — {name}{size_note} for Rs. {price:,.0f}.\n"
            "Tap Pay Now to pay securely by UPI or card."
        ),
        cta_url=placed["url"],
    )


def _build_user_context(user: dict | None, history: list[dict]) -> str | None:
    """Condense who the customer is and what they own into prompt-ready text."""
    if not user or not user.get("is_linked"):
        return None

    lines = [f"Customer name: {user.get('display_name') or 'unknown'}"]
    if history:
        lines.append("Previously purchased:")
        lines.extend(
            f"- {order['product_name']} (Rs. {order['price_inr']:.0f})" for order in history
        )
    else:
        lines.append("No previous purchases.")
    return "\n".join(lines)


def _reply_text(reply: "str | Reply") -> str:
    return reply.text if isinstance(reply, Reply) else reply


async def _remember(user_id: int | None, text: str, answer: str) -> None:
    """Add this exchange to the assistant's memory. Never blocks a reply."""
    if user_id is None:
        return
    try:
        await asyncio.to_thread(
            repo.remember_messages, user_id,
            [{"role": "user", "content": text[:500]}, {"role": "assistant", "content": answer[:700]}],
        )
    except Exception as exc:
        print(f"[webhook] could not remember the exchange: {exc!r}")


async def _log(user_id: int | None, question: str, answer: "str | Reply", product_ids: list[str], path: str) -> None:
    """Keep the exchange for the owner to rate on /admin/training. Never blocks a reply."""
    if user_id is None:
        return
    try:
        await asyncio.to_thread(training.log_reply, user_id, question, _reply_text(answer), product_ids, path)
    except Exception as exc:
        print(f"[webhook] could not log the reply: {exc!r}")


async def _record_shown(user_id: int | None, text: str, products: list[dict]) -> list[ProductMatch]:
    """Store the numbered list so "2", ADD and BUY refer to it next turn."""
    # Chroma metadata and catalogue records use other keys; normalise first
    matches = [ProductMatch.from_raw(product) for product in products]
    if user_id is not None:
        try:
            await asyncio.to_thread(
                repo.record_turn, user_id, text, [match.model_dump() for match in matches]
            )
        except Exception as exc:
            print(f"[webhook] could not record turn: {exc!r}")
    return matches


def _add_hint(product: dict) -> str:
    sizes = product.get("sizes_available") or []
    if len(sizes) > 1:
        return f"Sizes: {', '.join(sizes)}. Reply ADD {sizes[len(sizes) // 2]} to add it · CART to see your cart"
    return "Reply ADD to add it to your cart · CART to see your cart"


async def _assistant_reply(user_id: int, text: str, context: dict) -> "str | Reply":
    """Let the LLM assistant answer; raises AssistantUnavailable to fall back."""
    answer = await asyncio.to_thread(assistant.respond, user_id, text, context)
    print(f"[webhook] assistant used {answer.tools_used or 'no tools'}")
    await _remember(user_id, text, assistant.memory_text(answer))

    if answer.products:
        await _record_shown(user_id, text, answer.products)
        if answer.detail:
            # It described one product in its own words: send that with its photo
            product = answer.products[0]
            cart = await asyncio.to_thread(repo.get_cart, user_id)
            in_cart = any(item["product_id"] == product["id"] for item in cart["items"])
            hint = "Already in your cart · CART to review · CHECKOUT to pay" if in_cart else _add_hint(product)
            result: "str | Reply" = _detail_reply(answer.message, product["id"], hint)
        else:
            result = _list_reply(answer.message, answer.products, results_footer(len(answer.products)))
        await _log(user_id, text, result, [p["id"] for p in answer.products], "assistant")
        return result

    parts = [answer.message] if answer.message else []
    if answer.attach == "orders":
        parts.append(await _format_orders(user_id))
    elif answer.attach == "cart":
        parts.append(_format_cart(await asyncio.to_thread(repo.get_cart, user_id)))
    result = "\n\n".join(parts)
    await _log(user_id, text, result, [], "assistant")
    return result


async def _search_reply(user_id: int | None, text: str, user_context: str | None) -> "str | Reply":
    """The plain path: retrieval plus a one-line intro. Always answers."""
    # get_product_recommendations is synchronous and does network I/O
    reply, raw_matches = await asyncio.to_thread(_recommend, text, user_context, user_id)
    matches = await _record_shown(user_id, text, raw_matches)

    shown = "; ".join(f"{n}. {m.product_id} {m.name}" for n, m in enumerate(matches, 1))
    await _remember(user_id, text, f"{reply.split(chr(10))[0]}\n[Showed: {shown}]" if shown else reply)
    await _log(user_id, text, reply, [m.product_id for m in matches], "search")

    # Contract C1: the RAG side returns intro + numbered list; the command hints
    # belong to whoever owns the commands
    footer = results_footer(len(matches))
    products = [p for p in (catalog.get_by_id(m.product_id) for m in matches) if p]
    if products and len(products) == len(matches):
        lines = format_product_lines(products)
        lead = reply[: reply.rfind(lines)].strip() if lines in reply else ""
        return _list_reply(lead, products, footer)
    return f"{reply}\n\n{footer}" if footer else reply


async def _handle_text(sender: str, text: str, display_name: str | None) -> "str | Reply":
    """Persist the contact, answer the message, and remember the exchange.

    Order matters: exact commands ("2", ADD 42, CART, CHECKOUT, a product code)
    are instant and deterministic, and payment never depends on the LLM. Only
    free text reaches the assistant, and if it fails the plain search answers.
    Database problems must never stop a reply going out — a dead Postgres
    costs us conversation memory, not the conversation.
    """
    if text.strip().upper().startswith(LINK_PREFIX):
        return await _handle_link_code(sender, text.strip())

    user_id = None
    user = None
    previous: dict | None = None
    history: list[dict] = []
    try:
        user_id = await asyncio.to_thread(repo.get_or_create_user, sender, display_name)
        user = await asyncio.to_thread(repo.get_user_by_whatsapp, sender)
        if user and user.get("is_linked"):
            history = await asyncio.to_thread(repo.get_purchased_items, user_id, 5)
        previous = await asyncio.to_thread(repo.get_active_session, user_id)
        if previous and previous.get("last_query"):
            print(f"[webhook] prior turn for {sender}: {previous['last_query']!r}")

        follow_up = await _handle_follow_up(user_id, text, previous)
        if follow_up is not None:
            await _remember(user_id, text, _reply_text(follow_up))
            return follow_up

        # After commands, so "hi" never shadows one; before search, so it never
        # reaches retrieval and comes back as three random products
        if is_greeting(text):
            greeting = _greeting(user, history)
            await _remember(user_id, text, greeting)
            return greeting
    except Exception as exc:
        print(f"[webhook] user lookup failed, continuing stateless: {exc!r}")

    # "What's my total?", "show my cart", "my orders": facts, so code answers.
    # The model once replied "Your current total is Rs 17,498" by adding up two
    # prices from memory while the real cart held Rs 10,798.
    if user_id is not None and _is_lookup(text):
        try:
            if _ASKS_CART.search(text) or _ASKS_TOTAL.search(text):
                answer = _format_cart(await asyncio.to_thread(repo.get_cart, user_id))
            else:
                answer = await _format_orders(user_id)
            await _remember(user_id, text, answer)
            await _log(user_id, text, answer, [], "lookup")
            return answer
        except Exception as exc:
            print(f"[webhook] orders/cart lookup failed: {exc!r}")

    if user_id is not None and assistant.enabled():
        try:
            cart = await asyncio.to_thread(repo.get_cart, user_id)
            context = {
                "name": (user or {}).get("display_name") or display_name,
                "purchases": [f"{h['product_name']} (Rs {h['price_inr']:.0f})" for h in history],
                "cart_count": cart["count"],
                "cart": cart,
                "history": (previous or {}).get("messages") or [],
                "last_products_shown": (previous or {}).get("last_products_shown") or [],
                "selected_product": (previous or {}).get("selected_product"),
            }
            return await _assistant_reply(user_id, text, context)
        except assistant.AssistantUnavailable as exc:
            print(f"[webhook] assistant unavailable ({exc}); answering with plain search")
        except Exception as exc:
            print(f"[webhook] assistant failed ({exc!r}); answering with plain search")

    # Without the assistant (no key, rate-limited, down), the common questions
    # that are not searches still get a real answer instead of "no match"
    if user_id is not None:
        try:
            if _ASKS_ORDERS.search(text):
                answer = await _format_orders(user_id)
                await _remember(user_id, text, answer)
                return answer
            if _ASKS_CART.search(text):
                answer = _format_cart(await asyncio.to_thread(repo.get_cart, user_id))
                await _remember(user_id, text, answer)
                return answer
        except Exception as exc:
            print(f"[webhook] orders/cart lookup failed: {exc!r}")

    user_context = _build_user_context(user, history)
    if user_context:
        print(f"[webhook] personalizing for linked user {user_id}")
    return await _search_reply(user_id, text, user_context)


_ASKS_ORDERS = re.compile(
    r"\b(?:my|previous|past|recent|last)\s+(?:orders?|purchases?)\b|\bwhere\s+is\s+my\s+(?:order|parcel|package)\b"
    r"|\bwhat\s+(?:did|have)\s+i\s+(?:buy|bought|order(?:ed)?)\b|\border\s+(?:status|history)\b|\btrack\w*\s+(?:my\s+)?order",
    re.IGNORECASE,
)
_ASKS_CART = re.compile(
    r"\b(?:my|the)\s+(?:cart|bag|basket)\b|\bwhat'?s\s+in\s+(?:my|the)\b|\bshow\s+(?:me\s+)?(?:my\s+)?cart\b",
    re.IGNORECASE,
)
_ASKS_TOTAL = re.compile(
    r"\b(?:my|the|cart|bag|order)\s+total\b|\btotal\s+(?:amount|price|cost|bill)\b|\bwhat'?s\s+the\s+total\b"
    r"|\bhow\s+much\s+(?:do\s+i\s+(?:owe|have\s+to\s+pay|need\s+to\s+pay)|is\s+(?:my|the)\s+(?:cart|bag|total|bill))\b",
    re.IGNORECASE,
)
# Wanting to change the cart is the assistant's job, not a lookup
_CHANGES_CART = re.compile(r"\b(?:add|remove|delete|drop|put|take\s+out|replace|change|buy|checkout)\b", re.IGNORECASE)


def _is_lookup(text: str) -> bool:
    """A short question that is only asking to see the cart, total or orders.

    Longer messages ("what did I buy last time? suggest something to go with
    it") go to the assistant, which can do both.
    """
    if len(text.split()) > 9 or _CHANGES_CART.search(text):
        return False
    return bool(_ASKS_CART.search(text) or _ASKS_TOTAL.search(text) or _ASKS_ORDERS.search(text))


# Meta redelivers a message when our 200 is slow or lost. Answering twice would
# send two replies (or add to the cart twice), so recent message ids are kept.
_SEEN_MESSAGES: "OrderedDict[str, None]" = OrderedDict()


def _first_delivery(message_id: str | None) -> bool:
    if not message_id:
        return True
    if message_id in _SEEN_MESSAGES:
        return False
    _SEEN_MESSAGES[message_id] = None
    while len(_SEEN_MESSAGES) > 500:
        _SEEN_MESSAGES.popitem(last=False)
    return True


async def _handle_button(sender: str, button_id: str, display_name: str | None) -> "str | Reply":
    """A tap on "Choose" or "Not for me" under a product photo."""
    action, _, product_id = button_id.partition(":")
    product = catalog.get_by_id(product_id)
    if product is None or action not in ("pick", "hide"):
        return "That item is no longer available. Tell me what you are looking for."
    user_id = await asyncio.to_thread(repo.get_or_create_user, sender, display_name)

    if action == "pick":
        chosen = ProductMatch.from_raw(product).model_dump()
        await asyncio.to_thread(repo.record_selection, user_id, chosen)
        reply = _detail_reply(describe_choice(chosen, 0, product.get("sizes_available") or []), product["id"])
        await _remember(user_id, f"[tapped Choose on {product['name']}]", reply.text)
        return reply

    # "Not for me": personal only. It hides the item from this customer's future
    # results; the owner's training on /admin/training is what changes everyone's.
    previous = await asyncio.to_thread(repo.get_active_session, user_id)
    question = (previous or {}).get("last_query") or ""
    await asyncio.to_thread(training.rate, "customer", -1, question, product["id"], None, user_id,
                            "Not for me (button)")
    answer = (f"Got it — I won't show you the {product['name']} again. "
              "Pick another from the list, or tell me what you'd prefer instead.")
    await _remember(user_id, f"[tapped Not for me on {product['name']}]", answer)
    return answer


async def _handle_incoming(sender: str, text: str, display_name: str | None, button_id: str = "") -> "str | Reply":
    if button_id:
        return await _handle_button(sender, button_id, display_name)
    return await _handle_text(sender, text, display_name)


async def _answer_and_send(sender: str, text: str, display_name: str | None, button_id: str = "") -> None:
    """Background work for a real message: the LLM may take a few seconds."""
    try:
        reply = await _handle_incoming(sender, text, display_name, button_id)
    except Exception as exc:
        print(f"[webhook] could not answer {sender}: {exc!r}")
        reply = "Sorry, something went wrong on our side. Please send that again."
    await _safe_send(sender, reply if isinstance(reply, Reply) else Reply(reply))


@router.post("/webhook")
async def receive_message(request: Request, background: BackgroundTasks):
    """Receives incoming messages and status updates from WhatsApp.

    Real messages are answered in a background task, so Meta gets its 200 at
    once however long the assistant thinks. Local tests (X-Local-Test) are
    answered inline and the reply is returned in the response body instead.
    """
    payload = await request.json()
    local_test = request.headers.get("X-Local-Test") == "true"
    responses = []

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                if "statuses" in value:
                    # sent/delivered/read receipts for messages we sent
                    for status in value["statuses"]:
                        print(f"[webhook] status: {status['status']} for {status['recipient_id']}")

                names = _profile_names(value)

                for message in value.get("messages", []):
                    sender = message["from"]
                    button = (message.get("interactive") or {}).get("button_reply") \
                        if message["type"] == "interactive" else None
                    if message["type"] != "text" and not button:
                        print(f"[webhook] unhandled {message['type']!r} message from {sender}")
                        continue
                    # Local tests reuse one fake id; only Meta's real ids are unique
                    if not local_test and not _first_delivery(message.get("id")):
                        print(f"[webhook] duplicate delivery of {message.get('id')} ignored")
                        continue
                    button_id = (button or {}).get("id", "")
                    text = message["text"]["body"] if not button else button.get("title", "")
                    print(f"[webhook] {'button ' + button_id if button else 'message'} from {sender}: {text}")
                    if not local_test:
                        background.add_task(_answer_and_send, sender, text, names.get(sender), button_id)
                        continue
                    reply = await _handle_incoming(sender, text, names.get(sender), button_id)
                    if isinstance(reply, str):
                        reply = Reply(reply)
                    responses.append({"to": sender, "body": reply.text, "cta_url": reply.cta_url,
                                      "photos": [card.product_id for card in reply.cards]})
    except Exception as e:
        # Deliberately broad: any uncaught error here becomes a 500, which Meta
        # reads as failed delivery and retries — the same message arrives again
        # and fails again. Acking 200 is always the right answer.
        print(f"[webhook] handler error: {e!r} payload={payload}")

    return {"status": "received", "responses": responses}
