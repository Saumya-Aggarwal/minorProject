"""Thin wrapper over the Razorpay REST API. No business logic lives here.

Uses httpx directly rather than Razorpay's SDK: the SDK is synchronous (it
would block the event loop, or need a thread per call), and this module only
needs three endpoints.

Payment Links are used for both channels. The bot sends the link behind a Pay
Now button; the website redirects to it. One hosted payment page, one way to
confirm payment.
"""

import hashlib
import hmac
import os
import secrets
from decimal import Decimal
from typing import Any, Optional

import httpx

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT = httpx.Timeout(15.0)


class PaymentError(Exception):
    """Razorpay rejected a request or could not be reached."""


def _auth() -> tuple[str, str]:
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise PaymentError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set")
    return key_id, key_secret


def to_paise(amount_inr: Decimal | float | int) -> int:
    """Razorpay amounts are integer paise. Via Decimal, so 2499.99 is 249999."""
    return int((Decimal(str(amount_inr)) * 100).quantize(Decimal("1")))


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json().get("error", {}).get("description") or response.text
    except ValueError:
        return response.text


async def create_payment_link(
    order_id: int,
    amount_inr: Decimal | float | int,
    description: str,
    customer_name: Optional[str] = None,
    customer_phone: Optional[str] = None,
    customer_email: Optional[str] = None,
    callback_url: Optional[str] = None,
) -> dict[str, Any]:
    """Create a hosted payment page for one order. Returns {"id", "short_url"}.

    callback_url is where Razorpay sends the customer's browser after paying.
    Defaults to our /payments/callback page.

    reference_id must be unique across the Razorpay account forever, but local
    order ids restart whenever the tables are rebuilt. A random suffix keeps
    reference ids unique; the order is found again by the link id, not by it.
    """
    if callback_url is None:
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        callback_url = f"{base_url}/payments/callback" if base_url else None

    customer: dict[str, str] = {}
    if customer_name:
        customer["name"] = customer_name[:50]
    if customer_phone:
        customer["contact"] = "+" + customer_phone.lstrip("+")
    if customer_email:
        customer["email"] = customer_email

    payload: dict[str, Any] = {
        "amount": to_paise(amount_inr),
        "currency": "INR",
        "accept_partial": False,
        "reference_id": f"ord{order_id}-{secrets.token_hex(3)}",
        "description": description[:2048],
        # Our own WhatsApp message carries the link; Razorpay's SMS/email would
        # duplicate it, and test mode cannot deliver them anyway
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"order_id": str(order_id)},
    }
    if customer:
        payload["customer"] = customer
    if callback_url:
        payload["callback_url"] = callback_url
        payload["callback_method"] = "get"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.post(f"{API_BASE}/payment_links", json=payload, auth=_auth())
        except httpx.HTTPError as exc:
            raise PaymentError(f"could not reach Razorpay: {exc!r}") from exc

    if response.is_error:
        raise PaymentError(f"Razorpay {response.status_code}: {_error_message(response)}")
    data = response.json()
    return {"id": data["id"], "short_url": data["short_url"]}


async def fetch_payment_link(payment_link_id: str) -> dict[str, Any]:
    """The link as Razorpay sees it — the authoritative answer to "is it paid?"."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.get(
                f"{API_BASE}/payment_links/{payment_link_id}", auth=_auth()
            )
        except httpx.HTTPError as exc:
            raise PaymentError(f"could not reach Razorpay: {exc!r}") from exc

    if response.is_error:
        raise PaymentError(f"Razorpay {response.status_code}: {_error_message(response)}")
    return response.json()


def paid_payment_id(link: dict[str, Any]) -> Optional[str]:
    """The captured payment on a fetched link, or None if it is not paid."""
    if link.get("status") != "paid":
        return None
    for payment in link.get("payments") or []:
        if payment.get("status") == "captured":
            return payment.get("payment_id")
    # Paid with no payment listed should not happen; keep a trace rather than fail
    return "unknown"


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the raw request body with the webhook secret.

    Must be the raw bytes as received. Parsing the JSON and re-serialising it
    changes whitespace and key order, and the signature will never match.
    """
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # compare_digest: constant time, so the comparison does not leak how many
    # leading characters of a forged signature were correct
    return hmac.compare_digest(expected, signature)


# --- Standard Checkout: a Razorpay order, paid on our own page ---------------------
#
# Test mode allows only 30 payment links per account, ever, and this account has
# spent them ("Razorpay 429: test mode limit of 30 reached for payment_link").
# Orders have no such cap, and Checkout on our own page is the integration
# Razorpay documents first: create an order, let the customer pay against it,
# then verify the signature they come back with.

async def create_checkout_order(
    order_id: int,
    amount_inr: Decimal | float | int,
    description: str,
    notes: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Create the Razorpay order to pay against. Returns {"id": "order_..."}.

    receipt must be unique across the account, and our order ids restart
    whenever the tables are rebuilt, so it carries a random suffix.
    """
    payload: dict[str, Any] = {
        "amount": to_paise(amount_inr),
        "currency": "INR",
        "receipt": f"ord{order_id}-{secrets.token_hex(3)}",
        "notes": {"order_id": str(order_id), "description": description[:250], **(notes or {})},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.post(f"{API_BASE}/orders", json=payload, auth=_auth())
        except httpx.HTTPError as exc:
            raise PaymentError(f"could not reach Razorpay: {exc!r}") from exc
    if response.is_error:
        raise PaymentError(f"Razorpay {response.status_code}: {_error_message(response)}")
    return {"id": response.json()["id"]}


async def fetch_order_payments(razorpay_order_id: str) -> list[dict[str, Any]]:
    """Every payment attempt against a Razorpay order — the authoritative answer
    to "has this been paid?", used by the reconciler and the callback."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.get(f"{API_BASE}/orders/{razorpay_order_id}/payments", auth=_auth())
        except httpx.HTTPError as exc:
            raise PaymentError(f"could not reach Razorpay: {exc!r}") from exc
    if response.is_error:
        raise PaymentError(f"Razorpay {response.status_code}: {_error_message(response)}")
    return response.json().get("items") or []


def captured_payment_id(attempts: list[dict[str, Any]]) -> Optional[str]:
    """The captured payment among these attempts, or None (failed ones are kept
    by Razorpay, so the list is not the same as "paid")."""
    for payment in attempts:
        if payment.get("status") == "captured":
            return payment.get("id")
    return None


def verify_checkout_signature(razorpay_order_id: str, payment_id: str, signature: str) -> bool:
    """Checkout hands the browser order_id|payment_id signed with our key secret.

    Without this check anyone could POST an invented payment id and have the
    order marked paid. The browser is never trusted on its own: the reconciler
    and the webhook confirm against Razorpay as well.
    """
    _, key_secret = _auth()
    if not (razorpay_order_id and payment_id and signature):
        return False
    expected = hmac.new(key_secret.encode(), f"{razorpay_order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
