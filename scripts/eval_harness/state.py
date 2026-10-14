"""A throwaway customer per case: seeded, observed, and removed.

Only durable facts are written directly (profile, saved address, orders,
cart, a linked account). Conversation state (memory, the list on screen, the
item picked) is never written by hand: a case's `prelude` turns produce it
through the real routing with a scripted model (runner.py), so it always has
the shape the code of the day writes.

Eval customers have WhatsApp numbers starting EVAL_PREFIX (+990 is not an
assigned country code) and emails at munim.invalid, so a run that was killed
can be swept up at the start of the next one.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlmodel import select

import repository as repo
import tenancy
from db import session_scope
from models import Feedback, Order, User, utcnow

EVAL_PREFIX = "9900"
EVAL_EMAIL_DOMAIN = "munim.invalid"

# A saved delivery address that passes shipping.validate (10-digit mobile
# starting 6-9, 6-digit PIN). Invented; lives here, not in case files.
DEFAULT_ADDRESS = {
    "name": "Eval Customer", "phone": "9000000001", "line1": "1 Test Street",
    "line2": "Test Nagar", "city": "Bengaluru", "state": "Karnataka", "pincode": "560001",
}


def phone_for(*parts: Any) -> str:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return EVAL_PREFIX + str(int(digest[:12], 16))[-8:].rjust(8, "0")


def create_customer(store_id: int, phone: str, given: dict[str, Any]) -> dict[str, Any]:
    """Seed one customer from a case's `given`. Returns {user_id, orders: [ids]}."""
    with tenancy.use_store(store_id):
        user_id = repo.get_or_create_user(phone, given.get("name") or "Eval")
        if given.get("linked"):
            with session_scope() as db:
                user = db.get(User, user_id)
                user.email = f"eval+{phone}@{EVAL_EMAIL_DOMAIN}"
        profile = given.get("profile") or {}
        if profile.get("gender"):
            repo.remember_about(user_id, gender=profile["gender"])
        for note in profile.get("notes") or []:
            repo.remember_about(user_id, note=note)

        address = given.get("address")
        if address == "default":
            address = DEFAULT_ADDRESS
        if address:
            repo.save_address(user_id, dict(address))

        order_ids = []
        for n, order in enumerate(given.get("orders") or [], 1):
            order_ids.append(_seed_order(user_id, order, n, address or DEFAULT_ADDRESS))

        for line in given.get("cart") or []:
            repo.add_to_cart(user_id, line["product"], line.get("size") or "", int(line.get("qty") or 1))
    return {"user_id": user_id, "orders": order_ids}


def _seed_order(user_id: int, order: dict[str, Any], n: int, address: dict[str, Any]) -> int:
    import catalog

    items = []
    for line in order["items"]:
        product = catalog.get_by_id(line["product"])
        items.append({
            "product_id": product["id"], "product_name": product["name"], "price_inr": product["price"],
            "size": line.get("size") or "", "quantity": int(line.get("qty") or 1),
            "variant_id": (product.get("variant_ids") or {}).get(line.get("size") or ""),
        })
    order_id = repo.create_order_from_items(user_id, items, "bot", shipping=address)
    status = order.get("status", "paid")
    if status == "paid":
        repo.mark_order_paid(order_id, f"pay_eval_{order_id}")
    elif status == "cancelled":
        repo.cancel_order(order_id)
    days_ago = int(order.get("days_ago", 10 + 5 * n))
    when = utcnow() - timedelta(days=days_ago)
    with session_scope() as db:
        row = db.get(Order, order_id)
        row.created_at = when
        if row.captured_at is not None:
            row.captured_at = when + timedelta(minutes=5)
    return order_id


def snapshot(store_id: int, user_id: int) -> dict[str, Any]:
    """Everything a turn can change, in plain data, for before/after diffs."""
    with tenancy.use_store(store_id):
        cart = repo.get_cart(user_id)
        orders = repo.get_order_history(user_id, 50)
        session = repo.get_active_session(user_id) or {}
        profile = repo.get_profile(user_id) or {}
        shipping = repo.get_last_shipping(user_id)
    with session_scope() as db:
        hidden = sorted({f.product_id for f in db.exec(select(Feedback).where(
            Feedback.user_id == user_id, Feedback.scope == "customer")).all() if f.product_id})

    def code(product: Optional[dict[str, Any]]) -> Optional[str]:
        return (product or {}).get("product_id") or (product or {}).get("id")

    return {
        "cart": [{"product": i["product_id"], "size": i["size"], "qty": i["quantity"],
                  "line_total": i["line_total"], "name": i["name"]} for i in cart["items"]],
        "cart_total": cart["total_inr"],
        "cart_units": cart["count"],
        "orders": [{"order_id": o["order_id"], "status": o["status"], "total": o["total_inr"],
                    "items": [{"product": i["product_id"], "size": i["size"], "qty": i["quantity"]}
                              for i in o["items"]]} for o in orders],
        "session": {
            "selected": code(session.get("selected_product")),
            "shown": [code(p) for p in session.get("last_products_shown") or []],
            "pending_address": session.get("pending_address"),
            "reorder": session.get("reorder"),
            "messages": len(session.get("messages") or []),
        },
        "profile": {"gender": profile.get("gender"), "notes": list(profile.get("notes") or [])},
        "address": shipping,
        "hidden": hidden,
    }


def delete_customer(user_id: int) -> None:
    """Remove the customer and everything under them."""
    with session_scope() as db:
        params = {"u": user_id}
        db.execute(text("DELETE FROM turn_traces WHERE user_id = :u"), params)
        db.execute(text("DELETE FROM order_items WHERE order_id IN "
                        "(SELECT order_id FROM orders WHERE user_id = :u)"), params)
        for table in ("orders", "cart_items", "sessions", "link_tokens", "feedback", "bot_replies"):
            db.execute(text(f"DELETE FROM {table} WHERE user_id = :u"), params)
        db.execute(text("DELETE FROM users WHERE user_id = :u"), params)


def sweep(idle_minutes: int = 30) -> int:
    """Delete eval customers left behind by a killed run. Returns how many.

    Only those idle for `idle_minutes`: a run going on at the same time (a
    long baseline while the tests run) keeps its customer.
    """
    with session_scope() as db:
        ids = [row[0] for row in db.execute(text(
            "SELECT user_id FROM users WHERE (whatsapp_number LIKE :p OR email LIKE :e) "
            "AND last_active_at < now() - make_interval(mins => :idle)"),
            {"p": EVAL_PREFIX + "%", "e": f"%@{EVAL_EMAIL_DOMAIN}", "idle": idle_minutes}).all()]
        db.execute(text("DELETE FROM turn_traces WHERE channel = 'eval' AND user_id IS NULL "
                        "AND created_at < now() - make_interval(mins => :idle)"), {"idle": idle_minutes})
    for user_id in ids:
        delete_customer(user_id)
    return len(ids)
