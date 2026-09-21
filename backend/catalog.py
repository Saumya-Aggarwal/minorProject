"""Read access to the product catalog.

Products live in data/products.json, not Postgres: orders snapshot the name and
price at purchase time, so nothing needs a foreign key into the catalog.

Everything goes through this module so the backing store can become a database
table later without touching any caller.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "products.json"


@lru_cache(maxsize=1)
def _load() -> list[dict[str, Any]]:
    if not CATALOG_PATH.exists():
        print(f"[catalog] missing {CATALOG_PATH}")
        return []
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def get_all() -> list[dict[str, Any]]:
    return list(_load())


def get_by_id(product_id: str) -> Optional[dict[str, Any]]:
    return next((p for p in _load() if p["id"] == product_id), None)


def categories() -> list[str]:
    return sorted({p["category"] for p in _load()})


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
    "wedding": ("Weddings", {"Wedding", "Groom", "Bridal", "Reception", "Engagement",
                             "Summer wedding", "Winter wedding"}),
    "festive": ("Festive", {"Festive", "Diwali", "Eid", "Navratri", "Puja", "Haldi",
                            "Mehendi", "Traditional", "Temple"}),
    "party": ("Sangeet & parties", {"Sangeet", "Party", "Cocktail", "Evening", "Formal evening"}),
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
