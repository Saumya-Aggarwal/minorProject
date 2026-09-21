"""Product discovery pages: the catalogue grid and product detail.

Owned by Dev B ("finding"). Split out of store.py so the two developers never
edit the same router — see WORK_SPLIT.md. Buying controls on the product page
come from Dev A's templates/partials/buy_box.html via an include.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import catalog
from bot.chat import get_product_recommendations
from auth import current_user
from repository import get_purchased_product_ids

router = APIRouter(tags=["browse"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    purchased_ids = get_purchased_product_ids(user["user_id"]) if user else []
    category = request.query_params.get("category") or None
    gender = request.query_params.get("gender") or None
    max_price_value = request.query_params.get("max_price")
    try:
        max_price = float(max_price_value) if max_price_value else None
    except ValueError:
        max_price = None
    recommendations = catalog.get_similar_products(
        purchased_ids,
        exclude_ids=purchased_ids,
        k=4,
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "products": catalog.filter_products(category, gender, max_price),
            "categories": catalog.categories(),
            "selected_category": category or "",
            "selected_gender": gender or "",
            "selected_max_price": max_price_value or "",
            "recommendations": recommendations,
            "user": user,
        },
    )


@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_page(request: Request, product_id: str):
    product = catalog.get_by_id(product_id)
    if product is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "product.html",
        {"product": product, "user": current_user(request)},
    )


def _search_products(query: str) -> list[dict]:
    _, matches = get_product_recommendations(query, top_k=12)
    products = []
    for match in matches:
        product = catalog.get_by_id(match.get("id", ""))
        if product is not None:
            products.append(product)
    return products


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    query = request.query_params.get("q", "").strip()
    products = _search_products(query) if query else []
    return templates.TemplateResponse(
        request,
        "search.html",
        {"products": products, "query": query, "user": current_user(request)},
    )


@router.get("/api/search")
async def search_api(request: Request):
    query = request.query_params.get("q", "").strip()
    return {"query": query, "products": _search_products(query) if query else []}
