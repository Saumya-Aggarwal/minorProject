"""catalogue in postgres: products, variants and their vectors (pgvector)

Products were read from data/products.json and their vectors from Chroma.
Both now live here, per store. This migration only creates the tables; the
demo catalogue is loaded by catalog_sync.ensure_catalogues() on the next
start (it needs the embedding model, which a migration should not).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 22:13:35.697881

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate writes sqlmodel.sql.sqltypes.AutoString)
from sqlalchemy.dialects import postgresql
import pgvector.sqlalchemy

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('products',
    sa.Column('product_pk', sa.Integer(), nullable=False),
    sa.Column('store_id', sa.Integer(), nullable=False),
    sa.Column('code', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
    sa.Column('external_id', sqlmodel.sql.sqltypes.AutoString(length=120), nullable=True),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('category', sqlmodel.sql.sqltypes.AutoString(length=60), nullable=False),
    sa.Column('gender', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('mrp', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('image_url', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('search_text', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.VECTOR(), nullable=True),
    sa.Column('embedding_model', sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['store_id'], ['stores.store_id'], ),
    sa.PrimaryKeyConstraint('product_pk'),
    sa.UniqueConstraint('store_id', 'code', name='uq_products_store_code'),
    sa.UniqueConstraint('store_id', 'external_id', name='uq_products_store_external')
    )
    op.create_index(op.f('ix_products_store_id'), 'products', ['store_id'], unique=False)
    op.create_table('product_variants',
    sa.Column('variant_id', sa.Integer(), nullable=False),
    sa.Column('product_pk', sa.Integer(), nullable=False),
    sa.Column('store_id', sa.Integer(), nullable=False),
    sa.Column('external_id', sqlmodel.sql.sqltypes.AutoString(length=60), nullable=True),
    sa.Column('sku', sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
    sa.Column('size', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
    sa.Column('color', sqlmodel.sql.sqltypes.AutoString(length=60), nullable=False),
    sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('mrp', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('stock', sa.Integer(), nullable=True),
    sa.Column('available', sa.Boolean(), nullable=False),
    sa.Column('image_url', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['product_pk'], ['products.product_pk'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['store_id'], ['stores.store_id'], ),
    sa.PrimaryKeyConstraint('variant_id'),
    sa.UniqueConstraint('product_pk', 'size', name='uq_variants_product_size'),
    sa.UniqueConstraint('store_id', 'external_id', name='uq_variants_store_external')
    )
    op.create_index(op.f('ix_product_variants_product_pk'), 'product_variants', ['product_pk'], unique=False)
    op.create_index(op.f('ix_product_variants_store_id'), 'product_variants', ['store_id'], unique=False)
    op.add_column('cart_items', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_cart_items_variant_id', 'cart_items', 'product_variants', ['variant_id'], ['variant_id'], ondelete='SET NULL')
    op.add_column('order_items', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_order_items_variant_id', 'order_items', 'product_variants', ['variant_id'], ['variant_id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint("fk_order_items_variant_id", "order_items", type_="foreignkey")
    op.drop_column("order_items", "variant_id")
    op.drop_constraint("fk_cart_items_variant_id", "cart_items", type_="foreignkey")
    op.drop_column("cart_items", "variant_id")
    op.drop_index(op.f("ix_product_variants_store_id"), table_name="product_variants")
    op.drop_index(op.f("ix_product_variants_product_pk"), table_name="product_variants")
    op.drop_table("product_variants")
    op.drop_index(op.f("ix_products_store_id"), table_name="products")
    op.drop_table("products")
