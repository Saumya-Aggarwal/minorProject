"""Server side of live sync: /api/live, the fingerprint, and which pages opt in.

The browser logic is covered separately by scripts/test_live_js.mjs (Node).
Razorpay and WhatsApp are faked. Creates and removes only its own users.

Run from the repo root:  backend/.venv/Scripts/python scripts/test_live.py
"""

import hashlib
import hmac
import json
import os
import re
import sys
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
from main import app  # noqa: E402
from models import CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402

PHONE = "910000000021"
EMAIL = "test-live@example.com"
SECRET = "test-live-webhook-secret"

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


async def fake_link(order_id, amount_inr, description, **_):
    return {"id": f"plink_live{order_id}", "short_url": f"https://rzp.io/test/live{order_id}"}


async def no_whatsapp(to, body):
    return {}


payments.create_payment_link = fake_link
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"  # no background confirmations racing the checks
# Deterministic and free: no LLM calls; the assistant has its own test with a fake model
os.environ["LLM_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
checkout.send_whatsapp_message = no_whatsapp
os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(
            select(User).where((User.whatsapp_number == PHONE) | (User.email == EMAIL))
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


def whatsapp(client: TestClient, body: str) -> str:
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": PHONE, "profile": {"name": "Live Tester"}}],
            "messages": [{"from": PHONE, "id": "wamid.l", "timestamp": "1",
                          "type": "text", "text": {"body": body}}]}}]}]}
    return client.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()[
        "responses"][0]["body"]


def main() -> int:
    init_db()
    cleanup()

    with TestClient(app) as anonymous:
        check("/api/live needs sign-in", anonymous.get("/api/live").status_code == 401)
        home = anonymous.get("/").text
        check("signed-out pages do not load the poller", "/static/live.js" not in home)
        js = anonymous.get("/static/live.js")
        check("live.js is served", js.status_code == 200 and "javascript" in js.headers["content-type"])

    with TestClient(app) as web:
        web.post("/signup", data={"email": EMAIL, "password": "test-password-123",
                                  "display_name": "Live Tester"}, follow_redirects=False)
        user_id = web.get("/api/me").json()["user_id"]

        print("\n1. Pages")
        home = web.get("/").text
        check("signed-in pages load the poller and the badge",
              "/static/live.js" in home and 'id="cart-count"' in home)
        check("home page does not re-render in place",
              '<main id="main"' in home and "data-live-refresh" not in home.split("<main", 1)[1][:200])
        for path in ("/cart", "/account"):
            html = web.get(path).text
            check(f"{path} re-renders in place", "data-live-refresh" in html.split("<main", 1)[1][:200])

        print("\n2. Link code survives re-renders")
        first = web.get("/account").text
        second = web.get("/account").text
        def code(html: str) -> str:
            # Token alphabet is URL-safe base64, so it reads the same in the
            # wa.me link and in the fallback text
            found = re.search(r"LINK-([A-Za-z0-9_-]+)", html)
            return found.group(1) if found else ""

        check("account page shows a link code", bool(code(first)))
        check("the same code on every render", code(first) == code(second),
              f"{code(first)} vs {code(second)}")
        token = code(first)

        print("\n3. Fingerprint")
        state = web.get("/api/live").json()
        check("empty cart counts zero", state["cart_count"] == 0)
        check("fingerprint stable when nothing changes",
              web.get("/api/live").json()["fingerprint"] == state["fingerprint"])

        with session_scope() as db:
            before = db.get(User, user_id).last_active_at
        web.get("/api/live")
        with session_scope() as db:
            after = db.get(User, user_id).last_active_at
        check("polling does not write last_active_at", before == after)

        # The code shown on the page links WhatsApp, as a customer would
        check("linking with the displayed code works",
              "connected" in whatsapp(web, f"LINK-{token}").lower())

        whatsapp(web, "silk stole for a sherwani")
        whatsapp(web, "1")
        added = whatsapp(web, "ADD")
        after_add = web.get("/api/live").json()
        check("adding on WhatsApp changes the fingerprint",
              after_add["fingerprint"] != state["fingerprint"], added[:60])
        check("…and the count the website sees", after_add["cart_count"] == 1)
        items = repo.get_cart(user_id)["items"]
        check("the website cart page shows the item added in chat",
              len(items) == 1 and items[0]["name"] in web.get("/cart").text, f"cart: {items}")

        print("\n4. Payment flips the order")
        cart = repo.get_cart(user_id)
        for position, item in enumerate(cart["items"], start=1):
            if not item["size"] and len(item["sizes_available"]) > 1:
                whatsapp(web, f"SIZE {position} {item['sizes_available'][0]}")
        whatsapp(web, "CHECKOUT")
        before_pay = web.get("/api/live").json()
        order_id = max(int(i) for i in before_pay["orders"])
        check("new order visible as created", before_pay["orders"][str(order_id)] == "created")

        link_id = repo.get_order(order_id)["payment_link_id"]
        event = {"event": "payment_link.paid", "payload": {
            "payment_link": {"entity": {"id": link_id, "status": "paid"}},
            "payment": {"entity": {"id": "pay_live1", "order_id": "order_live1"}}}}
        raw = json.dumps(event).encode()
        signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        web.post("/razorpay/webhook", content=raw,
                 headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature})
        after_pay = web.get("/api/live").json()
        check("payment changes the fingerprint", after_pay["fingerprint"] != before_pay["fingerprint"])
        check("order shows as paid", after_pay["orders"][str(order_id)] == "captured")
        check("paid items left the cart", after_pay["cart_count"] == 0)

    print(f"\n{passed} passed, {failed} failed")
    cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
