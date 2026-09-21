"""JSON API — the durable interface to the backend.

The Jinja pages in store.py are one client of this logic; a Next.js frontend
later would be another. Business logic lives in catalog.py / repository.py /
auth.py, never in a route handler.
"""

import asyncio
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import catalog
import checkout as checkout_flow
import payments
import repository as repo
from auth import current_user
from repository import LINK_PREFIX

router = APIRouter(prefix="/api", tags=["api"])


def whatsapp_deep_link(token: str) -> str:
    """wa.me needs the display phone number, not the Phone Number ID."""
    number = os.getenv("WHATSAPP_DISPLAY_NUMBER", "").lstrip("+").replace(" ", "")
    text = quote(f"{LINK_PREFIX}{token}")
    if not number:
        print("[api] WHATSAPP_DISPLAY_NUMBER not set — deep link will not open a chat")
    return f"https://wa.me/{number}?text={text}"


@router.get("/products")
async def list_products():
    return {"products": catalog.get_all(), "categories": catalog.categories()}


@router.get("/products/{product_id}")
async def get_product(product_id: str):
    product = catalog.get_by_id(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("/orders")
async def list_orders(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    orders = await asyncio.to_thread(repo.get_order_history, user["user_id"], 20)
    return {"orders": orders}


@router.post("/link/start")
async def start_link(request: Request):
    """Mint a single-use code and return the wa.me link that carries it."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")

    token = await asyncio.to_thread(repo.create_link_token, user["user_id"])
    return {
        "token": token,
        "message": f"{LINK_PREFIX}{token}",
        "whatsapp_url": whatsapp_deep_link(token),
        "expires_in_minutes": repo.LINK_TOKEN_TTL_MINUTES,
    }


@router.get("/me")
async def me(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


# --- cart ---------------------------------------------------------------------


class CartAdd(BaseModel):
    product_id: str
    size: str = ""
    quantity: int = Field(default=1, ge=1, le=10)


class CartChange(BaseModel):
    quantity: int | None = Field(default=None, ge=0, le=10)
    size: str | None = None


def _signed_in(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


@router.get("/cart")
async def get_cart(request: Request):
    user = _signed_in(request)
    return await asyncio.to_thread(repo.get_cart, user["user_id"])


@router.post("/cart/items", status_code=201)
async def add_cart_item(request: Request, body: CartAdd):
    user = _signed_in(request)
    try:
        return await asyncio.to_thread(
            repo.add_to_cart, user["user_id"], body.product_id, body.size, body.quantity
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/cart/items/{cart_item_id}")
async def change_cart_item(request: Request, cart_item_id: int, body: CartChange):
    user = _signed_in(request)
    if body.quantity is not None:
        if not await asyncio.to_thread(
            repo.update_cart_item, user["user_id"], cart_item_id, body.quantity
        ):
            raise HTTPException(status_code=404, detail="Cart item not found")
    if body.size:
        try:
            if not await asyncio.to_thread(
                repo.update_cart_item_size, user["user_id"], cart_item_id, body.size
            ):
                raise HTTPException(status_code=404, detail="Cart item not found")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await asyncio.to_thread(repo.get_cart, user["user_id"])


@router.delete("/cart/items/{cart_item_id}")
async def delete_cart_item(request: Request, cart_item_id: int):
    user = _signed_in(request)
    if not await asyncio.to_thread(repo.remove_from_cart, user["user_id"], cart_item_id):
        raise HTTPException(status_code=404, detail="Cart item not found")
    return await asyncio.to_thread(repo.get_cart, user["user_id"])


@router.post("/checkout", status_code=201)
async def checkout(request: Request):
    user = _signed_in(request)
    try:
        placed = await checkout_flow.checkout_cart(user["user_id"], "web")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except payments.PaymentError as exc:
        raise HTTPException(status_code=502, detail="Payment provider unavailable") from exc
    if placed is None:
        raise HTTPException(status_code=422, detail="Cart is empty")
    # payment_url is where a client sends the customer to pay
    return {**placed, "payment_url": placed["url"]}


@router.get("/orders/{order_id}")
async def get_order(request: Request, order_id: int):
    user = _signed_in(request)
    order = await asyncio.to_thread(repo.get_order, order_id)
    if order is None or order["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    # Contact details are the customer's own, but the API has no need to echo them
    return {k: v for k, v in order.items() if k not in ("customer_email", "whatsapp_number")}
