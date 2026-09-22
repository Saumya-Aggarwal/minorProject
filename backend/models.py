"""SQLModel tables for users, conversation sessions, and orders.

Column names follow the schema agreed in the sprint plan. Timestamps are
timezone-aware; Postgres stores them as TIMESTAMPTZ.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tstz(**kwargs: Any) -> Column:
    return Column(DateTime(timezone=True), **kwargs)


class User(SQLModel, table=True):
    """One identity per customer, across both the website and WhatsApp.

    A single table rather than separate web/bot customer tables: the whole point
    of account linking is that the two channels resolve to the same person.

    Website signups start with an email and no whatsapp_number; bot contacts start
    with a whatsapp_number and no email. Linking fills in the missing half.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR whatsapp_number IS NOT NULL",
            name="ck_users_has_identity",
        ),
    )

    user_id: Optional[int] = Field(default=None, primary_key=True)
    # Digits only, no '+' — matches the "from" field in Meta's webhook payload
    whatsapp_number: Optional[str] = Field(
        default=None, max_length=20, unique=True, index=True
    )
    email: Optional[str] = Field(default=None, max_length=255, unique=True, index=True)
    # bcrypt hash; None for contacts who only ever used WhatsApp
    password_hash: Optional[str] = Field(default=None, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=100)
    created_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))
    last_active_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))
    # What the assistant has learned about the customer across chats:
    # {"gender": "Men"|"Women", "notes": ["shops for his brother", ...]}.
    # Shown on /account, where the customer can clear it.
    profile: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))
    # The delivery address the customer last confirmed, used before the one on
    # their most recent order (they may have changed it since)
    shipping: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))


class Session(SQLModel, table=True):
    """Conversation context for multi-turn RAG.

    last_products_shown stores the ProductMatch list from the previous turn, so
    "the second one" or "show me that in blue" can be resolved on the next message.
    """

    __tablename__ = "sessions"

    session_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.user_id", index=True)
    last_query: Optional[str] = None
    last_products_shown: Optional[list[dict[str, Any]]] = Field(
        default=None, sa_column=Column(JSONB)
    )
    # The item the customer picked from the last list, kept so a follow-up
    # "BUY" knows what it refers to. Razorpay will read this to build the order.
    selected_product: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSONB)
    )
    # Half-finished delivery address while the bot is collecting one in chat;
    # None when no address is being asked for
    pending_address: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))
    # The assistant's short-term memory: the last few {role, content} messages,
    # so "the second one" or "what about in blue?" makes sense next turn
    messages: Optional[list[dict[str, Any]]] = Field(
        default=None, sa_column=Column(JSONB)
    )
    # active | checkout | closed
    status: str = Field(default="active", max_length=20)
    updated_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))


class Order(SQLModel, table=True):
    """Order header: one per checkout, one Razorpay order, one payment.

    The products live in order_items. A cart checkout is one payment covering
    several products, which a one-row-per-product design cannot express:
    razorpay_order_id is unique, and a single payment spans every line.
    """

    __tablename__ = "orders"

    order_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.user_id", index=True)
    # Where the order came from: bot | web
    channel: str = Field(default="bot", max_length=10)
    # Sum of the lines at checkout. Stored rather than recomputed, so the amount
    # sent to Razorpay is exactly the amount recorded here.
    # Numeric, not float — money must not accumulate binary rounding error.
    total_inr: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    razorpay_order_id: Optional[str] = Field(default=None, max_length=100, unique=True)
    razorpay_payment_id: Optional[str] = Field(default=None, max_length=100)
    # The Razorpay Payment Link the customer pays through. Both confirmation
    # paths (the redirect back to us, and Razorpay's webhook) identify the order
    # by this id, so it is unique and indexed.
    payment_link_id: Optional[str] = Field(default=None, max_length=100, unique=True, index=True)
    payment_link_url: Optional[str] = Field(default=None, max_length=300)
    # True for cart checkouts: on payment, exactly the ordered quantities come
    # out of the cart. A single-item Buy now leaves the cart alone.
    from_cart: bool = Field(default=False)
    # created | captured | failed
    status: str = Field(default="created", max_length=20)
    created_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))
    captured_at: Optional[datetime] = Field(default=None, sa_column=_tstz(nullable=True))

    # Delivery address, snapshotted like the prices: an order keeps the address
    # it was sent to even if the customer later moves. Nullable because chat
    # orders may be placed before the customer has ever given an address.
    ship_name: Optional[str] = Field(default=None, max_length=100)
    ship_phone: Optional[str] = Field(default=None, max_length=15)
    ship_line1: Optional[str] = Field(default=None, max_length=200)
    ship_line2: Optional[str] = Field(default=None, max_length=200)
    ship_city: Optional[str] = Field(default=None, max_length=100)
    ship_state: Optional[str] = Field(default=None, max_length=60)
    ship_pincode: Optional[str] = Field(default=None, max_length=6)


