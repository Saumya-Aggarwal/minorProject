"""Storefront pages.

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
async def account(request: Request):
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
        {"user": user, "orders": orders, "link": link},
    )


@router.post("/buy/{product_id}")
async def buy(request: Request, product_id: str):
    """Placeholder checkout so order history has something in it before Razorpay."""
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    product = catalog.get_by_id(product_id)
    if product is None:
        return RedirectResponse("/", status_code=303)

    await asyncio.to_thread(
        repo.create_order,
        user["user_id"],
        product["id"],
        product["name"],
        product["price"],
        None,
        "web",
    )
    return RedirectResponse("/account", status_code=303)
