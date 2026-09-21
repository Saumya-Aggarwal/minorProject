"""Discovery pages: home, the shop listing, search, and product detail.

Owned by Dev A since the storefront redesign (see WORK_SPLIT.md). Filtering is
plain Python in catalog.py; search and "You may also like" come from search.py,
which uses the same vector index as the WhatsApp assistant.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import catalog
import repository as repo
import search
from auth import current_user
from templating import templates

router = APIRouter(tags=["browse"])

STATIC = Path(__file__).resolve().parent.parent / "static"

# Home page tiles: label, listing URL, and the product whose photo represents it
CATEGORY_TILES = [
    ("Men", "/shop?gender=Men", "EW006"),
    ("Women", "/shop?gender=Women", "EW017"),
    ("Sarees", "/shop?category=Saree", "EW020"),
    ("The Wedding Edit", "/shop?occasion=wedding", "EW018"),
    ("Lehengas", "/shop?category=Lehenga", "EW057"),
    ("Kurta Sets", "/shop?category=Kurta+Set", "EW033"),
    ("Footwear", "/shop?category=Footwear", "EW077"),
    ("Bags", "/shop?category=Bags", "EW079"),
]


def _query_builder(params: dict[str, str]):
    """qs(sort="price-asc") -> "?gender=Women&sort=price-asc": current filters, one changed."""
    def qs(**changes: str) -> str:
        merged = {**params, **changes}
        clean = {k: v for k, v in merged.items() if v not in (None, "")}
        return "?" + urlencode(clean) if clean else "?"
    return qs


def _clean_filters(gender: str, category: str, price: str, occasion: str) -> dict[str, str]:
    """Drop anything not in the catalogue, so a hand-edited URL cannot break the page."""
    return {
        "gender": gender if gender in ("Men", "Women") else "",
        "category": category if category in catalog.categories() else "",
        "price": price if price in catalog.PRICE_BANDS else "",
        "occasion": occasion if occasion in catalog.OCCASION_GROUPS else "",
    }


# Mass nouns and names that take no plural "s"
_NO_PLURAL = {"Bottomwear", "Footwear", "Headwear", "Indo-Western", "Sharara & Gharara"}


def _heading(filters: dict[str, str]) -> str:
    parts = []
    if filters["occasion"]:
        parts.append(catalog.OCCASION_GROUPS[filters["occasion"]][0])
    if filters["gender"]:
        parts.append(filters["gender"])
    if filters["category"]:
        category = filters["category"]
        # "Bottomwear" and similar mass nouns take no plural
        parts.append(category if category in _NO_PLURAL or category.endswith("s") else category + "s")
    return " · ".join(parts) or "All products"


def _hero_image() -> str:
    """A dedicated hero photo once one exists, else the flagship product's."""
    for name in ("hero.jpg", "hero.webp"):
        if (STATIC / "hero" / name).exists():
            return f"/static/hero/{name}"
    return catalog.get_by_id("EW006")["image_url"]


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    recommended = []
    if user:
        owned = await asyncio.to_thread(repo.get_purchased_product_ids, user["user_id"])
        if owned:
            recommended = await asyncio.to_thread(search.recommended_for, owned, 4)

    products = catalog.get_all()
    tiles = [
        {"label": label, "url": url, "image": (catalog.get_by_id(pid) or {}).get("image_url")}
        for label, url, pid in CATEGORY_TILES
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "hero_image": _hero_image(),
            "tiles": tiles,
            "recommended": recommended,
            "bestsellers": catalog.bestsellers(8),
            "occasions": catalog.OCCASION_GROUPS,
            "delivery_range": (min(p["delivery_days"] for p in products),
                               max(p["delivery_days"] for p in products)),
            "returns_range": (min(p["return_days"] for p in products),
                              max(p["return_days"] for p in products)),
        },
    )


@router.get("/shop", response_class=HTMLResponse)
async def shop(
    request: Request,
    gender: str = "",
    category: str = "",
    price: str = "",
    occasion: str = "",
    sort: str = "popular",
):
    filters = _clean_filters(gender, category, price, occasion)
    sort = sort if sort in catalog.SORTS else "popular"
    everything = catalog.get_all()
    products = catalog.sort_products(catalog.filter_products(everything, **filters), sort)
    return templates.TemplateResponse(
        request,
        "shop.html",
        {
            "user": current_user(request),
            "products": products,
            "filters": filters,
            "facets": catalog.facets(everything, **filters),
            "sort": sort,
            "sorts": catalog.SORTS,
            "heading": _heading(filters),
            "qs": _query_builder({**filters, "sort": sort if sort != "popular" else ""}),
            "base_path": "/shop",
            "query": "",
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    gender: str = "",
    category: str = "",
    price: str = "",
    occasion: str = "",
    sort: str = "relevance",
):
    q = q.strip()[:120]
    filters = _clean_filters(gender, category, price, occasion)
    sorts = {"relevance": "Best match", **catalog.SORTS}
    sort = sort if sort in sorts else "relevance"

    matches = await asyncio.to_thread(search.search_products, q) if q else []
    products = catalog.sort_products(catalog.filter_products(matches, **filters), sort)
    return templates.TemplateResponse(
        request,
        "shop.html",
        {
            "user": current_user(request),
            "products": products,
            "filters": filters,
            "facets": catalog.facets(matches, **filters),
            "sort": sort,
            "sorts": sorts,
            "heading": f"Results for “{q}”" if q else "Search",
            "qs": _query_builder({"q": q, **filters, "sort": sort if sort != "relevance" else ""}),
            "base_path": "/search",
            "query": q,
            "is_search": True,
        },
    )


@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_page(request: Request, product_id: str):
    product = catalog.get_by_id(product_id)
    if product is None:
        return templates.TemplateResponse(
            request, "404.html", {"user": current_user(request)}, status_code=404
        )
    similar = await asyncio.to_thread(search.similar_products, product_id, 4)
    return templates.TemplateResponse(
        request,
        "product.html",
        {
            "user": current_user(request),
            "product": product,
            "similar": similar,
            "ask_text": f"Tell me about the {product['name']} ({product['id']})",
        },
    )


@router.get("/api/search")
async def search_api(q: str = ""):
    """JSON search over the same vector index as the /search page."""
    q = q.strip()[:120]
    products = await asyncio.to_thread(search.search_products, q) if q else []
    return {"query": q, "products": products}
