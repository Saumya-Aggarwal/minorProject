"""Payment orchestration shared by the bot, the website and both confirmation paths.

    start_payment()          order → Razorpay Payment Link → stored on the order
    confirm_payment_link()   customer redirected back: ask Razorpay if it is paid
    confirm_from_webhook()   Razorpay's webhook says it is paid (signature checked)

Both confirmations end in repository.mark_order_paid(), which is idempotent and
row-locked. However many times and in whatever order the two paths fire, the
customer gets exactly one confirmation message.

The agent does everything except authorise the payment: RBI rules require the
customer's own UPI PIN or OTP, entered on Razorpay's page, not in our code.
"""

import asyncio
import os
from typing import Any, Optional

import payments
import repository as repo
from whatsapp import send_whatsapp_message


def _describe(order: dict[str, Any]) -> str:
    items = order["items"]
    if len(items) == 1:
        return f"Kurta & Co. order #{order['order_id']}: {items[0]['product_name']}"
    return f"Kurta & Co. order #{order['order_id']}: {len(items)} items"


def whatsapp_chat_url() -> Optional[str]:
    """A link that opens the chat with our WhatsApp number, or None if unset."""
    number = os.getenv("WHATSAPP_DISPLAY_NUMBER", "").lstrip("+").replace(" ", "")
    return f"https://wa.me/{number}" if number else None


def _return_url(order: dict[str, Any]) -> Optional[str]:
    """Where the customer's browser goes after paying.

    Chat orders go straight back to the WhatsApp chat, where the confirmation
    message is waiting. Sending them to our own page instead meant a phone
    browser that had never visited the site hit ngrok's free-plan warning page
    ("You are about to visit...") at the moment of paying.

    Website orders return to our confirmation page: that browser has already
    been through the warning once, so it does not appear again.

    None means payments.py's default, our /payments/callback page.
    """
    if order["channel"] == "bot":
        return whatsapp_chat_url()
    return None


async def start_payment(order_id: int) -> str:
    """Create the payment link for an order and store it. Returns the URL.

    Raises payments.PaymentError if Razorpay refuses or cannot be reached; the
    order itself is kept either way, so the customer can retry from /account.
    """
    order = await asyncio.to_thread(repo.get_order, order_id)
    if order is None:
        raise ValueError(f"unknown order {order_id}")
    if order["payment_link_url"]:
        # Paying twice for one order must be impossible, so one link per order
        return order["payment_link_url"]

    link = await payments.create_payment_link(
        order_id,
        order["total_inr"],
        _describe(order),
        customer_name=order["customer_name"],
        customer_phone=order["whatsapp_number"],
        customer_email=order["customer_email"],
        callback_url=_return_url(order),
    )
    await asyncio.to_thread(repo.attach_payment_link, order_id, link["id"], link["short_url"])
    print(f"[checkout] order {order_id} -> payment link {link['id']}")
    return link["short_url"]


