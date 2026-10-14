"""demo brand: the texts that were written into the code, now the demo store's settings (brand.py)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14 21:10:00

Until P2 step 3 the prompt, greeting, HELP and off-topic line said "Kurta &
Co." and "ethnic wear" for every store. They are brand settings now
(stores.brand); these are the demo's, word for word, so its prompt renders
exactly as before. Only an empty brand is filled: settings edited since are
kept.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEMO_BRAND = {
    "name": "Kurta & Co.",
    "role": "stylist",
    "description": "an Indian ethnic wear boutique",
    "what_we_sell": (
        "Indian ethnic wear, for every function (haldi, mehendi, sangeet, wedding, reception, festivals, "
        "office). Men: kurtas, kurta pyjama sets, dhoti kurta, Nehru jackets, waistcoats, sherwanis, "
        "Jodhpuri/bandhgala suits, Indo-Western, Pathani, churidar, stole; safa (turban), mojari and "
        "kolhapuri (UK sizes). Women: sarees, lehengas, chaniya choli, Anarkali, salwar suits, "
        "sharara/gharara, co-ord sets, kurtis, gowns, Indo-Western, dupattas; juttis, flats, heels (UK "
        "sizes), clutches and bags. No western wear or jewellery."),
    "scope": "ethnic wear",
    "vague_example": '"something for my wedding"',
    "ask_about": ("who wears it (groom, bride, guest), which function (haldi, mehendi, sangeet, ceremony, "
                  "reception), budget"),
    "clear_examples": '"haldi outfit for my brother", "garba outfit for me"',
    "one_size": "a bag, dupatta or stole",
    "quantity_example": '"remove 2 of the sarees"',
    "policies": "delivery is free across India",
    "off_topic": ("I only help with our ethnic wear, so I cannot help with that. "
                  "Tell me what you are shopping for and I will pick a few pieces."),
    "help_examples": ['"sherwani for my wedding"', '"saree for office"'],
    "vertical_pack": "indian_ethnic",
}


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE stores SET brand = CAST(:brand AS jsonb) WHERE store_id = 1 AND brand IS NULL"),
        {"brand": json.dumps(DEMO_BRAND)},
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("UPDATE stores SET brand = NULL WHERE store_id = 1"))
