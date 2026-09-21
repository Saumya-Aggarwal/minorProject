"""Account and transaction pages: auth, account, bag, checkout, orders.

Owned by Dev A ("buying"). Browsing pages live in browse.py.

Thin handlers: parse the form, call into auth/catalog/repository/checkout,
render. Anything worth reusing belongs in those modules, not here — that is
what keeps a future Next.js frontend a drop-in replacement for these templates.
"""

import asyncio
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import catalog
import checkout as checkout_flow
import payments
import repository as repo
import shipping as addresses
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
from templating import templates

router = APIRouter(tags=["store"])

MAX_QUANTITY = 10

CART_ERRORS = {
    "size": "Choose a size for every item before checking out.",
    "invalid": "That size is not available.",
    "empty": "Your bag is empty.",
    "payment": "Payments are unavailable for a moment, so nothing was charged. Please try again.",
}


def _clamp_quantity(quantity: int) -> int:
    return max(1, min(int(quantity), MAX_QUANTITY))


def _safe_next(target: Optional[str]) -> str:
    """Only same-site paths: "/cart" yes, "https://evil.example" or "//evil" no.

    Without this check the sign-in page would be an open redirect: a link
    could send a customer through our login and on to someone else's site.
    """
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/account"


def _to_login(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/login?{urlencode({'next': next_path})}", status_code=303)


# --- sign up, sign in -----------------------------------------------------------


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request, next: str = ""):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"user": None, "next": _safe_next(next)})


@router.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    next: str = Form(""),
):
    error = validate_credentials(email, password)
    if error is None and await asyncio.to_thread(find_user_by_email, email):
        error = "That email is already registered. Sign in instead?"

    if error:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": error, "email": email, "display_name": display_name,
             "user": None, "next": _safe_next(next)},
            status_code=400,
        )

    user_id = await asyncio.to_thread(create_web_user, email, password, display_name)
    login_user(request, user_id)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = ""):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"user": None, "next": _safe_next(next)})


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("")
):
    user = await asyncio.to_thread(find_user_by_email, email)

    # One message for both cases, so the form cannot be used to discover
    # which email addresses are registered
    if user is None or not verify_password(password, user.get("password_hash")):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect email or password.", "email": email,
             "user": None, "next": _safe_next(next)},
            status_code=401,
        )

    login_user(request, user["user_id"])
    return RedirectResponse(_safe_next(next), status_code=303)


@router.get("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/", status_code=303)


# --- account ----------------------------------------------------------------------


@router.get("/account", response_class=HTMLResponse)
async def account(request: Request, order: int | None = None):
    user = current_user(request)
    if user is None:
        return _to_login("/account")

    orders = await asyncio.to_thread(repo.get_order_history, user["user_id"], 20)
    address = await asyncio.to_thread(repo.get_last_shipping, user["user_id"])
    link = None
    if not user["whatsapp_number"]:
        token = await asyncio.to_thread(repo.get_or_create_link_token, user["user_id"])
        link = {
            "url": whatsapp_deep_link(token),
            "code": f"{repo.LINK_PREFIX}{token}",
            "ttl": repo.LINK_TOKEN_TTL_MINUTES,
        }

    return templates.TemplateResponse(
        request,
        "account.html",
        {"user": user, "orders": orders, "link": link, "address": address,
         "address_line": addresses.one_line(address) if address else "", "just_ordered": order},
    )


# --- bag ----------------------------------------------------------------------------


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, error: str = ""):
    user = current_user(request)
    if user is None:
        return _to_login("/cart")
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
        return _to_login(f"/product/{product_id}")

    product = catalog.get_by_id(product_id)
    if product is None:
        return RedirectResponse("/shop", status_code=303)
    # The website has size buttons, so unlike chat it always requires one
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
        return _to_login("/cart")

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
        return _to_login("/cart")
    await asyncio.to_thread(repo.remove_from_cart, user["user_id"], cart_item_id)
    return RedirectResponse("/cart", status_code=303)


# --- checkout -------------------------------------------------------------------------


def _single_item(product_id: str, size: str, quantity: int) -> Optional[dict[str, Any]]:
    """A Buy now line, shaped like a cart line so one template shows both."""
    product = catalog.get_by_id(product_id)
    if product is None:
        return None
    quantity = _clamp_quantity(quantity)
    return {
        "product_id": product["id"],
        "name": product["name"],
        "image_url": product.get("image_url"),
        "size": size,
        "sizes_available": product.get("sizes_available") or [],
        "quantity": quantity,
        "price_inr": float(product["price"]),
        "mrp_inr": float(product.get("mrp") or product["price"]),
        "line_total": float(product["price"]) * quantity,
        "available": True,
    }


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    items = [i for i in items if i["available"]]
    total = sum(i["line_total"] for i in items)
    mrp_total = sum(i["mrp_inr"] * i["quantity"] for i in items)
    return {"items": items, "total_inr": total, "mrp_total_inr": mrp_total,
            "savings_inr": max(mrp_total - total, 0), "count": sum(i["quantity"] for i in items)}


