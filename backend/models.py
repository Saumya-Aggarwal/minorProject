"""SQLModel tables for users, conversation sessions, and orders.

Column names follow the schema agreed in the sprint plan. Timestamps are
timezone-aware; Postgres stores them as TIMESTAMPTZ.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Numeric, text
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
    # active | checkout | closed
    status: str = Field(default="active", max_length=20)
    updated_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))


class Order(SQLModel, table=True):
    """Created when a Pay Now button is tapped, updated by the Razorpay webhook."""

    __tablename__ = "orders"

    order_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.user_id", index=True)
    # Where the order came from: bot | web
    channel: str = Field(default="bot", max_length=10)
    # Matches the product id in the Chroma metadata, e.g. "EW001"
    product_id: str = Field(max_length=20)
    product_name: str = Field(max_length=200)
    # Numeric, not float — money must not accumulate binary rounding error
    price_inr: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    razorpay_order_id: Optional[str] = Field(default=None, max_length=100, unique=True)
    razorpay_payment_id: Optional[str] = Field(default=None, max_length=100)
    # created | captured | failed
    status: str = Field(default="created", max_length=20)
    created_at: datetime = Field(default_factory=utcnow, sa_column=_tstz(nullable=False))
    captured_at: Optional[datetime] = Field(default=None, sa_column=_tstz(nullable=True))


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


# Lookups go by razorpay_order_id rather than our own primary key; the unique
# constraint on that column already provides the index.

# One active session per user is the invariant the webhook handler relies on.
Index(
    "idx_sessions_user_active",
    Session.__table__.c.user_id,
    unique=True,
    postgresql_where=text("status = 'active'"),
)
