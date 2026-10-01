"""Load a store's catalogue from its source into Postgres, and embed it.

Replaces scripts/ingest_catalog.py (Chroma). Re-run after editing
data/products.json; a changed EMBEDDING_PROVIDER is picked up by itself on the
next start (db.init_db re-embeds), or here with --reembed.

Run from the repo root:
    backend/.venv/Scripts/python scripts/sync_catalog.py              the demo store
    backend/.venv/Scripts/python scripts/sync_catalog.py some-slug    another store
    backend/.venv/Scripts/python scripts/sync_catalog.py --reembed    re-embed everything
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from sqlmodel import select, update  # noqa: E402

import catalog_sync  # noqa: E402
from common import embeddings  # noqa: E402
from connectors import connector_for  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from models import Product, Store  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    slug = args[0] if args else "demo"
    init_db()
    with session_scope() as db:
        store = db.exec(select(Store).where(Store.slug == slug)).first()
        if store is None:
            print(f"No store with slug {slug!r}")
            return 2
        store = store.model_dump()
    if "--reembed" in sys.argv:
        with session_scope() as db:
            db.exec(update(Product).where(Product.store_id == store["store_id"]).values(embedding=None))
    print(f"Syncing {store['name']} ({store['platform']}) with {embeddings.model_name()}")
    counts = connector_for(store).sync_catalog()
    print(f"Done: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
