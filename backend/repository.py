"""CRUD helpers for the webhook handler.

These are synchronous (psycopg2 has no async driver), so async callers should
wrap them: `await asyncio.to_thread(get_or_create_user, number, name)`.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select

import catalog
from db import session_scope
from models import CartItem, LinkToken, Order, OrderItem, Session, User, utcnow

LINK_TOKEN_TTL_MINUTES = 10
LINK_PREFIX = "LINK-"

# Orders that represent a real purchase. "failed" is excluded wherever history
# is used: a declined payment is not something the customer owns.
ACTIVE_ORDER_STATUSES = ("created", "captured")


def _money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def get_or_create_user(whatsapp_number: str, display_name: Optional[str] = None) -> int:
    """Return the user_id for a WhatsApp number, creating the row on first contact."""
    with session_scope() as db:
        user = db.exec(
            select(User).where(User.whatsapp_number == whatsapp_number)
        ).first()

        if user is None:
            user = User(whatsapp_number=whatsapp_number, display_name=display_name)
            db.add(user)
        else:
            user.last_active_at = utcnow()
            # Profile names change; keep the latest one Meta sends us
            if display_name and display_name != user.display_name:
                user.display_name = display_name

        db.flush()
        return user.user_id


def get_active_session(user_id: int) -> Optional[dict[str, Any]]:
    """Return the user's active session as a plain dict, or None."""
    with session_scope() as db:
        session = db.exec(
            select(Session)
            .where(Session.user_id == user_id, Session.status == "active")
            .order_by(Session.updated_at.desc())
        ).first()

        if session is None:
            return None
        return {
            "session_id": session.session_id,
            "last_query": session.last_query,
            "last_products_shown": session.last_products_shown,
            "selected_product": session.selected_product,
            "status": session.status,
        }


def record_turn(
    user_id: int,
    query: str,
    products_shown: list[dict[str, Any]],
) -> int:
    """Store what the user asked and what we showed, for the next turn's context."""
    with session_scope() as db:
        session = db.exec(
            select(Session)
            .where(Session.user_id == user_id, Session.status == "active")
            .order_by(Session.updated_at.desc())
        ).first()

        if session is None:
            session = Session(user_id=user_id)
            db.add(session)

        session.last_query = query
        session.last_products_shown = products_shown
        # A new list of results invalidates whatever was picked from the old one
        session.selected_product = None
        session.updated_at = utcnow()
        db.flush()
        return session.session_id


def record_selection(user_id: int, product: dict[str, Any]) -> None:
    """Remember which item the customer picked, for a later BUY or Pay Now."""
    with session_scope() as db:
        session = db.exec(
            select(Session)
            .where(Session.user_id == user_id, Session.status == "active")
            .order_by(Session.updated_at.desc())
        ).first()

        if session is None:
            session = Session(user_id=user_id)
            db.add(session)

        session.selected_product = product
        session.updated_at = utcnow()


def close_session(session_id: int, status: str = "closed") -> None:
    with session_scope() as db:
        session = db.get(Session, session_id)
        if session is not None:
            session.status = status
            session.updated_at = utcnow()


def create_order_from_items(
    user_id: int, items: list[dict[str, Any]], channel: str = "bot", from_cart: bool = False
) -> int:
    """Create an order header plus one line per item. Returns order_id.

    Each item: product_id, product_name, price_inr, size, quantity. The total is
    computed once, here, from the same values written to the lines.
    """
    if not items:
        raise ValueError("an order needs at least one item")

    total = sum(_money(item["price_inr"]) * int(item["quantity"]) for item in items)
    with session_scope() as db:
        order = Order(user_id=user_id, channel=channel, total_inr=total, from_cart=from_cart)
        db.add(order)
        db.flush()
        for item in items:
            db.add(
                OrderItem(
                    order_id=order.order_id,
                    product_id=item["product_id"],
                    product_name=item["product_name"],
                    price_inr=_money(item["price_inr"]),
                    size=item.get("size") or "",
                    quantity=int(item["quantity"]),
                )
            )
        return order.order_id