def _lines(items: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    return {(item["product_id"], item["size"], item["quantity"]) for item in items}


async def checkout_cart(user_id: int, channel: str) -> Optional[dict[str, Any]]:
    """Order the cart and return a payment link. None if the cart is empty.

    Typing CHECKOUT twice must not create two orders: if an unpaid order for
    exactly this cart already has a link, that link is sent again.

    Raises ValueError when a line still needs a size, and payments.PaymentError
    when Razorpay cannot create the link — in which case the new order is
    cancelled, so a retry starts clean instead of leaving a dangling order.
    """
    cart = await asyncio.to_thread(repo.get_cart, user_id)
    current = [item for item in cart["items"] if item["available"]]
    if not current:
        return None

    pending = await asyncio.to_thread(repo.get_pending_cart_order, user_id)
    if pending and _lines(pending["items"]) == _lines(current):
        return {
            "order_id": pending["order_id"],
            "total_inr": pending["total_inr"],
            "item_count": sum(item["quantity"] for item in pending["items"]),
            "url": pending["payment_link_url"],
            "reused": True,
        }

    placed = await asyncio.to_thread(repo.create_order_from_cart, user_id, channel)
    if placed is None:
        return None
    try:
        url = await start_payment(placed["order_id"])
    except Exception:
        await asyncio.to_thread(repo.cancel_order, placed["order_id"])
        raise
    return {**placed, "url": url, "reused": False}


async def checkout_single(
    user_id: int, product_id: str, size: str, quantity: int, channel: str
) -> dict[str, Any]:
    """Buy one product now, bypassing the cart. Same error contract as checkout_cart."""
    order_id = await asyncio.to_thread(
        repo.create_order_for_product, user_id, product_id, size, quantity, channel
    )
    try:
        url = await start_payment(order_id)
    except Exception:
        await asyncio.to_thread(repo.cancel_order, order_id)
        raise
    order = await asyncio.to_thread(repo.get_order, order_id)
    return {"order_id": order_id, "total_inr": order["total_inr"], "url": url}


async def _record_paid(
    order_id: int, payment_id: str, razorpay_order_id: Optional[str]
) -> dict[str, Any]:
    first_time = await asyncio.to_thread(
        repo.mark_order_paid, order_id, payment_id, razorpay_order_id
    )
    order = await asyncio.to_thread(repo.get_order, order_id)
    if first_time:
        print(f"[checkout] order {order_id} paid ({payment_id})")
        await notify_paid(order)
    return {"order": order, "first_time": first_time}


async def confirm_payment_link(payment_link_id: str) -> Optional[dict[str, Any]]:
    """Confirm by asking Razorpay directly, not by trusting the redirect URL.

    The customer's browser brings the link id back to us in query parameters,
    which anyone could type. Fetching the link from Razorpay with our secret key
    is authoritative: a forged URL cannot make Razorpay report "paid".

    Returns None if the link is unknown to us; otherwise the order and whether
    this call was the one that recorded the payment.
    """
    order_id = await asyncio.to_thread(repo.get_order_id_by_payment_link, payment_link_id)
    if order_id is None:
        print(f"[checkout] callback for unknown payment link {payment_link_id}")
        return None

    link = await payments.fetch_payment_link(payment_link_id)
    payment_id = payments.paid_payment_id(link)
    if payment_id is None:
        order = await asyncio.to_thread(repo.get_order, order_id)
        return {"order": order, "first_time": False, "link_status": link.get("status")}
    return await _record_paid(order_id, payment_id, link.get("order_id"))


async def confirm_from_webhook(
    payment_link_id: str, payment_id: str, razorpay_order_id: Optional[str]
) -> Optional[dict[str, Any]]:
    """The webhook's payload is already signature-verified, so no API round trip."""
    order_id = await asyncio.to_thread(repo.get_order_id_by_payment_link, payment_link_id)
    if order_id is None:
        print(f"[checkout] webhook for unknown payment link {payment_link_id}")
        return None
    return await _record_paid(order_id, payment_id, razorpay_order_id)


async def notify_paid(order: dict[str, Any]) -> None:
    """Confirm on WhatsApp — for web orders too, when the customer has linked.

    Free-form text only reaches someone who messaged us in the last 24 hours;
    outside that window Meta accepts the send but does not deliver it. Covering
    that needs an approved message template, which a test number cannot get.
    """
    number = order.get("whatsapp_number")
    if not number:
        return

    lines = [f"Payment received — thank you! Order #{order['order_id']} is confirmed."]
    for item in order["items"]:
        size = f" ({item['size']})" if item["size"] else ""
        lines.append(f"• {item['quantity']} × {item['product_name']}{size}")
    lines.append(f"Total paid: Rs. {order['total_inr']:,.0f}")
    try:
        await send_whatsapp_message(number, "\n".join(lines))
    except Exception as exc:
        # The payment is recorded regardless; a lost message must not undo that
        print(f"[checkout] payment confirmation to {number} not delivered: {exc!r}")


# --- reconciliation ------------------------------------------------------------

RECONCILE_MAX_AGE_MINUTES = 60
RECONCILE_BATCH = 10


async def reconcile_pending_payments() -> int:
    """Ask Razorpay about recent unpaid orders; confirm any that are paid.

    The safety net under the webhook. Chat orders no longer bring the customer
    back through our callback page, so without this a missing or misconfigured
    webhook would leave paid orders unconfirmed. Only recent orders are checked,
    so abandoned ones stop costing API calls after an hour.

    Returns how many orders this round confirmed for the first time.
    """
    pending = await asyncio.to_thread(
        repo.get_unpaid_link_orders, RECONCILE_MAX_AGE_MINUTES, RECONCILE_BATCH
    )
    confirmed = 0
    for _order_id, payment_link_id in pending:
        try:
            result = await confirm_payment_link(payment_link_id)
        except payments.PaymentError as exc:
            # Razorpay unreachable or keys wrong: try again next round
            print(f"[checkout] reconcile paused: {exc}")
            break
        if result and result.get("first_time"):
            confirmed += 1
    return confirmed


async def reconcile_forever(interval_seconds: float) -> None:
    """Background loop started with the app. Never raises; logs and carries on."""
    while True:
        try:
            confirmed = await reconcile_pending_payments()
            if confirmed:
                print(f"[checkout] reconcile confirmed {confirmed} order(s)")
        except Exception as exc:
            print(f"[checkout] reconcile error: {exc!r}")
        await asyncio.sleep(interval_seconds)
