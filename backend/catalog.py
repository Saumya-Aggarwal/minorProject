"""Read access to the product catalogue, per store.

Products live in Postgres (products + product_variants, written only by
catalog_sync.py). Every function here answers for the current store
(tenancy.py) and returns the same dicts data/products.json always held, so
nothing that reads a product had to change when the catalogue moved.

Stock and sizes come from the variants; the rest of a record is the source's
own fields. Orders still snapshot name and price, so an order never follows
a later catalogue edit.

Each store's catalogue is cached in memory for CACHE_SECONDS, and a sync
clears it at once.
"""

import time
from typing import Any, Optional

from sqlalchemy import text
from sqlmodel import select

import tenancy
from common import embeddings

CACHE_SECONDS = 60

# store_id -> (loaded_at, products in catalogue order, products by code)
_cache: dict[int, tuple[float, list[dict[str, Any]], dict[str, dict[str, Any]]]] = {}


def invalidate(store_id: Optional[int] = None) -> None:
    """Forget a store's cached catalogue (all stores when None)."""
    if store_id is None:
        _cache.clear()
    else:
        _cache.pop(store_id, None)


def _record(product: Any, variants: list[Any]) -> dict[str, Any]:
    record = dict(product.data)
    record["id"] = product.code
    if variants:
        record["sizes_available"] = [v.size for v in variants]
        # How many, or 1 when the source only says "available": code asks "> 0?"
        record["stock"] = {v.size: v.stock if v.stock is not None else int(v.available)
                           for v in variants}
        record["in_stock"] = bool(record.get("in_stock", True)) and any(
            v.available and (v.stock is None or v.stock > 0) for v in variants)
        record["variant_ids"] = {v.size: v.variant_id for v in variants}
    return record