def create_order_for_product(
    user_id: int, product_id: str, size: str = "", quantity: int = 1, channel: str = "bot"
) -> int:
    """Single-item "Buy now", priced from the catalogue rather than by the caller."""
    product = catalog.get_by_id(product_id)
    if product is None:
        raise ValueError(f"unknown product {product_id!r}")
    size = _resolve_size(product, size)
    _require_size(product["name"], size, product.get("sizes_available") or [])
    return create_order_from_items(
        user_id,
        [
            {
                "product_id": product["id"],
                "product_name": product["name"],
                "price_inr": product["price"],
                "size": size,
                "quantity": quantity,
            }
        ],
        channel,
    )


def create_order_from_cart(user_id: int, channel: str) -> Optional[dict[str, Any]]:
    """Turn the cart into an order. Returns None when the cart is empty.

    The cart is NOT cleared here. It is cleared when payment is captured, so a
    customer who abandons or fails the payment still has their cart.
    """
    cart = get_cart(user_id)
    lines = [item for item in cart["items"] if item["available"]]
    if not lines:
        return None
    for item in lines:
        _require_size(item["name"], item["size"], item["sizes_available"])

    order_id = create_order_from_items(
        user_id,
        [
            {
                "product_id": item["product_id"],
                "product_name": item["name"],
                "price_inr": item["price_inr"],
                "size": item["size"],
                "quantity": item["quantity"],
            }
            for item in lines
        ],
        channel,
        from_cart=True,
    )
    return {
        "order_id": order_id,
        "total_inr": sum(item["line_total"] for item in lines),
        "item_count": sum(item["quantity"] for item in lines),
    }


def mark_order_captured(razorpay_order_id: str, razorpay_payment_id: str) -> bool:
    """Idempotent: Razorpay can deliver payment.captured more than once.

    Returns True only on the first capture, so the caller sends one confirmation.
    """
    with session_scope() as db:
        order = db.exec(
            select(Order).where(Order.razorpay_order_id == razorpay_order_id)
        ).first()

        if order is None:
            print(f"[repository] capture for unknown order {razorpay_order_id}")
            return False
        if order.status == "captured":
            return False

        order.status = "captured"
        order.razorpay_payment_id = razorpay_payment_id
        order.captured_at = datetime.now(timezone.utc)
        return True


def attach_payment_link(order_id: int, payment_link_id: str, payment_link_url: str) -> None:
    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        order.payment_link_id = payment_link_id
        order.payment_link_url = payment_link_url


def get_order(order_id: int) -> Optional[dict[str, Any]]:
    """One order with its lines and the customer's contact details."""
    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            return None
        user = db.get(User, order.user_id)
        lines = db.exec(
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.order_item_id)
        ).all()

        # The slowest item decides when the order arrives. Counted from payment,
        # because nothing ships before it is paid for.
        products = {line.product_id: catalog.get_by_id(line.product_id) or {} for line in lines}
        delivery_days = max(
            (products[line.product_id].get("delivery_days", 5) for line in lines), default=5
        )
        deliver_by = (
            (order.captured_at + timedelta(days=delivery_days)).date()
            if order.status == "captured" and order.captured_at
            else None
        )
        return {
            "order_id": order.order_id,
            "user_id": order.user_id,
            "created_at": order.created_at,
            "captured_at": order.captured_at,
            "delivery_days": delivery_days,
            "deliver_by": deliver_by,
            "channel": order.channel,
            "status": order.status,
            "total_inr": float(order.total_inr),
            "from_cart": order.from_cart,
            "payment_link_id": order.payment_link_id,
            "payment_link_url": order.payment_link_url,
            "razorpay_payment_id": order.razorpay_payment_id,
            "customer_name": user.display_name if user else None,
            "customer_email": user.email if user else None,
            "whatsapp_number": user.whatsapp_number if user else None,
            "items": [
                {
                    "product_id": line.product_id,
                    "product_name": line.product_name,
                    "price_inr": float(line.price_inr),
                    "size": line.size,
                    "quantity": line.quantity,
                    "image_url": products[line.product_id].get("image_url"),
                }
                for line in lines
            ],
        }


