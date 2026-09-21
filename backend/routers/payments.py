"""Razorpay's two ways back to us. Owned by Dev A.

GET  /payments/callback   the customer's browser, redirected after paying
POST /razorpay/webhook    Razorpay's server, independently of the browser

Either one alone is enough to confirm a payment. Both exist because each can
fail on its own: the customer may close the tab before the redirect, and a
webhook can be delayed or misconfigured. checkout.mark_order_paid is idempotent,
so when both arrive the customer still gets one confirmation.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import checkout
import payments
from auth import current_user

router = APIRouter(tags=["payments"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/payments/callback", response_class=HTMLResponse)
async def payment_callback(request: Request):
    """Where Razorpay sends the customer after the payment page.

    Deliberately does not require sign-in: a customer who tapped Pay Now in
    WhatsApp lands here in their phone's browser, usually not logged in.
    Nothing is trusted from the URL except which link to look up.
    """
    link_id = request.query_params.get("razorpay_payment_link_id", "")
    context = {
        "user": current_user(request),
        "order": None,
        "state": "unknown",
        "whatsapp_url": checkout.whatsapp_chat_url(),
    }

    if not link_id.startswith("plink_"):
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