class OrderItem(SQLModel, table=True):
    """One product line in an order, with name and price snapshotted.

    Snapshotted, not looked up from the catalogue: an order must record what
    the customer actually paid, not follow later price edits.
    """

    __tablename__ = "order_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_order_items_quantity"),)

    order_item_id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.order_id", index=True)
    # Matches the product id in the catalogue and Chroma metadata, e.g. "EW001"
    product_id: str = Field(max_length=20)
    product_name: str = Field(max_length=200)
    price_inr: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    # "" when no size was chosen — see CartItem.size
    size: str = Field(default="", max_length=20)
    quantity: int = Field(default=1)


class CartItem(SQLModel, table=True):
    """The shared cart.

    Keyed by user_id, which is what makes it shared: users is one row per person
    across WhatsApp and the website, so the bot and the site read the same rows.

    Stores only what the customer chose. Name and price are read from the
    catalogue when the cart is shown or checked out, so a cart never displays a
    stale price. The price is frozen later, in order_items, at checkout.
    """

    __tablename__ = "cart_items"
    __table_args__ = (
        # Adding the same item twice increments quantity instead of duplicating
        UniqueConstraint("user_id", "product_id", "size", name="uq_cart_user_product_size"),
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity"),
    )

    cart_item_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.user_id", index=True)
    product_id: str = Field(max_length=20)
    # NOT NULL, with "" meaning "not chosen yet". A NULL would silently defeat
    # the unique constraint above: Postgres treats NULLs as distinct, so the
    # same item could be added twice as two separate rows.
    size: str = Field(default="", max_length=20, nullable=False)
    quantity: int = Field(default=1)
    added_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))


class LinkToken(SQLModel, table=True):
    """Single-use code that binds a WhatsApp number to a website account.

    A browser session cannot reach the bot — Meta's webhook carries only a phone
    number and a message. So the site mints a short code, the user sends it through
    a wa.me deep link, and the webhook uses it to prove which account is theirs.

    Short-lived and single-use: once consumed, the phone number itself is the
    credential for that account.
    """

    __tablename__ = "link_tokens"

    token_id: Optional[int] = Field(default=None, primary_key=True)
    # Short enough to retype by hand if the deep link misbehaves on desktop
    token: str = Field(max_length=32, unique=True, index=True)
    user_id: int = Field(foreign_key="users.user_id", index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))
    expires_at: datetime = Field(sa_column=_tstz(nullable=False))
    # Set on first successful use; a second attempt must be refused
    used_at: Optional[datetime] = Field(default=None, sa_column=_tstz(nullable=True))


class BotReply(SQLModel, table=True):
    """One answer the bot gave to a free-text message, kept for training.

    The store owner rates these on /admin/training. sessions.messages is only
    the last few messages for the model's memory; this is the full record.
    """

    __tablename__ = "bot_replies"

    reply_id: Optional[int] = Field(default=None, primary_key=True)
    # CASCADE: deleting a customer (tests, or a GDPR-style request) takes their log too
    user_id: int = Field(foreign_key="users.user_id", index=True, ondelete="CASCADE")
    question: str
    answer: str
    # Product ids shown, in the order they were numbered
    product_ids: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    # assistant | search | lookup — which path produced the answer
    path: str = Field(default="assistant", max_length=20)
    created_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False, index=True))


class Feedback(SQLModel, table=True):
    """A rating that changes what the bot shows.

    scope "owner": the store owner's judgement, applied to everyone asking a
    similar question (by embedding similarity). scope "customer": "Not for me",
    applied to that customer only — one customer can never skew the shop.
    product_id empty = a rating of the whole reply (used as a good example).
    """

    __tablename__ = "feedback"

    feedback_id: Optional[int] = Field(default=None, primary_key=True)
    scope: str = Field(max_length=10, index=True)          # owner | customer
    rating: int                                            # +1 | -1
    product_id: str = Field(default="", max_length=20)
    question: str = ""
    # The question's embedding, so similar future questions find this rating
    embedding: Optional[list[float]] = Field(default=None, sa_column=Column(JSONB))
    note: str = ""
    reply_id: Optional[int] = Field(default=None, foreign_key="bot_replies.reply_id", index=True,
                                    ondelete="SET NULL")
    # Customer feedback: whose results it changes. Owner feedback: who gave it.
    user_id: Optional[int] = Field(default=None, foreign_key="users.user_id", index=True,
                                   ondelete="CASCADE")
    created_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))


# Lookups go by razorpay_order_id rather than our own primary key; the unique
# constraint on that column already provides the index.

# One active session per user is the invariant the webhook handler relies on.
Index(
    "idx_sessions_user_active",
    Session.__table__.c.user_id,
    unique=True,
    postgresql_where=text("status = 'active'"),
)