def get_pending_cart_order(user_id: int) -> Optional[dict[str, Any]]:
    """The newest unpaid cart order that already has a payment link, if any."""
    with session_scope() as db:
        order = db.exec(
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.status == "created",
                Order.from_cart.is_(True),
                Order.payment_link_url.is_not(None),
            )
            .order_by(Order.created_at.desc(), Order.order_id.desc())
        ).first()
        order_id = order.order_id if order else None
    return get_order(order_id) if order_id else None


def cancel_order(order_id: int) -> None:
    """Mark an order that never reached payment as failed.

    Used when the payment link cannot be created: the customer never had a way
    to pay, so the order must not linger as "created" in their history.
    """
    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is not None and order.status == "created":
            order.status = "failed"


def get_order_id_by_payment_link(payment_link_id: str) -> Optional[int]:
    with session_scope() as db:
        order = db.exec(
            select(Order).where(Order.payment_link_id == payment_link_id)
        ).first()
        return order.order_id if order else None


def mark_order_paid(
    order_id: int, razorpay_payment_id: str, razorpay_order_id: Optional[str] = None
) -> bool:
    """Record a captured payment. True only the first time for an order.

    Both confirmation paths — the customer's redirect back to us and Razorpay's
    webhook — call this, often seconds apart and sometimes more than once each.
    Only the first call may send a confirmation, so the caller checks the result.

    The row is locked (SELECT ... FOR UPDATE) because the two paths can arrive
    at the same moment: without the lock both could read "created" and both
    report a first capture, sending the customer two confirmations.

    For a cart checkout, the ordered quantities leave the cart in the same
    transaction. Only those quantities: anything added to the cart after
    checkout stays there.
    """
    with session_scope() as db:
        order = db.exec(
            select(Order).where(Order.order_id == order_id).with_for_update()
        ).first()
        if order is None:
            print(f"[repository] payment for unknown order {order_id}")
            return False
        if order.status == "captured":
            return False

        order.status = "captured"
        order.razorpay_payment_id = razorpay_payment_id
        if razorpay_order_id and not order.razorpay_order_id:
            order.razorpay_order_id = razorpay_order_id
        order.captured_at = datetime.now(timezone.utc)

        if order.from_cart:
            lines = db.exec(select(OrderItem).where(OrderItem.order_id == order_id)).all()
            for line in lines:
                cart_line = db.exec(
                    select(CartItem).where(
                        CartItem.user_id == order.user_id,
                        CartItem.product_id == line.product_id,
                        CartItem.size == line.size,
                    )
                ).first()
                if cart_line is None:
                    continue
                if cart_line.quantity > line.quantity:
                    cart_line.quantity -= line.quantity
                else:
                    db.delete(cart_line)
        return True


