"""JSON API — the durable interface to the backend.

The Jinja pages in store.py are one client of this logic; a Next.js frontend
later would be another. Business logic lives in catalog.py / repository.py /
auth.py, never in a route handler.
"""

import asyncio
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

import catalog
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
