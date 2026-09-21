"""Payment checks: signatures, both confirmation paths, idempotency, cart effects.

Razorpay and WhatsApp are faked, so this runs offline and never charges or
messages anyone. The last section makes one real call to Razorpay's test API,
and is skipped (not failed) if the keys are not valid yet.

Creates its own throwaway users and removes only those.

Run from the repo root:  backend/.venv/Scripts/python scripts/test_payments.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import threading
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import checkout  # noqa: E402
import payments  # noqa: E402
import repository as repo  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from models import CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402

PHONES = ["910000000011", "910000000012", "910000000013"]
EMAILS = ["test-pay-web@example.com"]
WEBHOOK_SECRET = "test-webhook-secret-not-real"

passed, failed = 0, 0
sent_messages: list[tuple[str, str]] = []
fake_link_status: dict[str, dict] = {}
_counter = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


# --- fakes -------------------------------------------------------------------------

REAL_CREATE = payments.create_payment_link
REAL_FETCH = payments.fetch_payment_link


callback_for: dict[int, object] = {}  # order_id -> callback_url the link was created with


async def fake_create_payment_link(order_id, amount_inr, description, **options):
    global _counter
    _counter += 1
    callback_for[order_id] = options.get("callback_url")
    link_id = f"plink_test{order_id}n{_counter}"
    fake_link_status[link_id] = {"id": link_id, "status": "created", "payments": []}
    return {"id": link_id, "short_url": f"https://rzp.io/test/{link_id}"}


async def fake_fetch_payment_link(link_id):
    if link_id not in fake_link_status:
        raise payments.PaymentError("Razorpay 400: not found")
    return fake_link_status[link_id]


def pay_fake_link(link_id: str, payment_id: str) -> None:
    fake_link_status[link_id] = {
        "id": link_id,
        "status": "paid",
        "order_id": f"order_for_{link_id}",
        "payments": [{"payment_id": payment_id, "status": "captured"}],
    }


async def record_whatsapp(to, body):
    sent_messages.append((to, body))
    return {}


def install_fakes() -> None:
    payments.create_payment_link = fake_create_payment_link
    payments.fetch_payment_link = fake_fetch_payment_link
    checkout.send_whatsapp_message = record_whatsapp
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    # The background reconciler would confirm fake payments on its own and race
    # the checks below; section 9 turns it on deliberately to test it
    os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
    # Deterministic and free: no LLM calls; the assistant has its own test
    os.environ["LLM_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""


# --- helpers -----------------------------------------------------------------------


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(
            select(User).where(User.whatsapp_number.in_(PHONES) | User.email.in_(EMAILS))
        ).all()
        ids = [u.user_id for u in users]
        if not ids:
            return
        order_ids = [o.order_id for o in db.exec(select(Order).where(Order.user_id.in_(ids))).all()]
        if order_ids:
            for row in db.exec(select(OrderItem).where(OrderItem.order_id.in_(order_ids))).all():
                db.delete(row)
        for model, column in ((Order, Order.user_id), (CartItem, CartItem.user_id),
                              (Session, Session.user_id), (LinkToken, LinkToken.user_id)):
            for row in db.exec(select(model).where(column.in_(ids))).all():
                db.delete(row)
        db.flush()
        for user in users:
            db.delete(user)


def signed(event: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    raw = json.dumps(event).encode()
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def paid_event(link_id: str, payment_id: str) -> dict:
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": link_id, "status": "paid"}},
            # Each real payment link has its own Razorpay order, so the id must be
            # unique per payment — a shared one collides on orders_razorpay_order_id_key
            "payment": {"entity": {"id": payment_id, "order_id": f"order_{payment_id}",
                                   "status": "captured"}},
        },
    }


def messages_to(phone: str) -> list[str]:
    return [body for to, body in sent_messages if to == phone]


def status_of(order_id: int) -> str:
    return repo.get_order(order_id)["status"]


# --- checks ------------------------------------------------------------------------


def main() -> int:
    init_db()
    install_fakes()
    cleanup()
    phone = PHONES[0]
    user = repo.get_or_create_user(phone, "Payment Tester")

    print("\n1. Amounts and signatures")
    check("2499.99 rupees is 249999 paise", payments.to_paise(2499.99) == 249999)
    check("Decimal 8999 is 899900 paise", payments.to_paise(Decimal("8999")) == 899900)
    raw, sig = signed({"event": "x"})
    check("valid signature accepted", payments.verify_webhook_signature(raw, sig))
    check("tampered body rejected", not payments.verify_webhook_signature(raw + b" ", sig))
    check("signature from another secret rejected",
          not payments.verify_webhook_signature(raw, signed({"event": "x"}, "other")[1]))
    check("missing signature rejected", not payments.verify_webhook_signature(raw, ""))

    print("\n2. Cart checkout creates one order and one link")
    repo.add_to_cart(user, "EW001", "L", 2)
    placed = asyncio.run(checkout.checkout_cart(user, "bot"))
    order_id = placed["order_id"]
    order = repo.get_order(order_id)
    check("order has a payment link", order["payment_link_id"] and order["payment_link_url"])
    check("order total", order["total_inr"] == 2 * 2499)
    again = asyncio.run(checkout.checkout_cart(user, "bot"))
    check("CHECKOUT again for the same cart reuses the order",
          again["reused"] and again["order_id"] == order_id)
    with session_scope() as db:
        orders = db.exec(select(Order).where(Order.user_id == user)).all()
    check("still exactly one order", len(orders) == 1, f"found {len(orders)}")

    # Something added AFTER checkout must survive the payment
    repo.add_to_cart(user, "EW001", "L", 1)

    with TestClient(__import__("main").app) as client:
        print("\n3. Webhook")
        bad_raw, _ = signed(paid_event(order["payment_link_id"], "pay_forged"))
        forged = client.post("/razorpay/webhook", content=bad_raw,
                             headers={"Content-Type": "application/json",
                                      "X-Razorpay-Signature": "0" * 64})
        check("forged webhook gets 400", forged.status_code == 400)
        check("forged webhook changes nothing", status_of(order_id) == "created")

        raw, sig = signed(paid_event(order["payment_link_id"], "pay_W1"))
        headers = {"Content-Type": "application/json", "X-Razorpay-Signature": sig}
        first = client.post("/razorpay/webhook", content=raw, headers=headers)
        check("signed webhook accepted", first.status_code == 200)
        check("order marked paid", status_of(order_id) == "captured")
        check("payment id recorded", repo.get_order(order_id)["razorpay_payment_id"] == "pay_W1")
        check("one WhatsApp confirmation", len(messages_to(phone)) == 1, f"got {messages_to(phone)}")
        check("confirmation names the order", f"Order #{order_id}" in messages_to(phone)[0])

        client.post("/razorpay/webhook", content=raw, headers=headers)
        check("redelivered webhook sends nothing more", len(messages_to(phone)) == 1)

        cart = repo.get_cart(user)
        check("only the ordered quantity left the cart",
              [(i["product_id"], i["size"], i["quantity"]) for i in cart["items"]] == [("EW001", "L", 1)],
              f"got {cart['items']}")

        print("\n4. Customer redirect (callback)")
        single = asyncio.run(checkout.checkout_single(user, "EW012", "", 1, "bot"))
        link_id = repo.get_order(single["order_id"])["payment_link_id"]

        pending = client.get(f"/payments/callback?razorpay_payment_link_id={link_id}")
        check("unpaid link shows a pending page", pending.status_code == 200
              and "waiting on the payment" in pending.text)
        check("unpaid link leaves the order open", status_of(single["order_id"]) == "created")

        forged_query = client.get(
            f"/payments/callback?razorpay_payment_link_id={link_id}"
            "&razorpay_payment_link_status=paid&razorpay_payment_id=pay_fake"
        )
        check("query parameters claiming 'paid' are not trusted",
              status_of(single["order_id"]) == "created" and "waiting" in forged_query.text)

        pay_fake_link(link_id, "pay_C1")
        before = len(messages_to(phone))
        paid_page = client.get(f"/payments/callback?razorpay_payment_link_id={link_id}")
        check("paid link shows confirmation", "is confirmed" in paid_page.text)
        check("callback marks the order paid", status_of(single["order_id"]) == "captured")
        check("callback sends one confirmation", len(messages_to(phone)) == before + 1)

        print("\n5. Callback and webhook for the same payment")
        raw, sig = signed(paid_event(link_id, "pay_C1"))
        client.post("/razorpay/webhook", content=raw,
                    headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig})
        client.get(f"/payments/callback?razorpay_payment_link_id={link_id}")
        check("both paths together still send exactly one message",
              len(messages_to(phone)) == before + 1)
        check("single-item order did not touch the cart", len(repo.get_cart(user)["items"]) == 1)

        check("unknown link is a 404",
              client.get("/payments/callback?razorpay_payment_link_id=plink_nope").status_code == 404)
        check("malformed link id is a 400",
              client.get("/payments/callback?razorpay_payment_link_id=bad").status_code == 400)

    print("\n6a. Web order by a linked customer (A4) and order tracking (A5)")
    web_phone = PHONES[2]
    with TestClient(__import__("main").app) as web:
        web.post("/signup", data={"email": EMAILS[0], "password": "test-password-123",
                                  "display_name": "Web Payer"}, follow_redirects=False)
        web_user = web.get("/api/me").json()["user_id"]
        token = web.post("/api/link/start").json()["token"]
        repo.consume_link_token(token, web_phone)

        web.post("/cart/add/EW012", data={"size": "Free Size", "quantity": "1"}, follow_redirects=False)
        address = {"name": "Web Payer", "phone": "9876543210", "line1": "4 Park Street",
                   "city": "Kolkata", "state": "West Bengal", "pincode": "700016"}
        to_pay = web.post("/checkout", data=address, follow_redirects=False).headers.get("location", "")
        check("web checkout goes to the payment page", to_pay.startswith("https://rzp.io/test/"), to_pay)
        web_order = repo.get_order_history(web_user)[0]

        tracking = web.get(f"/orders/{web_order['order_id']}")
        check("tracking page before payment offers Pay now",
              tracking.status_code == 200 and "Awaiting payment" in tracking.text and to_pay in tracking.text)

        link_id = repo.get_order(web_order["order_id"])["payment_link_id"]
        raw, sig = signed(paid_event(link_id, "pay_WEB1"))
        web.post("/razorpay/webhook", content=raw,
                 headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig})
        check("A4: web order confirmed on the customer's WhatsApp",
              len(messages_to(web_phone)) == 1, f"got {messages_to(web_phone)}")

        tracking = web.get(f"/orders/{web_order['order_id']}")
        check("tracking page after payment shows paid and a delivery date",
              "Paid" in tracking.text and "Arriving by" in tracking.text)
        check("someone else's order is a 404", web.get(f"/orders/{order_id}").status_code == 404)

        api_own = web.get(f"/api/orders/{web_order['order_id']}")
        check("order API returns own order without contact details",
              api_own.status_code == 200 and "whatsapp_number" not in api_own.json())
        check("order API hides other people's orders", web.get(f"/api/orders/{order_id}").status_code == 404)
        check("account lists the order as paid", "paid" in web.get("/account").text)

    print("\n6. Simultaneous confirmations (row lock)")
    racer = repo.get_or_create_user(PHONES[1], "Racer")
    race_order = repo.create_order_for_product(racer, "EW012", "", 1, "bot")
    results: list[bool] = []

    def confirm() -> None:
        results.append(repo.mark_order_paid(race_order, "pay_race"))

    threads = [threading.Thread(target=confirm) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("six simultaneous confirmations: exactly one wins", results.count(True) == 1,
          f"got {results}")

    print("\n7. Razorpay down")
    async def broken(*_args, **_kwargs):
        raise payments.PaymentError("simulated outage")

    payments.create_payment_link = broken
    repo.add_to_cart(racer, "EW003", "M", 1)
    try:
        asyncio.run(checkout.checkout_cart(racer, "bot"))
        check("outage raises PaymentError", False)
    except payments.PaymentError:
        check("outage raises PaymentError", True)
    history = repo.get_order_history(racer)
    check("the order it created is cancelled, not left dangling",
          history[0]["status"] == "failed", f"got {[o['status'] for o in history]}")
    check("cart untouched by the outage", len(repo.get_cart(racer)["items"]) == 1)
    payments.create_payment_link = fake_create_payment_link

    print("\n9. Return to WhatsApp, and background reconciliation")
    returner = repo.get_or_create_user(PHONES[1], "Racer")
    chat_order = asyncio.run(checkout.checkout_single(returner, "EW012", "", 1, "bot"))
    check("chat orders return to the WhatsApp chat, not our site",
          str(callback_for[chat_order["order_id"]]).startswith("https://wa.me/"),
          f"got {callback_for[chat_order['order_id']]}")
    web_order_id = repo.get_order_history(repo.get_user_by_whatsapp(PHONES[2])["user_id"])[0]["order_id"]
    check("website orders keep the default (our confirmation page)",
          callback_for.get(web_order_id) is None, f"got {callback_for.get(web_order_id)}")

    check("reconcile: nothing to confirm while unpaid",
          asyncio.run(checkout.reconcile_pending_payments()) == 0)
    link_id = repo.get_order(chat_order["order_id"])["payment_link_id"]
    before = len(messages_to(PHONES[1]))
    pay_fake_link(link_id, "pay_RECON1")
    check("reconcile confirms a paid order the webhook never reported",
          asyncio.run(checkout.reconcile_pending_payments()) == 1
          and status_of(chat_order["order_id"]) == "captured")
    check("...with one WhatsApp confirmation", len(messages_to(PHONES[1])) == before + 1)
    check("reconcile again confirms nothing twice",
          asyncio.run(checkout.reconcile_pending_payments()) == 0
          and len(messages_to(PHONES[1])) == before + 1)

    stale = asyncio.run(checkout.checkout_single(returner, "EW012", "", 1, "bot"))
    with session_scope() as db:
        row = db.get(Order, stale["order_id"])
        row.created_at = row.created_at.replace(year=row.created_at.year - 1)
    pay_fake_link(repo.get_order(stale["order_id"])["payment_link_id"], "pay_OLD")
    asyncio.run(checkout.reconcile_pending_payments())
    check("old abandoned orders are not polled", status_of(stale["order_id"]) == "created")

    # The loop really starts with the app, and confirms without any request
    import time
    os.environ["PAYMENT_RECONCILE_SECONDS"] = "0.2"
    background = asyncio.run(checkout.checkout_single(returner, "EW012", "", 1, "bot"))
    pay_fake_link(repo.get_order(background["order_id"])["payment_link_id"], "pay_BG1")
    with TestClient(__import__("main").app):
        for _ in range(20):
            if status_of(background["order_id"]) == "captured":
                break
            time.sleep(0.1)
    os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
    check("background reconciler confirms on its own when the app is running",
          status_of(background["order_id"]) == "captured")

    with TestClient(__import__("main").app) as client:
        page = client.get(f"/payments/callback?razorpay_payment_link_id={link_id}").text
        check("confirmation page has a Back to WhatsApp button",
              "Back to WhatsApp" in page and "https://wa.me/" in page)

    print("\n10. Live Razorpay test API (one real call)")
    payments.create_payment_link = REAL_CREATE
    payments.fetch_payment_link = REAL_FETCH
    try:
        link = asyncio.run(payments.create_payment_link(0, 1, "Integration check — safe to ignore"))
        fetched = asyncio.run(payments.fetch_payment_link(link["id"]))
        check("real payment link created", link["id"].startswith("plink_") and link["short_url"])
        check("real link fetched back unpaid", fetched.get("status") == "created")
        print(f"        pay it to try the page: {link['short_url']}")
    except payments.PaymentError as exc:
        print(f"  SKIP  live API not reachable with these keys: {exc}")

    print(f"\n{passed} passed, {failed} failed")
    cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
