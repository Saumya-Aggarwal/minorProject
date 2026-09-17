"""CRUD helpers for the webhook handler.

These are synchronous (psycopg2 has no async driver), so async callers should
wrap them: `await asyncio.to_thread(get_or_create_user, number, name)`.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import select

from db import session_scope
from models import Order, Session, User, utcnow


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
        session.updated_at = utcnow()
        db.flush()
        return session.session_id


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
) -> int:
    with session_scope() as db:
        order = Order(
            user_id=user_id,
            product_id=product_id,
            product_name=product_name,
            price_inr=Decimal(str(price_inr)),
            razorpay_order_id=razorpay_order_id,
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


def mark_order_failed(razorpay_order_id: str) -> None:
    with session_scope() as db:
        order = db.exec(
            select(Order).where(Order.razorpay_order_id == razorpay_order_id)
        ).first()
        if order is not None and order.status != "captured":
            order.status = "failed"
