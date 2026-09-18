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
from auth import current_user

router = APIRouter(tags=["browse"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"products": catalog.get_all(), "user": current_user(request)},
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
