"""tenancy: stores, and store_id on users, orders, bot_replies, feedback

Everything that existed before becomes store #1, the demo ethnic-wear shop
("Kurta & Co."). Its WhatsApp settings stay in backend/.env (the demo
fallback), so the row carries no credentials.

A customer is now unique per store: the same phone number writing to two
brands is two customers.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEMO_STORE_ID = 1
SCOPED_TABLES = ("users", "orders", "bot_replies", "feedback")


def upgrade() -> None:
    op.create_table(
        "stores",
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("platform", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column("currency", sqlmodel.sql.sqltypes.AutoString(length=3), nullable=False),
        sa.Column("code_prefix", sqlmodel.sql.sqltypes.AutoString(length=4), nullable=False),
        sa.Column("wa_phone_number_id", sqlmodel.sql.sqltypes.AutoString(length=40), nullable=True),
        sa.Column("wa_display_number", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("wa_business_account_id", sqlmodel.sql.sqltypes.AutoString(length=40), nullable=True),
        sa.Column("wa_app_id", sqlmodel.sql.sqltypes.AutoString(length=40), nullable=True),
        sa.Column("wa_access_token_enc", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("wa_app_secret_enc", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("wa_verify_token_enc", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("connector", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("brand", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("handoff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("store_id"),
        sa.UniqueConstraint("slug"),
        sa.UniqueConstraint("wa_phone_number_id"),
    )
    op.execute(
        f"""INSERT INTO stores (store_id, slug, name, platform, status, currency, code_prefix,
                                connector, created_at, updated_at)
            VALUES ({DEMO_STORE_ID}, 'demo', 'Kurta & Co. (demo)', 'feed', 'live', 'INR', 'EW',
                    '{{"feed_path": "data/products.json"}}', now(), now())"""
    )
    # The explicit id above does not advance the sequence
    op.execute("SELECT setval('stores_store_id_seq', (SELECT max(store_id) FROM stores))")

    for table in SCOPED_TABLES:
        op.add_column(table, sa.Column("store_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET store_id = {DEMO_STORE_ID}")
        op.alter_column(table, "store_id", nullable=False)
        op.create_index(f"ix_{table}_store_id", table, ["store_id"])
        op.create_foreign_key(f"fk_{table}_store_id", table, "stores", ["store_id"], ["store_id"])

    # Unique per store instead of unique everywhere
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.drop_index("ix_users_whatsapp_number", table_name="users")
    op.create_index("ix_users_whatsapp_number", "users", ["whatsapp_number"], unique=False)
    op.create_unique_constraint("uq_users_store_email", "users", ["store_id", "email"])
    op.create_unique_constraint("uq_users_store_whatsapp", "users", ["store_id", "whatsapp_number"])


def downgrade() -> None:
    # Only safe while every row still belongs to the demo store
    op.drop_constraint("uq_users_store_whatsapp", "users", type_="unique")
    op.drop_constraint("uq_users_store_email", "users", type_="unique")
    op.drop_index("ix_users_whatsapp_number", table_name="users")
    op.create_index("ix_users_whatsapp_number", "users", ["whatsapp_number"], unique=True)
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    for table in reversed(SCOPED_TABLES):
        op.drop_constraint(f"fk_{table}_store_id", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_store_id", table_name=table)
        op.drop_column(table, "store_id")
    op.drop_table("stores")
