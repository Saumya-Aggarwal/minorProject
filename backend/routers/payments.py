"""Paying, and Razorpay's two ways back to us. Owned by Dev A.

GET  /pay/{order_id}      our own payment page (Razorpay Checkout)
POST /payments/verify     the browser returns a signed payment: check and record
GET  /payments/callback   the customer's browser, redirected after paying
POST /razorpay/webhook    Razorpay's server, independently of the browser

Either one alone is enough to confirm a payment. Both exist because each can
fail on its own: the customer may close the tab before the redirect, and a
webhook can be delayed or misconfigured. checkout.mark_order_paid is idempotent,
so when both arrive the customer still gets one confirmation.
"""

import asyncio
import json
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

import checkout
import payments
import repository as repo
from auth import current_user
from templating import templates

router = APIRouter(tags=["payments"])


def _contact(order: dict) -> str:
    """The customer's phone as Razorpay Checkout expects it, or ""."""
    number = (order.get("whatsapp_number") or (order.get("shipping") or {}).get("phone") or "").strip()
    if not number:
        return ""
    number = number.lstrip("+")
    return f"+{number}" if len(number) > 10 else f"+91{number}"


@router.get("/pay/{order_id}", response_class=HTMLResponse)
async def pay_page(request: Request, order_id: int):
    """Our own payment page: Razorpay Checkout against this order.

    No sign-in: a customer who tapped Pay Now in WhatsApp opens this in their
    phone browser. Nothing here is a secret — the key id is public and the
    Razorpay order id only lets you pay it — and the amount comes from our
    database, never from the URL.
    """
    order = await asyncio.to_thread(repo.get_order, order_id)
    context = {"user": current_user(request), "order": order, "state": "unknown"}
    if order is None:
        return templates.TemplateResponse(request, "payment_result.html", context, status_code=404)
    if order["status"] != "created":
        context["state"] = "paid" if order["status"] == "captured" else "cancelled"
        return templates.TemplateResponse(request, "payment_result.html", context)

    return templates.TemplateResponse(request, "pay.html", {
        "user": context["user"],
        "order": order,
        "key_id": os.getenv("RAZORPAY_KEY_ID", ""),
        "razorpay_order_id": order["payment_link_id"] or "",
        "amount_paise": payments.to_paise(order["total_inr"]),
        "return_url": checkout.return_after_paying(order),
        # Razorpay wants a dialling code ("+919876543210"); we store bare digits
        "contact": _contact(order),
    })


@router.post("/payments/verify")
async def verify_payment(request: Request, razorpay_order_id: str = Form(""),
                         razorpay_payment_id: str = Form(""), razorpay_signature: str = Form("")):
    """Checkout hands the browser a signed (order, payment) pair; we check the
    signature with our key secret before recording anything. A forged post
    cannot pass it, and the webhook and reconciler confirm independently."""
    if not payments.verify_checkout_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
        print(f"[payments] verify rejected: bad signature for {razorpay_order_id}")
        return JSONResponse({"ok": False, "error": "signature"}, status_code=400)

    order_id = await asyncio.to_thread(repo.get_order_id_by_payment_link, razorpay_order_id)
    if order_id is None:
        return JSONResponse({"ok": False, "error": "unknown order"}, status_code=404)

    result = await checkout.record_checkout_payment(order_id, razorpay_payment_id, razorpay_order_id)
    return {"ok": True, "redirect": checkout.return_after_paying(result["order"])
            or f"/payments/callback?razorpay_payment_link_id={razorpay_order_id}"}


@router.get("/payments/callback", response_class=HTMLResponse)
async def payment_callback(request: Request):
    """Where Razorpay sends the customer after the payment page.

    Deliberately does not require sign-in: a customer who tapped Pay Now in
    WhatsApp lands here in their phone's browser, usually not logged in.
    Nothing is trusted from the URL except which link to look up.
    """
    link_id = request.query_params.get("razorpay_payment_link_id", "")
    # The "Back to WhatsApp" link comes from the template global whatsapp_url();
    # a page variable of the same name would shadow it and break base.html
    context = {"user": current_user(request), "order": None, "state": "unknown"}

    if not (link_id.startswith("plink_") or link_id.startswith("order_")):
        return templates.TemplateResponse(request, "payment_result.html", context, status_code=400)

    try:
        result = await checkout.confirm_payment_link(link_id)
    except payments.PaymentError as exc:
        # Razorpay unreachable from here. If they paid, the webhook will still
        # record it and the WhatsApp confirmation will follow.
        print(f"[payments] could not confirm {link_id}: {exc}")
        context["state"] = "pending"
        return templates.TemplateResponse(request, "payment_result.html", context)

    if result is None:
        return templates.TemplateResponse(request, "payment_result.html", context, status_code=404)

    context["order"] = result["order"]
    context["state"] = "paid" if result["order"]["status"] == "captured" else "pending"
    return templates.TemplateResponse(request, "payment_result.html", context)


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request):
    """Razorpay's server-to-server notification.

    A bad signature gets 400 and nothing is written: it is either a forgery or
    a misconfigured secret, and neither should touch an order. Everything past
    the signature check returns 200 even on error — the same rule as the Meta
    webhook, because a non-200 makes Razorpay redeliver the same event, which
    would fail the same way.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not payments.verify_webhook_signature(raw, signature):
        print("[payments] webhook rejected: bad signature (check RAZORPAY_WEBHOOK_SECRET)")
        return JSONResponse({"status": "invalid signature"}, status_code=400)

    try:
        event = json.loads(raw)
        name = event.get("event")
        payload = event.get("payload", {})

        if name == "payment_link.paid":
            link = payload["payment_link"]["entity"]
            payment = (payload.get("payment") or {}).get("entity") or {}
            await checkout.confirm_from_webhook(
                link["id"], payment.get("id") or "unknown", payment.get("order_id")
            )
        elif name == "payment.captured":
            # Standard Checkout: the payment carries the Razorpay order id
            payment = payload["payment"]["entity"]
            if payment.get("order_id"):
                await checkout.confirm_from_webhook(
                    payment["order_id"], payment.get("id") or "unknown", payment.get("order_id"))
        elif name == "payment.failed":
            payment = payload.get("payment", {}).get("entity", {})
            # A failed attempt does not fail the order: the customer can retry on
            # the same payment link, so the order stays open
            print(
                f"[payments] payment attempt failed: {payment.get('id')} "
                f"({payment.get('error_description')})"
            )
        else:
            print(f"[payments] webhook event ignored: {name}")
    except Exception as exc:
        print(f"[payments] webhook handling error: {exc!r}")

    return {"status": "ok"}