def _load(store_id: Optional[int] = None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    from db import session_scope
    from models import Product, ProductVariant

    sid = tenancy.store_id() if store_id is None else store_id
    hit = _cache.get(sid)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    with session_scope() as db:
        products = db.exec(select(Product).where(Product.store_id == sid, Product.active)
                           .order_by(Product.code)).all()
        variants: dict[int, list[Any]] = {}
        for v in db.exec(select(ProductVariant).where(ProductVariant.store_id == sid)
                         .order_by(ProductVariant.product_pk, ProductVariant.position)).all():
            variants.setdefault(v.product_pk, []).append(v)
        records = [_record(p, variants.get(p.product_pk, [])) for p in products]
    by_code = {r["id"]: r for r in records}
    _cache[sid] = (time.time(), records, by_code)
    return records, by_code


def get_all() -> list[dict[str, Any]]:
    return list(_load()[0])


def get_by_id(product_id: str) -> Optional[dict[str, Any]]:
    return _load()[1].get(product_id)


def categories() -> list[str]:
    return sorted({p["category"] for p in _load()[0] if p["category"]})


def ranked_by_vector(vector: list[float], exclude: Optional[set[str]] = None,
                     gender: Optional[str] = None, limit: Optional[int] = None) -> list[tuple[str, float]]:
    """(code, cosine distance) for the current store's products, nearest first.

    An exact scan: with hundreds of products per store, reading every vector
    is faster than maintaining an approximate index, and never skips one.
    """
    from db import session_scope

    sql = ["SELECT code, embedding <=> CAST(:vector AS vector) AS distance FROM products",
           "WHERE store_id = :store AND active AND embedding_model = :model"]
    params: dict[str, Any] = {"vector": str(list(vector)), "store": tenancy.store_id(),
                              "model": embeddings.model_name()}
    if exclude:
        sql.append("AND NOT (code = ANY(:exclude))")
        params["exclude"] = list(exclude)
    if gender:
        sql.append("AND gender = :gender")
        params["gender"] = gender
    sql.append("ORDER BY distance, code")
    if limit:
        sql.append("LIMIT :limit")
        params["limit"] = limit
    with session_scope() as db:
        return [(code, float(distance)) for code, distance in db.exec(text(" ".join(sql)).bindparams(**params)).all()]


def stored_vector(product_id: str) -> Optional[list[float]]:
    """A product's own embedding (current model only)."""
    from db import session_scope
    from models import Product

    with session_scope() as db:
        vector = db.exec(select(Product.embedding).where(
            Product.store_id == tenancy.store_id(), Product.code == product_id,
            Product.embedding_model == embeddings.model_name())).first()
    return None if vector is None else [float(x) for x in vector]


# --- browsing: filters, sort, facet counts ---------------------------------------
# Plain Python over 32 records: no database or vector search needed to filter.

# Price bands: key -> (min, max) inclusive; None means open-ended
PRICE_BANDS: dict[str, tuple[str, int, Optional[int]]] = {
    "under-1500": ("Under ₹1,500", 0, 1499),
    "1500-3000": ("₹1,500 – ₹3,000", 1500, 3000),
    "3000-7500": ("₹3,000 – ₹7,500", 3001, 7500),
    "over-7500": ("Over ₹7,500", 7501, None),
}

# The catalogue tags 27 distinct occasions; customers think in four
OCCASION_GROUPS: dict[str, tuple[str, set[str]]] = {
    # One entry per function, in the order they happen; a product can be in several
    "haldi": ("Haldi", {"Haldi"}),
    "mehendi": ("Mehendi", {"Mehendi"}),
    "party": ("Sangeet & parties", {"Sangeet", "Party", "Cocktail", "Evening", "Formal evening"}),
    "wedding": ("Wedding", {"Wedding", "Groom", "Bridal", "Baraat", "Engagement",
                            "Summer wedding", "Winter wedding"}),
    "reception": ("Reception", {"Reception"}),
    "festive": ("Festive", {"Festive", "Diwali", "Eid", "Navratri", "Garba", "Puja", "Traditional",
                            "Temple", "Onam", "Vishu", "Lohri"}),
    "everyday": ("Everyday & office", {"Casual", "Daytime", "Office", "Everyday",
                                       "Everyday ethnic", "Family function"}),
}

SORTS: dict[str, str] = {
    "popular": "Most popular",
    "price-asc": "Price: low to high",
    "price-desc": "Price: high to low",
    "rating": "Top rated",
    "discount": "Biggest discount",
}


def _matches(product: dict[str, Any], gender: str, category: str, price: str, occasion: str) -> bool:
    if gender and product["gender"] != gender:
        return False
    if category and product["category"] != category:
        return False
    if price in PRICE_BANDS:
        _, low, high = PRICE_BANDS[price]
        if product["price"] < low or (high is not None and product["price"] > high):
            return False
    if occasion in OCCASION_GROUPS:
        if not set(product["occasion"]) & OCCASION_GROUPS[occasion][1]:
            return False
    return True


def filter_products(
    products: list[dict[str, Any]],
    gender: str = "",
    category: str = "",
    price: str = "",
    occasion: str = "",
) -> list[dict[str, Any]]:
    return [p for p in products if _matches(p, gender, category, price, occasion)]


def sort_products(products: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Unknown sort keys (including "relevance") keep the incoming order."""
    keys = {
        "popular": lambda p: -p["rating_count"],
        "price-asc": lambda p: p["price"],
        "price-desc": lambda p: -p["price"],
        "rating": lambda p: (-p["rating"], -p["rating_count"]),
        "discount": lambda p: -p["discount_percent"],
    }
    return sorted(products, key=keys[sort]) if sort in keys else list(products)


def facets(
    products: list[dict[str, Any]], gender: str, category: str, price: str, occasion: str
) -> dict[str, list[tuple[str, str, int]]]:
    """Options for each filter as (value, label, count).

    Each facet is counted with every OTHER active filter applied, so the counts
    say what choosing that option would return — the standard behaviour
    customers expect from a shop sidebar.
    """
    active = {"gender": gender, "category": category, "price": price, "occasion": occasion}

    def base_for(facet: str) -> list[dict[str, Any]]:
        others = {**active, facet: ""}
        return filter_products(products, **others)

    genders = base_for("gender")
    cats = base_for("category")
    prices = base_for("price")
    occasions = base_for("occasion")
    return {
        "gender": [(g, g, sum(p["gender"] == g for p in genders)) for g in ("Men", "Women")],
        "category": sorted(
            {(c, c, sum(p["category"] == c for p in cats)) for c in {p["category"] for p in cats}},
            key=lambda option: option[1],
        ),
        "price": [
            (key, label, len(filter_products(prices, price=key)))
            for key, (label, _, _) in PRICE_BANDS.items()
        ],
        "occasion": [
            (key, label, len(filter_products(occasions, occasion=key)))
            for key, (label, _) in OCCASION_GROUPS.items()
        ],
    }


def bestsellers(limit: int = 8) -> list[dict[str, Any]]:
    return sort_products(get_all(), "popular")[:limit]


# --- recommendations ---------------------------------------------

def _top_rated(exclude_ids: set[str], k: int, gender: str | None) -> list[dict[str, Any]]:
    products = [product for product in get_all() if product["id"] not in exclude_ids]
    if gender:
        products = [product for product in products if product.get("gender") == gender]
    return sorted(
        products,
        key=lambda product: (float(product.get("rating", 0)), int(product.get("rating_count", 0))),
        reverse=True,
    )[:k]


def get_similar_products(
    product_ids: list[str],
    exclude_ids: list[str],
    k: int = 4,
    gender: str | None = None,
) -> list[dict[str, Any]]:
    """Return full catalog records similar to the supplied products.

    Recommendations use the same vectors as chat search. If there is
    no purchase history or vector search is unavailable, popular catalog items
    provide a deterministic fallback for the storefront.
    """
    excluded = set(exclude_ids) | set(product_ids)
    source_products = [product for product in get_all() if product["id"] in product_ids]
    if not source_products:
        return _top_rated(excluded, k, gender)

    query = ". ".join(
        " ".join(
            [
                product.get("title", product.get("name", "")),
                f"Category: {product.get('gender', '')} {product.get('category', '')}",
                f"Fabric: {product.get('fabric', '')}",
                f"Suitable for: {', '.join(product.get('occasion', []))}",
                product.get("description", ""),
            ]
        )
        for product in source_products
    )

    try:
        ranked = ranked_by_vector(embeddings.embed_query(query), exclude=excluded, gender=gender, limit=k)
        recommendations = [p for p in (get_by_id(code) for code, _ in ranked) if p is not None]
        return recommendations or _top_rated(excluded, k, gender)
    except Exception as exc:
        print(f"[catalog] recommendation lookup failed, using top-rated items: {exc!r}")
        return _top_rated(excluded, k, gender)