async def _checkout_context(
    user: dict, product: str, size: str, quantity: int
) -> tuple[Optional[dict], Optional[str]]:
    """(order summary, redirect) — a redirect when there is nothing to check out."""
    if product:
        line = _single_item(product, size, quantity)
        if line is None:
            return None, "/shop"
        if not size and len(line["sizes_available"]) > 1:
            return None, f"/product/{product}"
        return _summary([line]), None

    cart = await asyncio.to_thread(repo.get_cart, user["user_id"])
    if not any(i["available"] for i in cart["items"]):
        return None, "/cart?error=empty"
    if any(i["available"] and not i["size"] and len(i["sizes_available"]) > 1 for i in cart["items"]):
        return None, "/cart?error=size"
    return _summary(cart["items"]), None


def _render_checkout(request, user, summary, address, errors, single, status_code=200):
    return templates.TemplateResponse(
        request,
        "checkout.html",
        {"user": user, "summary": summary, "address": address, "errors": errors,
         "states": addresses.INDIAN_STATES, "single": single},
        status_code=status_code,
    )


@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(
    request: Request, product: str = "", size: str = "", quantity: int = 1
):
    """The checkout page. With ?product= it is a Buy now for one item; else the bag."""
    user = current_user(request)
    if user is None:
        back = "/checkout" + (f"?{urlencode({'product': product, 'size': size, 'quantity': quantity})}" if product else "")
        return _to_login(back)

    summary, redirect = await _checkout_context(user, product, size, quantity)
    if redirect:
        return RedirectResponse(redirect, status_code=303)

    # Pre-fill: last address used, else what the account knows
    address = await asyncio.to_thread(repo.get_last_shipping, user["user_id"]) or {
        "name": user.get("display_name") or "",
        "phone": (user.get("whatsapp_number") or "")[-10:],
    }
    single = {"product": product, "size": size, "quantity": _clamp_quantity(quantity)} if product else None
    return _render_checkout(request, user, summary, address, {}, single)


@router.post("/checkout")
async def checkout(request: Request):
    """Validate the address, create the order, send the browser to Razorpay."""
    user = current_user(request)
    if user is None:
        return _to_login("/cart")

    form = dict(await request.form())
    product = str(form.get("product") or "")
    size = str(form.get("size") or "")
    try:
        quantity = int(form.get("quantity") or 1)
    except ValueError:
        quantity = 1

    summary, redirect = await _checkout_context(user, product, size, quantity)
    if redirect:
        return RedirectResponse(redirect, status_code=303)

    address, errors = addresses.validate(form)
    single = {"product": product, "size": size, "quantity": _clamp_quantity(quantity)} if product else None
    if errors:
        return _render_checkout(request, user, summary, address, errors, single, status_code=400)

    try:
        if product:
            placed = await checkout_flow.checkout_single(
                user["user_id"], product, size, _clamp_quantity(quantity), "web", address
            )
        else:
            placed = await checkout_flow.checkout_cart(user["user_id"], "web", address)
    except ValueError:
        return RedirectResponse("/cart?error=size", status_code=303)
    except payments.PaymentError as exc:
        print(f"[store] payment link failed: {exc}")
        return RedirectResponse("/cart?error=payment", status_code=303)
    if placed is None:
        return RedirectResponse("/cart?error=empty", status_code=303)
    # 303 to an external URL: the browser leaves for Razorpay's payment page
    return RedirectResponse(placed["url"], status_code=303)


@router.post("/buy/{product_id}")
async def buy(
    request: Request, product_id: str, size: str = Form(""), quantity: int = Form(1)
):
    """Older Buy now forms post here; every Buy now now goes through checkout."""
    query = urlencode({"product": product_id, "size": size, "quantity": _clamp_quantity(quantity)})
    return RedirectResponse(f"/checkout?{query}", status_code=303)


# --- orders ---------------------------------------------------------------------------


async def _own_order(request: Request, order_id: int):
    user = current_user(request)
    if user is None:
        return None, None
    order = await asyncio.to_thread(repo.get_order, order_id)
    # Someone else's order is a 404, not a 403: a 403 would confirm it exists
    if order is None or order["user_id"] != user["user_id"]:
        return user, None
    return user, order


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_page(request: Request, order_id: int):
    user, order = await _own_order(request, order_id)
    if user is None:
        return _to_login(f"/orders/{order_id}")
    if order is None:
        return templates.TemplateResponse(
            request, "order.html", {"user": user, "order": None}, status_code=404
        )
    return templates.TemplateResponse(
        request, "order.html",
        {"user": user, "order": order, "states": addresses.INDIAN_STATES, "errors": {},
         "address_line": addresses.one_line(order["shipping"]) if order["shipping"] else ""},
    )


@router.post("/orders/{order_id}/address", response_class=HTMLResponse)
async def order_address(request: Request, order_id: int):
    """Chat orders can be placed before we know where to send them."""
    user, order = await _own_order(request, order_id)
    if user is None:
        return _to_login(f"/orders/{order_id}")
    if order is None:
        return templates.TemplateResponse(
            request, "order.html", {"user": user, "order": None}, status_code=404
        )
    address, errors = addresses.validate(dict(await request.form()))
    if errors:
        return templates.TemplateResponse(
            request, "order.html",
            {"user": user, "order": order, "states": addresses.INDIAN_STATES,
             "errors": errors, "draft": address, "address_line": ""},
            status_code=400,
        )
    await asyncio.to_thread(repo.set_order_shipping, order_id, address)
    return RedirectResponse(f"/orders/{order_id}", status_code=303)
