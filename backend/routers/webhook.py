import asyncio
import inspect
import os
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response

import catalog
import checkout
import payments
import repository as repo
from bot.chat import get_product_recommendations
from common.schemas import ProductMatch
from repository import LINK_PREFIX
from selection import (
    describe_choice,
    match_size,
    parse_buy,
    parse_command,
    parse_selection,
    results_footer,
)
from whatsapp import send_cta_url, send_whatsapp_message

router = APIRouter()


@dataclass
class Reply:
    """A reply that carries a button, not just text — used for Pay Now.

    Handlers return a plain str for text, or a Reply when there is a link to tap.
    """

    text: str
    cta_url: str | None = None
    cta_label: str = "Pay Now"


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


def _recommend(query: str, user_context: str | None):
    """Call the RAG contract, passing user_context only if it accepts one.

    The shared contract is get_product_recommendations(query, top_k). Adding
    user_context is agreed but not yet implemented on the RAG side, so this
    checks the signature instead of assuming — the bot keeps working either way.
    """
    if user_context and "user_context" in inspect.signature(
        get_product_recommendations
    ).parameters:
        return get_product_recommendations(query, user_context=user_context)
    return get_product_recommendations(query)


async def _safe_send(to: str, reply: "Reply") -> None:
    """Send without letting a failure escape into the webhook response.

    A failed send must not 500: Meta would treat the delivery as failed and
    redeliver the same inbound message, which fails identically. One lost reply
    beats an infinite retry loop.

    A Pay Now button falls back to plain text with the link in it, so a rejected
    interactive message still leaves the customer a way to pay.
    """
    try:
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
) -> str | None:
    """Handle cart commands, "2", and BUY. None means "treat it as a search".

    Cart commands come first and need no previous turn: "CART" works even as the
    very first message. Selection and BUY refer back to the last list shown.
    """
    command = parse_command(text)
    if command is not None:
        name, args = command
        try:
            return await _handle_cart_command(user_id, name, args, previous)
        except Exception as exc:
            print(f"[webhook] cart command {name!r} failed: {exc!r}")
            return "Sorry, I could not reach your cart just now. Please try again in a moment."

    if not previous:
        return None

    shown = previous.get("last_products_shown") or []
    index = parse_selection(text, len(shown))
    if index is not None:
        product = shown[index]
        print(f"[webhook] selection: item {index + 1} ({product.get('name')})")
        await asyncio.to_thread(repo.record_selection, user_id, product)
        return describe_choice(product, index, _sizes_for(_product_id(product)))

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
        return f"Reply SIZE {position} {sizes[len(sizes) // 2]} to choose a size ({', '.join(sizes)})"
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


async def _handle_text(sender: str, text: str, display_name: str | None) -> str:
    """Persist the contact, answer the query, and record the turn.

    Database problems must never stop a reply going out — a dead Postgres
    costs us conversation memory, not the conversation.
    """
    if text.strip().upper().startswith(LINK_PREFIX):
        return await _handle_link_code(sender, text.strip())

    user_id = None
    user = None
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
            return follow_up
    except Exception as exc:
        print(f"[webhook] user lookup failed, continuing stateless: {exc!r}")

    user_context = _build_user_context(user, history)
    if user_context:
        print(f"[webhook] personalizing for linked user {user_id}")

    # get_product_recommendations is synchronous and does network I/O.
    # user_context is passed only when the RAG side supports it, so Dev B's
    # current two-argument signature keeps working untouched.
    reply, raw_matches = await asyncio.to_thread(
        _recommend, text, user_context
    )

    # Chroma metadata is unvalidated, so normalize before anything stores it
    matches = [ProductMatch.from_raw(match) for match in raw_matches]

    if user_id is not None:
        try:
            await asyncio.to_thread(
                repo.record_turn,
                user_id,
                text,
                [match.model_dump() for match in matches],
            )
        except Exception as exc:
            print(f"[webhook] could not record turn: {exc!r}")

    # Contract C1: the RAG side returns intro + numbered list; the command hints
    # belong to whoever owns the commands
    footer = results_footer(len(matches))
    return f"{reply}\n\n{footer}" if footer else reply


@router.post("/webhook")
async def receive_message(request: Request):
    """Receives incoming messages and status updates from WhatsApp."""
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
                    if message["type"] == "text":
                        text = message["text"]["body"]
                        print(f"[webhook] message from {sender}: {text}")
                        reply = await _handle_text(sender, text, names.get(sender))
                        if isinstance(reply, str):
                            reply = Reply(reply)
                        responses.append(
                            {"to": sender, "body": reply.text, "cta_url": reply.cta_url}
                        )
                        if not local_test:
                            await _safe_send(sender, reply)
                    else:
                        print(f"[webhook] unhandled {message['type']!r} message from {sender}")
    except Exception as e:
        # Deliberately broad: any uncaught error here becomes a 500, which Meta
        # reads as failed delivery and retries — the same message arrives again
        # and fails again. Acking 200 is always the right answer.
        print(f"[webhook] handler error: {e!r} payload={payload}")

    return {"status": "received", "responses": responses}