def get_order_history(user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Recent orders, newest first, each with its lines."""
    with session_scope() as db:
        orders = db.exec(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.order_id.desc())
            .limit(limit)
        ).all()
        if not orders:
            return []

        lines = db.exec(
            select(OrderItem)
            .where(OrderItem.order_id.in_([order.order_id for order in orders]))
            .order_by(OrderItem.order_item_id)
        ).all()
        by_order: dict[int, list[dict[str, Any]]] = {}
        for line in lines:
            by_order.setdefault(line.order_id, []).append(
                {
                    "product_id": line.product_id,
                    "product_name": line.product_name,
                    "price_inr": float(line.price_inr),
                    "size": line.size,
                    "quantity": line.quantity,
                }
            )

        return [
            {
                "order_id": order.order_id,
                "channel": order.channel,
                "status": order.status,
                "total_inr": float(order.total_inr),
                "created_at": order.created_at.isoformat(),
                "payment_link_url": order.payment_link_url,
                "items": by_order.get(order.order_id, []),
            }
            for order in orders
        ]


def get_purchased_items(user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Recently purchased lines, newest first — for greetings and user_context."""
    with session_scope() as db:
        rows = db.exec(
            select(OrderItem, Order)
            .join(Order, Order.order_id == OrderItem.order_id)
            .where(Order.user_id == user_id, Order.status.in_(ACTIVE_ORDER_STATUSES))
            .order_by(Order.created_at.desc(), OrderItem.order_item_id.desc())
            .limit(limit)
        ).all()
        return [
            {
                "product_id": line.product_id,
                "product_name": line.product_name,
                "price_inr": float(line.price_inr),
                "channel": order.channel,
            }
            for line, order in rows
        ]


def get_purchased_product_ids(user_id: int) -> list[str]:
    """Contract C2 (WORK_SPLIT.md): distinct product ids, most recent first.

    Deliberately narrow, so the retrieval side never depends on order tables.
    """
    with session_scope() as db:
        rows = db.exec(
            select(OrderItem.product_id)
            .join(Order, Order.order_id == OrderItem.order_id)
            .where(Order.user_id == user_id, Order.status.in_(ACTIVE_ORDER_STATUSES))
            .order_by(Order.created_at.desc(), OrderItem.order_item_id.desc())
        ).all()
    distinct: list[str] = []
    for product_id in rows:
        if product_id not in distinct:
            distinct.append(product_id)
    return distinct


# --- cart ---------------------------------------------------------------------


def _require_size(name: str, size: str, sizes_available: list[str]) -> None:
    """No order without a size for a product sold in several.

    Enforced here rather than in each channel, so the bot, the website and the
    JSON API cannot disagree about it. Channels still check first, to ask nicely.
    """
    if not size and len(sizes_available) > 1:
        raise ValueError(f"choose a size for {name}")


def _resolve_size(product: dict[str, Any], size: str) -> str:
    """Validate a chosen size; auto-pick when the product has only one."""
    sizes = product.get("sizes_available") or []
    size = (size or "").strip()
    if not size:
        return sizes[0] if len(sizes) == 1 else ""
    if sizes and size not in sizes:
        raise ValueError(f"size {size!r} not available for {product['id']}")
    return size


def add_to_cart(
    user_id: int, product_id: str, size: str = "", quantity: int = 1
) -> dict[str, Any]:
    """Add an item, or increase its quantity if already there. Returns the cart.

    An atomic upsert rather than read-then-write: a double-tapped button, or two
    messages arriving together, must not race into a unique-constraint error.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    product = catalog.get_by_id(product_id)
    if product is None:
        raise ValueError(f"unknown product {product_id!r}")
    size = _resolve_size(product, size)

    table = CartItem.__table__
    statement = pg_insert(table).values(
        user_id=user_id,
        product_id=product_id,
        size=size,
        quantity=quantity,
        added_at=utcnow(),
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_cart_user_product_size",
        set_={"quantity": table.c.quantity + statement.excluded.quantity},
    )
    with session_scope() as db:
        db.exec(statement)
    return get_cart(user_id)


def get_cart(user_id: int) -> dict[str, Any]:
    """The cart at current catalogue prices, in the order items were added.

    Items whose product has left the catalogue stay listed with available=False
    and are excluded from the total, rather than vanishing without explanation.
    """
    with session_scope() as db:
        rows = db.exec(
            select(CartItem)
            .where(CartItem.user_id == user_id)
            .order_by(CartItem.added_at, CartItem.cart_item_id)
        ).all()
        raw = [(row.cart_item_id, row.product_id, row.size, row.quantity) for row in rows]

    items = []
    for cart_item_id, product_id, size, quantity in raw:
        product = catalog.get_by_id(product_id)
        price = float(product["price"]) if product else 0.0
        items.append(
            {
                "cart_item_id": cart_item_id,
                "product_id": product_id,
                "name": product["name"] if product else product_id,
                "price_inr": price,
                "size": size,
                "sizes_available": product.get("sizes_available", []) if product else [],
                "image_url": product.get("image_url") if product else None,
                "quantity": quantity,
                "line_total": price * quantity,
                "available": product is not None,
            }
        )

    available = [item for item in items if item["available"]]
    return {
        "items": items,
        "total_inr": sum(item["line_total"] for item in available),
        "count": sum(item["quantity"] for item in available),
    }


def update_cart_item(user_id: int, cart_item_id: int, quantity: int) -> bool:
    """Set a quantity; zero or less removes the line. Scoped to the owner."""
    with session_scope() as db:
        row = db.get(CartItem, cart_item_id)
        if row is None or row.user_id != user_id:
            return False
        if quantity <= 0:
            db.delete(row)
        else:
            row.quantity = quantity
        return True


def update_cart_item_size(user_id: int, cart_item_id: int, size: str) -> bool:
    """Change a line's size, merging into an existing line with that size.

    Items added in chat usually have no size yet; the website is where it gets
    chosen. Without the merge, picking a size already in the cart would hit
    uq_cart_user_product_size.
    """
    with session_scope() as db:
        row = db.get(CartItem, cart_item_id)
        if row is None or row.user_id != user_id:
            return False
        product = catalog.get_by_id(row.product_id)
        if product is None:
            return False
        size = _resolve_size(product, size)
        if size == row.size:
            return True

        twin = db.exec(
            select(CartItem).where(
                CartItem.user_id == user_id,
                CartItem.product_id == row.product_id,
                CartItem.size == size,
            )
        ).first()
        if twin is not None:
            twin.quantity += row.quantity
            db.delete(row)
        else:
            row.size = size
        return True


def remove_from_cart(user_id: int, cart_item_id: int) -> bool:
    return update_cart_item(user_id, cart_item_id, 0)


def clear_cart(user_id: int) -> int:
    """Empty the cart, after a captured payment. Returns the lines removed."""
    with session_scope() as db:
        rows = db.exec(select(CartItem).where(CartItem.user_id == user_id)).all()
        for row in rows:
            db.delete(row)
        return len(rows)


def get_live_state(user_id: int) -> dict[str, Any]:
    """What an open browser tab needs to notice a change made elsewhere.

    The fingerprint covers every cart line and the status of recent orders, so
    it changes when the customer adds, removes or resizes anything on WhatsApp,
    and when a payment is confirmed. The page compares fingerprints and only
    re-renders when they differ, so an idle tab costs two small queries.
    """
    cart = get_cart(user_id)
    with session_scope() as db:
        recent = db.exec(
            select(Order.order_id, Order.status)
            .where(Order.user_id == user_id)
            .order_by(Order.order_id.desc())
            .limit(10)
        ).all()

    lines = sorted(
        (item["cart_item_id"], item["product_id"], item["size"], item["quantity"])
        for item in cart["items"]
    )
    orders = {str(order_id): status for order_id, status in recent}
    digest = hashlib.sha1(repr((lines, sorted(orders.items()))).encode()).hexdigest()
    return {"cart_count": cart["count"], "fingerprint": digest[:16], "orders": orders}


def create_link_token(user_id: int) -> str:
    """Mint a single-use code binding a WhatsApp number to this account."""
    with session_scope() as db:
        # Any earlier unused token for this user becomes dead on arrival, so a
        # code left in an old browser tab cannot be replayed later
        stale = db.exec(
            select(LinkToken).where(
                LinkToken.user_id == user_id, LinkToken.used_at.is_(None)
            )
        ).all()
        now = datetime.now(timezone.utc)
        for token_row in stale:
            token_row.used_at = now

        token = secrets.token_urlsafe(6)
        db.add(
            LinkToken(
                token=token,
                user_id=user_id,
                expires_at=now + timedelta(minutes=LINK_TOKEN_TTL_MINUTES),
            )
        )
        return token


def get_or_create_link_token(user_id: int, min_remaining_minutes: int = 3) -> str:
    """Reuse a still-valid code instead of minting a new one on every render.

    create_link_token() retires every earlier code. Calling it on each account
    page render meant a refresh — or the live-sync re-render — killed the code
    the customer had just opened in WhatsApp and was about to send. A code with
    a few minutes left is reused; only near expiry is a new one minted.
    """
    cutoff = datetime.now(timezone.utc) + timedelta(minutes=min_remaining_minutes)
    with session_scope() as db:
        existing = db.exec(
            select(LinkToken)
            .where(
                LinkToken.user_id == user_id,
                LinkToken.used_at.is_(None),
                LinkToken.expires_at > cutoff,
            )
            .order_by(LinkToken.created_at.desc())
        ).first()
        if existing is not None:
            return existing.token
    return create_link_token(user_id)


def consume_link_token(token: str, whatsapp_number: str) -> Optional[dict[str, Any]]:
    """Validate a link code and bind the number to its account.

    Returns the linked user, or None if the code is unknown, used, or expired.

    The whole thing is one transaction because of the merge below: a half-applied
    merge would leave orders pointing at a deleted user.
    """
    token = token.strip()
    with session_scope() as db:
        row = db.exec(select(LinkToken).where(LinkToken.token == token)).first()

        if row is None:
            print(f"[repository] unknown link token {token!r}")
            return None
        if row.used_at is not None:
            print(f"[repository] link token {token!r} already used")
            return None

        # expires_at comes back tz-aware from TIMESTAMPTZ
        if row.expires_at <= datetime.now(timezone.utc):
            print(f"[repository] link token {token!r} expired")
            return None

        target = db.get(User, row.user_id)
        if target is None:
            print(f"[repository] link token {token!r} points at a deleted user")
            return None

        # The number almost always already belongs to a bot-created row: the
        # customer messaged the bot before linking. Fold that row into the
        # website account rather than colliding with the unique constraint.
        existing = db.exec(
            select(User).where(User.whatsapp_number == whatsapp_number)
        ).first()

        if existing is not None and existing.user_id != target.user_id:
            print(
                f"[repository] merging bot user {existing.user_id} "
                f"into web user {target.user_id}"
            )
            for order in db.exec(
                select(Order).where(Order.user_id == existing.user_id)
            ).all():
                order.user_id = target.user_id
            # idx_sessions_user_active allows one active session per user: if
            # the target already has one, the orphan's is closed, not moved in
            target_has_active = db.exec(
                select(Session).where(
                    Session.user_id == target.user_id, Session.status == "active"
                )
            ).first() is not None
            for session in db.exec(
                select(Session).where(Session.user_id == existing.user_id)
            ).all():
                if target_has_active and session.status == "active":
                    session.status = "closed"
                session.user_id = target.user_id
            for old_token in db.exec(
                select(LinkToken).where(LinkToken.user_id == existing.user_id)
            ).all():
                old_token.user_id = target.user_id

            # The cart follows the customer. Where both carts hold the same
            # item and size, quantities are added: re-pointing that row instead
            # would violate uq_cart_user_product_size.
            target_cart = {
                (item.product_id, item.size): item
                for item in db.exec(
                    select(CartItem).where(CartItem.user_id == target.user_id)
                ).all()
            }
            for item in db.exec(
                select(CartItem).where(CartItem.user_id == existing.user_id)
            ).all():
                twin = target_cart.get((item.product_id, item.size))
                if twin is not None:
                    twin.quantity += item.quantity
                    db.delete(item)
                else:
                    item.user_id = target.user_id
            # Land the re-pointed rows before the user they referenced is deleted
            db.flush()

            if not target.display_name and existing.display_name:
                target.display_name = existing.display_name

            # Delete outright rather than blanking the number first: a row with
            # neither email nor whatsapp_number violates ck_users_has_identity.
            # The flush must land before the number is reassigned below, or the
            # unique index still sees the old row holding it.
            db.delete(existing)
            db.flush()

        target.whatsapp_number = whatsapp_number
        target.last_active_at = utcnow()
        row.used_at = datetime.now(timezone.utc)
        db.flush()

        return {
            "user_id": target.user_id,
            "email": target.email,
            "display_name": target.display_name,
            "whatsapp_number": target.whatsapp_number,
        }


def get_user_by_whatsapp(whatsapp_number: str) -> Optional[dict[str, Any]]:
    """Look up a contact without creating one."""
    with session_scope() as db:
        user = db.exec(
            select(User).where(User.whatsapp_number == whatsapp_number)
        ).first()
        if user is None:
            return None
        return {
            "user_id": user.user_id,
            "email": user.email,
            "display_name": user.display_name,
            # An email means the account came from the website, i.e. it is linked
            "is_linked": user.email is not None,
        }


def mark_order_failed(razorpay_order_id: str) -> None:
    with session_scope() as db:
        order = db.exec(
            select(Order).where(Order.razorpay_order_id == razorpay_order_id)
        ).first()
        if order is not None and order.status != "captured":
            order.status = "failed"
