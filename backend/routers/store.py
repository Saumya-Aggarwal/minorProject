"""Account and transaction pages: auth, account, cart, checkout, orders.

Owned by Dev A ("buying"). Browsing pages live in browse.py (Dev B).

Thin handlers: parse the form, call into auth/catalog/repository, render. Anything
worth reusing belongs in those modules, not here — that is what keeps a future
Next.js frontend a drop-in replacement for these templates.
"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import catalog
import checkout as checkout_flow
import payments
import repository as repo
from auth import (
    create_web_user,
    current_user,
    find_user_by_email,
    login_user,
    logout_user,
    validate_credentials,
    verify_password,
)
from routers.api import whatsapp_deep_link

router = APIRouter(tags=["store"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if current_user(request):
        return RedirectResponse("/account", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"user": None})


@router.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
):
    error = validate_credentials(email, password)
    if error is None and await asyncio.to_thread(find_user_by_email, email):
        error = "That email is already registered."

    if error:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": error, "email": email, "user": None},
            status_code=400,
        )

    user_id = await asyncio.to_thread(create_web_user, email, password, display_name)
    login_user(request, user_id)
    return RedirectResponse("/account", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/account", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"user": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = await asyncio.to_thread(find_user_by_email, email)

    # One message for both cases, so the form cannot be used to discover
    # which email addresses are registered
    if user is None or not verify_password(password, user.get("password_hash")):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect email or password.", "email": email, "user": None},
            status_code=401,
        )

    login_user(request, user["user_id"])
    return RedirectResponse("/account", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/", status_code=303)


@router.get("/account", response_class=HTMLResponse)
async def account(request: Request, order: int | None = None):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    orders = await asyncio.to_thread(repo.get_order_history, user["user_id"], 20)
    link = None
    if not user["whatsapp_number"]:
        token = await asyncio.to_thread(repo.create_link_token, user["user_id"])
        link = {
            "url": whatsapp_deep_link(token),
            "code": f"{repo.LINK_PREFIX}{token}",
            "ttl": repo.LINK_TOKEN_TTL_MINUTES,
        }

    return templates.TemplateResponse(
        request,
        "account.html",
        {"user": user, "orders": orders, "link": link, "just_ordered": order},
    )


@router.post("/buy/{product_id}")
async def buy(
    request: Request, product_id: str, size: str = Form(""), quantity: int = Form(1)
):
    """Single-item Buy now: create the order, then send the browser to pay for it."""
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    product = catalog.get_by_id(product_id)
    if product is None:
        return RedirectResponse("/", status_code=303)

    try:
        placed = await checkout_flow.checkout_single(
            user["user_id"], product["id"], size, _clamp_quantity(quantity), "web"
        )
    except ValueError:
        # Missing or invalid size. The form requires one, so this is a tampered
        # request: send them back to choose.
        return RedirectResponse(f"/product/{product_id}", status_code=303)
    except payments.PaymentError as exc:
        print(f"[store] payment link failed: {exc}")
        return RedirectResponse("/cart?error=payment", status_code=303)
    # 303 to an external URL: the browser leaves for Razorpay's hosted page
    return RedirectResponse(placed["url"], status_code=303)


# --- cart -----------------------------------------------------------------------
# The same cart_items rows the WhatsApp bot reads: add in chat, see it here.

MAX_QUANTITY = 10

CART_ERRORS = {
    "size": "Choose a size for every item before checking out.",
    "invalid": "That size is not available.",
    "empty": "Your cart is empty.",
    "payment": "Payments are unavailable for a moment, so nothing was charged. Please try again.",
}


def _clamp_quantity(quantity: int) -> int:
    return max(1, min(int(quantity), MAX_QUANTITY))


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, error: str = ""):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    cart = await asyncio.to_thread(repo.get_cart, user["user_id"])
    return templates.TemplateResponse(
        request,
        "cart.html",
        {"user": user, "cart": cart, "error": CART_ERRORS.get(error, "")},
    )


@router.post("/cart/add/{product_id}")
async def cart_add(
    request: Request, product_id: str, size: str = Form(""), quantity: int = Form(1)
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    product = catalog.get_by_id(product_id)
    if product is None:
        return RedirectResponse("/", status_code=303)
    # The website has a size dropdown, so unlike chat it always requires one
    if not size and len(product.get("sizes_available") or []) > 1:
        return RedirectResponse(f"/product/{product_id}", status_code=303)

    try:
        await asyncio.to_thread(
            repo.add_to_cart, user["user_id"], product_id, size, _clamp_quantity(quantity)
        )
    except ValueError:
        return RedirectResponse(f"/product/{product_id}", status_code=303)
    return RedirectResponse("/cart", status_code=303)


@router.post("/cart/update")
async def cart_update(
    request: Request,
    cart_item_id: int = Form(...),
    quantity: int = Form(1),
    size: str = Form(""),
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Quantity first, then size: a size change can merge this line into
    # another, and the merge should carry the new quantity with it
    await asyncio.to_thread(
        repo.update_cart_item, user["user_id"], cart_item_id, _clamp_quantity(quantity)
    )
    if size:
        try:
            await asyncio.to_thread(repo.update_cart_item_size, user["user_id"], cart_item_id, size)
        except ValueError:
            return RedirectResponse("/cart?error=invalid", status_code=303)
    return RedirectResponse("/cart", status_code=303)


@router.post("/cart/remove")
async def cart_remove(request: Request, cart_item_id: int = Form(...)):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    await asyncio.to_thread(repo.remove_from_cart, user["user_id"], cart_item_id)
    return RedirectResponse("/cart", status_code=303)


@router.post("/checkout")
async def checkout(request: Request):
    """Order the cart and send the browser to Razorpay's payment page."""
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        placed = await checkout_flow.checkout_cart(user["user_id"], "web")
    except ValueError:
        return RedirectResponse("/cart?error=size", status_code=303)
    except payments.PaymentError as exc:
        print(f"[store] payment link failed: {exc}")
        return RedirectResponse("/cart?error=payment", status_code=303)
    if placed is None:
        return RedirectResponse("/cart?error=empty", status_code=303)
    return RedirectResponse(placed["url"], status_code=303)
