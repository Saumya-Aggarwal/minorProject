"""CRUD helpers for the webhook handler.

These are synchronous (psycopg2 has no async driver), so async callers should
wrap them: `await asyncio.to_thread(get_or_create_user, number, name)`.
"""

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import select

from db import session_scope
from models import LinkToken, Order, Session, User, utcnow

LINK_TOKEN_TTL_MINUTES = 10
LINK_PREFIX = "LINK-"


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


def create_order(
    user_id: int,
    product_id: str,
    product_name: str,
    price_inr: Decimal | float | int,
    razorpay_order_id: Optional[str] = None,
    channel: str = "bot",
) -> int:
    with session_scope() as db:
        order = Order(
            user_id=user_id,
            product_id=product_id,
            product_name=product_name,
            price_inr=Decimal(str(price_inr)),
            razorpay_order_id=razorpay_order_id,
            channel=channel,
        )
        db.add(order)
        db.flush()
        return order.order_id


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


def get_order_history(user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Recent orders, newest first — the raw material for personalization."""
    with session_scope() as db:
        orders = db.exec(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        ).all()

        return [
            {
                "product_id": order.product_id,
                "product_name": order.product_name,
                "price_inr": float(order.price_inr),
                "status": order.status,
                "channel": order.channel,
                "created_at": order.created_at.isoformat(),
            }
            for order in orders
        ]


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
            for session in db.exec(
                select(Session).where(Session.user_id == existing.user_id)
            ).all():
                session.user_id = target.user_id
            for old_token in db.exec(
                select(LinkToken).where(LinkToken.user_id == existing.user_id)
            ).all():
                old_token.user_id = target.user_id

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
