"""Write a store's catalogue into Postgres and keep its vectors current.

Connectors (connectors/) fetch records; this module is the only writer of
products and product_variants:

  * normalise   every record gets every key catalogue code reads, so a
                T-shirt from Shopify and a sherwani from the demo feed look
                the same to search, the bot and the website
  * upsert      by product code; variants by size, updated in place so cart
                lines keep their variant_id; products the source no longer
                lists become inactive (carts and orders still name them)
  * embed       only rows whose text changed or whose vector came from
                another model (EMBEDDING_PROVIDER switched)

ensure_catalogues() runs on every start (db.init_db): it loads a feed store
that has no products yet (the demo store on a fresh database) and re-embeds
stale vectors. Cheap when there is nothing to do.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlmodel import select

import catalog
from common import embeddings
from db import session_scope
from models import Product, ProductVariant, Store, utcnow

ONE_SIZE = "Free Size"
_SYNC_LOCK = 724_002

# Keys every catalogue record has after normalise(), with their defaults
_TEXT_FIELDS = ("brand", "category", "subcategory", "gender", "fabric", "color", "pattern",
                "fit", "sleeve", "neck", "description", "seller", "care", "country_of_origin", "sku")
_LIST_FIELDS = ("occasion", "highlights")


def normalise(record: dict[str, Any]) -> dict[str, Any]:
    """A catalogue record with every key present (demo records pass unchanged)."""
    r = dict(record)
    r["name"] = r.get("name") or r.get("title") or r.get("id") or "Untitled"
    r["title"] = r.get("title") or r["name"]
    for key in _TEXT_FIELDS:
        r[key] = r.get(key) or ""
    for key in _LIST_FIELDS:
        r[key] = list(r.get(key) or [])
    r["price"] = r.get("price") or 0
    r["mrp"] = r.get("mrp") or r["price"]
    if "discount_percent" not in r:
        r["discount_percent"] = round(100 * (1 - r["price"] / r["mrp"])) if r["mrp"] else 0
    r.setdefault("rating", 0)
    r.setdefault("rating_count", 0)
    r.setdefault("currency", "INR")
    r.setdefault("delivery_days", 5)
    r.setdefault("return_days", 7)
    r.setdefault("image_url", None)
    r.setdefault("in_stock", True)

    variants = r.pop("variants", None)
    if variants is None:
        stock = r.get("stock") or {}
        sizes = r.get("sizes_available") or list(stock) or [ONE_SIZE]
        variants = [{"size": size, "stock": stock.get(size)} for size in sizes]
    r["_variants"] = [{
        "size": v.get("size") or ONE_SIZE,
        "color": v.get("color") or r["color"],
        "price": v.get("price") if v.get("price") is not None else r["price"],
        "mrp": v.get("mrp") if v.get("mrp") is not None else r["mrp"],
        "stock": v.get("stock"),
        "available": v.get("available", v.get("stock") is None or v.get("stock") > 0),
        "external_id": v.get("external_id"),
        "sku": v.get("sku") or None,
        "image_url": v.get("image_url"),
    } for v in variants]
    r["sizes_available"] = [v["size"] for v in r["_variants"]]
    r["stock"] = {v["size"]: _stock_number(v["stock"], v["available"]) for v in r["_variants"]}
    return r


def _stock_number(stock: Optional[int], available: bool) -> int:
    """A count for the record's stock map. A source that says only "available"
    (Shopify's public products.json) shows as 1: code only ever asks "> 0?"."""
    return stock if stock is not None else (1 if available else 0)


def build_document(product: dict[str, Any]) -> str:
    """The text that gets embedded, and therefore what can be found.

    Occasion, fabric and colour vocabulary carry the most weight: customers ask
    by event ("something for a sangeet") rather than by SKU. Price and stock are
    left out on purpose — they are filters, not meaning, and bare numbers add
    noise to the vector without helping any query. Empty fields are skipped, so
    a sparse Shopify record is not padded with "Fabric: . Colour: .".
    """
    def part(*pairs: tuple[str, str]) -> str:
        return ". ".join(f"{label}: {value}" for label, value in pairs if value)

    category = f"{product['gender']} {product['category']}".strip()
    if product["subcategory"]:
        category = f"{category}, {product['subcategory']}"
    pieces = [
        product["title"],
        part(("Category", category)),
        part(("Fabric", product["fabric"]), ("Colour", product["color"]), ("Pattern", product["pattern"])),
        part(("Fit", product["fit"]), ("Sleeve", product["sleeve"]), ("Neckline", product["neck"])),
        part(("Suitable for", ", ".join(product["occasion"]))),
        product["description"],
        " ".join(product["highlights"]),
    ]
    return ". ".join(piece for piece in pieces if piece)


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _next_codes(existing: set[str], prefix: str, count: int) -> list[str]:
    """New product codes after the highest one in use: PREFIX + 3 digits (or more)."""
    numbers = [int(m.group(1)) for c in existing if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", c))]
    start = max(numbers, default=0) + 1
    return [f"{prefix}{n:03d}" for n in range(start, start + count)]


def upsert(store_id: int, records: list[dict[str, Any]], code_prefix: str = "",
           embed: bool = True) -> dict[str, int]:
    """Make the store's catalogue match `records`. Returns what changed.

    A record's code is its "id" when the source has one (the demo feed);
    otherwise the product already holding its external_id keeps its code, and
    new ones get the next free code after code_prefix.
    """
    normalised = [normalise(r) for r in records]
    counts = {"added": 0, "updated": 0, "deactivated": 0, "embedded": 0}
    with session_scope() as db:
        db.exec(text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_SYNC_LOCK))
        existing = {p.code: p for p in db.exec(select(Product).where(Product.store_id == store_id)).all()}
        by_external = {p.external_id: p for p in existing.values() if p.external_id}

        unnamed = [r for r in normalised if not r.get("id") and r.get("external_id") not in by_external]
        for record, code in zip(unnamed, _next_codes(set(existing), code_prefix, len(unnamed))):
            record["id"] = code
        for record in normalised:
            if not record.get("id"):
                record["id"] = by_external[record["external_id"]].code

        seen: set[str] = set()
        for record in normalised:
            code = record["id"]
            seen.add(code)
            variants = record.pop("_variants")
            document = build_document(record)
            row = existing.get(code)
            if row is None:
                row = Product(store_id=store_id, code=code, price=_money(record["price"]), data={})
                db.add(row)
                counts["added"] += 1
            elif row.data != record or not row.active:
                counts["updated"] += 1
            if row.search_text != document:
                row.embedding = None            # re-embedded below
            row.external_id = record.get("external_id") or row.external_id
            row.name = record["name"][:200]
            row.category = record["category"][:60]
            row.gender = record["gender"][:20]
            row.price = _money(record["price"])
            row.mrp = _money(record["mrp"])
            row.image_url = record["image_url"]
            row.active = True
            row.data = record
            row.search_text = document
            row.updated_at = utcnow()
            db.flush()
            _upsert_variants(db, row, variants)

        for code, row in existing.items():
            if code not in seen and row.active:
                row.active = False
                counts["deactivated"] += 1

    if embed:
        counts["embedded"] = embed_stale(store_id)
    catalog.invalidate(store_id)
    return counts


def _upsert_variants(db, product: Product, variants: list[dict[str, Any]]) -> None:
    current = {v.size: v for v in db.exec(
        select(ProductVariant).where(ProductVariant.product_pk == product.product_pk)).all()}
    for position, v in enumerate(variants):
        row = current.pop(v["size"], None)
        if row is None:
            row = ProductVariant(product_pk=product.product_pk, store_id=product.store_id,
                                 size=v["size"], price=_money(v["price"]))
            db.add(row)
        row.color = v["color"][:60]
        row.price = _money(v["price"])
        row.mrp = _money(v["mrp"]) if v["mrp"] is not None else None
        row.stock = v["stock"]
        row.available = bool(v["available"])
        row.external_id = v["external_id"]
        row.sku = v["sku"]
        row.image_url = v["image_url"]
        row.position = position
    for gone in current.values():
        db.delete(gone)     # cart lines pointing at it keep their size; variant_id -> NULL


def embed_stale(store_id: Optional[int] = None, batch: int = 64) -> int:
    """Embed active products with no vector, or one from a different model."""
    model = embeddings.model_name()
    done = 0
    while True:
        with session_scope() as db:
            query = select(Product.product_pk, Product.search_text).where(
                Product.active, (Product.embedding.is_(None)) | (Product.embedding_model != model))
            if store_id is not None:
                query = query.where(Product.store_id == store_id)
            rows = db.exec(query.limit(batch)).all()
        if not rows:
            return done
        vectors = embeddings.embed_documents([search_text for _, search_text in rows])
        with session_scope() as db:
            for (pk, _), vector in zip(rows, vectors):
                product = db.get(Product, pk)
                product.embedding = vector
                product.embedding_model = model
        done += len(rows)


def ensure_catalogues() -> None:
    """Load feed stores that have no products yet; refresh stale vectors."""
    from connectors import connector_for

    with session_scope() as db:
        empty_feeds = db.exec(
            select(Store).where(Store.platform == "feed",
                                ~select(Product.product_pk).where(Product.store_id == Store.store_id)
                                .exists())).all()
        stores = [s.model_dump() for s in empty_feeds]
    for store in stores:
        counts = connector_for(store).sync_catalog()
        print(f"[catalog] loaded store {store['slug']}: {counts}")
    embedded = embed_stale()
    if embedded:
        print(f"[catalog] embedded {embedded} products with {embeddings.model_name()}")
